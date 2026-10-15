import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from django.utils import timezone
from rest_framework import status

from model_hub.models.ai_model import AIModel
from simulate.models import (
    AgentDefinition,
    HostedHarnessJob,
    RunTest,
    SimulatorAgent,
    TestExecution,
)
from simulate.services import run_traces_v3
from tracer.models.observability_provider import ObservabilityProvider, ProviderChoices
from tracer.models.project import Project
from tracer.services.clickhouse.client import CHError

URL = "/simulate/v3/test-executions/{}/traces/"
NOTHING = {
    "observability": "unavailable",
    "agent_id": None,
    "project_id": None,
    "count": 0,
    "page": 1,
    "page_size": 20,
    "total_pages": 1,
    "traces": [],
}


class FakeClickHouse:
    """Rows keyed by the job id whose mark they carry; the read keeps only the job asked for."""

    def __init__(self):
        self.rows = []
        self.error = None
        self.calls = []

    def execute_read(self, query, params=None, timeout_ms=None, settings=None):
        self.calls.append(
            SimpleNamespace(query=query, params=params, timeout_ms=timeout_ms)
        )
        if self.error is not None:
            raise self.error
        kept = [row for job_id, row in self.rows if job_id == params["job_id"]]
        if query.startswith("SELECT uniqExact(trace_id)"):
            return [(len(kept),)], [], 1.0
        kept.sort(key=lambda row: (-row[2].timestamp(), row[0]))
        offset = params["offset"]
        return kept[offset : offset + params["limit"]], [], 1.0


@pytest.fixture
def agent(db, organization, workspace):
    return AgentDefinition.objects.create(
        agent_name="code agent",
        agent_type=AgentDefinition.AgentTypeChoices.TEXT,
        inbound=True,
        organization=organization,
        workspace=workspace,
    )


@pytest.fixture
def simulator_agent(db, organization, workspace):
    return SimulatorAgent.objects.create(
        name="Traces simulator",
        prompt="You are a test simulator.",
        voice_provider="elevenlabs",
        voice_name="marissa",
        model="gpt-4",
        organization=organization,
        workspace=workspace,
    )


@pytest.fixture
def execution(db, organization, workspace, agent, simulator_agent):
    run_test = RunTest.objects.create(
        name="Traces run",
        agent_definition=agent,
        simulator_agent=simulator_agent,
        organization=organization,
        workspace=workspace,
    )
    return TestExecution.objects.create(
        run_test=run_test,
        status=TestExecution.ExecutionStatus.COMPLETED,
        simulator_agent=simulator_agent,
        agent_definition=agent,
    )


def _job(organization, workspace, **fields):
    return HostedHarnessJob.no_workspace_objects.create(
        organization=organization,
        workspace=workspace,
        run_id=uuid.uuid4(),
        idempotency_key=uuid.uuid4().hex,
        request_digest="digest",
        schema_version="1.6",
        seed=1,
        artifact_level="standard",
        max_artifact_bytes=1024,
        deadline_at=timezone.now() + timedelta(hours=1),
        scenario_count=1,
        payload={},
        **fields,
    )


@pytest.fixture
def job(db, organization, workspace, execution):
    return _job(
        organization, workspace, test_execution=execution, terminal_at=timezone.now()
    )


@pytest.fixture
def project(db, organization, workspace, user):
    return Project.objects.create(
        organization=organization,
        workspace=workspace,
        user=user,
        model_type=AIModel.ModelTypes.GENERATIVE_LLM,
        name=f"Agent project {uuid.uuid4()}",
        trace_type="observe",
    )


@pytest.fixture
def observability_row(db, organization, workspace, agent, project):
    row = ObservabilityProvider.objects.create(
        project=project,
        provider=ProviderChoices.OTHERS,
        enabled=True,
        organization=organization,
        workspace=workspace,
    )
    agent.observability_provider = row
    agent.save(update_fields=["observability_provider"])
    return row


@pytest.fixture
def clickhouse(monkeypatch):
    fake = FakeClickHouse()
    monkeypatch.setattr(run_traces_v3, "get_clickhouse_client", lambda: fake)
    return fake


@pytest.fixture
def summary_kind(monkeypatch):
    def set_kind(kind):
        monkeypatch.setattr(
            run_traces_v3, "agent_summary", lambda agent_id: SimpleNamespace(kind=kind)
        )

    set_kind("source")
    return set_kind


