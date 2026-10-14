"""A holding project's marked spans stay out of the readers that list no filters.

Dashboards, monitors and the project-wide span reads each decide for
themselves whether their project holds marked traces, because their callers
hand them no filter list that could carry the rule. The cases pin what makes
that safe: a holding project's statement carries the exclusion text for the map
column of its own schema, a project that does not hold sends exactly the
statement it always sent, and a read that names its traces by id is never
narrowed.

Nothing here opens a Postgres or ClickHouse connection. The holding answers
come from recording doubles patched where each reader imports them, and every
statement is captured by a recording client or analytics double.
"""

import re
import uuid
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from django.core.cache import cache
from django.test import override_settings

import tracer.services.clickhouse.query_builders.monitor_metrics as monitor_module
import tracer.services.clickhouse.v2.span_reader as span_reader_module
from tracer.models.monitor import MonitorMetricTypeChoices
from tracer.services.clickhouse.query_builders.dashboard import DashboardQueryBuilder
from tracer.services.clickhouse.query_service import QueryResult
from tracer.services.clickhouse.v2.query_builders.dashboard import (
    DashboardQueryBuilderV2,
)
from tracer.services.clickhouse.v2.query_builders.filters import rewrite_v1_sql_to_v2
from tracer.services.clickhouse.v2.query_builders.monitor_metrics import (
    MonitorMetricsQueryBuilderV2,
)
from tracer.services.clickhouse.v2.span_reader import CHSpanReader
from tracer.services.simulation_visibility import (
    RESOURCE_MARK_KEY,
    SIMULATION_MARK_KEY,
    row_exclusion_sql,
)
from tracer.views import dashboard as dashboard_view
from tracer.views.dashboard import (
    DashboardWidgetViewSet,
    _normalize_dashboard_query_filters,
    _read_dashboard_rollup_fast_path,
    _read_public_dashboard_query,
)

pytestmark = pytest.mark.unit

HOLDING = "11111111-1111-4111-8111-111111111111"
PLAIN = "22222222-2222-4222-8222-222222222222"
OTHER_PLAIN = "33333333-3333-4333-8333-333333333333"

# The span reader names the string map by its current column.
EXCLUSION_ATTRS = row_exclusion_sql(map_column="attrs_string")
# The builders write the legacy column and the v2 rewriter translates it, so the
# text a statement carries is the rewritten one.
EXCLUSION_LEGACY = row_exclusion_sql(map_column="span_attr_str")
EXCLUSION_SENT = rewrite_v1_sql_to_v2(EXCLUSION_LEGACY)


class _Holds:
    """``hide_simulated`` stand-in: answers per project and remembers each ask."""

    def __init__(self, *holding):
        self.holding = set(holding)
        self.asked = []

    def __call__(self, project_id):
        self.asked.append(project_id)
        return project_id in self.holding


class _HoldsAmong:
    """``holding_projects`` stand-in: names the holding ones among those asked."""

    def __init__(self, *holding):
        self.holding = set(holding)
        self.asked = []

    def __call__(self, project_ids):
        ids = list(project_ids)
        self.asked.append(ids)
        return frozenset(project_id for project_id in ids if project_id in self.holding)


def _flat(sql):
    return " ".join(sql.split())


def _join(*parts):
    return " ".join(part for part in parts if part)


def _carries_no_mark(sql):
    return SIMULATION_MARK_KEY not in sql and RESOURCE_MARK_KEY not in sql


# ─── span reader: the five project-wide reads ────────────────────────────────


class _RecordingClient:
    def __init__(self):
        self.sql = None
        self.parameters = None

    def query(self, sql, parameters=None, settings=None):
        self.sql, self.parameters = sql, parameters

        class _Result:
            result_rows = []

        return _Result()


def _reader(client):
    # Bypass __init__, which opens a real connection, and inject the recorder.
    reader = CHSpanReader.__new__(CHSpanReader)
    reader._client = client
    return reader


# (how the read is called, today's statement up to its ORDER/GROUP tail, the tail)
_POPULATION_READS = [
    pytest.param(
        lambda reader, project: reader.root_ids_by_project(project),
        "SELECT id FROM spans FINAL WHERE project_id = %(pid)s "
        "AND is_deleted = 0 AND (parent_span_id IS NULL OR parent_span_id = '')",
        "ORDER BY trace_id, start_time, id",
        id="root_ids_by_project",
    ),
    pytest.param(
        lambda reader, project: reader.ids_by_project(project),
        "SELECT id FROM spans FINAL WHERE project_id = %(pid)s AND is_deleted = 0",
        "ORDER BY start_time, id",
        id="ids_by_project",
    ),
    pytest.param(
        lambda reader, project: reader.recent_root_trace_ids_by_project(
            project, limit=5
        ),
        "SELECT toString(trace_id) AS tid, max(start_time) AS st "
        "FROM spans FINAL WHERE project_id = %(pid)s AND is_deleted = 0 "
        "AND parent_span_id = ''",
        "GROUP BY tid ORDER BY st DESC LIMIT %(lim)s",
        id="recent_root_trace_ids_by_project",
    ),
    pytest.param(
        lambda reader, project: reader.count_with_filters(project_id=project),
        "SELECT count() FROM spans FINAL WHERE is_deleted = 0 AND project_id = %(pid)s",
        "",
        id="count_with_filters",
    ),
    pytest.param(
        lambda reader, project: reader.per_project_group_by_name(project, limit=10),
        "SELECT name, count() AS usage_count, "
        "countIf(lower(status) = 'error') AS error_count, "
        "avg(latency_ms) AS avg_latency, sum(cost) AS total_cost "
        "FROM spans FINAL WHERE is_deleted = 0 AND project_id = %(pid)s",
        "GROUP BY name ORDER BY count() DESC LIMIT 10",
        id="per_project_group_by_name",
    ),
]


@pytest.mark.parametrize(("read", "head", "tail"), _POPULATION_READS)
def test_population_read_leaves_marked_spans_out_for_a_holding_project(
    monkeypatch, read, head, tail
):
    holds = _Holds(HOLDING)
    monkeypatch.setattr(span_reader_module, "hide_simulated", holds)
    client = _RecordingClient()
    read(_reader(client), HOLDING)

    monkeypatch.setattr(span_reader_module, "hide_simulated", _Holds())
    unhidden = _RecordingClient()
    read(_reader(unhidden), HOLDING)

    # One more AND on the spans scan, ahead of the ordering or grouping tail.
    assert _flat(client.sql) == _join(head, f"AND {EXCLUSION_ATTRS}", tail)
    assert holds.asked == [HOLDING]
    # The text names no placeholder, so the parameters are the unhidden read's.
    assert client.parameters == unhidden.parameters


