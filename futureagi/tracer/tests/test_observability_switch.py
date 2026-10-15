"""The observability switch, and the provider endpoint's hold on a project's flag.

The switch is the one place an agent's observability is turned on or off. The
holding flag is written by another process whenever a simulated run first exports
to the project, and nothing a request sends, and no save the switch makes, may
clear it.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.db import connection
from django.db.models import QuerySet
from structlog.testing import capture_logs

import tracer.services.observability_switch as switch
from accounts.models.workspace import Workspace
from model_hub.models.ai_model import AIModel
from simulate.models import AgentDefinition
from simulate.services import agent_setup
from simulate.tests.test_agent_setup_service import source_agent
from tfc.middleware.workspace_context import set_workspace_context
from tfc.utils.api_errors import build_error_envelope
from tfc.utils.general_methods import GeneralMethods
from tracer.models.observability_provider import (
    ObservabilityProvider,
    ProviderChoices,
)
from tracer.models.project import Project, ProjectSourceChoices
from tracer.services.simulation_visibility import HOLDING_FLAG, record_holding
from tracer.views.observability_provider import ObservabilityProviderViewSet

PROVIDERS_PATH = "/tracer/observability-provider/"


def _row(organization, workspace, metadata, *, holding=False):
    project = Project.no_workspace_objects.create(
        name=f"switch-{uuid.uuid4().hex[:10]}",
        organization=organization,
        workspace=workspace,
        model_type=AIModel.ModelTypes.GENERATIVE_LLM,
        trace_type="observe",
    )
    row = ObservabilityProvider.no_workspace_objects.create(
        project=project,
        provider=ProviderChoices.VAPI,
        enabled=True,
        organization=organization,
        workspace=workspace,
        metadata=metadata,
    )
    if holding:
        record_holding(str(project.id))
    return row


def _stored(row):
    """The row's (metadata, enabled) as the database holds them."""
    return ObservabilityProvider._base_manager.values_list("metadata", "enabled").get(
        pk=row.pk
    )


def _patch(client, row, payload):
    return client.patch(f"{PROVIDERS_PATH}{row.id}/", payload, format="json")


def _result(response):
    body = response.json()
    return body.get("result", body)


def _statements():
    """Record the SQL text of every statement the connection runs, in order."""
    seen = []

    def spy(execute, sql, params, many, context):
        seen.append(sql)
        return execute(sql, params, many, context)

    return seen, connection.execute_wrapper(spy)


def _depth_at(statements, index):
    """How many savepoints are open when the statement at ``index`` runs."""
    depth = 0
    for sql in statements[:index]:
        if sql.startswith("SAVEPOINT"):
            depth += 1
        elif sql.startswith(("RELEASE SAVEPOINT", "ROLLBACK TO SAVEPOINT")):
            depth -= 1
    return depth


def test_patch_metadata_keeps_holding(auth_client, organization, workspace):
    row = _row(organization, workspace, {"assistant_id": "a1"}, holding=True)
    assert _stored(row) == ({"assistant_id": "a1", HOLDING_FLAG: True}, True)

    # An empty object replaces the rest of metadata but not the flag.
    response = _patch(auth_client, row, {"metadata": {}})
    assert response.status_code == 200
    assert _result(response)["metadata"] == {HOLDING_FLAG: True}
    assert _stored(row) == ({HOLDING_FLAG: True}, True)

    # Another key lands beside the flag.
    response = _patch(auth_client, row, {"metadata": {"assistant_id": "a2"}})
    assert response.status_code == 200
    assert _result(response)["metadata"] == {"assistant_id": "a2", HOLDING_FLAG: True}
    assert _stored(row) == ({"assistant_id": "a2", HOLDING_FLAG: True}, True)

    # A request that names the flag cannot turn it off.
    response = _patch(
        auth_client, row, {"metadata": {HOLDING_FLAG: False, "assistant_id": "a3"}}
    )
    assert response.status_code == 200
    assert _stored(row) == ({"assistant_id": "a3", HOLDING_FLAG: True}, True)

    # A request with no metadata leaves the stored one, flag included.
    response = _patch(auth_client, row, {"enabled": False})
    assert response.status_code == 200
    assert _stored(row) == ({"assistant_id": "a3", HOLDING_FLAG: True}, False)

    # Metadata that is not an object is refused and nothing in the body is
    # written, `enabled` included.
    refused = GeneralMethods().bad_request({"metadata": ["metadata must be an object"]})
    before = _stored(row)
    for not_an_object in ("x", None, ["a"], 7):
        response = _patch(
            auth_client, row, {"metadata": not_an_object, "enabled": True}
        )
        assert response.status_code == 400
        assert response.json() == refused.data
        assert _stored(row) == before

    # A row that does not hold stores whatever it is sent, as it always did.
    plain = _row(organization, workspace, {"assistant_id": "p"})
    response = _patch(auth_client, plain, {"metadata": "x"})
    assert response.status_code == 200
    assert _stored(plain) == ("x", True)
    response = _patch(auth_client, plain, {"metadata": {"k": 1}})
    assert response.status_code == 200
    assert _stored(plain) == ({"k": 1}, True)


