"""Every project view and selector decides once, then hands the decision on.

A view reads whether its project holds simulated traces one time per request
and gives that answer to every builder and chart read it constructs; a read
over several projects asks once for all of them. These tests replace the
builders and chart reads with recorders, so they pin the argument each call
site receives and how often the decision was read, never the SQL (the builders
and charts have their own tests). The holding reads themselves are replaced at
the name each module imports them under: nothing here touches an
``ObservabilityProvider`` row or ClickHouse.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.parse import urlencode

import pytest

from model_hub.models.ai_model import AIModel
from model_hub.services import bulk_selection
from tracer.models.custom_eval_config import CustomEvalConfig
from tracer.models.eval_task import RowType, RunType
from tracer.models.project import Project
from tracer.selectors.eval_tasks import row_resolver
from tracer.services.clickhouse.query_builders.voice_call_list import (
    VOICE_CALL_ROOT_FILTER,
)
from tracer.services.simulation_visibility import (
    RESOURCE_MARK_KEY,
    SIMULATION_MARK_KEY,
)

TRACE_VIEW = "tracer.views.trace"
SPAN_VIEW = "tracer.views.observation_span"
CHARTS_VIEW = "tracer.views.charts"
PROJECT_VIEW = "tracer.views.project"
ROW_RESOLVER = "tracer.selectors.eval_tasks.row_resolver"
BULK_SELECTION = "model_hub.services.bulk_selection"

# Builders the views and selectors import inside their functions are replaced
# where they are defined; the one observation_span imports at module level is
# replaced in that module.
TRACE_LIST_BUILDER = (
    "tracer.services.clickhouse.v2.query_builders.trace_list.TraceListQueryBuilderV2"
)
SPAN_LIST_BUILDER = (
    "tracer.services.clickhouse.v2.query_builders.span_list.SpanListQueryBuilderV2"
)
VOICE_LIST_BUILDER = (
    "tracer.services.clickhouse.v2.query_builders.voice_call_list."
    "VoiceCallListQueryBuilderV2"
)
SESSION_LIST_BUILDER = (
    "tracer.services.clickhouse.v2.query_builders.session_list."
    "SessionListQueryBuilderV2"
)
V2_ANALYTICS = "tracer.services.clickhouse.v2.query_service.V2AnalyticsQueryService"

PROJECT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
WINDOW_START = datetime(2026, 1, 1, tzinfo=UTC)
WINDOW_END = datetime(2026, 1, 2, tzinfo=UTC)

HOLDING_OR_NOT = (
    pytest.param(True, id="holding"),
    pytest.param(False, id="not-holding"),
)


class _Stop(Exception):
    """Ends a request once the recorder has what the test needs."""


def _recorder(calls, *, stop_at=1):
    """Stand in for a builder class or a chart read and record each call.

    Raising once enough calls arrived keeps the view from running past the
    construction under test; the view answers its own error and the test reads
    the recorded arguments.
    """

    def record(*args, **kwargs):
        calls.append(SimpleNamespace(args=args, kwargs=kwargs))
        if len(calls) >= stop_at:
            raise _Stop
        return MagicMock()

    return record


def _request(call, *args, **kwargs):
    """Make a request; a recorder's stop that escapes a view is not a failure."""
    try:
        return call(*args, **kwargs)
    except _Stop:
        return None


class _Decisions:
    """The holding reads of one module: answers for one project, counts calls."""

    def __init__(self, monkeypatch, module, *, holds, several=True):
        self.holds = holds
        self.single = MagicMock(name="hide_simulated", return_value=holds)
        monkeypatch.setattr(f"{module}.project_hides_simulated", self.single)
        self.several = None
        if several:
            self.several = MagicMock(
                name="holding_projects",
                return_value=frozenset({"held"}) if holds else frozenset(),
            )
            monkeypatch.setattr(f"{module}.holding_projects", self.several)

    def assert_read_once_for(self, project_id):
        assert self.single.call_count == 1
        assert self.single.call_args.args == (str(project_id),)
        if self.several is not None:
            assert self.several.call_count == 0

    def assert_read_once_for_all(self, project_ids):
        assert self.several.call_count == 1
        assert sorted(self.several.call_args.args[0]) == sorted(project_ids)
        assert self.single.call_count == 0


def _carries_no_mark(value):
    """The decision travels as an argument, never as a filter or cursor entry."""
    text = json.dumps(value, default=str)
    assert SIMULATION_MARK_KEY not in text
    assert RESOURCE_MARK_KEY not in text


