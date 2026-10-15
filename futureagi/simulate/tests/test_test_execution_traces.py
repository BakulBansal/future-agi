import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from django.utils import timezone
from rest_framework import status

from model_hub.models.ai_model import AIModel
from simulate.models import (
    AgentDefinition,
    CallExecution,
    HostedHarnessJob,
    RunTest,
    Scenarios,
    SimulatorAgent,
    TestExecution,
)
from simulate.services import run_traces_v3
from tracer.models.project import Project
from tracer.services.clickhouse.client import CHError

URL = "/simulate/v3/test-executions/{}/traces/"
NOTHING = {"project_id": None, "traces": []}


class FakeClickHouse:
    """Spans as (project id, job id, row); the read keeps one project's rows of one job."""

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
        kept = [
            row
            for project_id, job_id, row in self.rows
            if project_id == params["project_id"] and job_id == params["job_id"]
        ]
        if "call_execution_id" in params:
            kept = [row for row in kept if row[6] == params["call_execution_id"]]
        kept.sort(key=lambda row: (bool(row[1]), -row[3].timestamp(), row[0]))
        return kept[: params["limit"]], [], 1.0


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


@pytest.fixture
def scenario(db, organization, workspace, agent):
    return Scenarios.objects.create(
        name="Book a ride",
        description="Test scenario",
        source="Test source",
        scenario_type=Scenarios.ScenarioTypes.DATASET,
        organization=organization,
        workspace=workspace,
        agent_definition=agent,
    )


@pytest.fixture
def call(db, execution, scenario):
    return CallExecution.objects.create(test_execution=execution, scenario=scenario)


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


def _project(organization, workspace, user, name):
    return Project.objects.create(
        organization=organization,
        workspace=workspace,
        user=user,
        model_type=AIModel.ModelTypes.GENERATIVE_LLM,
        name=name,
        trace_type="observe",
    )


@pytest.fixture
def hidden_project(db, organization, workspace, user, agent, monkeypatch):
    project = _project(organization, workspace, user, f"fi-simulation-{agent.id}")
    monkeypatch.setattr(
        run_traces_v3,
        "simulation_project_id",
        lambda of: str(project.id) if of.id == agent.id else None,
    )
    return project


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


def _row(
    trace_id, start_time, *, name="turn", status_text="OK", latency_ms=12, call_id=""
):
    return (trace_id, True, name, start_time, status_text, latency_ms, call_id)


def _rootless(trace_id, start_time, call_id=""):
    return (trace_id, False, "", start_time, "", 0, call_id)


