"""The hidden simulation project of an agent definition."""

import inspect
import json
import threading
import uuid

import pytest
import structlog
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection
from django.db.migrations import AddField, AlterModelOptions
from django.db.migrations.loader import MigrationLoader
from django.db.models import CASCADE, OneToOneField

from accounts.models.workspace import Workspace
from model_hub.models.ai_model import AIModel
from simulate.models.agent_definition import AgentDefinition
from simulate.models.run_test import RunTest
from simulate.models.test_execution import TestExecution
from tfc.middleware.workspace_context import (
    clear_workspace_context,
    set_workspace_context,
)
from tracer.models.project import Project
from tracer.services import simulation_project
from tracer.services.simulation_project import (
    SIMULATION_PROJECT_PREFIX,
    SimulationProjectError,
    agent_id_in_simulation_project_name,
    agent_of_run,
    ensure_simulation_project,
    simulation_project_name,
    simulation_project_of,
)
from tracer.utils.helper import get_default_project_version_config

pytestmark = pytest.mark.django_db

AGENT_ID = str(uuid.UUID("0c6f1a52-93b4-4c1e-8d27-5a3e9b7f4d10"))
RESERVED_NAME = SIMULATION_PROJECT_PREFIX + AGENT_ID
NOT_RESERVED_NAMES = [
    SIMULATION_PROJECT_PREFIX + AGENT_ID.upper(),
    SIMULATION_PROJECT_PREFIX + AGENT_ID.replace("-", ""),
    SIMULATION_PROJECT_PREFIX + "{" + AGENT_ID + "}",
    SIMULATION_PROJECT_PREFIX + "urn:uuid:" + AGENT_ID,
    SIMULATION_PROJECT_PREFIX + AGENT_ID[:-1],
    SIMULATION_PROJECT_PREFIX + AGENT_ID + "0",
    RESERVED_NAME + "\n",
    " " + RESERVED_NAME,
    RESERVED_NAME + " ",
    RESERVED_NAME + "-x",
    SIMULATION_PROJECT_PREFIX,
    "Fi-Simulation-" + AGENT_ID,
    "fi-simulation",
]

COLLECTORS_UPSERT = """
INSERT INTO tracer_project (id, name, organization_id, workspace_id, trace_type,
    model_type, source, tags, deleted, created_at, updated_at, simulation_agent_id)
SELECT %s, %s, a.organization_id, a.workspace_id, 'observe', 'GenerativeLLM',
    'simulator', '[]', false, now(), now(), a.id
FROM simulate_agent_definition a
WHERE a.id = %s AND a.deleted = false
ON CONFLICT (simulation_agent_id) DO UPDATE SET updated_at = EXCLUDED.updated_at
WHERE tracer_project.deleted = false
RETURNING id
"""


def _agent(organization, workspace, name="Simulated agent"):
    return AgentDefinition.objects.create(
        agent_name=name,
        description=f"{name} description",
        agent_type="text",
        inbound=True,
        organization=organization,
        workspace=workspace,
    )


def _agent_without_context(organization, workspace, name="Simulated agent"):
    clear_workspace_context()
    agent = _agent(organization, workspace, name)
    clear_workspace_context()
    return agent


def _collectors_upsert(agent):
    with connection.cursor() as cursor:
        cursor.execute(
            COLLECTORS_UPSERT,
            [uuid.uuid4(), simulation_project_name(agent.id), agent.id],
        )
        return cursor.fetchall()


def _row(project):
    return Project.with_simulation.filter(pk=project.pk).values().get()


def test_migration_adds_one_nullable_unique_column():
    field = Project._meta.get_field("simulation_agent")
    assert isinstance(field, OneToOneField)
    assert field.null is True
    assert field.unique is True
    assert field.remote_field.on_delete is CASCADE
    assert field.remote_field.related_name == "simulation_project"
    assert field.related_model is AgentDefinition

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'tracer_project' "
            "AND column_name = 'simulation_agent_id'"
        )
        assert cursor.fetchall() == [("uuid", "YES")]
        cursor.execute(
            "SELECT i.indpred IS NULL FROM pg_index i "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid "
            "AND a.attnum = ANY (i.indkey) "
            "WHERE i.indrelid = 'tracer_project'::regclass "
            "AND a.attname = 'simulation_agent_id' "
            "AND i.indisunique AND i.indnatts = 1"
        )
        assert cursor.fetchall() == [(True,)]

    graph = MigrationLoader(connection).graph
    migration = graph.nodes[("tracer", "0115_project_simulation_agent")]
    assert [type(operation) for operation in migration.operations] == [
        AlterModelOptions,
        AddField,
    ]
    assert ("simulate", "0097_merge_selected_runs_environment_v3") in (
        migration.dependencies
    )