def _organization_project_ids(organization):
    manager = getattr(Project, "no_workspace_objects", Project.objects)
    return [
        str(project_id)
        for project_id in manager.filter(
            organization=organization, deleted=False
        ).values_list("id", flat=True)
    ]


@pytest.fixture
def second_observe_project(db, organization, workspace):
    return Project.objects.create(
        name="Second Observe Project",
        organization=organization,
        workspace=workspace,
        model_type=AIModel.ModelTypes.GENERATIVE_LLM,
        trace_type="observe",
        metadata={},
    )


@pytest.fixture
def observe_eval_config(db, observe_project, eval_template):
    return CustomEvalConfig.objects.create(
        name="Observe Outcome",
        project=observe_project,
        eval_template=eval_template,
        config={},
        mapping={},
        filters={},
    )


# ---------------------------------------------------------------------------
# Trace chart, span chart, agent graph
# ---------------------------------------------------------------------------

CHART_READS = (
    pytest.param("SYSTEM_METRIC", "fetch_system_metric_graph_ch", id="system-metric"),
    pytest.param("EVAL", "fetch_eval_graph_ch", id="eval"),
    pytest.param("ANNOTATION", "fetch_annotation_graph_ch", id="annotation"),
)


def _chart_request(project, metric_type, eval_config, **extra):
    metric_id = {
        "SYSTEM_METRIC": "latency",
        "EVAL": str(eval_config.id),
        "ANNOTATION": str(uuid.uuid4()),
    }[metric_type]
    return {
        "project_id": str(project.id),
        "interval": "day",
        "property": "average",
        "req_data_config": {"id": metric_id, "type": metric_type},
        "filters": [],
        **extra,
    }


def _install_chart_reads(monkeypatch, module):
    reads = {
        name: []
        for name in (
            "fetch_system_metric_graph_ch",
            "fetch_eval_graph_ch",
            "fetch_annotation_graph_ch",
        )
    }
    for name, calls in reads.items():
        monkeypatch.setattr(f"{module}.{name}", _recorder(calls))
    monkeypatch.setattr(f"{module}.V2AnalyticsQueryService", lambda: object())
    return reads


@pytest.mark.integration
@pytest.mark.django_db
@pytest.mark.parametrize("holds", HOLDING_OR_NOT)
@pytest.mark.parametrize(("metric_type", "read"), CHART_READS)
def test_trace_chart_hands_the_decision_to_its_chart_read(
    auth_client,
    observe_project,
    observe_eval_config,
    monkeypatch,
    holds,
    metric_type,
    read,
):
    decisions = _Decisions(monkeypatch, TRACE_VIEW, holds=holds)
    reads = _install_chart_reads(monkeypatch, TRACE_VIEW)

    _request(
        auth_client.post,
        "/tracer/trace/get_graph_methods/",
        _chart_request(observe_project, metric_type, observe_eval_config),
        format="json",
    )

    assert len(reads[read]) == 1
    assert sum(len(calls) for calls in reads.values()) == 1
    assert reads[read][0].kwargs["hide_simulated"] is holds
    decisions.assert_read_once_for(observe_project.id)


@pytest.mark.integration
@pytest.mark.django_db
@pytest.mark.parametrize("holds", HOLDING_OR_NOT)
def test_voice_chart_hands_the_decision_on_and_keeps_its_filter_list(
    auth_client, observe_project, monkeypatch, holds
):
    decisions = _Decisions(monkeypatch, TRACE_VIEW, holds=holds)
    reads = _install_chart_reads(monkeypatch, TRACE_VIEW)

    _request(
        auth_client.post,
        "/tracer/trace/get_graph_methods/",
        {
            "project_id": str(observe_project.id),
            "interval": "day",
            "property": "average",
            "req_data_config": {"id": "latency", "type": "SYSTEM_METRIC"},
            "filters": [],
            "observe_type": "voice",
        },
        format="json",
    )

    (call,) = reads["fetch_system_metric_graph_ch"]
    assert call.kwargs["hide_simulated"] is holds
    assert call.kwargs["filters"][-1] == VOICE_CALL_ROOT_FILTER
    _carries_no_mark(call.kwargs["filters"])
    decisions.assert_read_once_for(observe_project.id)