def test_patch_keeps_flag_written_after_get_object(
    auth_client, organization, workspace, monkeypatch
):
    original = ObservabilityProviderViewSet.get_object

    def get_object_then_flag_the_project(self):
        found = original(self)
        # The flag lands after the view read the row and before it saves.
        record_holding(str(found.project_id))
        return found

    monkeypatch.setattr(
        ObservabilityProviderViewSet, "get_object", get_object_then_flag_the_project
    )

    for payload, expected in (
        # A save of the row as it was read would put its metadata back.
        ({"enabled": False}, {"assistant_id": "a1", HOLDING_FLAG: True}),
        # The row did not hold when it was read, so the request's metadata would
        # be taken as it is.
        (
            {"metadata": {"assistant_id": "a2"}},
            {"assistant_id": "a2", HOLDING_FLAG: True},
        ),
    ):
        row = _row(organization, workspace, {"assistant_id": "a1"})
        seen, wrapper = _statements()
        with wrapper:
            response = _patch(auth_client, row, payload)

        assert response.status_code == 200
        assert _result(response)["metadata"] == expected
        assert _stored(row)[0] == expected

        # The flag writer ran first, and the view then read the row again with
        # a lock that a later flag write has to wait for.
        flag_written = [i for i, sql in enumerate(seen) if "jsonb_typeof" in sql]
        locked_reads = [
            i
            for i, sql in enumerate(seen)
            if " FOR UPDATE" in sql and "tracer_observability_provider" in sql
        ]
        saved = [
            i
            for i, sql in enumerate(seen)
            if sql.startswith('UPDATE "tracer_observability_provider"')
        ]
        assert flag_written
        assert locked_reads
        assert saved
        assert flag_written[0] < locked_reads[0] < saved[-1]
        # The lock is taken and the save made inside one transaction opened after
        # the flag was written; a row lock lasts only as long as the transaction
        # that took it.
        assert _depth_at(seen, locked_reads[0]) == _depth_at(seen, flag_written[0]) + 1
        assert _depth_at(seen, saved[-1]) == _depth_at(seen, locked_reads[0])


# ---------------------------------------------------------------------------
# The switch
# ---------------------------------------------------------------------------

KEYWORD_ONLY = inspect.Parameter.KEYWORD_ONLY
NO_DEFAULT = inspect.Parameter.empty

SUMMARY_FIELDS = [
    "agent_definition_id",
    "agent_version_id",
    "agent_stored",
    "setup_stale",
    "provider",
    "provider_agent_id",
    "credential_id",
    "key_present",
    "type",
    "kind",
    "unit_ids",
    "instrumentation",
    "setup_hash",
    "provider_info_complete",
    "missing",
]
KEY_MESSAGE = "API key is required when observability is enabled"
AGENT_ID_MESSAGE = "Assistant ID is required when observability is enabled"
INSTRUMENTATION_MESSAGE = (
    "The agent's code carries no OpenTelemetry or traceAI instrumentation the "
    "sandbox can configure (an HTTP exporter, not gRPC alone); add one and "
    "author the agent again"
)
MESSAGE_OF_FIELD = {"api_key": KEY_MESSAGE, "assistant_id": AGENT_ID_MESSAGE}
CODE_OF_FIELD = {
    "api_key": "credential_missing",
    "assistant_id": "provider_agent_id_missing",
}


def make_summary(**overrides):
    """A complete summary of a listed, fully configured hosted agent."""
    values = {
        "agent_definition_id": uuid.uuid4(),
        "agent_version_id": None,
        "agent_stored": False,
        "setup_stale": False,
        "provider": "vapi",
        "provider_agent_id": "assistant-1",
        "credential_id": uuid.uuid4(),
        "key_present": True,
        "type": "voice",
        "kind": "hosted",
        "unit_ids": ("unit-1",),
        "instrumentation": "none",
        "setup_hash": None,
        "provider_info_complete": True,
        "missing": (),
    }
    values.update(overrides)
    if "provider_info_complete" not in overrides:
        values["provider_info_complete"] = not values["missing"]
    return agent_setup.AgentSummary(**values)


@contextmanager
def seams(summary, *, providers=("vapi", "retell", "bland"), published=True):
    """Stand in for the two reads the switch makes; yields their mocks."""
    if published:
        listing = {"return_value": SimpleNamespace(providers=dict.fromkeys(providers))}
    else:
        listing = {"side_effect": switch.ProviderListNotPublished}
    answer = (
        {"side_effect": summary} if callable(summary) else {"return_value": summary}
    )
    with (
        patch.object(switch, "_agent_summary", **answer) as read_summary,
        patch.object(switch, "_read_provider_list", **listing) as read_list,
    ):
        yield read_summary, read_list


def _agent(organization, workspace, **fields):
    values = {
        "agent_name": f"switch-agent-{uuid.uuid4().hex[:8]}",
        "agent_type": AgentDefinition.AgentTypeChoices.VOICE,
        "contact_number": "+12345678901",
        "inbound": True,
        "description": "Switch test agent",
        "provider": "vapi",
        "assistant_id": "assistant-1",
        "api_key": "test-key",
        "organization": organization,
        "workspace": workspace,
        "languages": ["en"],
    }
    values.update(fields)
    return AgentDefinition.objects.create(**values)


def _agent_with_row(organization, workspace, *, enabled, metadata=None, **fields):
    agent = _agent(organization, workspace, **fields)
    row = _row(organization, workspace, metadata or {})
    ObservabilityProvider.no_workspace_objects.filter(pk=row.pk).update(enabled=enabled)
    agent.observability_provider = ObservabilityProvider.no_workspace_objects.get(
        pk=row.pk
    )
    agent.save()
    return agent


def _stored_agent(agent):
    return AgentDefinition.no_workspace_objects.get(pk=agent.pk)


def _turn(agent, enabled, user, organization, workspace):
    return switch.set_agent_observability(
        agent_definition=agent,
        enabled=enabled,
        user_id=str(user.id),
        organization=organization,
        workspace=workspace,
    )


def _created():
    """How many projects and provider rows exist."""
    return (
        Project.no_workspace_objects.count(),
        ObservabilityProvider.no_workspace_objects.count(),
    )


def _events(logs, prefix="observability_switch"):
    return [entry for entry in logs if entry["event"].startswith(prefix)]


