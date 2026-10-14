"""Project charts, their snapshots and their refreshes leave out marked traces.

Nothing here opens a Postgres or ClickHouse connection. ClickHouse is a
recording double, the snapshot cache is the in-process test cache, the few
Postgres reads a chart makes are replaced where they happen, and the autouse
fixture below fails a test that reaches a socket or a database connection.
"""

from __future__ import annotations

import re
from contextlib import nullcontext
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

import pytest
from django.core.cache import cache

from tracer.services import exact_aggregation_cache as snapshot_cache
from tracer.services import simulation_visibility as visibility
from tracer.services.clickhouse import exact_graph_reads as exact
from tracer.services.clickhouse import graph_dispatch as dispatch
from tracer.services.clickhouse import graph_read_cost
from tracer.services.clickhouse.query_builders.exact_graph_predicates import (
    compile_exact_graph_row_predicates,
)
from tracer.services.clickhouse.query_builders.latest_filter_predicates import (
    compile_exact_graph_filter_predicates,
)
from tracer.services.clickhouse.query_builders.time_series import (
    TimeSeriesQueryBuilder,
    plan_hiding_simulated,
)
from tracer.services.clickhouse.v2 import query_service
from tracer.services.clickhouse.v2.query_builders.agent_graph import (
    AgentGraphQueryBuilderV2,
)
from tracer.services.clickhouse.v2.query_builders.eval_metrics import (
    EvalMetricsQueryBuilderV2,
)
from tracer.services.simulation_visibility import (
    RESOURCE_MARK_KEY,
    SIMULATION_MARK_KEY,
    resource_mark_sql,
    row_exclusion_sql,
)
from tracer.tasks import exact_aggregation
from tracer.tests._graph_cost_stub import affordable_scan_estimate
from tracer.utils import graphs_optimized

pytestmark = pytest.mark.unit

PROJECT = "11111111-1111-4111-8111-111111111111"
ORG = "22222222-2222-4222-8222-222222222222"
CONFIG = "33333333-3333-4333-8333-333333333333"
LABEL = "44444444-4444-4444-8444-444444444444"
TRACE = "55555555-5555-4555-8555-555555555555"
SPAN = "66666666-6666-4666-8666-666666666666"
END = datetime(2026, 9, 4)
COLUMNS = [
    "time_bucket",
    "avg_latency",
    "total_tokens",
    "avg_cost",
    "traffic_count",
    "prompt_tokens",
    "completion_tokens",
    "error_rate",
]
# The span-grain rule on the physical spans table, as the chart builders send it.
UNMARKED = row_exclusion_sql(map_column="attrs_string")
COMPLETE = {
    "metric_name": "metric",
    "data": [],
    "query_complete": True,
    "query_status": "complete",
    "query_sampled": False,
}


@pytest.fixture(autouse=True, scope="session")
def _drop_legacy_ch_spans_mvs():
    # Overrides the session fixture that talks to ClickHouse.
    yield


@pytest.fixture(autouse=True, scope="session")
def _ensure_test_score_tenant_column():
    # Overrides the session fixture that talks to ClickHouse.
    yield


