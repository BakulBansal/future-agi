"""The one place observability is turned on or off for an agent.

Every caller that switches an agent's observability goes through
``set_agent_observability``. A turn-on is refused, with one reason per field,
when the agent lacks what tracing needs: a key, the provider's agent id, or, for
an agent whose own code runs in our sandbox, an instrumenter in that code.
Otherwise it creates the same observe project and provider row as before.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

import structlog

from tfc.utils.api_errors import build_error_envelope
from tracer.models.observability_provider import (
    ObservabilityProvider,
    ProviderChoices,
)
from tracer.utils.observability_provider import create_observability_provider

if TYPE_CHECKING:
    from accounts.models.organization import Organization
    from accounts.models.workspace import Workspace
    from simulate.models import AgentDefinition

logger = structlog.get_logger(__name__)

REFUSAL_CODE = "observability_prerequisite_missing"


@dataclass(frozen=True)
class MissingPrerequisite:
    code: str
    field: str
    message: str


PREREQUISITES = {
    "credential": MissingPrerequisite(
        "credential_missing",
        "api_key",
        "API key is required when observability is enabled",
    ),
    "provider_agent_id": MissingPrerequisite(
        "provider_agent_id_missing",
        "assistant_id",
        "Assistant ID is required when observability is enabled",
    ),
    "instrumentation": MissingPrerequisite(
        "instrumentation_missing",
        "instrumentation",
        "The agent's code carries no OpenTelemetry or traceAI instrumentation "
        "the sandbox can configure (an HTTP exporter, not gRPC alone); add one "
        "and author the agent again",
    ),
}


class ObservabilityRefused(Exception):
    def __init__(self, missing):
        missing = tuple(missing)
        if not missing:
            raise ValueError("a refusal names at least one missing prerequisite")
        super().__init__("; ".join(entry.message for entry in missing))
        self.missing = missing


class ObservabilityProjectError(Exception):
    pass


# Mirrors the stored-agent summary field for field so the real one can replace it.
@dataclass(frozen=True)
class _AgentSummary:
    agent_definition_id: UUID
    agent_version_id: UUID | None
    agent_stored: bool
    setup_stale: bool
    provider: str
    provider_agent_id: str
    credential_id: UUID | None
    key_present: bool
    type: str
    kind: str | None
    unit_ids: tuple[str, ...]
    instrumentation: str
    setup_hash: str | None
    provider_info_complete: bool
    missing: tuple[str, ...]


class AgentNotFound(Exception):
    pass


class ProviderListNotPublished(Exception):
    pass


def _agent_summary(agent_definition_id) -> _AgentSummary:
    # Reads the agent through one seam so the stored-agent summary can replace
    # this stand-in without touching the switch.
    from simulate.models import AgentDefinition

    try:
        agent = AgentDefinition.no_workspace_objects.get(id=agent_definition_id)
    except AgentDefinition.DoesNotExist:
        raise AgentNotFound("agent definition not found") from None
    return _AgentSummary(
        agent_definition_id=agent.id,
        agent_version_id=None,
        agent_stored=False,
        setup_stale=False,
        provider=agent.provider or "",
        provider_agent_id=agent.assistant_id or "",
        credential_id=None,
        key_present=bool(agent.api_key),
        type=agent.agent_type or "",
        kind=None,
        unit_ids=(),
        instrumentation="none",
        setup_hash=None,
        provider_info_complete=True,
        missing=(),
    )


def _read_provider_list():
    # Until a provider list is published, no provider counts as listed.
    raise ProviderListNotPublished()


def agent_is_source(agent_definition: AgentDefinition) -> bool:
    return _agent_summary(agent_definition.id).kind == "source"


def _row_provider(agent_definition: AgentDefinition) -> str:
    # An unlisted code agent's own provider word may fail the row's validation.
    column = agent_definition.provider
    summary = _agent_summary(agent_definition.id)
    if summary.kind != "source":
        return column
    try:
        listed = summary.provider in _read_provider_list().providers
    except ProviderListNotPublished:
        return ProviderChoices.OTHERS.value
    return column if listed else ProviderChoices.OTHERS.value


def check_observability_prerequisites(agent_definition: AgentDefinition) -> None:
    summary = _agent_summary(agent_definition.id)
    agent_id = str(agent_definition.id)
    try:
        listed = summary.provider in _read_provider_list().providers
    except ProviderListNotPublished:
        logger.warning(
            "observability_switch_unchecked",
            agent_definition_id=agent_id,
            provider=_logged_provider(summary.provider),
            reason="provider_list_not_published",
        )
        listed = False
        if summary.kind != "source":
            return
    if listed:
        missing = tuple(
            PREREQUISITES[word] for word in summary.missing if word in PREREQUISITES
        )
        if missing:
            _refuse(agent_id, summary.provider, missing)
        if summary.credential_id is None and summary.key_present:
            logger.warning(
                "observability_switch_plain_column_key",
                agent_definition_id=agent_id,
                provider=_logged_provider(summary.provider),
            )
        return
    if summary.kind == "source":
        if summary.instrumentation not in ("traceai", "opentelemetry"):
            _refuse(agent_id, summary.provider, (PREREQUISITES["instrumentation"],))
        return


def _logged_provider(provider: str) -> str:
    # The provider column is free text; only our own words may reach a log line.
    return provider if provider in ProviderChoices.values else "unknown"


def _refuse(agent_id: str, provider: str, missing: tuple[MissingPrerequisite, ...]):
    logger.info(
        "observability_switch_refused",
        agent_definition_id=agent_id,
        provider=_logged_provider(provider),
        codes=[entry.code for entry in missing],
    )
    raise ObservabilityRefused(missing)


def set_agent_observability(
    *,
    agent_definition: AgentDefinition,
    enabled: bool,
    user_id: str | None,
    organization: Organization,
    workspace: Workspace | str | None,
) -> ObservabilityProvider | None:
    row = agent_definition.observability_provider
    if row is not None:
        if row.project.deleted:
            agent_definition.observability_provider = None
            agent_definition.save()
            return None
        if enabled and not row.enabled:
            check_observability_prerequisites(agent_definition)
        # Only these columns, so a holding flag written meanwhile is kept.
        row.enabled = enabled
        row.save(update_fields=["enabled", "updated_at"])
        return row
    if not enabled:
        return None
    check_observability_prerequisites(agent_definition)
    row = create_observability_provider(
        enabled=True,
        user_id=user_id,
        organization=organization,
        workspace=workspace,
        project_name=agent_definition.agent_name,
        provider=_row_provider(agent_definition),
    )
    if not isinstance(row, ObservabilityProvider):
        raise ObservabilityProjectError("observability project could not be created")
    agent_definition.observability_provider = row
    agent_definition.save()
    return row


def refusal_body(error: ObservabilityRefused) -> dict:
    reasons = "; ".join(entry.message for entry in error.missing)
    return build_error_envelope(
        f"Observability cannot be turned on: {reasons}",
        status_code=400,
        code=REFUSAL_CODE,
        details={entry.field: [entry.message] for entry in error.missing},
    )
