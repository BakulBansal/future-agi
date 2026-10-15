"""Emit spans to the fi-collector.

The collector's ClickHouse exporter is the sole writer of CH ``spans`` (legacy
PG->CH CDC chain dropped by default), so any span that must appear in observe
exports through it. Resolves the org's system ingest key and ships OTLP spans.
"""

from __future__ import annotations

import uuid
from typing import Any, NamedTuple

import structlog

logger = structlog.get_logger(__name__)

# Per-org system key reused to authenticate span emission to the collector.
_INGEST_KEY_NAME = "system_org_key"

# Names an agent's sandbox key for operators reading the table or the logs.
_EXPORT_KEY_NAME = "simulation_export_key"


class ExportCredentials(NamedTuple):
    api_key: str
    secret_key: str
    enabled: bool


def resolve_ingest_credentials(
    organization_id: str, workspace_id: str | None
) -> tuple[str, str] | None:
    """Resolve the org-scoped (api_key, secret_key) for span emission.

    Reuses the org's system key (one per org); else creates it carrying the
    workspace so the collector resolves projects there.
    """
    if not organization_id:
        return None
    from django.db import IntegrityError

    from accounts.models.user import OrgApiKey

    # IntegrityError catch re-fetches the winner of a create/create race.
    # Disabled keys are returned as-is so the collector surfaces the misconfig.
    lookup = {
        "organization_id": organization_id,
        "type": "system",
        "deleted": False,
    }
    try:
        key, _ = OrgApiKey.no_workspace_objects.get_or_create(
            defaults={"workspace_id": workspace_id, "name": _INGEST_KEY_NAME},
            **lookup,
        )
    except IntegrityError:
        key = (
            OrgApiKey.no_workspace_objects.filter(**lookup)
            .order_by("created_at")
            .first()
        )
    if key is None:
        return None
    return key.api_key, key.secret_key


def resolve_export_credentials(
    organization_id: str, workspace_id: str, agent_definition_id: str
) -> ExportCredentials | None:
    """Resolve the write-only key one agent's simulated runs export with.

    The process runs untrusted customer code, so it gets the agent's own
    ``ingest`` key, which the API refuses, never the system key. The collector
    files spans under the key's workspace and routes them by its agent, which
    is why both are required. A non-UUID agent id raises ``ValueError``.
    """
    if not organization_id or not workspace_id or not agent_definition_id:
        return None
    from accounts.models.user import OrgApiKey

    agent_id = str(uuid.UUID(str(agent_definition_id)))
    # ``enabled`` stays out of the lookup: an operator who disabled the key has
    # stopped the export, and re-creating or re-enabling it would undo that.
    # Two first launches of one agent race on the unique constraint;
    # ``get_or_create`` itself re-reads the winner's row when its insert loses.
    key, _ = OrgApiKey.no_workspace_objects.get_or_create(
        organization_id=organization_id,
        workspace_id=workspace_id,
        simulation_agent_id=agent_id,
        type="ingest",
        deleted=False,
        defaults={"name": f"{_EXPORT_KEY_NAME} {agent_id}", "user": None},
    )
    return ExportCredentials(key.api_key, key.secret_key, key.enabled)


def emit_spans_to_collector(
    spans: list[dict[str, Any]],
    *,
    project_name: str,
    project_type: str,
    organization_id: str,
    workspace_id: str | None,
    service_name: str = "fi-simulation",
) -> int:
    """Export ``spans`` to the collector. Best-effort: returns count exported, 0 on failure."""
    if not spans:
        return 0
    credentials = resolve_ingest_credentials(organization_id, workspace_id)
    if credentials is None:
        logger.warning("collector_emit_no_credentials", organization_id=organization_id)
        return 0
    api_key, secret_key = credentials
    from simulate.services.sim_collector_emit import export_sim_spans

    return export_sim_spans(
        spans,
        project_name=project_name,
        project_type=project_type,
        api_key=api_key,
        secret_key=secret_key,
        service_name=service_name,
    )
