"""The provider endpoint keeps a project's holding flag through an update.

The flag is written by another process whenever a simulated run first exports to
the project, and nothing a request sends may clear it.
"""

from __future__ import annotations

import uuid

from django.db import connection

from model_hub.models.ai_model import AIModel
from tfc.utils.general_methods import GeneralMethods
from tracer.models.observability_provider import (
    ObservabilityProvider,
    ProviderChoices,
)
from tracer.models.project import Project
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
