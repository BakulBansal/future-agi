"""The trace investigator never claims a job whose trace carries the simulation mark."""

import uuid
from datetime import timedelta

import pytest
from django.db import connection
from django.test import override_settings
from django.utils import timezone
from structlog.testing import capture_logs

from model_hub.models.ai_model import AIModel
from simulate.models import Scenarios
from simulate.models.run_test import RunTest
from simulate.models.test_execution import CallExecution, TestExecution
from tracer.models.observability_provider import ObservabilityProvider, ProviderChoices
from tracer.models.project import Project
from tracer.models.trace_investigation import (
    InvestigationWorkload,
    TraceInvestigationAttempt,
    TraceInvestigationJob,
    TraceInvestigationJobState,
)
from tracer.models.trace_scan import TraceScanConfig
from tracer.queries.trace_scanner import is_trace_sampled
from tracer.services import trace_investigation
from tracer.services.simulation_visibility import HOLDING_FLAG, MarkedLookupFailed
from tracer.services.trace_investigation import (
    claim_due_investigations,
    record_trace_notifications,
)
from tracer.tests.test_trace_investigation_control import _delivery

pytestmark = pytest.mark.django_db

ENGINE = "omega-v1"
WAITING = TraceInvestigationJobState.WAITING
RUNNING = TraceInvestigationJobState.RUNNING
CANCELLED = TraceInvestigationJobState.CANCELLED


def _row_locks_held() -> list[str]:
    """Tables on which this connection's open transaction holds row locks.

    SELECT ... FOR UPDATE takes RowShareLock on its table and an UPDATE takes
    RowExclusiveLock; both last until the transaction ends. Outside a
    transaction the list is empty.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relation::regclass::text FROM pg_locks"
            " WHERE pid = pg_backend_pid() AND locktype = 'relation'"
            " AND mode IN ('RowShareLock', 'RowExclusiveLock')"
        )
        return sorted(row[0] for row in cursor.fetchall())


class FakeClock:
    """The clock the claim reads lookup time from; only a lookup moves it."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeMarkLookup:
    """Stands in for the ClickHouse mark lookup and records where each call ran."""

    def __init__(self, marked=(), *, error=None, clock=None, seconds=0, during=None):
        self.marked = {str(trace_id) for trace_id in marked}
        self.error = error
        self.clock = clock
        self.seconds = seconds
        self.during = during
        self.calls = []

    def __call__(self, *, project_id, trace_ids, timeout_ms):
        self.calls.append(
            {
                "project_id": project_id,
                "trace_ids": list(trace_ids),
                "timeout_ms": timeout_ms,
                "in_atomic_block": connection.in_atomic_block,
                "row_locks": _row_locks_held(),
            }
        )
        if self.clock is not None:
            # A real lookup is cut off at its timeout.
            self.clock.advance(min(self.seconds, timeout_ms / 1000))
        if self.during is not None:
            self.during(len(self.calls))
        if self.error is not None:
            raise self.error
        return frozenset(trace_id for trace_id in trace_ids if trace_id in self.marked)


def _use(monkeypatch, lookup: FakeMarkLookup) -> FakeMarkLookup:
    monkeypatch.setattr(trace_investigation, "marked_trace_ids", lookup)
    return lookup


def _provider(project, metadata):
    return ObservabilityProvider.no_workspace_objects.create(
        project=project,
        provider=ProviderChoices.VAPI,
        organization=project.organization,
        workspace=project.workspace,
        metadata=metadata,
    )


def _project(
    organization,
    workspace,
    name,
    *,
    holding=False,
    last_claimed=None,
    sampling_rate=1.0,
):
    project = Project.no_workspace_objects.create(
        name=name,
        organization=organization,
        workspace=workspace,
        model_type=AIModel.ModelTypes.GENERATIVE_LLM,
        trace_type="observe",
    )
    TraceScanConfig.no_workspace_objects.create(
        project=project,
        sampling_rate=sampling_rate,
        enabled=True,
        scan_version=ENGINE,
        omega_last_claimed_at=last_claimed,
    )
    if holding:
        _provider(project, {HOLDING_FLAG: True})
    return project


