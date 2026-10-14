"""Contract probe for the hiding module, written out from the contract.

Every value here is typed in from the contract's text and compared with what the
module exports, so a renamed parameter, a moved keyword-only mark, a changed
default or a reworded event fails here before a caller meets it.
"""

from __future__ import annotations

import inspect
import typing
import uuid
from collections.abc import Iterable, Sequence
from typing import Literal

import pytest
from structlog.testing import capture_logs

import tracer.services.simulation_visibility as visibility
from model_hub.models.ai_model import AIModel
from tracer.models.observability_provider import (
    ObservabilityProvider,
    ProviderChoices,
)
from tracer.models.project import Project

POSITIONAL = inspect.Parameter.POSITIONAL_OR_KEYWORD
KEYWORD_ONLY = inspect.Parameter.KEYWORD_ONLY
NO_DEFAULT = inspect.Parameter.empty


def _shape(function):
    return [
        (parameter.name, parameter.kind, parameter.default)
        for parameter in inspect.signature(function).parameters.values()
    ]


def test_constants_have_the_contract_values():
    assert visibility.SIMULATION_MARK_KEY == "fi.simulator.call_execution_id"
    assert visibility.HOLDING_FLAG == "holds_simulated"
    assert visibility.RESOURCE_MARK_KEY == "fi.simulation.origin"
    assert visibility.MARKED_LOOKUP_TIMEOUT_MS == 2000


def test_signatures_are_the_contract_signatures():
    assert _shape(visibility.record_holding) == [("project_id", POSITIONAL, NO_DEFAULT)]
    assert _shape(visibility.holding_projects) == [
        ("project_ids", POSITIONAL, NO_DEFAULT)
    ]
    assert _shape(visibility.hide_simulated) == [("project_id", POSITIONAL, NO_DEFAULT)]
    assert _shape(visibility.resource_mark_sql) == [
        ("schema", KEYWORD_ONLY, NO_DEFAULT)
    ]
    assert _shape(visibility.row_exclusion_sql) == [
        ("map_column", KEYWORD_ONLY, NO_DEFAULT)
    ]
    assert _shape(visibility.marked_trace_ids) == [
        ("project_id", KEYWORD_ONLY, NO_DEFAULT),
        ("trace_ids", KEYWORD_ONLY, NO_DEFAULT),
        ("timeout_ms", KEYWORD_ONLY, 2000),
    ]

    hints = typing.get_type_hints
    assert hints(visibility.record_holding) == {"project_id": str, "return": type(None)}
    assert hints(visibility.holding_projects) == {
        "project_ids": Iterable[str],
        "return": frozenset[str],
    }
    assert hints(visibility.hide_simulated) == {"project_id": str, "return": bool}
    assert hints(visibility.resource_mark_sql) == {
        "schema": Literal["v1", "v2"],
        "return": str,
    }
    assert hints(visibility.row_exclusion_sql) == {"map_column": str, "return": str}
    assert hints(visibility.marked_trace_ids) == {
        "project_id": str,
        "trace_ids": Sequence[str],
        "timeout_ms": int,
        "return": frozenset[str],
    }
    assert issubclass(visibility.HoldingNotRecorded, Exception)
    assert issubclass(visibility.MarkedLookupFailed, Exception)


def test_texts_and_error_cases_are_the_contract_ones():
    assert visibility.resource_mark_sql(schema="v1") == (
        "JSONHas(resource_attributes_raw, 'fi.simulation.origin')"
    )
    assert visibility.resource_mark_sql(schema="v2") == (
        "resource_attrs.fi.simulation.origin.:String IS NOT NULL"
    )
    assert visibility.row_exclusion_sql(map_column="attrs_string") == (
        "NOT (mapContains(attrs_string, 'fi.simulator.call_execution_id') "
        "OR resource_attrs.fi.simulation.origin.:String IS NOT NULL)"
    )
    assert visibility.row_exclusion_sql(map_column="span_attr_str") == (
        "NOT (mapContains(span_attr_str, 'fi.simulator.call_execution_id') "
        "OR JSONHas(resource_attributes_raw, 'fi.simulation.origin'))"
    )
    with pytest.raises(
        ValueError, match="^map_column must be attrs_string or span_attr_str$"
    ):
        visibility.row_exclusion_sql(map_column="attrs_number")
    # No project to ask about is an answer, not a read.
    assert visibility.holding_projects([]) == frozenset()


@pytest.mark.django_db
def test_holding_events_carry_exactly_their_contract_fields(organization, workspace):
    project = Project.no_workspace_objects.create(
        name=f"probe-{uuid.uuid4().hex[:10]}",
        organization=organization,
        workspace=workspace,
        model_type=AIModel.ModelTypes.GENERATIVE_LLM,
        trace_type="observe",
    )
    project_id = str(project.id)

    with capture_logs() as logs:
        with pytest.raises(visibility.HoldingNotRecorded):
            visibility.record_holding(project_id)
    assert logs == [
        {
            "event": "simulation_holding_no_provider_row",
            "log_level": "error",
            "project_id": project_id,
        }
    ]

    ObservabilityProvider.no_workspace_objects.create(
        project=project,
        provider=ProviderChoices.VAPI,
        organization=organization,
        workspace=workspace,
    )
    with capture_logs() as logs:
        visibility.record_holding(project_id)
    assert logs == [
        {
            "event": "simulation_holding_recorded",
            "log_level": "info",
            "project_id": project_id,
        }
    ]