def test_hard_delete_of_the_agent_removes_the_project(organization, workspace):
    agent = _agent(organization, workspace)
    project = Project.with_simulation.create(
        name=simulation_project_name(agent.id),
        organization=organization,
        workspace=workspace,
        model_type=AIModel.ModelTypes.GENERATIVE_LLM,
        trace_type="observe",
        simulation_agent=agent,
    )
    assert Project.with_simulation.filter(pk=project.pk).exists()

    AgentDefinition._base_manager.filter(pk=agent.pk).delete()

    assert not Project.with_simulation.filter(pk=project.pk).exists()


def test_collectors_upsert_statement_runs_against_the_column(organization, workspace):
    agent = _agent(organization, workspace)

    [(project_id,)] = _collectors_upsert(agent)
    assert Project.with_simulation.filter(simulation_agent_id=agent.id).count() == 1
    assert not Project.objects.filter(pk=project_id).exists()

    assert _collectors_upsert(agent) == [(project_id,)]
    assert Project.with_simulation.filter(simulation_agent_id=agent.id).count() == 1

    Project.with_simulation.filter(pk=project_id).update(deleted=True)
    assert _collectors_upsert(agent) == []

    other = _agent(organization, workspace, "Another agent")
    AgentDefinition._base_manager.filter(pk=other.pk).update(deleted=True)
    assert _collectors_upsert(other) == []
    assert not Project.with_simulation.filter(simulation_agent_id=other.id).exists()