@pytest.mark.integration
@pytest.mark.django_db
@pytest.mark.parametrize("holds", HOLDING_OR_NOT)
@pytest.mark.parametrize(("metric_type", "read"), CHART_READS)
def test_span_chart_hands_the_decision_to_its_chart_read(
    auth_client,
    observe_project,
    observe_eval_config,
    monkeypatch,
    holds,
    metric_type,
    read,
):
    decisions = _Decisions(monkeypatch, SPAN_VIEW, holds=holds)
    reads = _install_chart_reads(monkeypatch, SPAN_VIEW)

    _request(
        auth_client.post,
        "/tracer/observation-span/get_graph_methods/",
        _chart_request(observe_project, metric_type, observe_eval_config),
        format="json",
    )

    assert len(reads[read]) == 1
    assert sum(len(calls) for calls in reads.values()) == 1
    assert reads[read][0].kwargs["hide_simulated"] is holds
    _carries_no_mark(reads[read][0].kwargs["filters"])
    decisions.assert_read_once_for(observe_project.id)


@pytest.mark.integration
@pytest.mark.django_db
@pytest.mark.parametrize("holds", HOLDING_OR_NOT)
def test_agent_graph_hands_the_decision_to_its_cached_read(
    auth_client, observe_project, monkeypatch, holds
):
    decisions = _Decisions(monkeypatch, TRACE_VIEW, holds=holds)
    calls = []
    monkeypatch.setattr(f"{TRACE_VIEW}.fetch_agent_graph_ch", _recorder(calls))

    _request(
        auth_client.get,
        "/tracer/trace/agent_graph/",
        {"project_id": str(observe_project.id)},
    )

    assert len(calls) == 1
    assert calls[0].kwargs["hide_simulated"] is holds
    decisions.assert_read_once_for(observe_project.id)


# ---------------------------------------------------------------------------
# Project charts and the single-metric and eval charts
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.django_db
@pytest.mark.parametrize("holds", HOLDING_OR_NOT)
def test_project_chart_hands_the_decision_to_the_all_metrics_read(
    auth_client, observe_project, monkeypatch, holds
):
    decisions = _Decisions(monkeypatch, PROJECT_VIEW, holds=holds, several=False)
    calls = []
    monkeypatch.setattr(f"{PROJECT_VIEW}.get_all_system_metrics", _recorder(calls))

    _request(
        auth_client.get,
        "/tracer/project/get_graph_data/",
        {"project_id": str(observe_project.id), "interval": "hour"},
    )

    assert len(calls) == 1
    assert calls[0].kwargs["hide_simulated"] is holds
    decisions.assert_read_once_for(observe_project.id)


CHARTS_READS = (
    pytest.param(
        {"id": "all", "type": "SYSTEM_METRICS"}, "get_all_system_metrics", id="all"
    ),
    pytest.param(
        {"id": "latency", "type": "SYSTEM_METRIC"},
        "get_system_metric_data",
        id="single",
    ),
    pytest.param({"type": "EVAL"}, "get_eval_graph_data", id="eval"),
)


@pytest.mark.integration
@pytest.mark.django_db
@pytest.mark.parametrize("holds", HOLDING_OR_NOT)
@pytest.mark.parametrize(("req_data_config", "read"), CHARTS_READS)
def test_charts_view_hands_one_decision_to_whichever_read_runs(
    auth_client, observe_project, monkeypatch, holds, req_data_config, read
):
    decisions = _Decisions(monkeypatch, CHARTS_VIEW, holds=holds, several=False)
    reads = {
        name: []
        for name in (
            "get_all_system_metrics",
            "get_system_metric_data",
            "get_eval_graph_data",
        )
    }
    for name, calls in reads.items():
        monkeypatch.setattr(f"{CHARTS_VIEW}.{name}", _recorder(calls))
    if req_data_config["type"] == "EVAL":
        req_data_config = {**req_data_config, "id": str(uuid.uuid4())}

    query = urlencode(
        {
            "project_id": str(observe_project.id),
            "interval": "day",
            "property": "average",
            "req_data_config": json.dumps(req_data_config),
        }
    )
    _request(auth_client.get, f"/tracer/charts/fetch_graph/?{query}")

    assert len(reads[read]) == 1
    assert sum(len(calls) for calls in reads.values()) == 1
    assert reads[read][0].kwargs["hide_simulated"] is holds
    decisions.assert_read_once_for(observe_project.id)


# ---------------------------------------------------------------------------
# Trace lists, previous and next, voice lists
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.django_db
@pytest.mark.parametrize("holds", HOLDING_OR_NOT)
def test_trace_previous_next_hands_the_decision_to_its_builder(
    auth_client, observe_project, monkeypatch, holds
):
    decisions = _Decisions(monkeypatch, TRACE_VIEW, holds=holds)
    builds = []
    monkeypatch.setattr(TRACE_LIST_BUILDER, _recorder(builds))
    monkeypatch.setattr(f"{TRACE_VIEW}.V2AnalyticsQueryService", lambda: object())

    _request(
        auth_client.get,
        "/tracer/trace/get_trace_id_by_index_observe/",
        {"project_id": str(observe_project.id), "trace_id": str(uuid.uuid4())},
    )

    assert len(builds) == 1
    assert builds[0].kwargs["hide_simulated"] is holds
    decisions.assert_read_once_for(observe_project.id)