def _jobs(project, count, *, first_due):
    """`count` due trace jobs, queued one second apart from `first_due`."""
    return TraceInvestigationJob.no_workspace_objects.bulk_create(
        [
            TraceInvestigationJob(
                organization=project.organization,
                workspace=project.workspace,
                project=project,
                trace_id=uuid.uuid4(),
                root_span_id="0123456789abcdef",
                root_end_time=first_due,
                not_before=first_due + timedelta(seconds=index),
            )
            for index in range(count)
        ]
    )


def _claim(limit):
    return claim_due_investigations(
        worker_id="test-worker", engine_version=ENGINE, limit=limit
    )["claims"]


def _state(job):
    job.refresh_from_db()
    return job.state


def _rotated_at(project):
    return TraceScanConfig.no_workspace_objects.get(
        project=project
    ).omega_last_claimed_at


def _cancelled(project):
    return TraceInvestigationJob.no_workspace_objects.filter(
        project=project, state=CANCELLED
    ).count()


def _events(logs, name):
    return [entry for entry in logs if entry["event"] == name]


def test_marked_job_is_never_claimed_past_any_page_bound(
    organization, workspace, monkeypatch
):
    project = _project(organization, workspace, "Holding", holding=True)
    jobs = _jobs(project, 402, first_due=timezone.now() - timedelta(days=1))
    marked, unmarked = jobs[:401], jobs[401]
    lookup = _use(monkeypatch, FakeMarkLookup(job.trace_id for job in marked))

    with capture_logs() as logs:
        assert _claim(limit=1) == []
    assert lookup.calls[0]["trace_ids"] == [str(job.trace_id) for job in marked[:200]]
    assert _cancelled(project) == 200
    assert len(_events(logs, "trace_investigation_skipped_simulated")) == 200

    assert _claim(limit=1) == []
    assert lookup.calls[1]["trace_ids"] == [
        str(job.trace_id) for job in marked[200:400]
    ]
    assert _cancelled(project) == 400

    claims = _claim(limit=1)
    assert lookup.calls[2]["trace_ids"] == [
        str(marked[400].trace_id),
        str(unmarked.trace_id),
    ]
    assert [claim["job_id"] for claim in claims] == [unmarked.id]
    assert _cancelled(project) == 401
    assert not TraceInvestigationAttempt.no_workspace_objects.filter(
        job__in=marked
    ).exists()


@pytest.mark.django_db(transaction=True)
def test_lookup_runs_outside_every_lock(organization, workspace, monkeypatch):
    # A committed transaction per candidate, as in production: inside a test
    # transaction a released savepoint would keep its row locks to the end.
    now = timezone.now()
    first = _project(
        organization,
        workspace,
        "Holding first",
        holding=True,
        last_claimed=now - timedelta(days=3),
    )
    second = _project(
        organization,
        workspace,
        "Holding second",
        holding=True,
        last_claimed=now - timedelta(days=2),
    )
    plain = _project(
        organization, workspace, "Not holding", last_claimed=now - timedelta(days=1)
    )
    due = now - timedelta(hours=1)
    raced, marked, wanted = _jobs(first, 3, first_due=due)
    (second_job,) = _jobs(second, 1, first_due=due)
    (plain_job,) = _jobs(plain, 1, first_due=due)

    def claimed_elsewhere(call_number):
        # Nothing is locked while the lookup runs, so another transaction may
        # move a job of the page in the meantime.
        if call_number == 1:
            TraceInvestigationJob.no_workspace_objects.filter(id=raced.id).update(
                state=RUNNING
            )

    lookup = _use(
        monkeypatch,
        FakeMarkLookup([raced.trace_id, marked.trace_id], during=claimed_elsewhere),
    )

    claims = _claim(limit=3)

    assert [call["project_id"] for call in lookup.calls] == [
        str(first.id),
        str(second.id),
    ]
    assert [(call["in_atomic_block"], call["row_locks"]) for call in lookup.calls] == [
        (False, []),
        (False, []),
    ]
    assert {claim["job_id"] for claim in claims} == {
        wanted.id,
        second_job.id,
        plain_job.id,
    }
    assert _state(marked) == CANCELLED
    # The cancel touches only a job still waiting.
    assert _state(raced) == RUNNING