@pytest.mark.parametrize(("read", "head", "tail"), _POPULATION_READS)
def test_population_read_is_untouched_for_a_project_that_does_not_hold(
    monkeypatch, read, head, tail
):
    holds = _Holds(HOLDING)
    monkeypatch.setattr(span_reader_module, "hide_simulated", holds)
    client = _RecordingClient()
    read(_reader(client), PLAIN)

    monkeypatch.setattr(span_reader_module, "hide_simulated", _Holds())
    answers_false = _RecordingClient()
    read(_reader(answers_false), PLAIN)

    assert (client.sql, client.parameters) == (
        answers_false.sql,
        answers_false.parameters,
    )
    assert _flat(client.sql) == _join(head, tail)
    assert _carries_no_mark(client.sql)
    assert holds.asked == [PLAIN]


def test_count_by_trace_ids_never_leaves_traces_out(monkeypatch):
    holds = _Holds(HOLDING)
    monkeypatch.setattr(span_reader_module, "hide_simulated", holds)
    client = _RecordingClient()

    _reader(client).count_with_filters(project_id=HOLDING, trace_ids=["t1", "t2"])

    # A read that names its traces counts those traces, marked or not.
    assert _flat(client.sql) == (
        "SELECT count() FROM spans FINAL WHERE is_deleted = 0 "
        "AND project_id = %(pid)s AND trace_id IN %(tids)s"
    )
    assert _carries_no_mark(client.sql)
    assert holds.asked == []


def test_count_of_a_holding_projects_traces_leaves_marked_ones_out(monkeypatch):
    holds = _Holds(HOLDING)
    monkeypatch.setattr(span_reader_module, "hide_simulated", holds)
    client = _RecordingClient()

    _reader(client).count_with_filters(
        project_id=HOLDING,
        roots_only=True,
        created_at_gte=datetime(2026, 9, 1, tzinfo=UTC),
    )

    assert _flat(client.sql) == (
        "SELECT count(DISTINCT trace_id) FROM spans FINAL WHERE is_deleted = 0 "
        "AND parent_span_id = '' AND project_id = %(pid)s "
        f"AND created_at >= %(cag)s AND {EXCLUSION_ATTRS}"
    )


def test_count_without_a_project_has_no_project_to_decide_for(monkeypatch):
    holds = _Holds(HOLDING)
    monkeypatch.setattr(span_reader_module, "hide_simulated", holds)
    client = _RecordingClient()

    _reader(client).count_with_filters(created_at_gte=datetime(2026, 9, 1, tzinfo=UTC))

    assert _flat(client.sql) == (
        "SELECT count() FROM spans FINAL WHERE is_deleted = 0 AND created_at >= %(cag)s"
    )
    assert holds.asked == []


# ─── monitors ────────────────────────────────────────────────────────────────

START = datetime(2026, 8, 1, tzinfo=UTC)
END = datetime(2026, 8, 8, tzinfo=UTC)
EVAL_CONFIG_ID = "44444444-4444-4444-8444-444444444444"


def _span_statements(builder):
    """The value, baseline and series statement of one evaluation."""
    return [
        builder.build_metric_value_query(
            MonitorMetricTypeChoices.COUNT_OF_ERRORS, START, END
        ),
        builder.build_historical_stats_query(
            MonitorMetricTypeChoices.SPAN_RESPONSE_TIME, START, END
        ),
        builder.build_time_series_query(
            MonitorMetricTypeChoices.COUNT_OF_ERRORS, START, END, 3600
        ),
    ]


def _eval_statements(builder):
    return [
        builder.build_metric_value_query(
            MonitorMetricTypeChoices.EVALUATION_METRICS, START, END
        ),
        builder.build_historical_stats_query(
            MonitorMetricTypeChoices.EVALUATION_METRICS, START, END
        ),
        builder.build_time_series_query(
            MonitorMetricTypeChoices.EVALUATION_METRICS, START, END, 3600
        ),
    ]


def _eval_builder(project_id=HOLDING):
    return MonitorMetricsQueryBuilderV2(
        project_id=project_id,
        eval_config_id=EVAL_CONFIG_ID,
        eval_output_type="SCORE",
    )


def test_every_spans_statement_of_a_monitor_leaves_marked_spans_out(monkeypatch):
    holds = _Holds(HOLDING)
    monkeypatch.setattr(monitor_module, "hide_simulated", holds)

    statements = _span_statements(MonitorMetricsQueryBuilderV2(project_id=HOLDING))

    for sql, _params in statements:
        spans_scans = _flat(sql).count("FROM spans")
        assert spans_scans >= 1
        assert _flat(sql).count(EXCLUSION_SENT) == spans_scans


def test_eval_monitor_hides_marked_spans_in_its_span_membership(monkeypatch):
    holds = _Holds(HOLDING)
    monkeypatch.setattr(monitor_module, "hide_simulated", holds)

    with override_settings(CH25_EVAL_LOGGER_TABLE="tracer_eval_logger_v2"):
        statements = _eval_statements(_eval_builder())

    for sql, _params in statements:
        assert _flat(sql).count(EXCLUSION_SENT) == 1


def test_monitor_statements_are_untouched_for_a_project_that_does_not_hold(
    monkeypatch,
):
    monkeypatch.setattr(monitor_module, "hide_simulated", _Holds(HOLDING))
    with override_settings(CH25_EVAL_LOGGER_TABLE="tracer_eval_logger_v2"):
        sent = [
            *_span_statements(MonitorMetricsQueryBuilderV2(project_id=PLAIN)),
            *_eval_statements(_eval_builder(PLAIN)),
        ]

        monkeypatch.setattr(monitor_module, "hide_simulated", _Holds())
        answers_false = [
            *_span_statements(MonitorMetricsQueryBuilderV2(project_id=PLAIN)),
            *_eval_statements(_eval_builder(PLAIN)),
        ]

    assert sent == answers_false
    assert all(_carries_no_mark(sql) for sql, _params in sent)