def _install_trace_list_doubles(monkeypatch, builds):
    monkeypatch.setattr(TRACE_LIST_BUILDER, _recorder(builds))
    monkeypatch.setattr(f"{TRACE_VIEW}.V2AnalyticsQueryService", lambda: object())
    # The export decides between the voice and the trace file before it lists.
    monkeypatch.setattr(f"{TRACE_VIEW}._has_configured_voice_agent", lambda _p: False)
    monkeypatch.setattr(
        f"{TRACE_VIEW}._has_voice_conversation_roots", lambda *_a, **_k: False
    )


@pytest.mark.integration
@pytest.mark.django_db
@pytest.mark.parametrize("holds", HOLDING_OR_NOT)
@pytest.mark.parametrize(
    "url",
    (
        pytest.param("/tracer/trace/list_traces_of_session/", id="list"),
        pytest.param("/tracer/trace/get_trace_export_data/", id="csv"),
    ),
)
def test_observe_trace_list_and_its_csv_hand_the_decision_to_the_builder(
    auth_client, observe_project, monkeypatch, holds, url
):
    decisions = _Decisions(monkeypatch, TRACE_VIEW, holds=holds)
    builds = []
    _install_trace_list_doubles(monkeypatch, builds)

    _request(auth_client.get, url, {"project_id": str(observe_project.id)})

    assert len(builds) == 1
    assert builds[0].kwargs["hide_simulated"] is holds
    assert builds[0].kwargs["project_id"] == str(observe_project.id)
    _carries_no_mark(builds[0].kwargs["filters"])
    decisions.assert_read_once_for(observe_project.id)


@pytest.mark.integration
@pytest.mark.django_db
@pytest.mark.parametrize("holds", HOLDING_OR_NOT)
def test_trace_list_over_every_project_asks_once_for_all_of_them(
    auth_client,
    organization,
    observe_project,
    second_observe_project,
    monkeypatch,
    holds,
):
    decisions = _Decisions(monkeypatch, TRACE_VIEW, holds=holds)
    builds = []
    _install_trace_list_doubles(monkeypatch, builds)
    expected = _organization_project_ids(organization)
    assert len(expected) == 2

    # No project id: the list covers every project of the organization.
    _request(auth_client.get, "/tracer/trace/list_traces_of_session/")

    assert len(builds) == 1
    assert builds[0].kwargs["project_id"] is None
    assert sorted(builds[0].kwargs["project_ids"]) == sorted(expected)
    assert builds[0].kwargs["hide_simulated"] is holds
    decisions.assert_read_once_for_all(expected)


@pytest.mark.integration
@pytest.mark.django_db
def test_trace_list_of_an_organization_without_projects_asks_for_an_empty_scope(
    auth_client, monkeypatch
):
    decisions = _Decisions(monkeypatch, TRACE_VIEW, holds=False)
    builds = []
    _install_trace_list_doubles(monkeypatch, builds)

    # No project id and no project in the organization: nothing to look up.
    _request(auth_client.get, "/tracer/trace/list_traces_of_session/")

    assert len(builds) == 1
    assert builds[0].kwargs["project_id"] is None
    assert builds[0].kwargs["project_ids"] is None
    assert builds[0].kwargs["hide_simulated"] is False
    decisions.assert_read_once_for_all([])


@pytest.mark.integration
@pytest.mark.django_db
@pytest.mark.parametrize("holds", HOLDING_OR_NOT)
def test_prototype_trace_list_hands_the_decision_to_its_builder(
    auth_client, project, project_version, monkeypatch, holds
):
    decisions = _Decisions(monkeypatch, TRACE_VIEW, holds=holds)
    builds = []
    monkeypatch.setattr(TRACE_LIST_BUILDER, _recorder(builds))
    monkeypatch.setattr(f"{TRACE_VIEW}.V2AnalyticsQueryService", lambda: object())

    _request(
        auth_client.get,
        "/tracer/trace/list_traces/",
        {"project_version_id": str(project_version.id)},
    )

    assert len(builds) == 1
    assert builds[0].kwargs["hide_simulated"] is holds
    decisions.assert_read_once_for(project.id)