def test_names_and_signatures_are_the_contract_ones():
    assert [
        f.name for f in dataclasses.fields(agent_setup.AgentSummary)
    ] == SUMMARY_FIELDS
    assert agent_setup.AgentSummary.__dataclass_params__.frozen
    assert [f.name for f in dataclasses.fields(switch.MissingPrerequisite)] == [
        "code",
        "field",
        "message",
    ]
    assert switch.MissingPrerequisite.__dataclass_params__.frozen
    assert [
        (p.name, p.kind, p.default)
        for p in inspect.signature(switch.set_agent_observability).parameters.values()
    ] == [
        (name, KEYWORD_ONLY, NO_DEFAULT)
        for name in (
            "agent_definition",
            "enabled",
            "user_id",
            "organization",
            "workspace",
        )
    ]
    assert list(inspect.signature(switch.refusal_body).parameters) == ["error"]
    assert list(
        inspect.signature(switch.check_observability_prerequisites).parameters
    ) == ["agent_definition"]
    assert issubclass(switch.ObservabilityRefused, Exception)
    assert issubclass(switch.ObservabilityProjectError, Exception)
    assert issubclass(switch.AgentNotFound, Exception)
    assert issubclass(switch.ProviderListNotPublished, Exception)


def test_prerequisites_are_the_contract_words():
    assert switch.PREREQUISITES == {
        "credential": switch.MissingPrerequisite(
            "credential_missing", "api_key", KEY_MESSAGE
        ),
        "provider_agent_id": switch.MissingPrerequisite(
            "provider_agent_id_missing", "assistant_id", AGENT_ID_MESSAGE
        ),
        "instrumentation": switch.MissingPrerequisite(
            "instrumentation_missing", "instrumentation", INSTRUMENTATION_MESSAGE
        ),
    }
    with pytest.raises(ValueError):
        switch.ObservabilityRefused(())
    refused = switch.ObservabilityRefused((switch.PREREQUISITES["credential"],))
    assert refused.missing == (switch.PREREQUISITES["credential"],)


def _stored_code_agent(organization, workspace, **agent_overrides):
    agent = _agent(
        organization,
        workspace,
        provider=None,
        assistant_id=None,
        api_key=None,
        agent_type=AgentDefinition.AgentTypeChoices.TEXT,
    )
    version = agent.create_version(
        description="", commit_message="first", status="active"
    )
    stored = agent_setup.store_agent(
        agent_version_id=version.id,
        agent=source_agent(**agent_overrides),
        sdk_version="0.2.3",
    )
    assert stored == "stored"
    return agent


def test_summary_comes_from_the_stored_agent_reader(organization, workspace):
    # Nothing stored: the reader answers today's agent, no kind and no instrumenter.
    plain = _agent(
        organization, workspace, provider="retell", assistant_id="assistant-9"
    )
    summary = switch._agent_summary(plain.id)
    assert (
        summary.agent_definition_id,
        summary.kind,
        summary.instrumentation,
        summary.provider,
        summary.provider_agent_id,
    ) == (plain.id, None, "none", "retell", "assistant-9")

    stored = switch._agent_summary(_stored_code_agent(organization, workspace).id)
    assert (stored.kind, stored.instrumentation) == ("source", "traceai")

    assert switch.AgentNotFound is agent_setup.AgentNotFound
    with pytest.raises(switch.AgentNotFound, match="^agent definition not found$"):
        switch._agent_summary(uuid.uuid4())
    with pytest.raises(switch.ProviderListNotPublished):
        switch._read_provider_list()


@pytest.mark.parametrize("instrumentation", ["traceai", "opentelemetry"])
def test_stored_code_agent_with_an_instrumenter_is_admitted(
    organization, workspace, user, instrumentation
):
    agent = _stored_code_agent(organization, workspace, instrumentation=instrumentation)
    row = _turn(agent, True, user, organization, workspace)
    assert isinstance(row, ObservabilityProvider)
    assert row.provider == "others"
    assert switch.agent_is_source(agent)


def test_stored_code_agent_without_an_instrumenter_is_refused(
    organization, workspace, user
):
    agent = _stored_code_agent(organization, workspace, instrumentation="none")
    before = _created()
    with pytest.raises(switch.ObservabilityRefused) as raised:
        _turn(agent, True, user, organization, workspace)
    assert raised.value.missing == (switch.PREREQUISITES["instrumentation"],)
    assert _created() == before


def test_agent_is_source_asks_the_summary(organization, workspace):
    agent = _agent(organization, workspace)
    with seams(make_summary(kind="source")):
        assert switch.agent_is_source(agent) is True
    for kind in (None, "hosted", "endpoint"):
        with seams(make_summary(kind=kind)):
            assert switch.agent_is_source(agent) is False


def test_on_list_complete_creates_project_and_row(organization, workspace, user):
    agent = _agent(organization, workspace)
    with seams(make_summary(agent_definition_id=agent.id)), capture_logs() as logs:
        row = _turn(agent, True, user, organization, workspace)

    assert isinstance(row, ObservabilityProvider)
    assert (row.enabled, row.provider) == (True, "vapi")
    project = Project.no_workspace_objects.get(pk=row.project_id)
    assert (project.name, project.trace_type, project.source) == (
        agent.agent_name,
        "observe",
        ProjectSourceChoices.SIMULATOR.value,
    )
    assert _stored_agent(agent).observability_provider_id == row.pk
    assert _events(logs) == []


@pytest.mark.parametrize(
    "missing, fields",
    [
        (("credential",), ["api_key"]),
        (("provider_agent_id",), ["assistant_id"]),
        (("credential", "provider_agent_id"), ["api_key", "assistant_id"]),
        (("provider", "credential"), ["api_key"]),
    ],
)
def test_missing_key_or_agent_id_refused_with_exact_body(
    organization, workspace, user, missing, fields
):
    agent = _agent(organization, workspace)
    before = _created()
    summary = make_summary(agent_definition_id=agent.id, missing=missing)
    with seams(summary), capture_logs() as logs:
        with pytest.raises(switch.ObservabilityRefused) as raised:
            _turn(agent, True, user, organization, workspace)

    error = raised.value
    assert [entry.field for entry in error.missing] == fields
    detail = "Observability cannot be turned on: " + "; ".join(
        MESSAGE_OF_FIELD[field] for field in fields
    )
    expected = build_error_envelope(
        detail,
        status_code=400,
        code="observability_prerequisite_missing",
        details={field: [MESSAGE_OF_FIELD[field]] for field in fields},
    )
    body = switch.refusal_body(error)
    assert body == expected
    assert json.dumps(body) == json.dumps(expected)
    assert body["code"] == "observability_prerequisite_missing"
    assert body["attr"] == fields[0]
    assert body["details"] == {field: [MESSAGE_OF_FIELD[field]] for field in fields}
    # Nothing was created and the agent still points at no row.
    assert _created() == before
    assert _stored_agent(agent).observability_provider_id is None
    assert _events(logs) == [
        {
            "event": "observability_switch_refused",
            "log_level": "info",
            "agent_definition_id": str(agent.id),
            "provider": "vapi",
            "codes": [CODE_OF_FIELD[field] for field in fields],
        }
    ]