def test_monitor_over_several_projects_hides_when_the_second_holds(monkeypatch):
    holds = _HoldsAmong(HOLDING)
    monkeypatch.setattr(monitor_module, "holding_projects", holds)

    statements = _span_statements(
        MonitorMetricsQueryBuilderV2(project_id=None, project_ids=[PLAIN, HOLDING])
    )

    assert all(EXCLUSION_SENT in sql for sql, _params in statements)
    assert holds.asked == [[PLAIN, HOLDING]]


def test_monitor_over_several_projects_none_holding_is_untouched(monkeypatch):
    holds = _HoldsAmong(HOLDING)
    monkeypatch.setattr(monitor_module, "holding_projects", holds)
    sent = _span_statements(
        MonitorMetricsQueryBuilderV2(project_id=None, project_ids=[PLAIN, OTHER_PLAIN])
    )

    monkeypatch.setattr(monitor_module, "holding_projects", _HoldsAmong())
    answers_false = _span_statements(
        MonitorMetricsQueryBuilderV2(project_id=None, project_ids=[PLAIN, OTHER_PLAIN])
    )

    assert sent == answers_false
    assert all(_carries_no_mark(sql) for sql, _params in sent)


def test_one_evaluation_asks_once_whatever_it_builds(monkeypatch):
    holds = _Holds(HOLDING)
    monkeypatch.setattr(monitor_module, "hide_simulated", holds)

    _span_statements(MonitorMetricsQueryBuilderV2(project_id=HOLDING))

    assert holds.asked == [HOLDING]


# ─── dashboard query builder ─────────────────────────────────────────────────


def _dashboard_config(metric, *, hide=None, project_ids=(HOLDING,)):
    config = {
        "project_ids": list(project_ids),
        "granularity": "day",
        # A fixed window: a preset is relative to now, so two builders would
        # bind different dates and their parameters could not be compared.
        "time_range": {
            "custom_start": "2026-08-01T00:00:00+00:00",
            "custom_end": "2026-09-01T00:00:00+00:00",
        },
        "metrics": [metric],
        "filters": [],
        "breakdowns": [],
    }
    if hide is not None:
        config["hide_simulated"] = hide
    return config


_LATENCY = {
    "id": "latency",
    "name": "latency",
    "type": "system_metric",
    "source": "traces",
    "aggregation": "avg",
}
_SCORE = {
    "id": "score",
    "name": "score",
    "type": "custom_attribute",
    "source": "traces",
    "aggregation": "avg",
    "attribute_key": "score",
    "attribute_type": "number",
}


def test_base_clauses_carry_the_exclusion_on_the_legacy_map_column():
    hiding = DashboardQueryBuilder(_dashboard_config(_LATENCY, hide=True))
    clauses, params = hiding._build_where_clauses("spans", "start_time", [], {})

    plain = DashboardQueryBuilder(_dashboard_config(_LATENCY))
    plain_clauses, plain_params = plain._build_where_clauses(
        "spans", "start_time", [], {}
    )

    assert clauses == [*plain_clauses, EXCLUSION_LEGACY]
    assert params == plain_params


@pytest.mark.parametrize(
    ("metric", "latest_state"),
    [
        pytest.param(_LATENCY, False, id="system_metric_raw"),
        pytest.param(_LATENCY, True, id="system_metric_latest_state"),
        pytest.param(_SCORE, False, id="custom_attribute_raw"),
    ],
)
def test_dashboard_statement_leaves_marked_spans_out_when_asked_to(
    metric, latest_state
):
    hiding = DashboardQueryBuilderV2(_dashboard_config(metric, hide=True))
    sql, params = hiding._build_metric_query_for_snapshot_mode(
        hiding.metrics[0], latest_state=latest_state
    )

    plain = DashboardQueryBuilderV2(_dashboard_config(metric))
    plain_sql, plain_params = plain._build_metric_query_for_snapshot_mode(
        plain.metrics[0], latest_state=latest_state
    )

    assert sql.count(EXCLUSION_SENT) == 1
    assert params == plain_params
    assert sql.replace(f" AND {EXCLUSION_SENT}", "") == plain_sql


@pytest.mark.parametrize("latest_state", [False, True], ids=["raw", "latest_state"])
def test_dashboard_statement_is_untouched_unless_asked_to_hide(latest_state):
    statements = []
    for hide in (None, False):
        builder = DashboardQueryBuilderV2(_dashboard_config(_LATENCY, hide=hide))
        statements.append(
            builder._build_metric_query_for_snapshot_mode(
                builder.metrics[0], latest_state=latest_state
            )
        )

    assert statements[0] == statements[1]
    assert _carries_no_mark(statements[0][0])


# ─── dashboard query builder: the latest-state statements ────────────────────
#
# A widget over the latest physical state of a span either aggregates a scalar
# winner tuple (a number metric with at most local filters and one breakdown)
# or reads a replay source that packs a narrowed winner. Neither goes through
# the base clauses alone, so each carries the exclusion in its own way.

_TIER = {
    "id": "tier",
    "name": "tier",
    "type": "custom_attribute",
    "source": "traces",
    "aggregation": "count_distinct",
    "attribute_key": "tier",
    "attribute_type": "string",
}
_REGION_BREAKDOWN = {
    "name": "region",
    "type": "custom_attribute",
    "source": "traces",
    "attribute_type": "string",
}
_TENANT_FILTER = {
    "column_id": "tenant",
    "filter_config": {
        "col_type": "SPAN_ATTRIBUTE",
        "filter_type": "text",
        "filter_op": "in",
        "filter_value": ["acme", "globex"],
        "attribute_value_types": ["string", "string"],
    },
}
_STATUS_FILTER = {
    "column_id": "final_status",
    "filter_config": {
        "col_type": "SPAN_ATTRIBUTE",
        "filter_type": "text",
        "filter_op": "equals",
        "filter_value": "settled",
    },
}

# The scalar statement tests the winner's own columns, so its text is the
# span-level exclusion qualified to the table that statement reads.
EXCLUSION_ON_WINNER = rewrite_v1_sql_to_v2(
    DashboardQueryBuilder._qualify_span_expression(
        EXCLUSION_LEGACY, "custom_metric_source"
    )
)