def _row(trace_id, name, start_time, status_text="OK", latency_ms=12.5):
    return (trace_id, name, start_time, status_text, latency_ms)


def _parsed(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_run_traces_listed_by_the_mark_for_a_bound_source_agent_with_observability_on(
    auth_client, execution, job, observability_row, clickhouse, summary_kind
):
    newest = timezone.now().replace(microsecond=0)
    older = newest - timedelta(minutes=3)
    other_job = str(uuid.uuid4())
    clickhouse.rows = [
        (str(job.id), _row("trace-a", "verify_identity", older, "ERROR", 420.0)),
        (other_job, _row("trace-x", "someone_elses", newest, "OK", 1.0)),
        (str(job.id), _row("trace-b", "book_ride", newest, "OK", 1830.0)),
    ]

    response = auth_client.get(URL.format(execution.id))

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["project_id"] == str(observability_row.project_id)
    assert (body["count"], body["page"], body["page_size"], body["total_pages"]) == (
        2,
        1,
        20,
        1,
    )
    assert [trace["trace_id"] for trace in body["traces"]] == ["trace-b", "trace-a"]
    first = body["traces"][0]
    assert first["name"] == "book_ride"
    assert first["status"] == "OK"
    assert first["latency_ms"] == 1830.0
    assert _parsed(first["start_time"]) == newest

    count_call, page_call = clickhouse.calls
    for call in (count_call, page_call):
        assert call.timeout_ms == 2000
        assert call.params["project_id"] == str(observability_row.project_id)
        assert call.params["job_id"] == str(job.id)
        assert call.params["window_start"] == job.created_at
        assert call.params["window_end"] == job.terminal_at
        assert "WHERE project_id = %(project_id)s" in call.query
        assert "resource_attrs.fi.simulation.job_id.:String = %(job_id)s" in call.query
        assert "start_time >= %(window_start)s" in call.query
        assert "start_time <= %(window_end)s" in call.query
    assert count_call.query.startswith("SELECT uniqExact(trace_id) ")
    assert "GROUP BY trace_id" in page_call.query
    assert "ORDER BY started_at DESC, trace_id" in page_call.query
    assert "LIMIT %(limit)s OFFSET %(offset)s" in page_call.query
    assert (page_call.params["limit"], page_call.params["offset"]) == (20, 0)


def _many(job, count):
    newest = timezone.now().replace(microsecond=0)
    return [
        (
            str(job.id),
            _row(f"trace-{n:02d}", f"call_{n:02d}", newest - timedelta(minutes=n)),
        )
        for n in range(count)
    ]


def test_a_later_page_lists_the_next_traces_and_the_whole_count(
    auth_client, execution, job, observability_row, clickhouse, summary_kind
):
    clickhouse.rows = _many(job, 7)

    body = auth_client.get(URL.format(execution.id), {"page": 2, "page_size": 3}).json()

    assert (body["count"], body["page"], body["page_size"], body["total_pages"]) == (
        7,
        2,
        3,
        3,
    )
    assert [trace["trace_id"] for trace in body["traces"]] == [
        "trace-03",
        "trace-04",
        "trace-05",
    ]
    assert (
        clickhouse.calls[-1].params["limit"],
        clickhouse.calls[-1].params["offset"],
    ) == (3, 3)


def test_the_last_page_is_short(
    auth_client, execution, job, observability_row, clickhouse, summary_kind
):
    clickhouse.rows = _many(job, 7)

    body = auth_client.get(URL.format(execution.id), {"page": 3, "page_size": 3}).json()

    assert [trace["trace_id"] for trace in body["traces"]] == ["trace-06"]


def test_a_page_past_the_end_reads_only_the_count(
    auth_client, execution, job, observability_row, clickhouse, summary_kind
):
    clickhouse.rows = _many(job, 2)

    body = auth_client.get(
        URL.format(execution.id), {"page": 5, "page_size": 20}
    ).json()

    assert (body["count"], body["total_pages"], body["traces"]) == (2, 1, [])
    assert len(clickhouse.calls) == 1


def test_a_run_with_no_traces_reads_only_the_count(
    auth_client, execution, job, observability_row, clickhouse, summary_kind
):
    body = auth_client.get(URL.format(execution.id)).json()

    assert body["project_id"] == str(observability_row.project_id)
    assert (body["count"], body["total_pages"], body["traces"]) == (0, 1, [])
    assert len(clickhouse.calls) == 1


@pytest.mark.parametrize("params", [{"page": 0}, {"page_size": 0}, {"page_size": 101}])
def test_out_of_range_paging_is_refused(
    auth_client, execution, job, observability_row, clickhouse, summary_kind, params
):
    response = auth_client.get(URL.format(execution.id), params)

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert clickhouse.calls == []


def test_a_first_launch_is_listed_by_the_job_id_alone(
    auth_client, execution, job, observability_row, clickhouse, summary_kind
):
    clickhouse.rows = [(str(job.id), _row("trace-a", "book_ride", timezone.now()))]

    response = auth_client.get(URL.format(execution.id))

    assert [trace["trace_id"] for trace in response.json()["traces"]] == ["trace-a"]
    for call in clickhouse.calls:
        assert "test_execution" not in call.query
        assert str(execution.id) not in {str(value) for value in call.params.values()}


def test_a_run_still_going_is_read_up_to_now(
    auth_client,
    execution,
    observability_row,
    clickhouse,
    summary_kind,
    organization,
    workspace,
):
    live_job = _job(organization, workspace, test_execution=execution, terminal_at=None)
    before = timezone.now()

    auth_client.get(URL.format(execution.id))

    window_end = clickhouse.calls[0].params["window_end"]
    assert live_job.created_at <= before <= window_end <= timezone.now()


def test_an_unbound_run_lists_nothing(
    auth_client, execution, observability_row, clickhouse, summary_kind
):
    response = auth_client.get(URL.format(execution.id))

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == NOTHING
    assert clickhouse.calls == []


def test_a_hosted_agent_lists_nothing(
    auth_client, execution, job, observability_row, clickhouse, summary_kind
):
    summary_kind("hosted")

    response = auth_client.get(URL.format(execution.id))

    assert response.json() == NOTHING
    assert clickhouse.calls == []


def test_an_agent_with_no_stored_summary_lists_nothing(
    auth_client, execution, job, observability_row, clickhouse, summary_kind
):
    summary_kind(None)

    assert auth_client.get(URL.format(execution.id)).json() == NOTHING
    assert clickhouse.calls == []


def _off(agent):
    return {**NOTHING, "observability": "off", "agent_id": str(agent.id)}


def test_observability_off_says_so_and_names_the_agent(
    auth_client, execution, job, agent, observability_row, clickhouse, summary_kind
):
    observability_row.enabled = False
    observability_row.save(update_fields=["enabled"])

    assert auth_client.get(URL.format(execution.id)).json() == _off(agent)
    assert clickhouse.calls == []


def test_an_agent_never_switched_on_is_off(
    auth_client, execution, job, agent, project, clickhouse, summary_kind
):
    assert auth_client.get(URL.format(execution.id)).json() == _off(agent)
    assert clickhouse.calls == []


def test_a_deleted_project_is_off(
    auth_client,
    execution,
    job,
    agent,
    observability_row,
    project,
    clickhouse,
    summary_kind,
):
    project.deleted = True
    project.save(update_fields=["deleted"])

    assert auth_client.get(URL.format(execution.id)).json() == _off(agent)
    assert clickhouse.calls == []


def test_a_traced_run_says_observability_is_on(
    auth_client, execution, job, agent, observability_row, clickhouse, summary_kind
):
    body = auth_client.get(URL.format(execution.id)).json()

    assert (body["observability"], body["agent_id"]) == ("on", str(agent.id))
    assert body["project_id"] == str(observability_row.project_id)


@pytest.mark.parametrize(
    "error", [CHError("boom"), TimeoutError("ClickHouse read deadline exhausted")]
)
def test_a_clickhouse_failure_is_the_views_error(
    auth_client, execution, job, observability_row, clickhouse, summary_kind, error
):
    clickhouse.error = error

    response = auth_client.get(URL.format(execution.id))

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert response.json() == {"detail": "Traces are unavailable right now"}


def test_a_run_of_another_organization_is_not_found(
    auth_client, execution, job, observability_row, clickhouse, summary_kind
):
    assert (
        auth_client.get(URL.format(uuid.uuid4())).status_code
        == status.HTTP_404_NOT_FOUND
    )
    assert clickhouse.calls == []
