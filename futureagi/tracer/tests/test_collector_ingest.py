"""The export key a simulated run's own process sends its spans with."""

from unittest.mock import patch

import pytest
from django.db import IntegrityError, connection

from accounts.models.user import OrgApiKey
from accounts.models.workspace import Workspace
from tracer.services.collector_ingest import (
    ExportCredentials,
    resolve_export_credentials,
)

# fi-collector/pkg/auth/pgresolver.go, ValidateKey, with psycopg placeholders.
COLLECTOR_VALIDATE_KEY_SQL = """
    SELECT k.organization_id, k.workspace_id, k.user_id, k.type
    FROM accounts_orgapikey k
    WHERE k.api_key = %s AND k.secret_key = %s
      AND k.enabled = true AND k.deleted = false
    LIMIT 1
"""


def _second_workspace(organization, user):
    return Workspace.objects.create(
        name="Second Workspace",
        organization=organization,
        is_default=False,
        is_active=True,
        created_by=user,
    )


def _ingest_rows(organization, workspace=None):
    rows = OrgApiKey.no_workspace_objects.filter(
        organization=organization, type="ingest"
    )
    if workspace is not None:
        rows = rows.filter(workspace=workspace)
    return rows


@pytest.mark.integration
@pytest.mark.django_db
def test_resolve_export_credentials_creates_once_per_organisation_and_workspace(
    user, organization, workspace
):
    system = OrgApiKey.no_workspace_objects.create(
        organization=organization, workspace=workspace, type="system"
    )

    first = resolve_export_credentials(str(organization.id), str(workspace.id))

    assert isinstance(first, ExportCredentials)
    assert first.enabled is True
    assert first.api_key != system.api_key
    row = _ingest_rows(organization, workspace).get()
    assert row.name == "simulation_export_key"
    assert row.type == "ingest"
    assert row.user_id is None
    assert row.workspace_id == workspace.id
    assert (first.api_key, first.secret_key) == (row.api_key, row.secret_key)

    again = resolve_export_credentials(str(organization.id), str(workspace.id))
    assert again == first
    assert _ingest_rows(organization, workspace).count() == 1

    other_workspace = _second_workspace(organization, user)
    other = resolve_export_credentials(str(organization.id), str(other_workspace.id))
    assert other is not None
    assert other.api_key != first.api_key
    assert _ingest_rows(organization, other_workspace).get().api_key == other.api_key
    assert _ingest_rows(organization).count() == 2


@pytest.mark.integration
@pytest.mark.django_db
def test_resolve_export_credentials_survives_a_create_race(
    user, organization, workspace
):
    winner = OrgApiKey(
        name="simulation_export_key",
        organization=organization,
        workspace=workspace,
        type="ingest",
        user=None,
        api_key=OrgApiKey.generate_api_key(),
        secret_key=OrgApiKey.generate_secret_key(),
    )

    def losing_insert(self, **kwargs):
        # The other launch's row lands first; this insert then hits the constraint.
        OrgApiKey.no_workspace_objects.bulk_create([winner])
        raise IntegrityError("duplicate key value")

    with patch(
        "django.db.models.query.QuerySet.create",
        autospec=True,
        side_effect=losing_insert,
    ) as create:
        result = resolve_export_credentials(str(organization.id), str(workspace.id))

    create.assert_called_once()
    assert result == ExportCredentials(winner.api_key, winner.secret_key, True)
    assert _ingest_rows(organization, workspace).count() == 1


@pytest.mark.integration
@pytest.mark.django_db
def test_resolve_export_credentials_returns_a_disabled_key_as_disabled(
    user, organization, workspace
):
    disabled = OrgApiKey.no_workspace_objects.create(
        name="simulation_export_key",
        organization=organization,
        workspace=workspace,
        type="ingest",
        user=None,
        enabled=False,
    )

    result = resolve_export_credentials(str(organization.id), str(workspace.id))

    assert result == ExportCredentials(disabled.api_key, disabled.secret_key, False)
    assert _ingest_rows(organization).count() == 1
    disabled.refresh_from_db()
    assert disabled.enabled is False


@pytest.mark.integration
@pytest.mark.django_db
def test_resolve_export_credentials_refuses_empty_ids(user, organization, workspace):
    assert resolve_export_credentials("", str(workspace.id)) is None
    assert resolve_export_credentials(str(organization.id), "") is None
    assert resolve_export_credentials(None, str(workspace.id)) is None
    assert resolve_export_credentials(str(organization.id), None) is None
    assert not _ingest_rows(organization).exists()


@pytest.mark.integration
@pytest.mark.django_db
def test_collector_validate_key_query_accepts_the_ingest_row(
    user, organization, workspace
):
    credentials = resolve_export_credentials(str(organization.id), str(workspace.id))

    with connection.cursor() as cursor:
        cursor.execute(
            COLLECTOR_VALIDATE_KEY_SQL,
            [credentials.api_key, credentials.secret_key],
        )
        rows = cursor.fetchall()

    assert len(rows) == 1
    organization_id, workspace_id, user_id, key_type = rows[0]
    assert str(organization_id) == str(organization.id)
    assert str(workspace_id) == str(workspace.id)
    assert user_id is None
    assert key_type == "ingest"