def _latest_state_builder(metric, *, hide, filters=(), breakdowns=()):
    config = _dashboard_config(metric, hide=hide)
    config["filters"] = list(filters)
    config["breakdowns"] = list(breakdowns)
    builder = DashboardQueryBuilderV2(_normalize_dashboard_query_filters(config))
    builder._latest_state_spans_required = True
    return builder


def _latest_state_statement(metric, *, hide, filters=(), breakdowns=()):
    builder = _latest_state_builder(
        metric, hide=hide, filters=filters, breakdowns=breakdowns
    )
    return builder.build_metric_query(builder.metrics[0])


def _narrowed_keys(sql):
    """The string-Map keys the replay source keeps in its winner tuple."""
    match = re.search(r"mapFilter\(\(k, v\) -> \(k IN \((?P<keys>[^)]*)\)\)", sql)
    assert match, "the statement narrows no attribute Map"
    return [key.strip().strip("'") for key in match.group("keys").split(",")]


@pytest.mark.parametrize(
    ("breakdowns", "filters", "mark_index"),
    [
        pytest.param([], [], 5, id="plain"),
        pytest.param([], [_TENANT_FILTER], 6, id="filtered"),
        pytest.param([_REGION_BREAKDOWN], [], 7, id="broken_down"),
        pytest.param(
            [_REGION_BREAKDOWN], [_TENANT_FILTER], 8, id="broken_down_and_filtered"
        ),
    ],
)
def test_latest_state_custom_metric_leaves_marked_spans_out_of_the_final_winner(
    breakdowns, filters, mark_index
):
    sql, params = _latest_state_statement(
        _SCORE, hide=True, filters=filters, breakdowns=breakdowns
    )
    plain_sql, plain_params = _latest_state_statement(
        _SCORE, hide=None, filters=filters, breakdowns=breakdowns
    )

    winner_state, live_state = sql.split("), live_custom_metric_spans AS (", 1)
    mark_test = f"AND tupleElement(latest_metric_state, {mark_index}) = 1"
    # The test rides in the winner tuple, after the filter's, and the live row
    # must pass it; reading it at that element number, not another, is what
    # keeps the filter's own test and the breakdown's intact.
    assert winner_state.count(f", ({EXCLUSION_ON_WINNER})") == 1
    assert live_state.count(mark_test) == 1
    # Nothing else moves: no new parameter, every other token as without it.
    assert params == plain_params
    stripped = (
        _flat(sql)
        .replace(f", ({EXCLUSION_ON_WINNER})", "")
        .replace(f" {mark_test}", "")
    )
    assert stripped == _flat(plain_sql)
    assert _carries_no_mark(plain_sql)


@pytest.mark.parametrize(
    ("metric", "filters", "read_keys"),
    [
        pytest.param(
            _LATENCY, [_STATUS_FILTER], ["final_status"], id="filtered_widget"
        ),
        pytest.param(_TIER, [], ["tier"], id="non_number_custom_metric"),
    ],
)
def test_exact_replay_source_keeps_both_marks_readable_for_a_holding_project(
    metric, filters, read_keys
):
    sql, params = _latest_state_statement(metric, hide=True, filters=filters)
    plain_sql, plain_params = _latest_state_statement(
        metric, hide=None, filters=filters
    )

    # The statement is on the replay source, the one that packs a winner tuple.
    assert "dashboard_filter_candidate_identities AS" in sql
    # The span mark survives the narrowing of the string Map to the keys read;
    # a narrowing that dropped it would read every marked span as clean.
    assert _narrowed_keys(sql) == [*read_keys, SIMULATION_MARK_KEY]
    # The resource mark is a column of the winner: packed, then unpacked, or the
    # outer test names a column the derived table does not have.
    assert "dashboard_candidate_source.resource_attrs" in sql
    assert re.search(r"dashboard_candidate_winner\.\d+ AS resource_attrs\b", sql)
    assert sql.count(EXCLUSION_SENT) == 1
    assert params == plain_params
    # Without the decision the source is the one that always ran.
    assert _narrowed_keys(plain_sql) == read_keys
    assert "dashboard_candidate_source.resource_attrs" not in plain_sql


@pytest.mark.parametrize(
    ("metric", "filters", "breakdowns"),
    [
        pytest.param(_SCORE, [], [], id="scalar"),
        pytest.param(_SCORE, [_TENANT_FILTER], [], id="scalar_filtered"),
        pytest.param(_SCORE, [], [_REGION_BREAKDOWN], id="scalar_broken_down"),
        pytest.param(
            _SCORE,
            [_TENANT_FILTER],
            [_REGION_BREAKDOWN],
            id="scalar_broken_down_and_filtered",
        ),
        pytest.param(_LATENCY, [_STATUS_FILTER], [], id="replay_filtered_widget"),
        pytest.param(_TIER, [], [], id="replay_non_number_custom_metric"),
    ],
)
def test_latest_state_statement_is_untouched_for_a_project_that_does_not_hold(
    metric, filters, breakdowns
):
    statements = [
        _latest_state_statement(
            metric, hide=hide, filters=filters, breakdowns=breakdowns
        )
        for hide in (None, False)
    ]

    assert statements[0] == statements[1]
    sql = statements[0][0]
    assert _carries_no_mark(sql)
    # The replay source packs no resource column and narrows to no extra key.
    assert not re.search(r"dashboard_candidate_winner\.\d+ AS resource_attrs\b", sql)
    assert "dashboard_candidate_source.resource_attrs" not in sql


# ─── dashboard query builder: eval and annotation metrics ────────────────────
#
# These two read a side table (eval rows, scores) whose spans join is a LEFT
# JOIN, so the base clauses never reach them. A holding project's statement
# tests the row's own trace id against the marked traces among the traces the
# statement itself can count. The test is negative, because a row with no
# trace, or whose trace is not found among the marked ones, must still be
# counted, and it carries no time bound, because a score or an eval can be made
# long after its trace. Both metrics are built with a UUID template and an
# explicit output type, so neither resolves anything through Postgres.

ORGANIZATION_ID = "44444444-4444-4444-8444-444444444444"
WORKSPACE_ID = "55555555-5555-4555-8555-555555555555"
EVAL_TEMPLATE = "66666666-6666-4666-8666-666666666666"
ANNOTATION_LABEL = "77777777-7777-4777-8777-777777777777"