def _parsed(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_run_traces_listed_from_the_hidden_project_for_a_bound_source_agent(
    auth_client,
    execution,
    job,
    call,
    hidden_project,
    clickhouse,
    summary_kind,
    organization,
    workspace,
    user,
):
    production = _project(organization, workspace, user, "Production")
    newest = timezone.now().replace(microsecond=0)
    older = newest - timedelta(minutes=3)
    hidden, this_job = str(hidden_project.id), str(job.id)
    clickhouse.rows = [
        (
            hidden,
            this_job,
            _row(
                "trace-a",
                older,
                name="verify_identity",
                status_text="ERROR",
                latency_ms=420,
            ),
        ),
        (hidden, str(uuid.uuid4()), _row("trace-x", newest)),
        (
            hidden,
            this_job,
            _row(
                "trace-b",
                newest,
                name="book_ride",
                latency_ms=1830,
                call_id=str(call.id),
            ),
        ),
        (str(production.id), this_job, _row("trace-p", newest)),
        (hidden, this_job, _rootless("trace-r", older)),
    ]

    response = auth_client.get(URL.format(execution.id))

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["project_id"] == hidden
    assert [trace["trace_id"] for trace in body["traces"]] == [
        "trace-r",
        "trace-b",
        "trace-a",
    ]
    rootless, linked, unlinked = body["traces"]
    assert rootless == {
        "trace_id": "trace-r",
        "name": None,
        "start_time": None,
        "status": None,
        "latency_ms": None,
        "call_execution_id": None,
    }
    assert (linked["name"], linked["status"], linked["latency_ms"]) == (
        "book_ride",
        "OK",
        1830,
    )
    assert _parsed(linked["start_time"]) == newest
    assert linked["call_execution_id"] == str(call.id)
    assert (unlinked["name"], unlinked["status"], unlinked["call_execution_id"]) == (
        "verify_identity",
        "ERROR",
        None,
    )


def test_the_statement_is_bounded_to_the_project_the_job_and_its_window(
    auth_client, execution, job, hidden_project, clickhouse, summary_kind
):
    auth_client.get(URL.format(execution.id))

    (read,) = clickhouse.calls
    assert "FROM spans FINAL" in read.query
    assert "project_id = %(project_id)s" in read.query
    assert "resource_attrs.fi.simulation.job_id.:String = %(job_id)s" in read.query
    assert "GROUP BY trace_id" in read.query
    assert "ORDER BY has_root ASC, root_start_time DESC, trace_id" in read.query
    assert "HAVING" not in read.query
    assert read.timeout_ms == 2000
    assert read.params == {
        "project_id": str(hidden_project.id),
        "job_id": str(job.id),
        "window_start": job.created_at,
        "window_end": job.terminal_at,
        "limit": 200,
    }


def test_a_first_launch_is_listed_by_the_job_id_alone(
    auth_client, execution, job, hidden_project, clickhouse, summary_kind
):
    clickhouse.rows = [
        (str(hidden_project.id), str(job.id), _row("trace-a", timezone.now()))
    ]

    response = auth_client.get(URL.format(execution.id))

    assert [trace["trace_id"] for trace in response.json()["traces"]] == ["trace-a"]
    (read,) = clickhouse.calls
    assert "test_execution" not in read.query
    assert str(execution.id) not in {str(value) for value in read.params.values()}


def test_a_run_still_going_is_read_up_to_now(
    auth_client,
    execution,
    hidden_project,
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


def test_one_calls_traces(
    auth_client,
    execution,
    job,
    call,
    scenario,
    hidden_project,
    clickhouse,
    summary_kind,
):
    other_call = CallExecution.objects.create(
        test_execution=execution, scenario=scenario
    )
    now = timezone.now()
    hidden, this_job = str(hidden_project.id), str(job.id)
    clickhouse.rows = [
        (hidden, this_job, _row("trace-a", now, call_id=str(call.id))),
        (hidden, this_job, _row("trace-b", now, call_id=str(other_call.id))),
        (hidden, this_job, _row("trace-c", now)),
    ]

    response = auth_client.get(
        URL.format(execution.id), {"call_execution_id": str(call.id)}
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["project_id"] == hidden
    assert [trace["trace_id"] for trace in response.json()["traces"]] == ["trace-a"]
    (read,) = clickhouse.calls
    assert "HAVING call_execution_id = %(call_execution_id)s" in read.query
    assert read.params["call_execution_id"] == str(call.id)


@pytest.mark.parametrize("value", ["not-a-uuid", "", str(uuid.uuid4())])
def test_a_call_that_is_not_this_runs_lists_nothing(
    auth_client, execution, job, hidden_project, clickhouse, summary_kind, value
):
    response = auth_client.get(URL.format(execution.id), {"call_execution_id": value})

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"project_id": str(hidden_project.id), "traces": []}
    assert clickhouse.calls == []


def test_a_call_of_another_run_lists_nothing(
    auth_client,
    execution,
    job,
    scenario,
    simulator_agent,
    hidden_project,
    clickhouse,
    summary_kind,
):
    sibling = TestExecution.objects.create(
        run_test=execution.run_test,
        status=TestExecution.ExecutionStatus.COMPLETED,
        simulator_agent=simulator_agent,
        agent_definition=execution.agent_definition,
    )
    foreign_call = CallExecution.objects.create(
        test_execution=sibling, scenario=scenario
    )

    response = auth_client.get(
        URL.format(execution.id), {"call_execution_id": str(foreign_call.id)}
    )

    assert response.json()["traces"] == []
    assert clickhouse.calls == []


def test_an_unbound_run_lists_nothing(
    auth_client, execution, hidden_project, clickhouse, summary_kind
):
    response = auth_client.get(URL.format(execution.id))

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == NOTHING
    assert clickhouse.calls == []


@pytest.mark.parametrize("kind", ["hosted", None])
def test_an_agent_that_is_not_source_lists_nothing(
    auth_client, execution, job, hidden_project, clickhouse, summary_kind, kind
):
    summary_kind(kind)

    assert auth_client.get(URL.format(execution.id)).json() == NOTHING
    assert clickhouse.calls == []


def test_an_agent_with_no_hidden_project_lists_nothing(
    auth_client, execution, job, clickhouse, summary_kind, monkeypatch
):
    monkeypatch.setattr(run_traces_v3, "simulation_project_id", lambda agent: None)

    assert auth_client.get(URL.format(execution.id)).json() == NOTHING
    assert clickhouse.calls == []


@pytest.mark.parametrize(
    "error", [CHError("boom"), TimeoutError("ClickHouse read deadline exhausted")]
)
def test_a_clickhouse_failure_is_the_views_error(
    auth_client, execution, job, hidden_project, clickhouse, summary_kind, error
):
    clickhouse.error = error

    response = auth_client.get(URL.format(execution.id))

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert response.json() == {"detail": "Traces are unavailable right now"}


def test_a_run_of_another_organization_is_not_found(
    auth_client, execution, job, hidden_project, clickhouse, summary_kind
):
    assert (
        auth_client.get(URL.format(uuid.uuid4())).status_code
        == status.HTTP_404_NOT_FOUND
    )
    assert clickhouse.calls == []