@pytest.fixture(autouse=True)
def offline(monkeypatch, settings):
    import socket

    from django.db.backends.base.base import BaseDatabaseWrapper

    def forbidden(*args, **kwargs):
        pytest.fail("a chart test reached a database or the network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(BaseDatabaseWrapper, "ensure_connection", forbidden)
    settings.DASHBOARD_TRACE_REPLICA_SHARD_CLUSTER = ""
    cache.clear()
    yield
    cache.clear()


def leaf(key, value, *, kind="text", op="equals", family="SPAN_ATTRIBUTE"):
    return {
        "column_id": key,
        "filter_config": {
            "col_type": family,
            "filter_type": kind,
            "filter_op": op,
            "filter_value": value,
        },
    }


def window(days=7):
    return leaf(
        "created_at",
        [(END - timedelta(days=days)).isoformat(), END.isoformat()],
        kind="datetime",
        op="between",
        family="SYSTEM_METRIC",
    )


def squash(sql):
    return " ".join(sql.split())


def graph_clause(observe_type, days=7):
    """The clause the graph compiler adds for a hiding read."""

    clause, _params = compile_exact_graph_filter_predicates(
        [window(days)],
        project_id=PROJECT,
        observe_type=observe_type,
        hide_simulated=True,
    )
    assert clause
    return squash(clause)


class RecordingAnalytics:
    """Answers the cost probe with a chosen estimate and every graph read with no rows."""

    supports_per_query_read_settings = True

    def __init__(self, estimate_rows=1_000):
        self.calls = []
        self.estimate_rows = estimate_rows

    def execute_ch_query(self, query, params=None, **kwargs):
        self.calls.append((query, dict(params or {})))
        if "EXPLAIN ESTIMATE" in query:
            return affordable_scan_estimate(self.estimate_rows)
        return SimpleNamespace(data=[], columns=COLUMNS, query_time_ms=1)

    @property
    def statements(self):
        """What the read itself sent, without the cost probes."""

        return [call for call in self.calls if "EXPLAIN ESTIMATE" not in call[0]]

    @property
    def probes(self):
        return [call for call in self.calls if "EXPLAIN ESTIMATE" in call[0]]


class SeedOnceAnalytics(RecordingAnalytics):
    """Hands the trace lane one candidate trace, so it seeds and then classifies."""

    def execute_ch_query(self, query, params=None, **kwargs):
        first = not self.statements
        result = super().execute_ch_query(query, params, **kwargs)
        if first and "EXPLAIN ESTIMATE" not in query:
            return SimpleNamespace(
                data=[{"trace_id": TRACE, "start_time": END - timedelta(hours=3)}],
                columns=["trace_id", "start_time"],
                query_time_ms=1,
            )
        return result


@pytest.fixture
def no_snapshot(monkeypatch):
    """Every cache probe misses, so the read runs its statement inline."""

    monkeypatch.setattr(
        dispatch, "read_or_schedule_exact_snapshot", lambda *a, **k: None
    )


@pytest.fixture
def identities(monkeypatch):
    """Each identity a chart read hands to the snapshot cache, with its namespace."""

    seen = []

    def record(namespace, identity, **kwargs):
        seen.append((namespace, dict(identity)))
        return kwargs["pending_payload"]

    monkeypatch.setattr(dispatch, "read_or_schedule_exact_snapshot", record)
    return seen


@pytest.fixture
def eval_config(monkeypatch):
    """The one Postgres read of an eval chart, answered in memory."""

    config = SimpleNamespace(
        name="quality",
        eval_template=SimpleNamespace(config={"output": "SCORE"}, choices=[]),
    )
    monkeypatch.setattr(
        exact.CustomEvalConfig.objects,
        "select_related",
        lambda *_args: SimpleNamespace(get=lambda **_kwargs: config),
    )


class _ScoreRows:
    def __init__(self, rows):
        self.rows = list(rows)

    def order_by(self, *_args):
        return self

    def values(self, *_args):
        return self

    def iterator(self, chunk_size=None):
        return iter(self.rows)


class _ScoreManager:
    def __init__(self, row):
        self.row = row

    def filter(self, **kwargs):
        inside = (
            kwargs["created_at__gte"]
            <= self.row["created_at"]
            < kwargs["created_at__lt"]
        )
        return _ScoreRows([self.row] if inside else [])


@pytest.fixture
def annotation_rows(monkeypatch):
    """The label and Score reads of an annotation chart, answered in memory."""

    label = SimpleNamespace(name="quality", type="numeric")
    row = {
        "trace_id": TRACE,
        "observation_span_id": SPAN,
        "created_at": END - timedelta(hours=3),
        "value": {"rating": 4},
    }
    monkeypatch.setattr(
        exact,
        "get_annotation_labels_for_project",
        lambda _project_id: SimpleNamespace(get=lambda **_kwargs: label),
    )
    monkeypatch.setattr(
        exact, "Score", SimpleNamespace(no_workspace_objects=_ScoreManager(row))
    )


def assert_rule_sent(run, carries_rule):
    """``run(analytics, **hide)`` sends the rule only when told to hide.

    Omitting the argument and passing false send equal statements, with their
    parameters, and neither carries the rule; passing true sends it.
    """

    omitted, off, on = RecordingAnalytics(), RecordingAnalytics(), RecordingAnalytics()
    run(omitted)
    run(off, hide_simulated=False)
    run(on, hide_simulated=True)
    assert omitted.statements, "the read sent no statement"
    assert off.statements == omitted.statements
    assert not any(carries_rule(sql) for sql, _ in omitted.statements)
    assert any(carries_rule(sql) for sql, _ in on.statements)
    assert on.statements != omitted.statements


def has_unmarked(sql):
    return UNMARKED in sql


def mentions_a_mark(calls):
    """Whether any statement, or any parameter it carries, names either mark."""

    text = " ".join(f"{sql} {sorted(params.items())!r}" for sql, params in calls)
    return SIMULATION_MARK_KEY in text or RESOURCE_MARK_KEY in text


def has_graph_clause(observe_type, days=7):
    clause = graph_clause(observe_type, days)
    return lambda sql: clause in squash(sql)


def has_hidden_scan(sql):
    """The eval chart's own read of the marked rows, which needs no compiled clause."""

    return f"NOT ({squash(UNMARKED)})" in squash(sql)


# ---------------------------------------------------------------------------
# The five cache identities
# ---------------------------------------------------------------------------


def _fetch_system(**hide):
    return dispatch.fetch_system_metric_graph_ch(
        analytics=RecordingAnalytics(),
        project_id=PROJECT,
        filters=[window()],
        interval="day",
        metric_id="latency",
        observe_type="trace",
        organization_id=ORG,
        **hide,
    )


def _fetch_agent(**hide):
    return dispatch.fetch_agent_graph_ch(
        project_id=PROJECT,
        filters=[window()],
        organization_id=ORG,
        **hide,
    )


def _fetch_eval(**hide):
    return dispatch.fetch_eval_graph_ch(
        analytics=RecordingAnalytics(),
        project_id=PROJECT,
        filters=[window()],
        interval="day",
        req_data_config={"id": CONFIG},
        observe_type="trace",
        organization_id=ORG,
        **hide,
    )


def _fetch_eval_series(**hide):
    return dispatch.fetch_eval_chart_series_ch(
        analytics=RecordingAnalytics(),
        project_id=PROJECT,
        filters=[window()],
        interval="day",
        req_data_config={"id": CONFIG},
        eval_name="quality",
        organization_id=ORG,
        **hide,
    )


def _fetch_annotation(**hide):
    return dispatch.fetch_annotation_graph_ch(
        analytics=RecordingAnalytics(),
        project_id=PROJECT,
        filters=[window()],
        interval="day",
        req_data_config={"id": LABEL},
        observe_type="trace",
        organization_id=ORG,
        **hide,
    )


IDENTITY_READS = {
    "system": ("observe-system-graph", _fetch_system),
    "agent": ("observe-agent-graph", _fetch_agent),
    "eval": ("observe-eval-graph", _fetch_eval),
    "eval_series": ("observe-eval-chart-series", _fetch_eval_series),
    "annotation": ("observe-annotation-graph", _fetch_annotation),
}


@pytest.mark.parametrize("name", list(IDENTITY_READS))
def test_identity_gains_the_key_exactly_when_hiding(name, identities, monkeypatch):
    namespace, fetch = IDENTITY_READS[name]
    # The eval and annotation charts key a snapshot only once their read is
    # handed to the background lane.
    monkeypatch.setattr(
        dispatch,
        "_affordable_raw_log_membership_read",
        lambda **_: dispatch._GraphReadUnaffordable(estimated_rows=None),
    )

    fetch()
    fetch(hide_simulated=False)
    fetch(hide_simulated=True)

    assert [seen for seen, _ in identities] == [namespace] * 3
    omitted, off, on = (identity for _, identity in identities)
    assert "hide_simulated" not in omitted
    assert off == omitted
    assert on == {**omitted, "hide_simulated": True}


def test_snapshot_from_before_holding_is_not_served(settings):
    settings.EXACT_AGGREGATION_TASK_QUEUE = "exact_aggregation"
    payload = {
        "nodes": [{"id": "stored"}],
        "edges": [],
        "path_edges": [],
        "query_complete": True,
        "query_status": "complete",
        "query_sampled": False,
    }

    with mock.patch(
        "tracer.tasks.exact_aggregation.refresh_exact_aggregation_snapshot.apply_async"
    ) as enqueue:
        _fetch_agent()
        task = enqueue.call_args.kwargs["kwargs"]
        assert snapshot_cache.publish_exact_snapshot_for_refresh(
            task["namespace"], task["identity"], payload, task["refresh_token"]
        )
        snapshot_cache.finish_exact_refresh(
            task["namespace"], task["identity"], task["refresh_token"], succeeded=True
        )
        scheduled_before = enqueue.call_count

        served = _fetch_agent()
        hidden = _fetch_agent(hide_simulated=True)

    assert served["nodes"] == payload["nodes"]
    assert hidden["nodes"] == []
    assert hidden["query_status"] != "complete"
    assert enqueue.call_count == scheduled_before + 1
    assert enqueue.call_args.kwargs["kwargs"]["identity"]["hide_simulated"] is True


# ---------------------------------------------------------------------------
# The unfiltered system chart
# ---------------------------------------------------------------------------


def test_unfiltered_system_chart_takes_the_exact_path_when_hiding(
    monkeypatch, no_snapshot
):
    rollup = mock.Mock(return_value=dict(COMPLETE))
    monkeypatch.setattr(dispatch, "_fetch_rollup_system_metric_graph", rollup)

    def read(hide):
        analytics = RecordingAnalytics()
        dispatch.fetch_system_metric_graph_ch(
            analytics=analytics,
            project_id=PROJECT,
            filters=[window()],
            interval="day",
            metric_id="traffic",
            observe_type="span",
            hide_simulated=hide,
        )
        return analytics

    read(False)
    rollup.assert_called_once()

    rollup.reset_mock()
    hiding = read(True)
    rollup.assert_not_called()
    assert any(UNMARKED in sql for sql, _ in hiding.statements)


@pytest.mark.parametrize("observe_type", ["trace", "span"])
@pytest.mark.parametrize("metric_id", ["latency", "traffic"])
def test_system_chart_entry_point_carries_the_rule(
    observe_type, metric_id, no_snapshot
):
    # A filtered chart never takes the rollup, so omitted and false are compared
    # on the same exact statement the true read adds the rule to.
    def run(analytics, **hide):
        dispatch.fetch_system_metric_graph_ch(
            analytics=analytics,
            project_id=PROJECT,
            filters=[window(), leaf("company_id", "company")],
            interval="day",
            metric_id=metric_id,
            observe_type=observe_type,
            **hide,
        )

    assert_rule_sent(run, has_unmarked)


def test_background_system_read_carries_the_rule():
    def run(analytics, **hide):
        dispatch.fetch_background_raw_system_metric_graph(
            analytics=analytics,
            project_id=PROJECT,
            filters=[window()],
            interval="day",
            metric_id="latency",
            observe_type="trace",
            **hide,
        )

    assert_rule_sent(run, has_unmarked)


def test_all_system_metrics_entry_point_carries_the_rule():
    def run(analytics, **hide):
        dispatch.fetch_all_system_metrics_ch(
            analytics=analytics,
            project_id=PROJECT,
            filters=[window()],
            interval="day",
            **hide,
        )

    assert_rule_sent(run, has_unmarked)


def test_eval_graph_entry_point_carries_the_rule(eval_config):
    def run(analytics, **hide):
        dispatch.fetch_eval_graph_ch(
            analytics=analytics,
            project_id=PROJECT,
            filters=[window()],
            interval="day",
            req_data_config={"id": CONFIG, "eval_output_type": "SCORE"},
            observe_type="trace",
            **hide,
        )

    assert_rule_sent(run, has_hidden_scan)


@pytest.mark.parametrize("observe_type", ["trace", "span"])
def test_annotation_graph_entry_point_carries_the_rule(observe_type, annotation_rows):
    def run(analytics, **hide):
        dispatch.fetch_annotation_graph_ch(
            analytics=analytics,
            project_id=PROJECT,
            filters=[window(days=1)],
            interval="day",
            req_data_config={"id": LABEL, "output_type": "float"},
            observe_type=observe_type,
            **hide,
        )

    assert_rule_sent(run, has_graph_clause(observe_type, days=1))


# ---------------------------------------------------------------------------
# The exact reads
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("observe_type", ["trace", "span"])
def test_exact_system_graph_carries_the_rule(observe_type, monkeypatch):
    # No request filter is a plan of its own: the lane is chosen from the
    # request's filters, so the rule joins one statement and not a replay.
    monkeypatch.setattr(
        exact,
        "_enumerate_exact_trace_ids",
        lambda **_: pytest.fail("the rule moved the read onto the replay lane"),
    )

    def run(analytics, **hide):
        exact.read_exact_system_graph(
            analytics=analytics,
            project_id=PROJECT,
            filters=[window()],
            interval="day",
            metric_id="traffic",
            observe_type=observe_type,
            **hide,
        )

    assert_rule_sent(run, has_unmarked)
    omitted = RecordingAnalytics()
    run(omitted, hide_simulated=True)
    assert len(omitted.statements) == 1


def test_exact_span_graph_with_a_request_filter_hides_in_every_partition():
    analytics = RecordingAnalytics()
    exact.read_exact_system_graph(
        analytics=analytics,
        project_id=PROJECT,
        filters=[window(days=1), leaf("company_id", "company")],
        interval="day",
        metric_id="traffic",
        observe_type="span",
        hide_simulated=True,
    )

    assert analytics.statements
    assert all(UNMARKED in sql for sql, _ in analytics.statements)


@pytest.mark.parametrize("hide", [False, True])
def test_exact_trace_graph_enumerates_its_traces_under_the_rule(hide, monkeypatch):
    seen = {}

    def enumerate_ids(**kwargs):
        seen.update(kwargs)
        return [TRACE], 0, 0

    monkeypatch.setattr(exact, "_enumerate_exact_trace_ids", enumerate_ids)
    exact.read_exact_system_graph(
        analytics=RecordingAnalytics(),
        project_id=PROJECT,
        filters=[window(), leaf("company_id", "company")],
        interval="day",
        metric_id="traffic",
        observe_type="trace",
        hide_simulated=hide,
    )

    assert seen["hide_simulated"] is hide


def test_trace_enumeration_builds_its_list_under_the_rule():
    # The filtered trace lane trusts the trace list builder with the rule, so
    # the statements it sends, seed and classifier alike, are what is read.
    def run(analytics, **hide):
        exact.read_exact_system_graph(
            analytics=analytics,
            project_id=PROJECT,
            filters=[window(days=1), leaf("company_id", "company")],
            interval="day",
            metric_id="traffic",
            observe_type="trace",
            **hide,
        )

    omitted, off, on = SeedOnceAnalytics(), SeedOnceAnalytics(), SeedOnceAnalytics()
    run(omitted)
    run(off, hide_simulated=False)
    run(on, hide_simulated=True)

    assert len(omitted.statements) > 1, "the lane did not reach its classifier"
    assert off.calls == omitted.calls
    assert not mentions_a_mark(omitted.calls)
    assert mentions_a_mark(on.statements)


def test_exact_all_system_metrics_carries_the_rule():
    def run(analytics, **hide):
        exact.read_exact_all_system_metrics(
            analytics=analytics,
            project_id=PROJECT,
            filters=[window()],
            interval="day",
            **hide,
        )

    assert_rule_sent(run, has_unmarked)


def test_exact_agent_graph_carries_the_rule():
    def run(analytics, **hide):
        exact.read_exact_agent_graph(
            analytics=analytics,
            project_id=PROJECT,
            filters=[window()],
            **hide,
        )

    assert_rule_sent(run, has_unmarked)


@pytest.mark.parametrize("all_series", [False, True])
def test_exact_eval_graph_carries_the_rule(all_series, eval_config):
    def run(analytics, **hide):
        exact.read_exact_eval_graph(
            analytics=analytics,
            project_id=PROJECT,
            filters=[window()],
            interval="day",
            req_data_config={"id": CONFIG, "eval_output_type": "SCORE"},
            observe_type="trace",
            all_series=all_series,
            **hide,
        )

    assert_rule_sent(run, has_hidden_scan)


@pytest.mark.parametrize("observe_type", ["trace", "span"])
def test_exact_annotation_graph_carries_the_rule(observe_type, annotation_rows):
    # A span chart matches its annotated spans by the span-grain compile alone,
    # a trace chart by the trace-grain one, so each grain is read on its own.
    def run(analytics, **hide):
        exact.read_exact_annotation_graph(
            analytics=analytics,
            project_id=PROJECT,
            filters=[window(days=1)],
            interval="day",
            req_data_config={"id": LABEL, "output_type": "float"},
            observe_type=observe_type,
            **hide,
        )

    assert_rule_sent(run, has_graph_clause(observe_type, days=1))


# ---------------------------------------------------------------------------
# The background refresh
# ---------------------------------------------------------------------------


class HoldingSpy:
    """Stands in for the project's holding state and records every question."""

    def __init__(self):
        self.holds = False
        self.asked = []

    def holding_projects(self, project_ids):
        self.asked.append(("holding_projects", tuple(project_ids)))
        return frozenset(project_ids if self.holds else ())

    def hide_simulated(self, project_id):
        self.asked.append(("hide_simulated", project_id))
        return self.holds


@pytest.fixture
def refresh_worker(monkeypatch):
    """The worker entry with its tenant check stubbed, and a holding-state spy."""

    monkeypatch.setattr(
        exact_aggregation, "_reauthorize_exact_observe_project", lambda _identity: None
    )
    spy = HoldingSpy()
    monkeypatch.setattr(visibility, "holding_projects", spy.holding_projects)
    monkeypatch.setattr(visibility, "hide_simulated", spy.hide_simulated)
    # A module that bound either name at import would not see the patches above.
    for name in ("holding_projects", "hide_simulated"):
        monkeypatch.setattr(exact_aggregation, name, getattr(spy, name), raising=False)
    return spy


def _identity(**extra):
    return {
        "project_id": PROJECT,
        "organization_id": ORG,
        "filters": [window()],
        "interval": "day",
        "metric_id": "latency",
        "observe_type": "trace",
        "req_data_config": {"id": CONFIG},
        **extra,
    }


READER_OF = {
    "observe-agent-graph": (exact, "read_exact_agent_graph"),
    "observe-system-graph": (dispatch, "fetch_background_raw_system_metric_graph"),
    "observe-all-system-graphs": (exact, "read_exact_all_system_metrics"),
    "observe-eval-graph": (exact, "read_exact_eval_graph"),
    "observe-eval-chart-series": (exact, "read_exact_eval_graph"),
    "observe-annotation-graph": (exact, "read_exact_annotation_graph"),
}


@pytest.mark.parametrize("namespace", list(READER_OF))
@pytest.mark.parametrize(
    ("identity_says", "project_holds"),
    [(True, False), (False, True), (True, True), (False, False)],
)
def test_refresh_hides_by_its_identity_alone(
    namespace, identity_says, project_holds, monkeypatch, refresh_worker
):
    # The project's state answers the opposite of the identity in two of the
    # four cases, so a refresh that consulted it instead would flip.
    refresh_worker.holds = project_holds
    monkeypatch.setattr(
        exact_aggregation, "_exact_observe_analytics", lambda: nullcontext(object())
    )
    calls = []

    def reader(**kwargs):
        calls.append(kwargs)
        return dict(COMPLETE)

    module, name = READER_OF[namespace]
    monkeypatch.setattr(module, name, reader)
    identity = _identity(**({"hide_simulated": True} if identity_says else {}))

    exact_aggregation._observe_payload(namespace, identity)

    (kwargs,) = calls
    assert kwargs.get("hide_simulated", False) is identity_says
    assert refresh_worker.asked == []


SCHEDULED_READS = {
    "agent": (_fetch_agent, lambda sql: has_unmarked(sql)),
    "eval": (_fetch_eval, has_hidden_scan),
    "eval_series": (_fetch_eval_series, has_hidden_scan),
}


@pytest.mark.parametrize("name", list(SCHEDULED_READS))
def test_snapshot_stored_under_the_hiding_identity_was_computed_with_the_rule(
    name, monkeypatch, settings, refresh_worker, eval_config
):
    # What the worker publishes under a hiding key is exactly what the key
    # promises: its own statement carries the rule, not only its identity.
    settings.EXACT_AGGREGATION_TASK_QUEUE = "exact_aggregation"
    monkeypatch.setattr(
        dispatch,
        "_affordable_raw_log_membership_read",
        lambda **_: dispatch._GraphReadUnaffordable(estimated_rows=None),
    )
    fetch, carries_rule = SCHEDULED_READS[name]
    with mock.patch(
        "tracer.tasks.exact_aggregation.refresh_exact_aggregation_snapshot.apply_async"
    ) as enqueue:
        fetch(hide_simulated=True)
    task = enqueue.call_args.kwargs["kwargs"]
    assert task["identity"]["hide_simulated"] is True

    worker = RecordingAnalytics()
    monkeypatch.setattr(
        exact_aggregation, "_exact_observe_analytics", lambda: nullcontext(worker)
    )
    exact_aggregation._observe_payload(task["namespace"], task["identity"])

    assert worker.statements
    assert any(carries_rule(sql) for sql, _ in worker.statements)


@pytest.mark.parametrize(
    ("namespace", "name"),
    [
        ("observe-user-system-graph", "read_exact_user_system_graph"),
        ("observe-session-system-graph", "read_exact_session_system_graph"),
    ],
)
def test_user_and_session_refreshes_take_no_rule(
    namespace, name, monkeypatch, refresh_worker
):
    monkeypatch.setattr(
        exact_aggregation, "_exact_observe_analytics", lambda: nullcontext(object())
    )
    calls = []
    monkeypatch.setattr(
        exact, name, lambda **kwargs: calls.append(kwargs) or dict(COMPLETE)
    )

    exact_aggregation._observe_payload(namespace, _identity(hide_simulated=True))

    (kwargs,) = calls
    assert "hide_simulated" not in kwargs


def test_background_refresh_sends_the_statement_of_its_interactive_read(
    monkeypatch, refresh_worker, no_snapshot
):
    interactive, background, plain = (
        RecordingAnalytics(),
        RecordingAnalytics(),
        RecordingAnalytics(),
    )
    dispatch.fetch_system_metric_graph_ch(
        analytics=interactive,
        project_id=PROJECT,
        filters=[window()],
        interval="day",
        metric_id="latency",
        observe_type="trace",
        hide_simulated=True,
    )
    analytics_of_the_worker = iter([background, plain])
    monkeypatch.setattr(
        exact_aggregation,
        "_exact_observe_analytics",
        lambda: nullcontext(next(analytics_of_the_worker)),
    )

    exact_aggregation._observe_payload(
        "observe-system-graph", _identity(hide_simulated=True)
    )
    exact_aggregation._observe_payload("observe-system-graph", _identity())

    assert any(UNMARKED in sql for sql, _ in interactive.statements)
    assert background.statements == interactive.statements
    assert plain.statements
    assert not any(UNMARKED in sql for sql, _ in plain.statements)


def test_background_all_metrics_refresh_sends_the_statement_of_its_interactive_read(
    monkeypatch, refresh_worker
):
    interactive, background = RecordingAnalytics(), RecordingAnalytics()
    dispatch.fetch_all_system_metrics_ch(
        analytics=interactive,
        project_id=PROJECT,
        filters=[window()],
        interval="day",
        hide_simulated=True,
    )
    monkeypatch.setattr(
        exact_aggregation, "_exact_observe_analytics", lambda: nullcontext(background)
    )

    exact_aggregation._observe_payload(
        "observe-all-system-graphs", _identity(hide_simulated=True)
    )

    assert background.statements == interactive.statements
    assert any(UNMARKED in sql for sql, _ in background.statements)


# ---------------------------------------------------------------------------
# The project chart helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def chart_reads(monkeypatch):
    """The two readers the project chart helpers call, recording their keywords."""

    seen = {"all": [], "eval": []}
    monkeypatch.setattr(query_service, "V2AnalyticsQueryService", lambda: "analytics")
    monkeypatch.setattr(
        dispatch,
        "fetch_all_system_metrics_ch",
        lambda **kwargs: (
            seen["all"].append(kwargs)
            or {"latency": [], "tokens": [], "cost": [], "traffic": []}
        ),
    )
    monkeypatch.setattr(
        dispatch,
        "fetch_eval_chart_series_ch",
        lambda **kwargs: seen["eval"].append(kwargs) or [],
    )
    config = SimpleNamespace(
        name="quality",
        eval_template=SimpleNamespace(config={"output": "SCORE"}, choices=[]),
    )
    monkeypatch.setattr(
        graphs_optimized.CustomEvalConfig.objects,
        "select_related",
        lambda *_args: SimpleNamespace(get=lambda **_kwargs: config),
    )
    return seen


def _direct(**hide):
    return graphs_optimized._read_direct_system_metrics(
        project_id=PROJECT, filters=[window()], interval="day", **hide
    )


def _all_metrics(**hide):
    return graphs_optimized.get_all_system_metrics(
        interval="day",
        filters=[window()],
        property="average",
        system_metric_filters={"project_id": PROJECT},
        **hide,
    )


def _one_metric(**hide):
    return graphs_optimized.get_system_metric_data(
        interval="day",
        filters=[window()],
        property="average",
        req_data_config={"id": "latency"},
        system_metric_filters={"project_id": PROJECT},
        observe_type="charts",
        **hide,
    )


def _eval_chart(**hide):
    return graphs_optimized.get_eval_graph_data(
        interval="day",
        filters=[window()],
        property="average",
        observe_type="charts",
        req_data_config={"id": CONFIG},
        eval_logger_filters={"project_id": PROJECT},
        **hide,
    )


@pytest.mark.parametrize(
    ("read", "reader"),
    [
        (_direct, "all"),
        (_all_metrics, "all"),
        (_one_metric, "all"),
        (_eval_chart, "eval"),
    ],
)
def test_project_chart_helpers_pass_the_argument_only_when_hiding(
    read, reader, chart_reads
):
    read()
    read(hide_simulated=False)
    read(hide_simulated=True)

    omitted, off, on = chart_reads[reader]
    assert "hide_simulated" not in omitted
    assert off == omitted
    assert on == {**omitted, "hide_simulated": True}


# ---------------------------------------------------------------------------
# The time-series builder
# ---------------------------------------------------------------------------


def _builder(**kwargs):
    return TimeSeriesQueryBuilder(
        project_id=PROJECT,
        filters=kwargs.pop("filters", []),
        interval="day",
        start_date=END - timedelta(days=1),
        end_date=END,
        **kwargs,
    )


def test_unfiltered_builder_scans_raw_rows_with_the_rule_when_hiding():
    plain, _ = _builder().build()
    hiding, _ = _builder(hide_simulated=True).build()

    # The hourly states carry no attribute, so the rule cannot ride on them.
    assert "countMerge" in plain and UNMARKED not in plain
    assert "countMerge" not in hiding
    assert "count() AS traffic_count" in hiding
    assert UNMARKED in hiding


def test_filtered_builder_keeps_its_filters_beside_the_rule():
    filters = [leaf("company_id", "company")]
    plain, plain_params = _builder(filters=filters).build()
    hiding, hiding_params = _builder(filters=filters, hide_simulated=True).build()

    assert UNMARKED not in plain
    assert UNMARKED in hiding
    assert {k: v for k, v in hiding_params.items() if k in plain_params} == plain_params


@pytest.mark.parametrize(
    ("observe_type", "kept"),
    [
        ("span", "(graph_row_match_0 = 1)"),
        ("trace", "graph_match_0 = 0"),
    ],
)
def test_exact_builder_adds_the_rule_to_its_plan(observe_type, kept):
    plain, _ = _builder(exact_snapshot=True, observe_type=observe_type).build()
    hiding, _ = _builder(
        exact_snapshot=True, observe_type=observe_type, hide_simulated=True
    ).build()

    assert UNMARKED not in plain and "graph_row_match_0" not in plain
    assert UNMARKED in hiding
    assert kept in squash(hiding)


def test_exact_span_partition_adds_the_rule_to_a_plan_it_is_handed():
    builder = _builder(exact_snapshot=True, observe_type="span", hide_simulated=True)
    plan = compile_exact_graph_row_predicates(
        [window()], project_id=PROJECT, observe_type="span"
    )
    start = END - timedelta(days=1)

    hiding, _ = builder.build_exact_span_partition(
        partition_start=start,
        partition_end=start + timedelta(hours=1),
        exact_filter_plan=plan,
    )
    unhidden = _builder(exact_snapshot=True, observe_type="span")
    plain, _ = unhidden.build_exact_span_partition(
        partition_start=start, partition_end=start + timedelta(hours=1)
    )

    assert UNMARKED in hiding
    assert UNMARKED not in plain
    assert plan.predicates == ()


def test_plan_hiding_simulated_adds_one_requirement_and_keeps_the_original():
    span_plan = compile_exact_graph_row_predicates(
        [window()], project_id=PROJECT, observe_type="span"
    )
    span = plan_hiding_simulated(span_plan, observe_type="span")
    assert span_plan.predicates == ()
    assert span.predicates == (UNMARKED,)
    assert span.required_matches == (True,)
    assert span.match_condition_groups == (((0, True),),)

    trace_plan = compile_exact_graph_row_predicates(
        [window(), leaf("company_id", "company")],
        project_id=PROJECT,
        observe_type="trace",
    )
    before = len(trace_plan.predicates)
    trace = plan_hiding_simulated(trace_plan, observe_type="trace")
    assert len(trace_plan.predicates) == before
    marked_span = trace.predicates[-1]
    assert marked_span == f"NOT ({UNMARKED})"
    assert "parent_span_id" not in marked_span
    assert trace.required_matches[-1] is False
    assert trace.output_window_only[-1] is False
    assert trace.match_condition_groups[-1] == ((before, False),)


def test_trace_rule_leaves_out_a_trace_marked_on_children_whose_root_never_arrived():
    # A code agent's children carry the resource mark even when its root span
    # never reached the project. Whether any live span of the trace carries a
    # mark is the whole test: a root requirement would count those children.
    plain, _ = _builder(exact_snapshot=True, observe_type="trace").build()
    hiding, _ = _builder(
        exact_snapshot=True, observe_type="trace", hide_simulated=True
    ).build()
    flat = squash(hiding)

    assert resource_mark_sql(schema="v2") in flat
    assert f"NOT ({UNMARKED})" in flat
    assert "graph_match_0 = 0" in flat
    assert flat.count("parent_span_id") == squash(plain).count("parent_span_id")


# ---------------------------------------------------------------------------
# The Agent Graph and eval builders, which no entry point reaches by name
# ---------------------------------------------------------------------------


def _agent(**kwargs):
    return AgentGraphQueryBuilderV2(
        project_id=PROJECT, filters=kwargs.pop("filters", [window(days=1)]), **kwargs
    )


def _prewhere(sql):
    """What the Agent Graph statement reads from ``spans`` before it collapses."""

    return sql.split("PREWHERE", 1)[1].split("GROUP BY", 1)[0]


def test_agent_graph_builder_hides_marked_traces():
    omitted, off, on = (
        _agent().build(),
        _agent(hide_simulated=False).build(),
        _agent(hide_simulated=True).build(),
    )

    assert off == omitted
    assert UNMARKED not in omitted[0]
    assert f"NOT ({UNMARKED})" in squash(on[0])
    assert "graph_match_0 = 0" in squash(on[0])
    assert on[1] == omitted[1]


def test_agent_graph_hiding_alone_keeps_the_partition_prunable_window():
    plain = _prewhere(_agent().build()[0])
    hiding = _prewhere(_agent(hide_simulated=True).build()[0])
    filtered = _prewhere(
        _agent(
            filters=[window(days=1), leaf("company_id", "company")],
            hide_simulated=True,
        ).build()[0]
    )

    assert "start_time >= %(start_date)s" in plain
    assert hiding == plain
    # A request filter widens the read, as it does without the rule.
    assert "start_time >=" not in filtered


def _eval(**kwargs):
    return EvalMetricsQueryBuilderV2(
        custom_eval_config_id=CONFIG,
        project_id=PROJECT,
        start_date=END - timedelta(days=1),
        end_date=END,
        interval="day",
        eval_output_type="SCORE",
        **kwargs,
    )


# A membership that keeps only the eval rows whose trace has a span in the window.
POSITIVE_MEMBERSHIP = re.compile(r"(?<!NOT )IN \(SELECT (?:DISTINCT )?trace_id")


@pytest.mark.parametrize("observe_type", ["trace", "span"])
@pytest.mark.parametrize(
    "filters", [[], [window(days=1)]], ids=["no-filters", "date-only"]
)
def test_eval_builder_hides_marked_rows_without_a_positive_membership(
    observe_type, filters
):
    omitted = _eval(filters=filters, observe_type=observe_type).build()
    off = _eval(
        filters=filters, observe_type=observe_type, hide_simulated=False
    ).build()
    sql, params = _eval(
        filters=filters, observe_type=observe_type, hide_simulated=True
    ).build()
    flat = squash(sql)

    assert off == omitted
    assert not has_hidden_scan(omitted[0])
    assert has_hidden_scan(sql)
    # An eval made today over last month's traces has no span in the window and
    # must keep counting, so the rule can only take rows out.
    assert not POSITIVE_MEMBERSHIP.search(flat)
    assert not POSITIVE_MEMBERSHIP.search(squash(omitted[0]))
    # A row with no trace cannot be marked and stays.
    assert "ifNull(toString(raw_eval_logger.trace_id), '')" in flat
    # The marked rows are read once, for either grain, in a window a day wider
    # either side of the eval window.
    assert flat.count("FROM spans") == 1
    assert params["hidden_scan_start_date"] == END - timedelta(days=2)
    assert params["hidden_scan_end_date"] == END + timedelta(days=1)


def test_eval_builder_with_a_membership_filter_carries_the_rule_inside_it():
    filters = [window(days=1), leaf("company_id", "company")]
    plain, _ = _eval(filters=filters).build()
    hiding, _ = _eval(filters=filters, hide_simulated=True).build()
    clause, _ = compile_exact_graph_filter_predicates(
        filters, project_id=PROJECT, observe_type="trace", hide_simulated=True
    )

    assert POSITIVE_MEMBERSHIP.search(squash(plain))
    assert squash(clause) in squash(hiding)
    assert POSITIVE_MEMBERSHIP.search(squash(hiding))
    assert "NOT IN (SELECT DISTINCT trace_id FROM spans FINAL" not in squash(hiding)


# ---------------------------------------------------------------------------
# The eval and annotation charts are costed before the rule's membership scan
# ---------------------------------------------------------------------------

COSTED_CHARTS = {
    "eval": (
        dispatch.fetch_eval_graph_ch,
        {"id": CONFIG, "eval_output_type": "SCORE"},
        "observe-eval-graph",
        "eval_config",
    ),
    "annotation": (
        dispatch.fetch_annotation_graph_ch,
        {"id": LABEL, "output_type": "float"},
        "observe-annotation-graph",
        "annotation_rows",
    ),
}


def _interactive_rows(times=1):
    """A scan estimate that is `times` the rows one rule scan can cover in the wall."""

    return int(
        times
        * dispatch.GRAPH_INTERACTIVE_QUERY_TIMEOUT_MS
        * dispatch._hiding_scan_rows_per_ms()
    )


def _chart(name, analytics, *, filters=None, observe_type="trace", **hide):
    fetch, config, _namespace, _fixture = COSTED_CHARTS[name]
    return fetch(
        analytics=analytics,
        project_id=PROJECT,
        filters=[window()] if filters is None else filters,
        interval="day",
        req_data_config=config,
        observe_type=observe_type,
        organization_id=ORG,
        **hide,
    )


def _gate(analytics, **kwargs):
    """The cost gate of the eval and annotation charts, asked about a hiding read."""

    wall = dispatch.GRAPH_INTERACTIVE_QUERY_TIMEOUT_MS
    return dispatch._affordable_raw_log_membership_read(
        analytics=dispatch._DeadlineBoundGraphAnalytics(
            analytics, dispatch.ReadDeadline.start(wall)
        ),
        project_id=PROJECT,
        filters=[window()],
        interactive_deadline_ms=wall,
        hide_simulated=True,
        **kwargs,
    )


@pytest.mark.parametrize("name", list(COSTED_CHARTS))
def test_hiding_chart_beyond_the_interactive_wall_goes_to_the_worker(name, settings):
    # The rule adds a membership scan over the whole window that no filter
    # announces; a window the wall cannot absorb is scheduled, not scanned.
    settings.EXACT_AGGREGATION_TASK_QUEUE = "exact_aggregation"
    analytics = RecordingAnalytics(estimate_rows=_interactive_rows(2))

    with mock.patch(
        "tracer.tasks.exact_aggregation.refresh_exact_aggregation_snapshot.apply_async"
    ) as enqueue:
        payload = _chart(name, analytics, hide_simulated=True)

    assert analytics.probes
    assert analytics.statements == []
    assert payload["query_status"] == "pending"
    assert enqueue.call_count == 1
    task = enqueue.call_args.kwargs["kwargs"]
    assert task["namespace"] == COSTED_CHARTS[name][2]
    assert task["identity"]["hide_simulated"] is True


@pytest.mark.parametrize("name", list(COSTED_CHARTS))
def test_hiding_chart_no_wall_can_absorb_is_refused_not_scanned(name, settings):
    settings.EXACT_AGGREGATION_TASK_QUEUE = "exact_aggregation"
    beyond_background = _interactive_rows(
        2
        * dispatch.GRAPH_WALL_DEADLINE_MS
        / dispatch.GRAPH_INTERACTIVE_QUERY_TIMEOUT_MS
    )
    analytics = RecordingAnalytics(estimate_rows=beyond_background)

    with mock.patch(
        "tracer.tasks.exact_aggregation.refresh_exact_aggregation_snapshot.apply_async"
    ) as enqueue:
        payload = _chart(name, analytics, hide_simulated=True)

    assert analytics.statements == []
    assert enqueue.call_count == 0
    assert payload["query_status"] == "degraded"
    assert payload["query_provenance"] == "read_cost_gate"


@pytest.mark.parametrize("name", list(COSTED_CHARTS))
def test_chart_that_does_not_hide_is_not_costed_and_reads_inline(name, request):
    request.getfixturevalue(COSTED_CHARTS[name][3])
    analytics = RecordingAnalytics(estimate_rows=_interactive_rows(2))

    _chart(name, analytics)
    _chart(name, analytics, hide_simulated=False)

    assert analytics.probes == []
    assert analytics.statements


def test_provisional_hiding_rate_is_one_workers_share_of_the_measured_rate(settings):
    settings.DASHBOARD_TRACE_READ_MAX_THREADS = 4
    settings.FILTER_SELECTOR_MAX_THREADS = 1
    assert (
        dispatch._hiding_scan_rows_per_ms()
        == graph_read_cost._RAW_SCAN_ROWS_PER_MS // 4
    )
    settings.FILTER_SELECTOR_MAX_THREADS = 2
    assert (
        dispatch._hiding_scan_rows_per_ms()
        == graph_read_cost._RAW_SCAN_ROWS_PER_MS // 2
    )


@pytest.mark.parametrize("name", list(COSTED_CHARTS))
def test_hiding_chart_is_not_priced_at_the_four_worker_rate(name, settings):
    # Half of what the four-worker rate admits in the wall is more than the one
    # worker the rule's FINAL scan runs on can cover, so it goes to the worker.
    settings.EXACT_AGGREGATION_TASK_QUEUE = "exact_aggregation"
    four_worker_rows = (
        dispatch.GRAPH_INTERACTIVE_QUERY_TIMEOUT_MS
        * graph_read_cost._RAW_SCAN_ROWS_PER_MS
    )
    analytics = RecordingAnalytics(estimate_rows=four_worker_rows // 2)

    with mock.patch(
        "tracer.tasks.exact_aggregation.refresh_exact_aggregation_snapshot.apply_async"
    ) as enqueue:
        payload = _chart(name, analytics, hide_simulated=True)

    assert analytics.statements == []
    assert enqueue.call_count == 1
    assert payload["query_status"] == "pending"


@pytest.mark.parametrize("scans", [1, 2, 3])
def test_every_scan_of_the_rule_is_priced(scans):
    one_scan = _interactive_rows()
    inside = int(one_scan / scans * 0.8)
    outside = int(one_scan / scans * 1.25)

    assert _gate(RecordingAnalytics(estimate_rows=inside), hide_scans=scans) is None
    verdict = _gate(RecordingAnalytics(estimate_rows=outside), hide_scans=scans)
    assert verdict is not None
    assert verdict.estimated_rows == outside


def test_hiding_read_that_could_not_be_costed_is_not_admitted_inline():
    class Unanswerable(RecordingAnalytics):
        def execute_ch_query(self, query, params=None, **kwargs):
            raise RuntimeError("the cost probe could not answer")

    verdict = _gate(Unanswerable(), hide_scans=1)

    assert verdict is not None
    assert verdict.estimated_rows is None


class _Stop(Exception):
    pass


@pytest.mark.parametrize(
    ("name", "observe_type", "filters", "scans"),
    [
        ("eval", "trace", [window()], 1),
        ("eval", "trace", [window(), leaf("company_id", "company")], 2),
        ("eval", "span", [window(), leaf("company_id", "company")], 1),
        ("annotation", "trace", [window()], 2),
        ("annotation", "span", [window()], 1),
    ],
    ids=[
        "eval-trace-window-only",
        "eval-trace-filtered",
        "eval-span-filtered",
        "annotation-trace",
        "annotation-span",
    ],
)
def test_each_chart_prices_the_scans_its_statement_makes(
    name, observe_type, filters, scans, monkeypatch
):
    seen = {}

    def stop(**kwargs):
        seen.update(kwargs)
        raise _Stop

    monkeypatch.setattr(dispatch, "_affordable_raw_log_membership_read", stop)
    with pytest.raises(_Stop):
        _chart(
            name,
            RecordingAnalytics(),
            filters=filters,
            observe_type=observe_type,
            hide_simulated=True,
        )

    assert seen["hide_scans"] == scans


@pytest.mark.parametrize(("name", "refused"), [("eval", False), ("annotation", True)])
def test_background_wall_prices_the_same_scans(name, refused, settings):
    # Three quarters of what one worker covers in the background wall fits one
    # scan, which the eval chart makes, and not the two the annotation chart
    # makes for each batch.
    settings.EXACT_AGGREGATION_TASK_QUEUE = "exact_aggregation"
    background_rows = (
        dispatch.GRAPH_WALL_DEADLINE_MS * dispatch._hiding_scan_rows_per_ms()
    )
    analytics = RecordingAnalytics(estimate_rows=int(background_rows * 0.75))

    with mock.patch(
        "tracer.tasks.exact_aggregation.refresh_exact_aggregation_snapshot.apply_async"
    ) as enqueue:
        payload = _chart(name, analytics, hide_simulated=True)

    assert analytics.statements == []
    if refused:
        assert enqueue.call_count == 0
        assert payload["query_status"] == "degraded"
        assert payload["query_provenance"] == "read_cost_gate"
    else:
        assert enqueue.call_count == 1
        assert payload["query_status"] == "pending"