_EVAL_METRIC = {
    "id": "pass",
    "name": EVAL_TEMPLATE,
    "config_id": EVAL_TEMPLATE,
    "type": "eval_metric",
    "source": "traces",
    "aggregation": "pass_rate",
    "output_type": "PASS_FAIL",
}
_ANNOTATION_METRIC = {
    "id": "stars",
    "name": ANNOTATION_LABEL,
    "label_id": ANNOTATION_LABEL,
    "type": "annotation_metric",
    "source": "traces",
    "aggregation": "avg",
    "output_type": "numeric",
}
# The trace each statement tests: the eval row's own, in the canonical form the
# statement's project scope uses, and a score's, which is its own trace or the
# trace of the span it is attached to.
_EVAL_ROW_TRACE = "toString(toUUIDOrZero(e.eval_trace_id))"
_SCORE_SUBJECT_TRACE = (
    "if(a.trace_id IS NOT NULL, toString(a.trace_id), annotation_subject_span.trace_id)"
)
# What names the traces each statement can count, as the statement writes it.
_EVAL_CANDIDATES = "usage_span_trace_candidates"
_ANNOTATION_CANDIDATES = "annotation_subject_candidate"

_SIDE_TABLE_METRICS = [
    pytest.param(_EVAL_METRIC, _EVAL_ROW_TRACE, _EVAL_CANDIDATES, id="eval_metric"),
    pytest.param(
        _ANNOTATION_METRIC,
        _SCORE_SUBJECT_TRACE,
        _ANNOTATION_CANDIDATES,
        id="annotation_metric",
    ),
]
_BUILDERS = [
    pytest.param(DashboardQueryBuilder, id="legacy_statement"),
    pytest.param(DashboardQueryBuilderV2, id="v2_statement"),
]


def _side_table_statement(builder_class, metric, *, hide, breakdowns=()):
    config = _dashboard_config(metric, hide=hide)
    config["organization_id"] = ORGANIZATION_ID
    config["workspace_id"] = WORKSPACE_ID
    config["breakdowns"] = list(breakdowns)
    builder = builder_class(config)
    return builder.build_metric_query(builder.metrics[0])


def _as_sent(text, builder_class):
    """Fixed text as the builder writes it, then as the v2 rewriter sends it."""
    if builder_class is DashboardQueryBuilderV2:
        return rewrite_v1_sql_to_v2(text)
    return text


def _hidden_clause(sql, trace_expr, builder_class):
    """The clause the statement carries, cut out of it between its fixed ends."""
    # The exclusion is ``NOT (<mark>)``; the subquery wants the marked rows, so
    # it is negated once more, as the project eval chart does.
    head = _as_sent(
        f"\n  AND ifNull({trace_expr}, '') NOT IN ("
        "SELECT trace_id FROM spans "
        "WHERE project_id IN %(project_ids)s AND _peerdb_is_deleted = 0 "
        "AND trace_id IN (",
        builder_class,
    )
    tail = _as_sent(f") AND NOT ({EXCLUSION_LEGACY}))", builder_class)
    assert sql.count(head) == 1
    start = sql.index(head)
    end = sql.index(tail, start) + len(tail)
    return sql[start:end]


@pytest.mark.parametrize("builder_class", _BUILDERS)
@pytest.mark.parametrize(("metric", "trace_expr", "candidates"), _SIDE_TABLE_METRICS)
def test_side_table_metric_leaves_marked_traces_out_for_a_holding_project(
    builder_class, metric, trace_expr, candidates
):
    sql, params = _side_table_statement(builder_class, metric, hide=True)
    plain_sql, plain_params = _side_table_statement(builder_class, metric, hide=None)

    clause = _hidden_clause(sql, trace_expr, builder_class)
    sent_exclusion = _as_sent(EXCLUSION_LEGACY, builder_class)
    # The mark is read from spans of the request's projects, only for the traces
    # the statement can count, and the clause rides on a line of its own.
    assert sql.count(sent_exclusion) == 1
    assert candidates in clause
    assert sql.replace(clause, "") == plain_sql
    # No new placeholder: the clause reuses the statement's own.
    assert params == plain_params
    assert _carries_no_mark(plain_sql)


@pytest.mark.parametrize("builder_class", _BUILDERS)
@pytest.mark.parametrize(("metric", "trace_expr", "candidates"), _SIDE_TABLE_METRICS)
def test_side_table_lookup_has_no_time_bound_and_reads_only_the_candidates(
    builder_class, metric, trace_expr, candidates
):
    sql, _ = _side_table_statement(builder_class, metric, hide=True)
    clause = _hidden_clause(sql, trace_expr, builder_class)

    # A score or an eval made weeks after its trace still finds the mark: the
    # lookup is bounded by the traces it is asked about, never by the window.
    assert "start_time" not in clause
    assert "INTERVAL" not in clause
    assert "FINAL" not in clause
    assert "trace_id IN (SELECT" in clause
    assert candidates in clause
    # The statement's window enters only through the candidate set, which is
    # made of the rows being counted.
    assert "%(start_date)s" in clause


@pytest.mark.parametrize("builder_class", _BUILDERS)
@pytest.mark.parametrize(("metric", "trace_expr", "candidates"), _SIDE_TABLE_METRICS)
def test_side_table_clause_is_a_negative_test_that_keeps_rows_without_a_trace(
    builder_class, metric, trace_expr, candidates
):
    sql, _ = _side_table_statement(builder_class, metric, hide=True)

    # A row with an empty trace id is not in the marked set, so it stays; the
    # same holds for a row whose trace is not found among the marked ones.
    assert f"ifNull({trace_expr}, '') NOT IN (SELECT trace_id FROM spans" in sql
    # A positive membership would drop exactly those rows.
    assert f"ifNull({trace_expr}, '') IN (" not in sql
    assert not re.search(r"(?<!NOT )IN \(SELECT trace_id FROM spans", sql)