def test_refusal_body_for_both_entries_is_the_contract_body():
    error = switch.ObservabilityRefused(
        (switch.PREREQUISITES["credential"], switch.PREREQUISITES["provider_agent_id"])
    )
    detail = f"Observability cannot be turned on: {KEY_MESSAGE}; {AGENT_ID_MESSAGE}"
    assert switch.refusal_body(error) == {
        "status": False,
        "type": "validation_error",
        "code": "observability_prerequisite_missing",
        "detail": detail,
        "message": detail,
        "error": detail,
        "result": detail,
        "attr": "api_key",
        "details": {
            "api_key": [KEY_MESSAGE],
            "assistant_id": [AGENT_ID_MESSAGE],
        },
    }


def test_plain_column_key_passes_and_logs(organization, workspace, user):
    agent = _agent(organization, workspace)
    plain = make_summary(agent_definition_id=agent.id, credential_id=None)
    with seams(plain), capture_logs() as logs:
        row = _turn(agent, True, user, organization, workspace)

    assert isinstance(row, ObservabilityProvider)
    assert _events(logs) == [
        {
            "event": "observability_switch_plain_column_key",
            "log_level": "warning",
            "agent_definition_id": str(agent.id),
            "provider": "vapi",
        }
    ]


def test_outbound_agent_is_checked(organization, workspace, user):
    agent = _agent(organization, workspace, inbound=False)
    before = _created()
    summary = make_summary(agent_definition_id=agent.id, missing=("credential",))
    with seams(summary):
        with pytest.raises(switch.ObservabilityRefused):
            _turn(agent, True, user, organization, workspace)
    assert _created() == before


def test_already_on_is_never_checked(organization, workspace, user):
    agent = _agent_with_row(organization, workspace, enabled=True)
    refusing = make_summary(missing=("credential", "provider_agent_id"))
    with seams(refusing) as (read_summary, read_list):
        row = _turn(agent, True, user, organization, workspace)

    assert row.pk == agent.observability_provider_id
    assert _stored(row)[1] is True
    read_summary.assert_not_called()
    read_list.assert_not_called()


def test_off_is_never_refused(organization, workspace, user):
    refusing = make_summary(missing=("credential", "provider_agent_id"))
    agent = _agent_with_row(organization, workspace, enabled=True)
    with seams(refusing) as (read_summary, read_list):
        row = _turn(agent, False, user, organization, workspace)
    assert _stored(row)[1] is False
    read_summary.assert_not_called()
    read_list.assert_not_called()

    # No row and off: nothing to switch, nothing created.
    bare = _agent(organization, workspace)
    before = _created()
    with seams(refusing) as (read_summary, read_list):
        assert _turn(bare, False, user, organization, workspace) is None
    assert _created() == before
    read_summary.assert_not_called()


def test_turn_on_of_an_existing_off_row_is_checked(organization, workspace, user):
    agent = _agent_with_row(organization, workspace, enabled=False)
    refusing = make_summary(agent_definition_id=agent.id, missing=("credential",))
    with seams(refusing):
        with pytest.raises(switch.ObservabilityRefused):
            _turn(agent, True, user, organization, workspace)
    assert _stored(agent.observability_provider)[1] is False

    with seams(make_summary(agent_definition_id=agent.id)):
        row = _turn(agent, True, user, organization, workspace)
    assert _stored(row)[1] is True


def test_row_of_a_deleted_project_is_cleared(organization, workspace, user):
    agent = _agent_with_row(organization, workspace, enabled=True)
    Project._base_manager.filter(pk=agent.observability_provider.project_id).update(
        deleted=True
    )
    agent = _stored_agent(agent)
    with seams(make_summary()) as (read_summary, _):
        assert _turn(agent, True, user, organization, workspace) is None
    assert _stored_agent(agent).observability_provider_id is None
    read_summary.assert_not_called()


@pytest.mark.parametrize("kind", [None, "hosted", "endpoint"])
def test_unlisted_provider_as_today(organization, workspace, user, kind):
    agent = _agent(organization, workspace)
    # It would be refused if it were checked.
    summary = make_summary(
        agent_definition_id=agent.id,
        provider="acme",
        kind=kind,
        missing=("credential", "provider_agent_id"),
    )
    with seams(summary), capture_logs() as logs:
        row = _turn(agent, True, user, organization, workspace)

    assert isinstance(row, ObservabilityProvider)
    # The row takes the agent's own column, not the summary's word.
    assert row.provider == "vapi"
    assert _events(logs) == []


@pytest.mark.parametrize("kind", [None, "hosted", "endpoint"])
def test_list_not_published_as_today_with_event(organization, workspace, user, kind):
    agent = _agent(organization, workspace)
    summary = make_summary(
        agent_definition_id=agent.id, kind=kind, missing=("credential",)
    )
    with seams(summary, published=False), capture_logs() as logs:
        row = _turn(agent, True, user, organization, workspace)

    assert isinstance(row, ObservabilityProvider)
    assert row.provider == "vapi"
    assert _events(logs) == [
        {
            "event": "observability_switch_unchecked",
            "log_level": "warning",
            "agent_definition_id": str(agent.id),
            "provider": "vapi",
            "reason": "provider_list_not_published",
        }
    ]