@override_settings(ERROR_FEED_OMEGA_PROJECT_CONCURRENCY=2)
def test_locked_job_outside_the_decided_set_is_left_waiting(
    organization, workspace, monkeypatch
):
    now = timezone.now()
    project = _project(
        organization,
        workspace,
        "Holding",
        holding=True,
        last_claimed=now - timedelta(days=1),
    )
    (looked_up,) = _jobs(project, 1, first_due=now - timedelta(hours=1))
    arrived = []

    def arrive_during_lookup(call_number):
        if call_number == 1:
            arrived.extend(_jobs(project, 1, first_due=now - timedelta(hours=2)))

    lookup = _use(monkeypatch, FakeMarkLookup(during=arrive_during_lookup))
    before = timezone.now()

    assert _claim(limit=1) == []
    (late,) = arrived
    assert lookup.calls[0]["trace_ids"] == [str(looked_up.trace_id)]
    assert (_state(late), _state(looked_up)) == (WAITING, WAITING)
    assert not TraceInvestigationAttempt.no_workspace_objects.exists()
    assert _rotated_at(project) >= before

    # Once its own trace has been looked up, the job that heads the queue is claimed.
    claims = _claim(limit=1)
    assert lookup.calls[1]["trace_ids"] == [
        str(late.trace_id),
        str(looked_up.trace_id),
    ]
    assert [claim["job_id"] for claim in claims] == [late.id]

    # A marked job put back in the queue after its cancel is still outside the
    # decided set.
    (revived,) = _jobs(project, 1, first_due=now - timedelta(hours=3))
    lookup.marked = {str(revived.trace_id)}
    cancel = trace_investigation._cancel_marked_jobs

    def cancel_then_root_write(*args):
        cancel(*args)
        TraceInvestigationJob.no_workspace_objects.filter(id=revived.id).update(
            state=WAITING
        )

    monkeypatch.setattr(
        trace_investigation, "_cancel_marked_jobs", cancel_then_root_write
    )
    before_revival = timezone.now()
    assert _claim(limit=1) == []
    assert _state(revived) == WAITING
    assert not TraceInvestigationAttempt.no_workspace_objects.filter(
        job=revived
    ).exists()
    assert _rotated_at(project) >= before_revival


@pytest.mark.parametrize(
    ("error", "error_type"),
    [
        (MarkedLookupFailed("mark lookup timed out"), "MarkedLookupFailed"),
        (RuntimeError("connection reset"), "RuntimeError"),
    ],
    ids=["typed_failure", "untyped_error"],
)
def test_lookup_failure_skips_candidate_and_moves_rotation(
    organization, workspace, monkeypatch, error, error_type
):
    now = timezone.now()
    holding = _project(
        organization,
        workspace,
        "Holding",
        holding=True,
        last_claimed=now - timedelta(days=2),
    )
    plain = _project(
        organization, workspace, "Not holding", last_claimed=now - timedelta(days=1)
    )
    marked, unmarked = _jobs(holding, 2, first_due=now - timedelta(hours=1))
    (plain_job,) = _jobs(plain, 1, first_due=now - timedelta(hours=1))
    lookup = _use(monkeypatch, FakeMarkLookup([marked.trace_id], error=error))
    before = timezone.now()

    with capture_logs() as logs:
        claims = _claim(limit=1)

    assert [claim["job_id"] for claim in claims] == [plain_job.id]
    assert (_state(marked), _state(unmarked)) == (WAITING, WAITING)
    assert not TraceInvestigationAttempt.no_workspace_objects.filter(
        job__project=holding
    ).exists()
    assert _rotated_at(holding) >= before
    assert _events(logs, "trace_investigation_mark_lookup_failed") == [
        {
            "event": "trace_investigation_mark_lookup_failed",
            "log_level": "warning",
            "project_id": str(holding.id),
            "error_type": error_type,
        }
    ]

    # No backoff: the next cycle looks the project up again.
    lookup.error = None
    claims = _claim(limit=1)
    assert len(lookup.calls) == 2
    assert [claim["job_id"] for claim in claims] == [unmarked.id]
    assert _state(marked) == CANCELLED