def test_eval_row_trace_is_compared_in_the_form_the_project_scope_uses():
    sql, _ = _side_table_statement(DashboardQueryBuilder, _EVAL_METRIC, hide=True)

    # The scope matches the row's trace as ``toUUIDOrZero(eval_trace_id)``, and
    # spans hold the canonical string, so an upper-case or dashless id would
    # never match the raw column and its row would be kept.
    assert "ifNull(toString(toUUIDOrZero(e.eval_trace_id)), '') NOT IN (" in sql
    assert "ifNull(e.eval_trace_id, '') NOT IN (" not in sql


@pytest.mark.parametrize("builder_class", _BUILDERS)
@pytest.mark.parametrize(("metric", "trace_expr", "candidates"), _SIDE_TABLE_METRICS)
def test_side_table_statement_is_untouched_for_a_project_that_does_not_hold(
    builder_class, metric, trace_expr, candidates
):
    undecided = _side_table_statement(builder_class, metric, hide=None)
    declined = _side_table_statement(builder_class, metric, hide=False)
    hiding = _side_table_statement(builder_class, metric, hide=True)

    assert undecided == declined
    sql, params = undecided
    assert _carries_no_mark(sql)
    assert "NOT IN (SELECT trace_id FROM spans" not in sql
    # Nothing follows the last conjunct but the newline it always had, so the
    # text up to GROUP BY is what it was before the clause existed.
    assert re.search(r"\S\nGROUP BY time_bucket\nORDER BY time_bucket", sql)
    assert sql == hiding[0].replace(
        _hidden_clause(hiding[0], trace_expr, builder_class), ""
    )
    assert params == hiding[1]


_SPANS_BREAKDOWN = [{"name": "model", "type": "system_metric"}]
_EVAL_CANDIDATE_TEXT = " ".join(
    """
    SELECT toString(trace_id) AS trace_id FROM (
        SELECT DISTINCT
            toUUIDOrZero(usage_trace_candidate.eval_trace_id) AS trace_id
        FROM usage_apicalllog AS usage_trace_candidate
        PREWHERE usage_trace_candidate.workspace_id = toUUID(%(workspace_id)s)
          AND usage_trace_candidate.source_id = %(eval_template_id)s
          AND usage_trace_candidate.created_at >= %(start_date)s
          AND usage_trace_candidate.created_at < %(end_date)s
        WHERE usage_trace_candidate.eval_trace_id != ''
    ) AS usage_span_trace_candidates
    """.split()
)


@pytest.mark.parametrize("builder_class", _BUILDERS)
def test_eval_candidate_set_is_the_same_text_with_and_without_a_spans_join(
    builder_class,
):
    # A breakdown on a span column joins the spans table, and that join already
    # names the eval's traces. The lookup reuses that text; building it for
    # every statement must not change what the join sends.
    plain, _ = _side_table_statement(
        builder_class, _EVAL_METRIC, hide=None, breakdowns=_SPANS_BREAKDOWN
    )
    hiding, _ = _side_table_statement(
        builder_class, _EVAL_METRIC, hide=True, breakdowns=_SPANS_BREAKDOWN
    )

    flat_plain = " ".join(plain.split())
    flat_hiding = " ".join(hiding.split())
    assert _EVAL_CANDIDATE_TEXT in flat_plain
    assert flat_plain.count(_EVAL_CANDIDATE_TEXT) == 1
    # The same set bounds the join and the lookup in a holding statement.
    assert flat_hiding.count(_EVAL_CANDIDATE_TEXT) == 2
    clause = _hidden_clause(hiding, _EVAL_ROW_TRACE, builder_class)
    assert hiding.replace(clause, "") == plain


def _flat(text):
    return " ".join(text.split())


# What the annotation statement can count, as its lookup names it: the traces of
# the window's scores, and the traces of the spans those scores are attached to.
_ANNOTATION_TRACE_BRANCH = _flat(
    """
    SELECT toString(trace_id) AS trace_id
    FROM (
        SELECT DISTINCT annotation_trace_candidate.trace_id AS trace_id
        FROM model_hub_score AS annotation_trace_candidate
        PREWHERE annotation_trace_candidate.organization_id =
              toUUID(%(annotation_organization_id)s)
          AND annotation_trace_candidate.label_id = toUUID(%(annotation_label_id)s)
          AND annotation_trace_candidate.created_at >= %(start_date)s
          AND annotation_trace_candidate.created_at < %(end_date)s
        WHERE annotation_trace_candidate.trace_id IS NOT NULL
    )
    UNION ALL
    """
)
_ANNOTATION_SPAN_BRANCH = _flat(
    """
    SELECT trace_id
    FROM (
        SELECT
            annotation_span_latest.id AS id,
            tupleElement(annotation_span_latest.latest_state, 1) AS trace_id,
            tupleElement(annotation_span_latest.latest_state, 2) AS project_id
        FROM (
            SELECT annotation_span_scan.id AS id,
    """
)
_ANNOTATION_SPAN_SCAN = _flat(
    """
    FROM spans AS annotation_span_scan
    PREWHERE annotation_span_scan.project_id IN %(project_ids)s
    WHERE annotation_span_scan.id IN (
        SELECT DISTINCT annotation_span_candidate.observation_span_id AS id
        FROM model_hub_score AS annotation_span_candidate
        PREWHERE annotation_span_candidate.organization_id =
              toUUID(%(annotation_organization_id)s)
          AND annotation_span_candidate.label_id = toUUID(%(annotation_label_id)s)
          AND annotation_span_candidate.created_at >= %(start_date)s
          AND annotation_span_candidate.created_at < %(end_date)s
        WHERE annotation_span_candidate.observation_span_id IS NOT NULL
          AND annotation_span_candidate.observation_span_id != ''
    )
    GROUP BY annotation_span_scan.id
    """
)


@pytest.mark.parametrize("builder_class", _BUILDERS)
def test_annotation_candidate_set_names_trace_and_span_attached_scores_in_the_window(
    builder_class,
):
    sql, _ = _side_table_statement(builder_class, _ANNOTATION_METRIC, hide=True)
    clause = _flat(_hidden_clause(sql, _SCORE_SUBJECT_TRACE, builder_class))

    # The lookup is bounded by one set, named in full: the traces of the scores
    # made in the window (trace-attached), joined by the traces of the spans the
    # remaining scores point at (span-attached). A subject trace is exactly one
    # of the two, so dropping either branch lets a marked trace's scores through.
    assert clause.count(_ANNOTATION_CANDIDATES) == 1
    assert clause.count(_ANNOTATION_TRACE_BRANCH) == 1
    # Span-attached branch: it follows the trace branch, reads the spans of the
    # request's projects, and takes its span ids from the scores of the same
    # label, organization and window as the statement.
    assert clause.count(f"{_ANNOTATION_TRACE_BRANCH} {_ANNOTATION_SPAN_BRANCH}") == 1
    assert clause.count(_ANNOTATION_SPAN_SCAN) == 1
    assert clause.count("%(start_date)s") == 2
    assert clause.count("%(end_date)s") == 2
    # Nothing else in the clause reads the scores or the spans: the lookup, the
    # span scan, and the two score reads above.
    assert clause.count("FROM spans ") == 2
    assert clause.count("FROM model_hub_score AS") == 2