def test_list_not_published_still_decides_a_source_agent(organization, workspace, user):
    agent = _agent(organization, workspace, provider=None)
    none = make_summary(
        agent_definition_id=agent.id,
        kind="source",
        provider="",
        instrumentation="none",
    )
    before = _created()
    with seams(none, published=False), capture_logs() as logs:
        with pytest.raises(switch.ObservabilityRefused):
            _turn(agent, True, user, organization, workspace)
    assert _created() == before
    assert [entry["event"] for entry in _events(logs)] == [
        "observability_switch_unchecked",
        "observability_switch_refused",
    ]

    traced = dataclasses.replace(none, instrumentation="traceai")
    with seams(traced, published=False):
        row = _turn(agent, True, user, organization, workspace)
    assert row.provider == "others"


def test_kind_none_changes_nothing(organization, workspace, user):
    agent = _agent(organization, workspace)
    unlisted = make_summary(
        agent_definition_id=agent.id,
        provider="acme",
        kind=None,
        missing=("credential",),
        instrumentation="none",
    )
    with seams(unlisted), capture_logs() as logs:
        row = _turn(agent, True, user, organization, workspace)
    assert isinstance(row, ObservabilityProvider)
    assert row.provider == "vapi"
    assert _events(logs) == []


@pytest.mark.parametrize("kind", [None, "hosted", "source", "endpoint"])
def test_listed_provider_is_checked_whatever_the_kind(
    organization, workspace, user, kind
):
    agent = _agent(organization, workspace)
    before = _created()
    summary = make_summary(
        agent_definition_id=agent.id,
        kind=kind,
        instrumentation="traceai",
        missing=("credential",),
    )
    with seams(summary):
        with pytest.raises(switch.ObservabilityRefused) as raised:
            _turn(agent, True, user, organization, workspace)
    assert [entry.code for entry in raised.value.missing] == ["credential_missing"]
    assert _created() == before


@pytest.mark.parametrize("column", [None, "livekit", "phone"])
@pytest.mark.parametrize("instrumenter", ["traceai", "opentelemetry"])
def test_source_agent_admitted_with_an_instrumenter_and_refused_without(
    organization, workspace, user, column, instrumenter
):
    # An unlisted code agent: admitted when its code names an instrumenter, and
    # its row takes the generic provider word whatever its own column says.
    agent = _agent(organization, workspace, provider=column)
    summary = make_summary(
        agent_definition_id=agent.id,
        kind="source",
        provider=column or "",
        instrumentation=instrumenter,
        missing=("credential", "provider_agent_id"),
    )
    with seams(summary), capture_logs() as logs:
        row = _turn(agent, True, user, organization, workspace)
    assert isinstance(row, ObservabilityProvider)
    assert row.provider == "others"
    assert _events(logs) == []

    # Without one: refused before anything is created, with the one entry.
    refused_agent = _agent(organization, workspace, provider=column)
    before = _created()
    without = dataclasses.replace(
        summary, agent_definition_id=refused_agent.id, instrumentation="none"
    )
    with seams(without), capture_logs() as logs:
        with pytest.raises(switch.ObservabilityRefused) as raised:
            _turn(refused_agent, True, user, organization, workspace)
    assert raised.value.missing == (switch.PREREQUISITES["instrumentation"],)
    (entry,) = raised.value.missing
    assert (entry.code, entry.field, entry.message) == (
        "instrumentation_missing",
        "instrumentation",
        INSTRUMENTATION_MESSAGE,
    )
    assert switch.refusal_body(raised.value)["details"] == {
        "instrumentation": [INSTRUMENTATION_MESSAGE]
    }
    assert _created() == before
    assert _events(logs) == [
        {
            "event": "observability_switch_refused",
            "log_level": "info",
            "agent_definition_id": str(refused_agent.id),
            # Only our own provider words reach the log; free text reads "unknown".
            "provider": "livekit" if column == "livekit" else "unknown",
            "codes": ["instrumentation_missing"],
        }
    ]


def test_source_agent_with_a_listed_provider_goes_through_todays_checks(
    organization, workspace, user
):
    agent = _agent(organization, workspace)
    complete = make_summary(
        agent_definition_id=agent.id, kind="source", instrumentation="none"
    )
    with seams(complete):
        row = _turn(agent, True, user, organization, workspace)
    assert row.provider == "vapi"

    keyless = _agent(organization, workspace)
    without_key = dataclasses.replace(
        complete, agent_definition_id=keyless.id, missing=("credential",)
    )
    with seams(without_key):
        with pytest.raises(switch.ObservabilityRefused) as raised:
            _turn(keyless, True, user, organization, workspace)
    assert [entry.code for entry in raised.value.missing] == ["credential_missing"]


def test_project_error_when_no_row_comes_back(organization, workspace, user):
    agent = _agent(organization, workspace)
    with seams(make_summary(agent_definition_id=agent.id)):
        with patch.object(
            switch, "create_observability_provider", return_value={"error": "Invalid"}
        ):
            with pytest.raises(
                switch.ObservabilityProjectError,
                match="^observability project could not be created$",
            ):
                _turn(agent, True, user, organization, workspace)
    assert _stored_agent(agent).observability_provider_id is None


def test_switch_saves_only_enabled(organization, workspace, user):
    agent = _agent_with_row(
        organization, workspace, enabled=False, metadata={"assistant_id": "a1"}
    )
    summary = make_summary(agent_definition_id=agent.id)

    def flag_lands_during_the_check(agent_definition_id):
        # After the switch loaded the row and before it saves it.
        record_holding(str(agent.observability_provider.project_id))
        return summary

    with seams(flag_lands_during_the_check):
        row = _turn(agent, True, user, organization, workspace)

    assert _stored(row) == ({"assistant_id": "a1", HOLDING_FLAG: True}, True)