@override_settings(ERROR_FEED_OMEGA_DELAY_SECONDS=0)
def test_revived_job_is_cancelled_again(organization, workspace, monkeypatch):
    project = _project(organization, workspace, "Holding", holding=True)
    marked_trace = uuid.uuid4()
    record_trace_notifications(
        deliveries=[_delivery(project, offset=1, trace_id=marked_trace)]
    )
    job = TraceInvestigationJob.no_workspace_objects.get(
        project=project, trace_id=marked_trace
    )
    lookup = _use(monkeypatch, FakeMarkLookup([marked_trace]))

    with capture_logs() as logs:
        assert _claim(limit=1) == []
    job.refresh_from_db()
    assert (job.state, job.generation) == (CANCELLED, 1)
    assert _events(logs, "trace_investigation_skipped_simulated") == [
        {
            "event": "trace_investigation_skipped_simulated",
            "log_level": "info",
            "job_id": str(job.id),
            "project_id": str(project.id),
            "trace_id": str(marked_trace),
        }
    ]

    # A repeated root write puts the cancelled job back in the queue, ahead of
    # an unmarked trace written with it.
    unmarked_trace = uuid.uuid4()
    record_trace_notifications(
        deliveries=[
            _delivery(project, offset=2, trace_id=marked_trace),
            _delivery(project, offset=3, trace_id=unmarked_trace),
        ]
    )
    job.refresh_from_db()
    assert (job.state, job.generation) == (WAITING, 2)

    with capture_logs() as logs:
        claims = _claim(limit=1)

    assert lookup.calls[1]["trace_ids"] == [str(marked_trace), str(unmarked_trace)]
    job.refresh_from_db()
    assert (job.state, job.generation) == (CANCELLED, 2)
    # Cancelled before the lock: the unmarked job behind it is claimed this cycle.
    assert [claim["trace_id"] for claim in claims] == [unmarked_trace]
    assert len(_events(logs, "trace_investigation_skipped_simulated")) == 1
    assert not TraceInvestigationAttempt.no_workspace_objects.filter(job=job).exists()


def test_non_holding_candidate_claims_as_today(organization, workspace, monkeypatch):
    now = timezone.now()
    with_provider = _project(
        organization,
        workspace,
        "Provider without the flag",
        last_claimed=now - timedelta(days=2),
    )
    _provider(with_provider, {"region": "us"})
    without_provider = _project(
        organization, workspace, "No provider", last_claimed=now - timedelta(days=1)
    )
    provider_head, provider_next = _jobs(
        with_provider, 2, first_due=now - timedelta(hours=1)
    )
    (plain_head,) = _jobs(without_provider, 1, first_due=now - timedelta(hours=1))
    # Answers "marked" for both heads, so a lookup that should not run shows.
    lookup = _use(
        monkeypatch, FakeMarkLookup([provider_head.trace_id, plain_head.trace_id])
    )

    with capture_logs() as logs:
        claims = _claim(limit=2)

    assert lookup.calls == []
    assert [(claim["job_id"], claim["trace_id"]) for claim in claims] == [
        (provider_head.id, provider_head.trace_id),
        (plain_head.id, plain_head.trace_id),
    ]
    assert claims[0]["evidence_source"] == "conversation"
    assert "evidence_source" not in claims[1]
    assert (_state(provider_head), _state(provider_next), _state(plain_head)) == (
        RUNNING,
        WAITING,
        RUNNING,
    )
    assert [
        entry["event"]
        for entry in logs
        if entry["event"].startswith("trace_investigation_mark")
        or entry["event"] == "trace_investigation_skipped_simulated"
    ] == []