# ─── dashboard fast path ─────────────────────────────────────────────────────


class _RollupAnalytics:
    def __init__(self):
        self.calls = []

    def execute_ch_query(self, query, params, *, timeout_ms, settings):
        self.calls.append(query)
        aliases = [
            part.split()[0].rstrip(",")
            for part in query.split(" AS ")[2:]
            if part.startswith("metric_")
        ]
        row = {"time_bucket": params["start_date"]}
        row.update({alias: index + 1 for index, alias in enumerate(aliases)})
        return SimpleNamespace(data=[row], columns=["time_bucket", *aliases])


def _fast_path_config():
    return {
        "project_ids": [HOLDING],
        "time_range": {"preset": "30D"},
        "granularity": "day",
        "metrics": [
            {
                "id": "tokens",
                "name": "tokens",
                "type": "system_metric",
                "source": "traces",
                "aggregation": "sum",
                "filters": [],
            }
        ],
        "filters": [],
        "breakdowns": [],
    }


def _cold_snapshot(query_config):
    """What ``read_or_schedule_exact_snapshot`` returns for a cold identity."""
    return {
        **dashboard_view._pending_dashboard_payload(query_config),
        "query_refreshing": False,
        "query_refresh_failed": True,
    }


def test_fast_path_answers_bounded_shape_unavailable_for_a_holding_project(
    monkeypatch,
):
    monkeypatch.setattr(
        dashboard_view,
        "V2AnalyticsQueryService",
        lambda: pytest.fail("a holding project must not read the hourly states"),
    )

    result = _read_dashboard_rollup_fast_path(_fast_path_config(), hide_simulated=True)

    assert result["query_status"] == "degraded"
    assert result["query_error_code"] == "bounded_shape_unavailable"


def test_fast_path_still_reads_the_hourly_states_for_a_project_that_does_not_hold(
    monkeypatch,
):
    analytics = _RollupAnalytics()
    monkeypatch.setattr(dashboard_view, "V2AnalyticsQueryService", lambda: analytics)

    result = _read_dashboard_rollup_fast_path(_fast_path_config())

    assert result["query_status"] == "complete"
    assert len(analytics.calls) == 1


def test_public_read_passes_the_decision_to_the_fast_path(monkeypatch):
    config = _fast_path_config()
    monkeypatch.setattr(
        dashboard_view,
        "read_or_schedule_exact_snapshot",
        lambda *args, **kwargs: _cold_snapshot(config),
    )
    monkeypatch.setattr(
        dashboard_view,
        "V2AnalyticsQueryService",
        lambda: pytest.fail("a holding project must not read the hourly states"),
    )

    result = _read_public_dashboard_query(
        config,
        cache_identity={"workspace_id": "workspace", "query_config": {}},
        refresh=False,
        try_rollup=True,
        hide_simulated=True,
    )

    assert result["query_error_code"] == "bounded_shape_unavailable"


def test_public_read_without_the_decision_reads_the_hourly_states(monkeypatch):
    config = _fast_path_config()
    analytics = _RollupAnalytics()
    monkeypatch.setattr(
        dashboard_view,
        "read_or_schedule_exact_snapshot",
        lambda *args, **kwargs: _cold_snapshot(config),
    )
    monkeypatch.setattr(dashboard_view, "V2AnalyticsQueryService", lambda: analytics)

    result = _read_public_dashboard_query(
        config,
        cache_identity={"workspace_id": "workspace", "query_config": {}},
        refresh=False,
        try_rollup=True,
    )

    assert result["query_status"] == "complete"
    assert len(analytics.calls) == 1


# ─── dashboard execution: scope decision, snapshot identity, statements ──────


class _StubAnalytics:
    """Records every statement the view sends to ClickHouse."""

    def __init__(self):
        self.statements = []

    def execute_ch_query(self, sql, params=None, timeout_ms=None, settings=None):
        self.statements.append(SimpleNamespace(sql=sql, params=params))
        return QueryResult(
            data=[],
            row_count=0,
            backend_used="clickhouse",
            query_time_ms=5.0,
            columns=[],
            read_bytes=100,
        )

    @property
    def reads(self):
        return [s for s in self.statements if not s.sql.startswith("EXPLAIN ")]


_RENDERABLE = {
    "metrics": [],
    "query_complete": True,
    "query_status": "complete",
    "query_sampled": False,
}


def _query(project_ids, *, source="traces"):
    end = datetime(2026, 9, 1, tzinfo=UTC)
    return {
        "project_ids": list(project_ids),
        "granularity": "day",
        "time_range": {
            "custom_start": (end - timedelta(days=30)).isoformat(),
            "custom_end": end.isoformat(),
        },
        "metrics": [
            {
                "id": "latency",
                "name": "latency",
                "type": "system_metric",
                "source": source,
                "aggregation": "avg",
            }
        ],
        "filters": [],
        "breakdowns": [],
    }