def test_switch_saves_only_enabled_when_turning_off(organization, workspace, user):
    agent = _agent_with_row(
        organization, workspace, enabled=True, metadata={"assistant_id": "a1"}
    )
    row = agent.observability_provider
    real_save = ObservabilityProvider.save

    def flag_then_save(self, *args, **kwargs):
        record_holding(str(self.project_id))
        return real_save(self, *args, **kwargs)

    with patch.object(ObservabilityProvider, "save", flag_then_save):
        _turn(agent, False, user, organization, workspace)

    assert _stored(row) == ({"assistant_id": "a1", HOLDING_FLAG: True}, False)


def test_agent_with_nothing_stored_keeps_todays_rule(
    auth_client, organization, workspace
):
    # Neither read is replaced: a vapi agent with no key is turned on through the
    # version view exactly as before, and the switch says it did not check.
    agent = _agent(organization, workspace, api_key=None, assistant_id=None)
    with capture_logs() as logs:
        response = auth_client.post(
            f"/simulate/agent-definitions/{agent.id}/versions/create/",
            {"commit_message": "turn on", "observability_enabled": True},
            format="json",
        )

    assert response.status_code == 201
    row = _stored_agent(agent).observability_provider
    assert row is not None
    assert (row.enabled, row.provider) == (True, "vapi")
    assert _events(logs) == [
        {
            "event": "observability_switch_unchecked",
            "log_level": "warning",
            "agent_definition_id": str(agent.id),
            "provider": "vapi",
            "reason": "provider_list_not_published",
        }
    ]


# ---------------------------------------------------------------------------
# The provider endpoint
# ---------------------------------------------------------------------------


def _put(client, row, payload):
    return client.put(f"{PROVIDERS_PATH}{row.id}/", payload, format="json")


@pytest.mark.parametrize("send", [_patch, _put])
def test_patch_on_provider_row_is_checked_before_perform_update(
    auth_client, organization, workspace, send
):
    agent = _agent_with_row(
        organization, workspace, enabled=False, metadata={"assistant_id": "a1"}
    )
    row = agent.observability_provider
    refusing = make_summary(
        agent_definition_id=agent.id, missing=("credential", "provider_agent_id")
    )
    with seams(refusing) as (read_summary, _):
        response = send(auth_client, row, {"enabled": True, "metadata": {"k": "v"}})

    refusal = switch.ObservabilityRefused(
        (switch.PREREQUISITES["credential"], switch.PREREQUISITES["provider_agent_id"])
    )
    assert response.status_code == 400
    assert response.json() == switch.refusal_body(refusal)
    # Refused before anything of the request was saved.
    assert _stored(row) == ({"assistant_id": "a1"}, False)
    read_summary.assert_called_once_with(agent.id)

    with seams(make_summary(agent_definition_id=agent.id)):
        response = send(auth_client, row, {"enabled": True})
    assert response.status_code == 200
    assert _stored(row) == ({"assistant_id": "a1"}, True)


def test_patch_checks_only_a_turn_on_of_a_row_with_a_live_agent(
    auth_client, organization, workspace
):
    refusing = make_summary(missing=("credential",))

    # Turning on a row whose agent is gone: not checked.
    gone = _agent_with_row(organization, workspace, enabled=False)
    AgentDefinition.no_workspace_objects.filter(pk=gone.pk).update(deleted=True)
    with seams(refusing) as (read_summary, _):
        response = _patch(auth_client, gone.observability_provider, {"enabled": True})
    assert response.status_code == 200
    read_summary.assert_not_called()

    # A row no agent points at: not checked.
    loose = _row(organization, workspace, {})
    ObservabilityProvider.no_workspace_objects.filter(pk=loose.pk).update(enabled=False)
    with seams(refusing) as (read_summary, _):
        response = _patch(auth_client, loose, {"enabled": True})
    assert response.status_code == 200
    read_summary.assert_not_called()

    # A request that does not turn it on, or that turns it off: not checked.
    live = _agent_with_row(organization, workspace, enabled=True)
    row = live.observability_provider
    with seams(refusing) as (read_summary, _):
        assert _patch(auth_client, row, {"enabled": True}).status_code == 200
        assert _patch(auth_client, row, {"metadata": {"k": 1}}).status_code == 200
        assert _patch(auth_client, row, {"enabled": False}).status_code == 200
        assert _patch(auth_client, row, {"metadata": {"k": 2}}).status_code == 200
    read_summary.assert_not_called()
    assert _stored(row)[1] is False


def test_delete_keeps_flag_written_after_get_object(
    auth_client, organization, workspace, monkeypatch
):
    original = ObservabilityProviderViewSet.get_object

    def get_object_then_flag_the_project(self):
        found = original(self)
        # The flag lands after the view read the row and before it saves.
        record_holding(str(found.project_id))
        return found

    monkeypatch.setattr(
        ObservabilityProviderViewSet, "get_object", get_object_then_flag_the_project
    )
    row = _row(organization, workspace, {"assistant_id": "a1"})
    seen, wrapper = _statements()
    with wrapper:
        response = auth_client.delete(f"{PROVIDERS_PATH}{row.id}/")

    assert response.status_code == 200
    assert _result(response) == "Observability provider deleted successfully."
    stored = ObservabilityProvider._base_manager.get(pk=row.pk)
    assert stored.deleted is True
    assert stored.deleted_at is not None
    assert stored.metadata == {"assistant_id": "a1", HOLDING_FLAG: True}
    # The soft delete wrote its own columns and no others.
    updates = [sql for sql in seen if sql.startswith('UPDATE "tracer_observability_')]
    assert updates
    assert all('"metadata"' not in sql and '"enabled"' not in sql for sql in updates)