@pytest.mark.integration
@pytest.mark.django_db
@pytest.mark.parametrize("holds", HOLDING_OR_NOT)
@pytest.mark.parametrize(
    "url",
    (
        pytest.param("/tracer/trace/list_voice_calls/", id="list"),
        pytest.param("/tracer/trace/get_trace_export_data/", id="csv"),
    ),
)
def test_voice_list_and_its_csv_hand_one_decision_to_both_builders(
    auth_client, observe_project, monkeypatch, holds, url
):
    decisions = _Decisions(monkeypatch, TRACE_VIEW, holds=holds)
    builds = []
    monkeypatch.setattr(VOICE_LIST_BUILDER, _recorder(builds, stop_at=2))
    monkeypatch.setattr(f"{TRACE_VIEW}.V2AnalyticsQueryService", lambda: object())
    # The depth preflight reads its answer from the recorder's stand-in builder.
    monkeypatch.setattr(
        f"{TRACE_VIEW}.bounded_numbered_page_depth_exceeded", lambda **_k: False
    )
    monkeypatch.setattr(f"{TRACE_VIEW}._has_configured_voice_agent", lambda _p: True)

    _request(auth_client.get, url, {"project_id": str(observe_project.id)})

    # The depth preflight and the page read are two builders of one request.
    assert len(builds) == 2
    assert [call.kwargs["hide_simulated"] for call in builds] == [holds, holds]
    for call in builds:
        _carries_no_mark(call.kwargs["filters"])
    decisions.assert_read_once_for(observe_project.id)


# ---------------------------------------------------------------------------
# Span lists, previous and next
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.django_db
@pytest.mark.parametrize("holds", HOLDING_OR_NOT)
@pytest.mark.parametrize(
    "url",
    (
        pytest.param("/tracer/observation-span/list_spans_observe/", id="list"),
        pytest.param("/tracer/observation-span/get_spans_export_data/", id="csv"),
    ),
)
def test_observe_span_list_and_its_csv_hand_the_decision_to_the_builder(
    auth_client, observe_project, monkeypatch, holds, url
):
    decisions = _Decisions(monkeypatch, SPAN_VIEW, holds=holds)
    builds = []
    monkeypatch.setattr(f"{SPAN_VIEW}.SpanListQueryBuilderV2", _recorder(builds))
    monkeypatch.setattr(f"{SPAN_VIEW}.V2AnalyticsQueryService", lambda: object())

    _request(
        auth_client.get,
        url,
        {"project_id": str(observe_project.id), "filters": "[]"},
    )

    assert len(builds) == 1
    assert builds[0].kwargs["hide_simulated"] is holds
    assert builds[0].kwargs["project_id"] == str(observe_project.id)
    _carries_no_mark(builds[0].kwargs["filters"])
    decisions.assert_read_once_for(observe_project.id)


@pytest.mark.integration
@pytest.mark.django_db
@pytest.mark.parametrize("holds", HOLDING_OR_NOT)
def test_span_list_over_every_project_asks_once_for_all_of_them(
    auth_client,
    organization,
    observe_project,
    second_observe_project,
    monkeypatch,
    holds,
):
    decisions = _Decisions(monkeypatch, SPAN_VIEW, holds=holds)
    builds = []
    monkeypatch.setattr(f"{SPAN_VIEW}.SpanListQueryBuilderV2", _recorder(builds))
    monkeypatch.setattr(f"{SPAN_VIEW}.V2AnalyticsQueryService", lambda: object())
    expected = _organization_project_ids(organization)
    assert len(expected) == 2

    _request(
        auth_client.get,
        "/tracer/observation-span/list_spans_observe/",
        {"filters": "[]"},
    )

    assert len(builds) == 1
    assert builds[0].kwargs["project_id"] is None
    assert sorted(builds[0].kwargs["project_ids"]) == sorted(expected)
    assert builds[0].kwargs["hide_simulated"] is holds
    decisions.assert_read_once_for_all(expected)


@pytest.mark.integration
@pytest.mark.django_db
@pytest.mark.parametrize("holds", HOLDING_OR_NOT)
def test_prototype_span_list_hands_the_decision_to_its_builder(
    auth_client, project, project_version, monkeypatch, holds
):
    decisions = _Decisions(monkeypatch, SPAN_VIEW, holds=holds)
    builds = []
    monkeypatch.setattr(f"{SPAN_VIEW}.SpanListQueryBuilderV2", _recorder(builds))
    monkeypatch.setattr(f"{SPAN_VIEW}.V2AnalyticsQueryService", lambda: object())

    _request(
        auth_client.get,
        "/tracer/observation-span/list_spans/",
        {"project_version_id": str(project_version.id), "filters": "[]"},
    )

    assert len(builds) == 1
    assert builds[0].kwargs["hide_simulated"] is holds
    decisions.assert_read_once_for(project.id)