def _execute(
    query,
    workspace,
    *,
    holding=(),
    refresh=False,
    exact_worker=False,
    override=None,
    fast_path_result=None,
):
    analytics = _StubAnalytics()
    holds = _HoldsAmong(*holding)
    identities = []

    def _cold_miss(_namespace, identity, **kwargs):
        identities.append(deepcopy(identity))
        payload = dict(kwargs["pending_payload"])
        payload.update(query_refreshing=False, query_refresh_failed=False)
        return payload

    fast_path = MagicMock(return_value=fast_path_result)
    scheduled = MagicMock(return_value={"query_status": "pending"})
    project_count = len(query["project_ids"])
    with (
        patch(
            "tracer.views.dashboard._materialize_dashboard_query_scope",
            side_effect=lambda config, *_args, **_kwargs: config,
        ),
        patch("tracer.views.dashboard._read_dashboard_rollup_fast_path", fast_path),
        patch(
            "tracer.views.dashboard.read_or_schedule_exact_snapshot",
            side_effect=_cold_miss,
        ),
        patch("tracer.views.dashboard._read_public_dashboard_query", scheduled),
        patch(
            "tracer.views.dashboard.V2AnalyticsQueryService",
            side_effect=lambda **_kwargs: analytics,
        ),
        patch(
            "tracer.views.dashboard._project_queryset_for_dashboard_scope",
            return_value=MagicMock(
                filter=MagicMock(
                    return_value=MagicMock(count=lambda: project_count),
                )
            ),
        ),
        patch(
            "tracer.views.dashboard.Project.objects.filter",
            return_value=MagicMock(values_list=MagicMock(return_value=[])),
        ),
        patch("tracer.views.dashboard.holding_projects", holds),
    ):
        response = DashboardWidgetViewSet()._execute_ch_query_config(
            query,
            workspace,
            refresh=refresh,
            _exact_worker=exact_worker,
            cache_identity_override=override,
        )
    return SimpleNamespace(
        response=response,
        analytics=analytics,
        holds=holds,
        identities=identities,
        fast_path=fast_path,
        scheduled=scheduled,
    )


@pytest.fixture(autouse=True)
def _clean_density_cache(settings):
    settings.DASHBOARD_ATTR_ROLLUP_ENABLED = False
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def workspace():
    return SimpleNamespace(id=uuid.uuid4(), organization_id=uuid.uuid4())


def test_dashboard_over_two_projects_hides_when_the_second_holds(workspace):
    run = _execute(_query([PLAIN, HOLDING]), workspace, holding=[HOLDING])

    assert run.holds.asked == [[PLAIN, HOLDING]]
    assert run.analytics.reads
    for read in run.analytics.reads:
        assert EXCLUSION_SENT in read.sql
        assert list(read.params["project_ids"]) == [PLAIN, HOLDING]


def test_dashboard_over_two_projects_none_holding_sends_the_statement_as_before(
    workspace,
):
    run = _execute(_query([PLAIN, OTHER_PLAIN]), workspace, holding=[HOLDING])

    assert run.holds.asked == [[PLAIN, OTHER_PLAIN]]
    assert run.analytics.reads
    assert all(_carries_no_mark(read.sql) for read in run.analytics.reads)


def test_snapshot_identity_gains_the_key_only_when_the_scope_holds(workspace):
    holding = _execute(_query([HOLDING]), workspace, holding=[HOLDING])
    plain = _execute(_query([PLAIN]), workspace, holding=[HOLDING])

    (held,) = holding.identities
    (unheld,) = plain.identities
    assert held["hide_simulated"] is True
    assert set(held) == {
        "workspace_id",
        "query_config",
        "trace_snapshot_semantics",
        "hide_simulated",
    }
    assert set(unheld) == {"workspace_id", "query_config", "trace_snapshot_semantics"}
    # The decision is the identity's, never a field of the stored query.
    assert "hide_simulated" not in held["query_config"]
    assert "hide_simulated" not in unheld["query_config"]


def test_scheduled_refresh_carries_the_decision_and_the_identity(workspace):
    holding = _execute(_query([HOLDING]), workspace, holding=[HOLDING], refresh=True)
    plain = _execute(_query([PLAIN]), workspace, holding=[HOLDING], refresh=True)

    held_call = holding.scheduled.call_args.kwargs
    assert held_call["hide_simulated"] is True
    assert held_call["cache_identity"]["hide_simulated"] is True
    plain_call = plain.scheduled.call_args.kwargs
    assert plain_call["hide_simulated"] is False
    assert "hide_simulated" not in plain_call["cache_identity"]


def test_a_dashboard_without_trace_metrics_hands_the_decision_to_the_fast_path(
    workspace,
):
    holding = _execute(
        _query([HOLDING], source="simulation"),
        workspace,
        holding=[HOLDING],
        fast_path_result=_RENDERABLE,
    )
    plain = _execute(
        _query([PLAIN], source="simulation"),
        workspace,
        holding=[HOLDING],
        fast_path_result=_RENDERABLE,
    )

    assert holding.fast_path.call_args.kwargs["hide_simulated"] is True
    assert plain.fast_path.call_args.kwargs["hide_simulated"] is False


def test_background_reentry_recomputes_the_predicate_and_leaves_the_override_alone(
    workspace,
):
    # A refresh scheduled before the project held: its identity has no key.
    override = {
        "workspace_id": str(workspace.id),
        "query_config": _query([HOLDING]),
        "trace_snapshot_semantics": "physical-latest-complete-series-v2",
    }
    before = deepcopy(override)

    run = _execute(
        deepcopy(override["query_config"]),
        workspace,
        holding=[HOLDING],
        exact_worker=True,
        override=override,
    )

    assert run.analytics.reads
    assert all(EXCLUSION_SENT in read.sql for read in run.analytics.reads)
    assert override == before


def test_a_replayed_identity_is_used_as_given(workspace):
    without_key = {
        "workspace_id": str(workspace.id),
        "query_config": _query([HOLDING]),
    }
    with_key = {**deepcopy(without_key), "hide_simulated": True}

    scheduled_before_holding = _execute(
        deepcopy(without_key["query_config"]),
        workspace,
        holding=[HOLDING],
        override=without_key,
    )
    scheduled_after_holding = _execute(
        deepcopy(with_key["query_config"]),
        workspace,
        holding=[],
        override=with_key,
    )

    (unkeyed,) = scheduled_before_holding.identities
    assert "hide_simulated" not in unkeyed
    (keyed,) = scheduled_after_holding.identities
    assert keyed["hide_simulated"] is True


def test_scope_decision_skips_ids_that_cannot_name_a_project(monkeypatch):
    holds = _HoldsAmong(HOLDING)
    monkeypatch.setattr(dashboard_view, "holding_projects", holds)

    assert dashboard_view._scope_holds_simulated(["all", HOLDING]) is True
    assert dashboard_view._scope_holds_simulated(None) is False
    assert holds.asked == [[HOLDING], []]