@pytest.mark.parametrize(
    ("budget_override", "step_seconds", "timeouts", "error"),
    [
        # Two lookups use the budget exactly, on the budget as shipped.
        (None, 2, [2000, 2000], None),
        # What is left of the budget caps the next lookup's timeout.
        (3000, 2, [2000, 1000], None),
        # Under a millisecond left is spent, not handed on as a timeout of 0.
        (None, 1.9998, [2000, 2000], None),
        # A lookup cut off at its timeout spends the budget as one that answers.
        (None, 2, [2000, 2000], MarkedLookupFailed("mark lookup timed out")),
    ],
    ids=[
        "whole_budget",
        "remainder_caps_timeout",
        "under_a_millisecond_left",
        "failed_lookups",
    ],
)
def test_mark_budget_bounds_one_claim_call(
    organization,
    workspace,
    monkeypatch,
    budget_override,
    step_seconds,
    timeouts,
    error,
):
    budget_ms = budget_override or 4000
    if budget_override is not None:
        monkeypatch.setattr(
            trace_investigation, "INVESTIGATION_MARK_BUDGET_MS", budget_override
        )
    now = timezone.now()
    holding = [
        _project(
            organization,
            workspace,
            f"Holding {index}",
            holding=True,
            last_claimed=now - timedelta(days=2) + timedelta(seconds=index),
        )
        for index in range(30)
    ]
    plain = [
        _project(
            organization,
            workspace,
            f"Not holding {index}",
            last_claimed=now - timedelta(days=1) + timedelta(seconds=index),
        )
        for index in range(3)
    ]
    jobs = {
        project.id: _jobs(project, 1, first_due=now - timedelta(hours=1))[0]
        for project in holding + plain
    }
    # A fake clock instead of real two-second sleeps: the claim reads lookup
    # time through one name, so the budget is exercised exactly and the suite
    # neither waits four seconds nor flakes on a slow machine.
    clock = FakeClock()
    clock_start = clock.now
    monkeypatch.setattr(trace_investigation, "monotonic", clock)
    lookup = _use(
        monkeypatch, FakeMarkLookup(clock=clock, seconds=step_seconds, error=error)
    )
    before = timezone.now()

    with capture_logs() as logs:
        claims = _claim(limit=20)

    assert clock.now - clock_start <= budget_ms / 1000
    assert [call["timeout_ms"] for call in lookup.calls] == timeouts
    answered = set() if error else {holding[0].id, holding[1].id}
    assert {claim["project_id"] for claim in claims} == answered | {
        project.id for project in plain
    }
    skipped = holding[2:]
    assert {_state(jobs[project.id]) for project in skipped} == {WAITING}
    rotated = {_rotated_at(project) for project in skipped}
    assert len(rotated) == 1 and rotated.pop() >= before
    assert _events(logs, "trace_investigation_mark_budget_spent") == [
        {
            "event": "trace_investigation_mark_budget_spent",
            "log_level": "warning",
            "skipped_count": 28,
        }
    ]


def test_null_trace_id_job_is_not_looked_up(organization, workspace, monkeypatch):
    now = timezone.now()
    # The sampling check reads a null trace id as the string "None". At this rate
    # that string is unsampled, so a null job that reached the check, or got past
    # the decided-set check, would be cancelled instead of left for the
    # simulation loop.
    unsampled_rate = 0.4
    assert not is_trace_sampled("None", unsampled_rate)
    project = _project(
        organization,
        workspace,
        "Holding",
        holding=True,
        last_claimed=now - timedelta(days=1),
        sampling_rate=unsampled_rate,
    )
    scenario = Scenarios.objects.create(
        name="Refund",
        source="Refund policy",
        organization=organization,
        workspace=workspace,
    )
    run = RunTest.objects.create(
        name="Simulation run", organization=organization, workspace=workspace
    )
    execution = TestExecution.objects.create(
        run_test=run, status=TestExecution.ExecutionStatus.COMPLETED
    )
    call_execution = CallExecution.objects.create(
        test_execution=execution,
        scenario=scenario,
        status=CallExecution.CallStatus.COMPLETED,
    )
    # The claim lock does not filter the workload, so this job heads the queue.
    no_trace = TraceInvestigationJob.no_workspace_objects.create(
        organization=organization,
        workspace=workspace,
        project=project,
        workload_type=InvestigationWorkload.SIMULATION_TEST_EXECUTION,
        test_execution=execution,
        call_execution=call_execution,
        not_before=now - timedelta(hours=2),
    )
    (traced,) = _jobs(project, 1, first_due=now - timedelta(hours=1))
    lookup = _use(monkeypatch, FakeMarkLookup())
    before = timezone.now()

    claims = _claim(limit=2)

    assert [call["trace_ids"] for call in lookup.calls] == [[str(traced.trace_id)]]
    assert _rotated_at(project) >= before
    assert _state(traced) == WAITING
    # The trace loop left it waiting; the simulation loop claimed it with its own memory.
    assert [claim["job_id"] for claim in claims] == [no_trace.id]
    assert {claim["memory"]["snapshot_id"] for claim in claims} == {
        str(uuid.uuid5(uuid.NAMESPACE_URL, f"simulation-memory:{no_trace.id}"))
    }