@pytest.mark.django_db(transaction=True)
def test_ensure_is_idempotent_and_race_safe(organization, workspace, monkeypatch):
    agent = _agent_without_context(organization, workspace)

    with structlog.testing.capture_logs() as events:
        first = ensure_simulation_project(agent)
        second = ensure_simulation_project(agent)

    assert first.id == second.id
    assert Project.with_simulation.filter(simulation_agent_id=agent.id).count() == 1
    [event] = [e for e in events if e["event"] == "simulation_project_created"]
    assert set(event) == {
        "event",
        "log_level",
        "agent_definition_id",
        "project_id",
        "by",
    }
    assert event["agent_definition_id"] == str(agent.id)
    assert event["project_id"] == str(first.id)
    assert event["by"] == "platform"

    racing_agent = _agent_without_context(organization, workspace, "Racing agent")
    barrier = threading.Barrier(2, timeout=10)
    save = Project.save

    def save_when_both_missed(self, *args, **kwargs):
        if self.simulation_agent_id == racing_agent.id:
            barrier.wait()
        return save(self, *args, **kwargs)

    monkeypatch.setattr(Project, "save", save_when_both_missed)
    project_ids = []
    errors = []

    def ensure():
        try:
            project_ids.append(ensure_simulation_project(racing_agent).id)
        except BaseException as exc:
            errors.append(exc)
        finally:
            connection.close()

    with structlog.testing.capture_logs() as race_events:
        threads = [threading.Thread(target=ensure) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

    assert errors == []
    assert len(project_ids) == 2
    assert project_ids[0] == project_ids[1]
    assert (
        Project.with_simulation.filter(simulation_agent_id=racing_agent.id).count()
        == 1
    )
    assert [e["event"] for e in race_events].count("simulation_project_created") == 1


def test_name_workspace_type_source_and_no_user(organization, workspace):
    agent = _agent_without_context(organization, workspace)

    project = ensure_simulation_project(agent)

    project = Project.with_simulation.get(pk=project.pk)
    expected_name = "fi-simulation-" + str(agent.id)
    assert project.name == expected_name
    assert len(project.name) == 50
    assert simulation_project_name(str(agent.id).upper()) == expected_name
    assert project.trace_type == "observe"
    assert project.source == "simulator"
    assert project.model_type == "GenerativeLLM"
    default_config = json.loads(json.dumps(get_default_project_version_config()))
    assert project.config == default_config
    assert project.user_id is None
    assert project.deleted is False
    assert project.simulation_agent_id == agent.id
    assert project.organization_id == organization.id
    assert project.workspace_id == workspace.id


def test_deleted_agent_has_no_project_and_raises(organization, workspace):
    with pytest.raises(SimulationProjectError):
        ensure_simulation_project(None)

    deleted_agent = _agent_without_context(organization, workspace, "Deleted agent")
    deleted_agent.delete()
    with pytest.raises(SimulationProjectError):
        ensure_simulation_project(deleted_agent)
    assert not Project.with_simulation.filter(
        simulation_agent_id=deleted_agent.id
    ).exists()

    stale_agent = _agent_without_context(organization, workspace, "Stale agent")
    AgentDefinition._base_manager.filter(pk=stale_agent.pk).update(deleted=True)
    assert stale_agent.deleted is False
    with pytest.raises(SimulationProjectError):
        ensure_simulation_project(stale_agent)
    assert not Project.with_simulation.filter(
        simulation_agent_id=stale_agent.id
    ).exists()

    agent = _agent_without_context(organization, workspace, "Agent with a project")
    project = ensure_simulation_project(agent)
    before = _row(project)
    AgentDefinition._base_manager.filter(pk=agent.pk).update(deleted=True)
    with pytest.raises(SimulationProjectError):
        ensure_simulation_project(agent)
    assert _row(project) == before


def test_agent_without_workspace_gets_the_default(organization, user, workspace):
    first = Workspace.objects.create(
        name="First", organization=organization, created_by=user
    )
    second = Workspace.objects.create(
        name="Second", organization=organization, created_by=user
    )

    # (a) no workspace: the organization's default
    agent = _agent_without_context(organization, None, "No workspace")
    agent.refresh_from_db()
    assert agent.workspace_id is None
    project = ensure_simulation_project(agent)
    assert project.workspace_id == workspace.id

    # (b) its own non-default workspace
    agent = _agent_without_context(organization, first, "Own workspace")
    project = ensure_simulation_project(agent)
    assert project.workspace_id == first.id

    # (c) the ambient workspace is neither of them
    agent = _agent_without_context(organization, None, "Ambient, no workspace")
    agent.refresh_from_db()
    assert agent.workspace_id is None
    set_workspace_context(workspace=second, organization=organization)
    project = ensure_simulation_project(agent)
    assert project.workspace_id == workspace.id
    clear_workspace_context()

    agent = _agent_without_context(organization, first, "Ambient, own workspace")
    set_workspace_context(workspace=second, organization=organization)
    project = ensure_simulation_project(agent)
    assert project.workspace_id == first.id
    clear_workspace_context()

    # (d) an inactive default workspace is not a fallback, but it is the agent's own
    Workspace.objects.filter(pk=workspace.pk).update(is_active=False)
    agent = _agent_without_context(organization, None, "Inactive default")
    agent.refresh_from_db()
    assert agent.workspace_id is None
    with pytest.raises(SimulationProjectError):
        ensure_simulation_project(agent)
    assert not Project.with_simulation.filter(simulation_agent_id=agent.id).exists()

    agent = _agent_without_context(organization, workspace, "In the inactive default")
    project = ensure_simulation_project(agent)
    assert project.workspace_id == workspace.id

    # a soft-deleted default workspace is not a fallback either
    Workspace.objects.filter(pk=workspace.pk).update(is_active=True, deleted=True)
    agent = _agent_without_context(organization, None, "Deleted default")
    agent.refresh_from_db()
    assert agent.workspace_id is None
    with pytest.raises(SimulationProjectError):
        ensure_simulation_project(agent)
    assert not Project.with_simulation.filter(simulation_agent_id=agent.id).exists()

    # (e) never without a workspace
    assert not Project.with_simulation.filter(
        organization=organization, workspace__isnull=True
    ).exists()


def test_agent_of_run_is_the_executions_agent_else_the_run_tests(
    organization, workspace
):
    clear_workspace_context()
    run_agent = _agent(organization, workspace, "Run agent")
    execution_agent = _agent(organization, workspace, "Execution agent")

    def execution(run_test_agent, agent):
        run = RunTest.objects.create(
            name="Run",
            organization=organization,
            workspace=workspace,
            agent_definition=run_test_agent,
        )
        created = TestExecution.objects.create(
            run_test=run,
            status=TestExecution.ExecutionStatus.COMPLETED,
            agent_definition=agent,
        )
        return TestExecution._base_manager.get(pk=created.pk)

    assert agent_of_run(execution(None, execution_agent)) == execution_agent
    assert agent_of_run(execution(run_agent, None)) == run_agent
    assert agent_of_run(execution(run_agent, execution_agent)) == execution_agent
    assert agent_of_run(execution(None, None)) is None

    execution_agent.delete()
    returned = agent_of_run(execution(run_agent, execution_agent))
    assert returned == execution_agent
    assert returned.deleted is True


def test_public_signatures_and_constants():
    assert list(inspect.signature(simulation_project_name).parameters) == [
        "agent_definition_id"
    ]
    assert list(
        inspect.signature(agent_id_in_simulation_project_name).parameters
    ) == ["name"]
    assert list(inspect.signature(ensure_simulation_project).parameters) == [
        "agent_definition"
    ]
    assert list(inspect.signature(simulation_project_of).parameters) == [
        "agent_definition_id"
    ]
    assert list(inspect.signature(agent_of_run).parameters) == ["test_execution"]
    assert simulation_project.SIMULATION_PROJECT_PREFIX == "fi-simulation-"
    assert (
        simulation_project.RESERVED_PROJECT_NAME_MESSAGE
        == "that project name is reserved"
    )
    assert issubclass(SimulationProjectError, Exception)


def test_agent_id_in_name_is_exact():
    assert agent_id_in_simulation_project_name(RESERVED_NAME) == AGENT_ID
    for name in NOT_RESERVED_NAMES + [None, 7]:
        assert agent_id_in_simulation_project_name(name) is None, repr(name)


def test_ensure_refuses_a_deleted_hidden_project(organization, workspace):
    agent = _agent_without_context(organization, workspace)
    project = ensure_simulation_project(agent)
    visible = Project.objects.create(
        name="A visible project",
        organization=organization,
        workspace=workspace,
        model_type=AIModel.ModelTypes.GENERATIVE_LLM,
        trace_type="observe",
    )

    assert simulation_project_of(agent.id) == project
    assert simulation_project_of(uuid.uuid4()) is None
    assert simulation_project_of(None) is None

    Project.with_simulation.filter(pk=project.pk).update(deleted=True)
    assert simulation_project_of(agent.id) == project
    before = _row(project)
    with pytest.raises(SimulationProjectError):
        ensure_simulation_project(agent)
    assert _row(project) == before
    assert before["deleted"] is True


def test_name_conflict_with_an_older_visible_project_propagates(
    organization, user, workspace
):
    agent = _agent_without_context(organization, workspace)
    Project.no_workspace_objects.create(
        name=simulation_project_name(agent.id),
        organization=organization,
        workspace=workspace,
        model_type=AIModel.ModelTypes.GENERATIVE_LLM,
        trace_type="observe",
    )

    with pytest.raises((ValidationError, IntegrityError)):
        ensure_simulation_project(agent)
    assert not Project.with_simulation.filter(simulation_agent_id=agent.id).exists()

    other_workspace = Workspace.objects.create(
        name="Other", organization=organization, created_by=user
    )
    set_workspace_context(workspace=other_workspace, organization=organization)
    with pytest.raises((ValidationError, IntegrityError)):
        ensure_simulation_project(agent)
    clear_workspace_context()
    assert not Project.with_simulation.filter(simulation_agent_id=agent.id).exists()


def test_collectors_upsert_meets_the_platform_row(organization, workspace):
    agent = _agent_without_context(organization, workspace)
    project = ensure_simulation_project(agent)

    assert _collectors_upsert(agent) == [(project.id,)]
    assert Project.with_simulation.filter(simulation_agent_id=agent.id).count() == 1

    other = _agent_without_context(organization, workspace, "Collector first")
    [(project_id,)] = _collectors_upsert(other)
    with structlog.testing.capture_logs() as events:
        ensured = ensure_simulation_project(other)
    assert ensured.id == project_id
    assert Project.with_simulation.filter(simulation_agent_id=other.id).count() == 1
    assert not [e for e in events if e["event"] == "simulation_project_created"]


def test_deleting_an_agent_leaves_its_hidden_project_untouched_and_ensure_refuses(
    organization, workspace
):
    from simulate.views.agent_definition import (
        soft_delete_agent_definition_and_versions,
    )

    def delete_through_the_model(agent):
        agent.delete()

    def delete_through_the_view_helper(agent):
        soft_delete_agent_definition_and_versions(agent)

    def delete_through_a_queryset(agent):
        AgentDefinition._base_manager.filter(pk=agent.pk).update(deleted=True)

    for delete in (
        delete_through_the_model,
        delete_through_the_view_helper,
        delete_through_a_queryset,
    ):
        agent = _agent_without_context(organization, workspace, delete.__name__)
        project = ensure_simulation_project(agent)
        before = _row(project)

        delete(agent)

        assert _row(project) == before
        with pytest.raises(SimulationProjectError):
            ensure_simulation_project(agent)
        assert _row(project) == before