@pytest.mark.parametrize("word", ["", "grpc", "TRACEAI"])
def test_source_agent_with_an_unknown_instrumentation_word_is_refused(
    organization, workspace, user, word
):
    # Only the two named instrumenters admit a code agent; any other word fails closed.
    agent = _agent(organization, workspace, provider=None)
    summary = make_summary(
        agent_definition_id=agent.id,
        kind="source",
        provider="",
        instrumentation=word,
        missing=("credential", "provider_agent_id"),
    )
    before = _created()
    with seams(summary):
        with pytest.raises(switch.ObservabilityRefused) as raised:
            _turn(agent, True, user, organization, workspace)
    assert raised.value.missing == (switch.PREREQUISITES["instrumentation"],)
    assert _created() == before


def _short_tag(agent):
    return str(agent.id).replace("-", "")[:8]


def _turn_on(agent, user, organization, workspace):
    with seams(make_summary(agent_definition_id=agent.id)):
        return _turn(agent, True, user, organization, workspace)


def _project(organization, workspace, name, *, trace_type="observe", deleted=False):
    project = Project.no_workspace_objects.create(
        name=name,
        organization=organization,
        workspace=workspace,
        model_type=AIModel.ModelTypes.GENERATIVE_LLM,
        trace_type=trace_type,
    )
    if deleted:
        Project._base_manager.filter(pk=project.pk).update(deleted=True)
    return project


def _other_workspace(organization, user):
    return Workspace.objects.create(
        name=f"Other {uuid.uuid4().hex[:8]}",
        organization=organization,
        is_default=False,
        is_active=True,
        created_by=user,
    )


def _project_name(row):
    return Project.no_workspace_objects.get(pk=row.project_id).name


def test_two_agents_with_one_name_get_two_projects(organization, workspace, user):
    name = f"Shared {uuid.uuid4().hex[:8]}"
    first = _agent(organization, workspace, agent_name=name)
    second = _agent(organization, workspace, agent_name=name)

    first_row = _turn_on(first, user, organization, workspace)
    second_row = _turn_on(second, user, organization, workspace)

    assert first_row.pk != second_row.pk
    assert first_row.project_id != second_row.project_id
    assert _project_name(first_row) == name
    assert _project_name(second_row) == f"{name} {_short_tag(second)}"
    assert _stored_agent(second).observability_provider_id == second_row.pk


def test_toggle_reuses_the_agents_own_project(organization, workspace, user):
    # The agent's own project holds its name, so a toggle that went through the
    # name check again would tag it.
    name = f"Toggled {uuid.uuid4().hex[:8]}"
    first = _agent(organization, workspace, agent_name=name)
    second = _agent(organization, workspace, agent_name=name)
    first_row = _turn_on(first, user, organization, workspace)
    second_row = _turn_on(second, user, organization, workspace)
    created = _created()

    for agent, row, expected in (
        (first, first_row, name),
        (second, second_row, f"{name} {_short_tag(second)}"),
    ):
        agent = _stored_agent(agent)
        with seams(make_summary(agent_definition_id=agent.id)):
            _turn(agent, False, user, organization, workspace)
            again = _turn(agent, True, user, organization, workspace)
        assert (again.pk, again.project_id) == (row.pk, row.project_id)
        assert _project_name(again) == expected
    assert _created() == created


def test_second_turn_on_of_the_same_agent_reuses_the_row(organization, workspace, user):
    # A request that loaded the agent before another one committed its row.
    name = f"Twice {uuid.uuid4().hex[:8]}"
    agent = _agent(organization, workspace, agent_name=name)
    first = _turn_on(_stored_agent(agent), user, organization, workspace)
    created = _created()

    second = _turn_on(agent, user, organization, workspace)

    assert (second.pk, second.project_id) == (first.pk, first.project_id)
    assert second.enabled
    assert ObservabilityProvider.no_workspace_objects.get(pk=first.pk).enabled
    assert _project_name(second) == name
    assert _created() == created
    assert _stored_agent(agent).observability_provider_id == first.pk


def test_project_no_row_uses_still_forces_the_tag(organization, workspace, user):
    # Made by an Observe-only user; it may belong to another agent.
    name = f"Customer {uuid.uuid4().hex[:8]}"
    own = _project(organization, workspace, name)
    agent = _agent(organization, workspace, agent_name=name)

    row = _turn_on(agent, user, organization, workspace)

    assert row.project_id != own.pk
    assert _project_name(row) == f"{name} {_short_tag(agent)}"


def test_project_of_another_trace_type_does_not_force_the_tag(
    organization, workspace, user
):
    name = f"Experiment {uuid.uuid4().hex[:8]}"
    _project(organization, workspace, name, trace_type="experiment")
    agent = _agent(organization, workspace, agent_name=name)

    row = _turn_on(agent, user, organization, workspace)

    assert _project_name(row) == name


def test_name_of_a_deleted_project_is_free(organization, workspace, user):
    name = f"Removed {uuid.uuid4().hex[:8]}"
    old = _project(organization, workspace, name, deleted=True)
    agent = _agent(organization, workspace, agent_name=name)

    row = _turn_on(agent, user, organization, workspace)

    assert row.project_id != old.pk
    assert _project_name(row) == name


def test_name_in_another_workspace_does_not_force_the_tag(
    organization, workspace, user
):
    name = f"Elsewhere {uuid.uuid4().hex[:8]}"
    _project(organization, _other_workspace(organization, user), name)
    agent = _agent(organization, workspace, agent_name=name)

    assert _project_name(_turn_on(agent, user, organization, workspace)) == name


def test_tag_is_stable(organization, workspace):
    name = f"Stable {uuid.uuid4().hex[:8]}"
    _project(organization, workspace, name)
    agent = _agent(organization, workspace, agent_name=name)

    first = switch._project_name_for_new_row(agent, organization, workspace)
    second = switch._project_name_for_new_row(agent, organization, workspace)

    assert first == second == f"{name} {_short_tag(agent)}"


