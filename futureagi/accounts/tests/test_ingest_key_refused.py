"""The ``ingest`` key writes spans to the collector and nothing else.

It is handed to a customer's own, untrusted process, so every pair
authentication of the platform must refuse it, and one workspace holds one.
"""

import base64
from unittest.mock import MagicMock

import pytest
from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.exceptions import AuthenticationFailed

from accounts.authentication import LangfuseBasicAuthentication
from accounts.models.user import OrgApiKey
from accounts.models.workspace import Workspace

USER_INFO_URL = "/accounts/user-info/"


def _ingest_key(organization, workspace, **extra):
    return OrgApiKey.no_workspace_objects.create(
        name="simulation_export_key",
        organization=organization,
        workspace=workspace,
        type="ingest",
        user=None,
        **extra,
    )


def _system_key(organization, workspace):
    return OrgApiKey.no_workspace_objects.create(
        organization=organization,
        workspace=workspace,
        type="system",
    )


def _basic_request(key):
    # Built as test_multi_org_auth builds one: a bare mock with no auto-attributes.
    encoded = base64.b64encode(f"{key.api_key}:{key.secret_key}".encode()).decode(
        "ascii"
    )
    request = MagicMock(spec=[])
    request.method = "GET"
    request.path = "/api/test/"
    request.headers = {}
    request.GET = {}
    request.META = {"HTTP_AUTHORIZATION": f"Basic {encoded}"}
    return request


@pytest.mark.integration
@pytest.mark.django_db
def test_ingest_key_is_refused_by_the_api_key_class(
    api_client, user, organization, workspace
):
    ingest = _ingest_key(organization, workspace)
    system = _system_key(organization, workspace)

    refused = api_client.get(
        USER_INFO_URL,
        HTTP_X_API_KEY=ingest.api_key,
        HTTP_X_SECRET_KEY=ingest.secret_key,
    )
    assert refused.status_code == status.HTTP_401_UNAUTHORIZED
    assert "ingest keys cannot call the API" in refused.content.decode()

    accepted = api_client.get(
        USER_INFO_URL,
        HTTP_X_API_KEY=system.api_key,
        HTTP_X_SECRET_KEY=system.secret_key,
    )
    assert accepted.status_code == status.HTTP_200_OK


@pytest.mark.integration
@pytest.mark.django_db
def test_ingest_key_is_refused_by_langfuse_basic_and_mcp(user, organization, workspace):
    from mcp_server import mcp_app

    ingest = _ingest_key(organization, workspace)
    system = _system_key(organization, workspace)

    with pytest.raises(AuthenticationFailed, match="No user found"):
        LangfuseBasicAuthentication().authenticate(_basic_request(ingest))
    assert (
        mcp_app._authenticate_and_set_context(ingest.api_key, ingest.secret_key) is None
    )

    # The same calls with the system pair succeed, so the refusals above come
    # from the key's type and not from a badly built request.
    authenticated_user, _ = LangfuseBasicAuthentication().authenticate(
        _basic_request(system)
    )
    assert authenticated_user == user
    try:
        context = mcp_app._authenticate_and_set_context(
            system.api_key, system.secret_key
        )
        assert context is not None
        assert context.user == user
    finally:
        mcp_app._clear_context()


@pytest.mark.integration
@pytest.mark.django_db
def test_one_ingest_key_per_organisation_and_workspace(user, organization, workspace):
    first = _ingest_key(organization, workspace)

    with pytest.raises(IntegrityError), transaction.atomic():
        _ingest_key(organization, workspace)

    # Other key types in the same workspace are untouched by the constraint.
    _system_key(organization, workspace)
    OrgApiKey.no_workspace_objects.create(
        organization=organization, workspace=workspace, type="user", user=user
    )

    other_workspace = Workspace.objects.create(
        name="Second Workspace",
        organization=organization,
        is_default=False,
        is_active=True,
        created_by=user,
    )
    _ingest_key(organization, other_workspace)

    OrgApiKey.no_workspace_objects.filter(id=first.id).update(deleted=True)
    replacement = _ingest_key(organization, workspace)
    assert replacement.id != first.id
    assert (
        OrgApiKey.all_objects.filter(
            organization=organization, workspace=workspace, type="ingest"
        ).count()
        == 2
    )