@pytest.mark.integration
@pytest.mark.django_db
@pytest.mark.parametrize("holds", HOLDING_OR_NOT)
@pytest.mark.parametrize("variant", ("observe", "base"))
def test_span_previous_next_hands_the_decision_to_its_builder(
    auth_client, observe_project, project, project_version, monkeypatch, holds, variant
):
    decisions = _Decisions(monkeypatch, SPAN_VIEW, holds=holds)
    builds = []
    monkeypatch.setattr(SPAN_LIST_BUILDER, _recorder(builds))
    monkeypatch.setattr(f"{SPAN_VIEW}.V2AnalyticsQueryService", lambda: object())

    if variant == "observe":
        owner = observe_project
        url = "/tracer/observation-span/get_trace_id_by_index_spans_as_observe/"
        params = {"project_id": str(owner.id)}
    else:
        owner = project
        url = "/tracer/observation-span/get_trace_id_by_index_spans_as_base/"
        params = {"project_version_id": str(project_version.id)}
    _request(auth_client.get, url, {"span_id": "span-1", "filters": "[]", **params})

    assert len(builds) == 1
    assert builds[0].kwargs["hide_simulated"] is holds
    decisions.assert_read_once_for(owner.id)


# ---------------------------------------------------------------------------
# Eval tasks
# ---------------------------------------------------------------------------

HIDING_ROW_TYPES = (
    pytest.param(RowType.SPANS, id="spans"),
    pytest.param(RowType.TRACES, id="traces"),
    pytest.param(RowType.VOICE_CALLS, id="voice-calls"),
)


def _task(row_type, *, run_type=RunType.HISTORICAL, limit=25):
    return SimpleNamespace(
        id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        project_id=PROJECT_ID,
        row_type=row_type,
        run_type=run_type,
        spans_limit=limit,
        sampling_rate=100.0,
        filters={},
        continuous_cursor=WINDOW_START,
        start_time=WINDOW_START,
        created_at=WINDOW_START,
    )