@pytest.mark.parametrize("length", [255, 255 - 9, 255 - 8])
def test_truncation_keeps_the_longest_prefix_that_fits(organization, workspace, length):
    # The tag is a space and 8 characters: a name of 246 characters fits whole.
    name = "n" * length
    _project(organization, workspace, name)
    agent = _agent(organization, workspace, agent_name=name)

    chosen = switch._project_name_for_new_row(agent, organization, workspace)

    suffix = f" {_short_tag(agent)}"
    assert len(chosen) == 255
    assert chosen == name[: 255 - len(suffix)] + suffix


def test_tagged_name_taken_too_uses_the_longer_tag(organization, workspace, user):
    name = f"Crowded {uuid.uuid4().hex[:8]}"
    agent = _agent(organization, workspace, agent_name=name)
    _project(organization, workspace, name)
    _project(organization, workspace, f"{name} {_short_tag(agent)}")

    row = _turn_on(agent, user, organization, workspace)

    assert _project_name(row) == f"{name} {str(agent.id).replace('-', '')}"


def test_every_name_taken_is_a_project_error_and_writes_nothing(
    organization, workspace, user
):
    name = f"Full {uuid.uuid4().hex[:8]}"
    agent = _agent(organization, workspace, agent_name=name)
    _project(organization, workspace, name)
    _project(organization, workspace, f"{name} {_short_tag(agent)}")
    _project(organization, workspace, f"{name} {str(agent.id).replace('-', '')}")
    before = _created()

    with seams(make_summary(agent_definition_id=agent.id)):
        with pytest.raises(
            switch.ObservabilityProjectError,
            match="^observability project could not be created$",
        ):
            _turn(agent, True, user, organization, workspace)

    assert _created() == before
    assert _stored_agent(agent).observability_provider_id is None


def test_no_workspace_is_checked_in_the_default_workspace(
    organization, workspace, user
):
    other = _other_workspace(organization, user)
    taken = f"Default taken {uuid.uuid4().hex[:8]}"
    elsewhere = f"Elsewhere {uuid.uuid4().hex[:8]}"
    _project(organization, workspace, taken)
    _project(organization, other, elsewhere)
    first = _agent(organization, workspace, agent_name=taken)
    second = _agent(organization, workspace, agent_name=elsewhere)

    row = _turn_on(first, user, organization, None)
    free = _turn_on(second, user, organization, None)

    assert _project_name(row) == f"{taken} {_short_tag(first)}"
    assert _project_name(free) == elsewhere
    assert Project.no_workspace_objects.get(pk=row.project_id).workspace_id == (
        workspace.pk
    )


def test_malformed_workspace_id_is_a_project_error(organization, workspace):
    agent = _agent(organization, workspace)

    with pytest.raises(
        switch.ObservabilityProjectError,
        match="^observability project could not be created$",
    ):
        switch._project_name_for_new_row(agent, organization, "not-a-workspace-id")


def test_name_check_ignores_the_requests_workspace(organization, workspace, user):
    # The scope is the agent's workspace, not the one the request runs in.
    name = f"Scoped {uuid.uuid4().hex[:8]}"
    _project(organization, workspace, name)
    agent = _agent(organization, workspace, agent_name=name)
    set_workspace_context(
        workspace=_other_workspace(organization, user),
        organization=organization,
        user=user,
    )
    try:
        chosen = switch._project_name_for_new_row(agent, organization, workspace)
    finally:
        set_workspace_context(workspace=workspace, organization=organization, user=user)

    assert chosen == f"{name} {_short_tag(agent)}"


def test_name_check_reads_the_primary(organization, workspace):
    with patch.object(
        QuerySet, "using", autospec=True, side_effect=QuerySet.using
    ) as using:
        switch._project_name_taken(organization, str(workspace.id), "any name")

    assert "default" in [call.args[1] for call in using.call_args_list]


def test_workspace_is_locked_once_inside_the_switchs_own_savepoint(
    organization, workspace, user
):
    # Taken before the first project read, and still held when the row is
    # created: the lock, the checks of every candidate and the inserts share one
    # savepoint that the switch opens, so a caller outside a transaction is
    # covered too.
    name = f"Locked {uuid.uuid4().hex[:8]}"
    _project(organization, workspace, name)
    agent = _agent(organization, workspace, agent_name=name)
    seen = []

    def spy(execute, sql, params, many, context):
        seen.append((sql, params))
        return execute(sql, params, many, context)

    with connection.execute_wrapper(spy):
        _turn_on(agent, user, organization, workspace)

    sqls = [sql for sql, _ in seen]

    def first(match):
        return next(i for i, sql in enumerate(sqls) if match(sql))

    agent_locked = first(
        lambda sql: "simulate_agent_definition" in sql and " FOR NO KEY UPDATE" in sql
    )
    locks = [i for i, sql in enumerate(sqls) if "pg_advisory_xact_lock" in sql]
    project_read = first(
        lambda sql: sql.startswith("SELECT") and '"tracer_project"' in sql
    )
    project_insert = first(lambda sql: sql.startswith('INSERT INTO "tracer_project"'))
    row_insert = first(
        lambda sql: sql.startswith('INSERT INTO "tracer_observability_provider"')
    )
    assert len(locks) == 1
    lock = locks[0]
    assert agent_locked < lock < project_read < project_insert < row_insert
    assert seen[lock][1] == [
        switch._workspace_lock_key(organization, str(workspace.id))
    ]
    assert _depth_at(sqls, agent_locked) == _depth_at(sqls, lock) == 1
    assert _depth_at(sqls, row_insert) >= 1


def test_lock_key_follows_organization_and_workspace(organization, workspace):
    key = switch._workspace_lock_key(organization, str(workspace.id))
    assert key == switch._workspace_lock_key(organization, str(workspace.id))
    assert key != switch._workspace_lock_key(organization, str(uuid.uuid4()))
    other_org = SimpleNamespace(id=uuid.uuid4())
    assert key != switch._workspace_lock_key(other_org, str(workspace.id))
    assert -(2**63) <= key < 2**63