class _FakeBuilder:
    """Answers what the bounded historical resolver asks before it rebuilds."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def supports_bounded_filter_scan(self):
        return True

    def parse_time_range(self, _filters):
        return WINDOW_START, WINDOW_END

    def recommended_filter_classify_batch_size(self):
        return 100

    def recommended_filter_seed_batch_size(self):
        return 200

    def recommended_filter_classify_read_settings(self):
        return None

    def recommended_filter_candidate_witness_fallback_classify_batch_size(self):
        return None

    def use_identity_only_filter_classification(self):
        return False


def _install_resolver_doubles(monkeypatch, builds, *, holds, stop_at=1):
    decisions = _Decisions(monkeypatch, ROW_RESOLVER, holds=holds, several=False)

    def get_v2_class(query_type):
        def build(**kwargs):
            builds.append(SimpleNamespace(query_type=query_type, kwargs=kwargs))
            if len(builds) >= stop_at:
                raise _Stop
            return _FakeBuilder(**kwargs)

        return build

    monkeypatch.setattr(
        "tracer.services.clickhouse.v2.dispatch.get_v2_class", get_v2_class
    )
    monkeypatch.setattr(V2_ANALYTICS, lambda: object())
    monkeypatch.setattr(
        "tracer.selectors.eval_tasks.continuous_candidates."
        "discover_continuous_candidates",
        lambda *_a, **_k: SimpleNamespace(
            classifier_ids=("candidate",),
            public_ids=("candidate",),
            covered_through=None,
        ),
    )
    return decisions


def _resolve(task):
    with pytest.raises(_Stop):
        row_resolver.resolve_desired_rows(task)


@pytest.mark.unit
@pytest.mark.parametrize("holds", HOLDING_OR_NOT)
@pytest.mark.parametrize("row_type", HIDING_ROW_TYPES)
@pytest.mark.parametrize(
    ("run_type", "limit"),
    (
        pytest.param(RunType.CONTINUOUS, None, id="continuous"),
        pytest.param(RunType.HISTORICAL, 25, id="bounded-historical"),
        pytest.param(RunType.HISTORICAL, None, id="sampled-historical"),
    ),
)
def test_eval_task_hands_the_decision_to_each_row_type_builder(
    monkeypatch, holds, row_type, run_type, limit
):
    builds = []
    decisions = _install_resolver_doubles(monkeypatch, builds, holds=holds)

    _resolve(_task(row_type, run_type=run_type, limit=limit))

    assert len(builds) == 1
    assert builds[0].kwargs["hide_simulated"] is holds
    _carries_no_mark(builds[0].kwargs["filters"])
    decisions.assert_read_once_for(PROJECT_ID)


@pytest.mark.unit
@pytest.mark.parametrize("holds", HOLDING_OR_NOT)
def test_eval_task_rebuild_for_trace_witnesses_keeps_the_same_decision(
    monkeypatch, holds
):
    builds = []
    decisions = _install_resolver_doubles(monkeypatch, builds, holds=holds, stop_at=2)
    # A trace filter on any span of the trace sends a task this large through
    # the exact workflow, which builds its classifier a second time.
    monkeypatch.setattr(
        row_resolver,
        "_trace_any_span_filter_descriptors",
        lambda _filters: (("attribute", "SPAN_ATTRIBUTE"),),
    )

    _resolve(_task(RowType.TRACES, limit=10_000))

    assert len(builds) == 2
    assert builds[1].kwargs["bounded_include_filter_witnesses"] is True
    assert [call.kwargs["hide_simulated"] for call in builds] == [holds, holds]
    decisions.assert_read_once_for(PROJECT_ID)


@pytest.mark.unit
@pytest.mark.parametrize("holds", HOLDING_OR_NOT)
@pytest.mark.parametrize("row_type", HIDING_ROW_TYPES)
def test_eval_task_escalation_to_the_exact_workflow_keeps_the_same_decision(
    monkeypatch, holds, row_type
):
    builds = []
    decisions = _install_resolver_doubles(monkeypatch, builds, holds=holds, stop_at=2)
    reads = []

    def read_page(**kwargs):
        reads.append(kwargs)
        # An interactive read that ran out of time re-enters as the workflow.
        return SimpleNamespace(complete=False, error_code="deadline_exceeded", rows=[])

    monkeypatch.setattr(
        "tracer.selectors.trace_filter_reads.read_bounded_filter_page", read_page
    )

    _resolve(_task(row_type, limit=25))

    assert len(reads) == 1
    assert reads[0]["workflow_exact"] is False
    assert len(builds) == 2
    assert [call.kwargs["hide_simulated"] for call in builds] == [holds, holds]
    for call in builds:
        _carries_no_mark(call.kwargs["filters"])
    decisions.assert_read_once_for(PROJECT_ID)


# A creation-time filter no row can satisfy: the selector proves the interval empty.
EMPTY_INTERVAL_FILTER = {
    "column_id": "created_at",
    "filter_config": {
        "col_type": "SYSTEM_METRIC",
        "filter_type": "datetime",
        "filter_op": "is_null",
    },
}


@pytest.mark.unit
@pytest.mark.parametrize(
    "task_fields",
    (
        pytest.param(
            {"filters": {"filters": [EMPTY_INTERVAL_FILTER]}}, id="no-interval"
        ),
        pytest.param({"sampling_rate": 0.0}, id="no-sampling"),
    ),
)
def test_eval_task_that_provably_selects_nothing_looks_up_no_project(
    monkeypatch, task_fields
):
    builds = []
    decisions = _install_resolver_doubles(monkeypatch, builds, holds=True)
    task = _task(RowType.SPANS)
    for name, value in task_fields.items():
        setattr(task, name, value)

    resolved = row_resolver.resolve_desired_rows(task)

    assert resolved.matched_ids == ()
    assert resolved.full_state is True
    assert builds == []
    assert decisions.single.call_count == 0


# ---------------------------------------------------------------------------
# Annotation queue selection
# ---------------------------------------------------------------------------


def _install_bulk_doubles(monkeypatch, builds, builder_path, *, holds):
    decisions = _Decisions(monkeypatch, BULK_SELECTION, holds=holds, several=False)
    monkeypatch.setattr(builder_path, _recorder(builds))
    monkeypatch.setattr(V2_ANALYTICS, lambda: object())
    return decisions


@pytest.mark.integration
@pytest.mark.django_db
@pytest.mark.parametrize("holds", HOLDING_OR_NOT)
@pytest.mark.parametrize(
    ("builder_path", "is_voice_call"),
    (
        pytest.param(TRACE_LIST_BUILDER, False, id="trace"),
        pytest.param(VOICE_LIST_BUILDER, True, id="voice"),
    ),
)
def test_queue_selection_over_traces_hands_one_decision_to_its_builder(
    organization, observe_project, monkeypatch, holds, builder_path, is_voice_call
):
    builds = []
    decisions = _install_bulk_doubles(monkeypatch, builds, builder_path, holds=holds)

    with pytest.raises(_Stop):
        bulk_selection.resolve_filtered_trace_ids(
            project_id=observe_project.id,
            filters=[],
            organization=organization,
            cap=5,
            is_voice_call=is_voice_call,
        )

    assert len(builds) == 1
    assert builds[0].kwargs["hide_simulated"] is holds
    _carries_no_mark(builds[0].kwargs["filters"])
    decisions.assert_read_once_for(observe_project.id)


@pytest.mark.integration
@pytest.mark.django_db
@pytest.mark.parametrize("holds", HOLDING_OR_NOT)
def test_queue_selection_over_spans_hands_the_decision_to_its_builder(
    organization, observe_project, monkeypatch, holds
):
    builds = []
    decisions = _install_bulk_doubles(
        monkeypatch, builds, SPAN_LIST_BUILDER, holds=holds
    )

    with pytest.raises(_Stop):
        bulk_selection.resolve_filtered_span_ids(
            project_id=observe_project.id,
            filters=[],
            organization=organization,
            cap=5,
        )

    assert len(builds) == 1
    assert builds[0].kwargs["hide_simulated"] is holds
    _carries_no_mark(builds[0].kwargs["filters"])
    decisions.assert_read_once_for(observe_project.id)


# ---------------------------------------------------------------------------
# What stays as it is
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("holds", HOLDING_OR_NOT)
@pytest.mark.parametrize(
    ("run_type", "limit"),
    (
        pytest.param(RunType.CONTINUOUS, None, id="continuous"),
        pytest.param(RunType.HISTORICAL, 25, id="bounded-historical"),
        pytest.param(RunType.HISTORICAL, None, id="sampled-historical"),
    ),
)
def test_session_list_builder_of_an_eval_task_receives_no_hide_simulated(
    monkeypatch, holds, run_type, limit
):
    builds = []
    decisions = _install_resolver_doubles(monkeypatch, builds, holds=holds)

    _resolve(_task(RowType.SESSIONS, run_type=run_type, limit=limit))

    assert len(builds) == 1
    assert builds[0].query_type == "SESSION_LIST"
    assert "hide_simulated" not in builds[0].kwargs
    # No session is ever left out, so nothing is looked up for the task either.
    assert decisions.single.call_count == 0


@pytest.mark.unit
def test_session_list_builder_of_queue_selection_receives_no_hide_simulated(
    monkeypatch,
):
    builds = []
    decisions = _install_bulk_doubles(
        monkeypatch, builds, SESSION_LIST_BUILDER, holds=True
    )
    monkeypatch.setattr(
        bulk_selection, "_prepare_session_ch_filters", lambda *_a, **_k: []
    )

    with pytest.raises(_Stop):
        bulk_selection._resolve_session_ids_clickhouse(
            project_id=PROJECT_ID,
            non_score_filters=[],
            score_filters=[],
            exclude_ids=set(),
            organization=None,
            cap=5,
        )

    assert len(builds) == 1
    assert "hide_simulated" not in builds[0].kwargs
    assert decisions.single.call_count == 0


@pytest.mark.integration
@pytest.mark.django_db
def test_reads_by_id_never_ask_whether_a_project_holds(auth_client, monkeypatch):
    asked = MagicMock(name="holding_read")
    # Replaced both where the view imports them and where they are defined, so
    # a read that reaches them through the module is caught too.
    for target in (
        f"{TRACE_VIEW}.project_hides_simulated",
        f"{TRACE_VIEW}.holding_projects",
        "tracer.services.simulation_visibility.hide_simulated",
        "tracer.services.simulation_visibility.holding_projects",
    ):
        monkeypatch.setattr(target, asked)
    monkeypatch.setattr(f"{TRACE_VIEW}.V2AnalyticsQueryService", lambda: object())

    detail_reads = []

    def read_trace_detail(**kwargs):
        detail_reads.append(kwargs)
        from tracer.services.clickhouse.v2.trace_detail_reads import (
            TraceDetailNotFound,
        )

        raise TraceDetailNotFound

    monkeypatch.setattr(f"{TRACE_VIEW}.read_trace_detail", read_trace_detail)
    handler = MagicMock(name="TraceDetailHandlerV2")
    handler.return_value.fetch.return_value = {}
    monkeypatch.setattr(
        "tracer.services.clickhouse.v2.query_builders.trace_detail."
        "TraceDetailHandlerV2",
        handler,
    )

    _request(
        auth_client.get,
        "/tracer/trace/voice_call_detail/",
        {"trace_id": str(uuid.uuid4())},
    )
    _request(auth_client.get, f"/tracer/trace/{uuid.uuid4()}/")

    # Both requests reached their reader, and neither asked about a project.
    assert len(detail_reads) == 1
    assert handler.call_count == 1
    assert asked.call_count == 0
