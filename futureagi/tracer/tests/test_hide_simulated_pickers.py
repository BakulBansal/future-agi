"""The pickers that offer values or keys read from spans leave marked traces out.

Every reader of span values or attribute keys for a filter dropdown takes a
``hide_simulated`` decision from its caller and, when it is set, leaves out the
spans of marked traces. The cases pin what makes that safe: a hiding statement
carries the exclusion text for the v2 string map, a statement that does not
hide is exactly the one it always was, a latest-state replay reads a marked
winner as deleted instead of handing the win to an older version, and a cursor,
snapshot or cache entry made under one answer is never served under the other.

Nothing here opens a Postgres or ClickHouse connection. The holding answers come
from recording doubles patched where each caller imports them, and every
statement is captured by a recording executor or analytics double.
"""

import ast
import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from inspect import unwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
from django.db import DatabaseError
from django.db.models import Exists, Q
from rest_framework.test import APIRequestFactory, force_authenticate

import model_hub.services.bulk_selection as bulk_selection_module
import tracer.selectors.eval_tasks.row_resolver as row_resolver_module
import tracer.services.clickhouse.exact_attribute_detail as detail_module
import tracer.services.clickhouse.exact_graph_reads as exact_reads_module
import tracer.services.clickhouse.graph_dispatch as graph_dispatch_module
import tracer.services.clickhouse.query_service as query_service_module
import tracer.services.clickhouse.span_attribute_lookups as lookups_module
import tracer.services.dashboard_metrics_catalog as catalog_module
import tracer.services.simulation_visibility as visibility_module
import tracer.utils.helper as helper_module
import tracer.views.observation_span as observation_span_module
import tracer.views.span_attributes as span_attributes_module
import tracer.views.trace as trace_view
from model_hub.views import ai_filter as ai_filter_view
from tracer.services.clickhouse.attribute_cursor_state import AttributeCursorSeenState
from tracer.services.clickhouse.attribute_reads import (
    _LATEST_BROWSE_SQL,
    _LATEST_CARDINALITY_SQL,
    _LATEST_JSON_TARGET_SQL,
    _LATEST_TARGET_SQL,
    _LATEST_TYPED_BROWSE_SQL,
    _LATEST_TYPED_TARGET_SQL,
    _LATEST_TYPED_VERSION_SQL,
    AttributeKeyCursorPageRead,
    AttributeKeyRead,
    AttributeKeyRow,
    AttributeQueryPage,
    AttributeReadMetadata,
    AttributeReadSelector,
    AttributeValueCursorPageRead,
    AttributeValueRead,
    IncompleteLatestStateReplay,
    attribute_key_cursor_digest,
    attribute_key_type_cursor_digest,
)
from tracer.services.clickhouse.exact_attribute_detail import (
    EXACT_ATTRIBUTE_DETAIL_SQL,
    read_exact_attribute_detail,
)
from tracer.services.clickhouse.filter_value_reads import (
    FilterValueCursorPageRead,
    FilterValueRead,
    _latest_span_value_cte,
    _latest_voice_value_cte,
    read_span_system_filter_value_cursor_page,
    read_span_system_filter_values,
)
from tracer.services.clickhouse.list_cursor import (
    ListCursorError,
    cursor_scope_for_request,
    decode_list_cursor,
)
from tracer.services.clickhouse.query_service import AnalyticsQueryService
from tracer.services.clickhouse.v2.property_catalog.value_reader import (
    PropertyCatalogValueNotReady,
    PropertyCatalogValueUnavailable,
)
from tracer.services.simulation_visibility import (
    RESOURCE_MARK_KEY,
    SIMULATION_MARK_KEY,
    MarkedLookupFailed,
    postgres_span_exclusion_sql,
    row_exclusion_sql,
)
from tracer.tasks import exact_aggregation
from tracer.utils.sql_queries import SQL_query_handler
from tracer.views import dashboard as dashboard_view

pytestmark = pytest.mark.unit

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
HOLDING = "11111111-1111-4111-8111-111111111111"
PLAIN = "22222222-2222-4222-8222-222222222222"
WORKSPACE = "00000000-0000-4000-8000-000000000002"

SPAN_TIME = NOW - timedelta(hours=1)

# The picker tables are the v2 spans table, whose string map is ``attrs_string``.
UNMARKED = row_exclusion_sql(map_column="attrs_string")
# A replay reads the deletion state of the winning version; a marked winner
# reads as deleted.
REPLAY_STATE = f"if({UNMARKED}, is_deleted, 1)"
# A candidate sample and the raw value proof keep a row only when it is
# unmarked; the system-value statements compute the same test one level down.
# Both are asserted as whole fragments: counting the exclusion text alone would
# also pass a condition written the wrong way round.
KEPT_WHEN_UNMARKED = f")\n      AND {UNMARKED}"
ROW_TEST = f"toUInt8({UNMARKED}) AS row_unmarked"


def _carries_no_mark(sql):
    return (
        SIMULATION_MARK_KEY not in sql
        and RESOURCE_MARK_KEY not in sql
        and "row_unmarked" not in sql
        and "latest_unmarked" not in sql
    )


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


def _metadata(*, complete=True):
    return AttributeReadMetadata(
        query_complete=complete,
        query_status="complete" if complete else "degraded",
        query_error_code=None if complete else "deadline",
        query_window_start=NOW - timedelta(days=365),
        query_window_end=NOW,
        query_count=1,
    )


# ─── system-value readers ────────────────────────────────────────────────────


class _Analytics:
    """Records each statement a filter-value reader sends; answers no rows."""

    def __init__(self):
        self.calls = []

    def execute_ch_query(self, query, params, *, timeout_ms, settings):
        self.calls.append((query, dict(params)))
        return SimpleNamespace(data=[])


# A plain column, a root-only name, a remapped session, and the voice
# expression that renames ``attrs_string`` one level up.
_SYSTEM_METRICS = ["status", "name", "session", "ended_reason"]


def _system_values(metric, **kwargs):
    analytics = _Analytics()
    read_span_system_filter_values(
        analytics, project_ids=[HOLDING], metric_name=metric, now=NOW, **kwargs
    )
    assert len(analytics.calls) == 1
    return analytics.calls[0]


@pytest.mark.parametrize("metric", _SYSTEM_METRICS)
def test_system_values_leave_marked_spans_out_when_hiding(metric):
    query, params = _system_values(metric, hide_simulated=True)
    plain_query, plain_params = _system_values(metric)

    assert query.count(UNMARKED) == 1
    assert ROW_TEST in query
    # The mark is read per version and the winning version decides, like
    # deletion: an older unmarked version never takes a marked span's place.
    assert "argMax(row_unmarked, _version) AS latest_unmarked" in query
    assert "AND latest_unmarked = 1" in query
    # The test runs below the aggregation: the voice statement renames
    # ``attrs_string`` to its latest value, which a test written above it would
    # read as an aggregate.
    assert query.index(UNMARKED) < query.index("GROUP BY project_id, trace_id")
    assert params == plain_params
    assert _carries_no_mark(plain_query)


@pytest.mark.parametrize("metric", _SYSTEM_METRICS)
def test_system_values_are_untouched_when_not_hiding(metric):
    default = _system_values(metric)
    explicit = _system_values(metric, hide_simulated=False)

    assert default == explicit
    assert _carries_no_mark(default[0])


def test_system_value_cte_text_is_today_s_when_not_hiding():
    # The text before this change, so a drift in the plain path shows here.
    assert (
        _latest_span_value_cte("status")
        == """
        latest_spans AS (
            SELECT
                project_id,
                trace_id,
                id,
                start_time,
                argMax(is_deleted, _version) AS latest_is_deleted,
                argMax(tuple(parent_span_id), _version).1 AS latest_parent_span_id,
                argMax(tuple(status), _version).1 AS raw_value
            FROM spans
            PREWHERE project_id IN %(project_ids)s
              AND start_time >= %(window_start)s
              AND start_time < %(window_end)s
            GROUP BY project_id, trace_id, id, start_time
        )
    """
    )
    assert (
        _latest_voice_value_cte()
        == """
        latest_spans AS (
            SELECT
                project_id,
                trace_id,
                id,
                start_time,
                argMax(is_deleted, _version) AS latest_is_deleted,
                argMax(tuple(parent_span_id), _version).1 AS latest_parent_span_id,
                argMax(observation_type, _version) AS latest_observation_type,
                argMax(tuple(provider), _version).1 AS provider,
                argMax(tuple(cost), _version).1 AS cost,
                argMax(attrs_string, _version) AS attrs_string,
                argMax(attrs_number, _version) AS attrs_number,
                argMax(tuple(attributes_extra), _version).1 AS attributes_extra
            FROM spans
            PREWHERE project_id IN %(project_ids)s
              AND start_time >= %(window_start)s
              AND start_time < %(window_end)s
            GROUP BY project_id, trace_id, id, start_time
        )
    """
    )


def test_a_metric_that_reads_a_column_the_statement_already_has_is_not_repeated():
    cte = _latest_span_value_cte("project_id", hide_simulated=True)

    assert (
        "SELECT project_id, trace_id, id, start_time, _version, is_deleted, "
        "parent_span_id,\n"
    ) in cte


def _cursor_walk(metric, **kwargs):
    analytics = _Analytics()
    read_span_system_filter_value_cursor_page(
        analytics,
        project_ids=[HOLDING],
        metric_name=metric,
        page_size=5,
        window_start=NOW - timedelta(hours=2),
        window_end=NOW,
        **kwargs,
    )
    assert analytics.calls
    return analytics.calls


@pytest.mark.parametrize("metric", _SYSTEM_METRICS)
def test_system_value_cursor_walk_leaves_marked_spans_out_when_hiding(metric):
    calls = _cursor_walk(metric, hide_simulated=True)
    plain = _cursor_walk(metric)

    # Every slice of the walk carries the rule, whatever the metric.
    assert all(query.count(UNMARKED) == 1 for query, _ in calls)
    assert all(ROW_TEST in query for query, _ in calls)
    assert all("AND latest_unmarked = 1" in query for query, _ in calls)
    assert [params for _, params in calls] == [params for _, params in plain]


@pytest.mark.parametrize("metric", _SYSTEM_METRICS)
def test_system_value_cursor_walk_is_untouched_when_not_hiding(metric):
    default = _cursor_walk(metric)
    explicit = _cursor_walk(metric, hide_simulated=False)

    assert default == explicit
    assert all(_carries_no_mark(query) for query, _ in default)


# ─── attribute selector ──────────────────────────────────────────────────────


class _Executor:
    """Records each statement the selector sends; answers what the case says."""

    def __init__(self, responder=None):
        self.calls = []
        self.responder = responder or (lambda query, params: [])

    def execute(self, query, params, *, timeout_ms, settings):
        self.calls.append((query, dict(params)))
        return AttributeQueryPage(
            data=list(self.responder(query, params)), query_time_ms=1.0
        )


def _selector(executor, **kwargs):
    return AttributeReadSelector(executor, now=NOW, typed_only=True, **kwargs)


def _replay_row(*_args):
    return {
        "project_id": HOLDING,
        "trace_id": "trace",
        "id": "span",
        "start_time": SPAN_TIME,
    }


_PUBLIC_READS = {
    "discover_keys": lambda selector: selector.discover_keys([HOLDING]),
    "discover_exact_key": lambda selector: selector.discover_keys(
        [HOLDING], exact_key="customer.plan"
    ),
    "read_values": lambda selector: selector.read_values([HOLDING], "customer.plan"),
    "read_key_cursor_page": lambda selector: selector.read_key_cursor_page(
        [HOLDING],
        page_size=5,
        window_start=NOW - timedelta(days=1),
        window_end=NOW,
    ),
    "read_value_cursor_page": lambda selector: selector.read_value_cursor_page(
        [HOLDING],
        "customer.plan",
        page_size=5,
        window_start=NOW - timedelta(days=1),
        window_end=NOW,
    ),
    "sample_cardinality": lambda selector: selector.sample_cardinality([HOLDING]),
}


@pytest.mark.parametrize("read", _PUBLIC_READS)
def test_every_statement_of_a_public_read_carries_the_rule_when_hiding(read):
    executor = _Executor()
    _PUBLIC_READS[read](_selector(executor, hide_simulated=True))

    assert executor.calls
    assert all(UNMARKED in query for query, _ in executor.calls)


@pytest.mark.parametrize("read", _PUBLIC_READS)
def test_a_public_read_sends_today_s_statements_when_not_hiding(read):
    default = _Executor()
    _PUBLIC_READS[read](_selector(default))
    explicit = _Executor()
    _PUBLIC_READS[read](_selector(explicit, hide_simulated=False))

    assert default.calls
    assert default.calls == explicit.calls
    assert all(_carries_no_mark(query) for query, _ in default.calls)


def test_a_candidate_sample_and_its_replay_both_carry_the_rule():
    # The sample finds an unmarked version; the replay then judges the span by
    # its winning version, so both statements of one read must carry the rule.
    def respond(query, params):
        if "candidate_limit" in params:
            return [_replay_row()]
        return [
            {
                **_replay_row(),
                "is_deleted": 0,
                "string_keys": [],
                "number_keys": [],
                "boolean_keys": [],
            }
        ]

    executor = _Executor(respond)
    _selector(executor, hide_simulated=True).discover_keys([HOLDING])

    sample = [query for query, params in executor.calls if "candidate_limit" in params]
    replay = [
        query for query, params in executor.calls if "candidate_limit" not in params
    ]
    assert sample and replay
    assert all(query.count(UNMARKED) == 1 for query in sample)
    assert all(KEPT_WHEN_UNMARKED in query for query in sample)
    assert all(query.count(REPLAY_STATE) == 1 for query in replay)


_CANDIDATE_SHAPES = {
    "plain": {},
    "ordered": {"ordered": True},
    "stratified": {"stratified": True},
    "ordered stratified": {"stratified": True, "ordered": True},
    "versioned": {"include_versions": True},
    "ordered versioned": {"include_versions": True, "ordered": True},
    "after a checkpoint": {"before_identity": (HOLDING, "trace", "span", SPAN_TIME)},
}


@pytest.mark.parametrize("shape", _CANDIDATE_SHAPES)
def test_every_candidate_statement_shape_carries_the_rule_once(shape):
    def candidates(*, hide):
        executor = _Executor()
        _selector(executor, hide_simulated=hide)._candidate_ids(
            (HOLDING,),
            (NOW - timedelta(days=1), NOW),
            predicate="length(attrs_string.keys) > 0",
            attribute_key=None,
            candidate_limit=5,
            **_CANDIDATE_SHAPES[shape],
        )
        (call,) = executor.calls
        return call

    query, params = candidates(hide=True)
    plain_query, plain_params = candidates(hide=False)

    assert query.count(UNMARKED) == 1
    assert KEPT_WHEN_UNMARKED in query
    assert params == plain_params
    assert _carries_no_mark(plain_query)


_REPLAY_TEMPLATES = {
    "target": _LATEST_TARGET_SQL,
    "typed target": _LATEST_TYPED_TARGET_SQL,
    "json target": _LATEST_JSON_TARGET_SQL,
    "typed version": _LATEST_TYPED_VERSION_SQL,
    "browse": _LATEST_BROWSE_SQL,
    "typed browse": _LATEST_TYPED_BROWSE_SQL,
    "cardinality": _LATEST_CARDINALITY_SQL,
}


@pytest.mark.parametrize("template", _REPLAY_TEMPLATES)
def test_every_replay_reads_a_marked_winner_as_deleted(template):
    identity = (HOLDING, "trace", "span", SPAN_TIME)

    def replay(*, hide):
        executor = _Executor(lambda query, params: [_replay_row()])
        _selector(executor, hide_simulated=hide)._verify_latest(
            sql=_REPLAY_TEMPLATES[template],
            project_ids=(HOLDING,),
            candidate_ids=(identity,),
            attribute_key="customer.plan",
        )
        (call,) = executor.calls
        return call

    query, params = replay(hide=True)
    plain_query, plain_params = replay(hide=False)

    # One raw deletion state per template, now guarded by the mark test. A row
    # filter instead would hand an older, unmarked version the win and would
    # return fewer identities than the replay was asked about.
    assert query.count(REPLAY_STATE) == 1
    assert params == plain_params
    assert _carries_no_mark(plain_query)


def test_a_replay_that_has_no_deletion_state_to_guard_is_refused():
    executor = _Executor(lambda query, params: [_replay_row()])

    with pytest.raises(IncompleteLatestStateReplay):
        _selector(executor, hide_simulated=True)._verify_latest(
            sql="SELECT 1 FROM spans WHERE {candidate_predicate}",
            project_ids=(HOLDING,),
            candidate_ids=((HOLDING, "trace", "span", SPAN_TIME),),
        )

    assert executor.calls == []


@pytest.mark.parametrize(
    "before_identity",
    [None, (HOLDING, "trace", "span", SPAN_TIME)],
    ids=["first page", "after a checkpoint"],
)
@pytest.mark.parametrize(
    "lanes",
    [{"string": "1"}, {"string": "1", "number": "1"}],
    ids=["one lane", "two lanes"],
)
def test_the_value_proof_carries_the_rule_with_and_without_a_checkpoint(
    before_identity, lanes
):
    def proof(*, hide):
        executor = _Executor()
        _selector(executor, hide_simulated=hide)._seen_value_slice_groups(
            project_ids=(HOLDING,),
            attribute_key="customer.plan",
            attribute_type="string",
            search="",
            candidate_predicates=lanes,
            candidate_predicate_params={},
            segment=(NOW - timedelta(days=1), NOW),
            before_identity=before_identity,
            distinct_limit=8,
        )
        (call,) = executor.calls
        return call

    query, params = proof(hide=True)
    plain_query, plain_params = proof(hide=False)

    assert query.count(UNMARKED) == 1
    assert KEPT_WHEN_UNMARKED in query
    assert params == plain_params
    assert _carries_no_mark(plain_query)


def test_the_selector_refuses_a_rule_that_is_not_a_bool():
    with pytest.raises(TypeError):
        AttributeReadSelector(_Executor(), hide_simulated="yes")


# ─── exact attribute detail ──────────────────────────────────────────────────


def _detail(**kwargs):
    executor = _Executor()
    read_exact_attribute_detail(
        project_id=HOLDING,
        attribute_key="customer.plan",
        executor=executor,
        window_end=NOW,
        **kwargs,
    )
    (call,) = executor.calls
    return call


def test_exact_detail_reads_a_marked_winner_as_deleted_when_hiding():
    query, params = _detail(hide_simulated=True)
    _, plain_params = _detail()

    assert query.count(f"if({UNMARKED}, attribute_source.is_deleted, 1),") == 1
    assert query.count(UNMARKED) == 1
    assert params == plain_params


def test_exact_detail_sends_today_s_statement_when_not_hiding():
    default = _detail()
    explicit = _detail(hide_simulated=False)

    assert default == explicit
    assert default[0] == EXACT_ATTRIBUTE_DETAIL_SQL


def test_exact_detail_refuses_a_statement_with_no_deletion_state_to_guard(
    monkeypatch,
):
    monkeypatch.setattr(detail_module, "EXACT_ATTRIBUTE_DETAIL_SQL", "SELECT 1")

    with pytest.raises(RuntimeError):
        detail_module._detail_sql(hide_simulated=True)


def _detail_identity(**extra):
    return {
        "organization_id": "org",
        "workspace_id": WORKSPACE,
        "project_id": HOLDING,
        "attribute_key": "customer.plan",
        "horizon_days": 365,
        **extra,
    }


def test_the_detail_refresh_takes_the_rule_from_the_identity_it_refreshes(monkeypatch):
    seen = []
    workspace = SimpleNamespace(organization=SimpleNamespace(id="org"))
    monkeypatch.setattr(
        "accounts.models.Workspace",
        SimpleNamespace(
            objects=SimpleNamespace(
                select_related=lambda *_: SimpleNamespace(
                    get=lambda **_kwargs: workspace
                )
            )
        ),
    )
    monkeypatch.setattr(
        "tracer.utils.workspace_scope.project_queryset_for_request",
        lambda _request: SimpleNamespace(
            filter=lambda **_kwargs: SimpleNamespace(exists=lambda: True)
        ),
    )
    monkeypatch.setattr(
        detail_module,
        "read_exact_attribute_detail",
        lambda **kwargs: seen.append(kwargs) or {},
    )

    exact_aggregation._attribute_detail_payload(_detail_identity())
    exact_aggregation._attribute_detail_payload(_detail_identity(hide_simulated=True))

    # The snapshot is stored under the identity, so the identity decides.
    assert [call["hide_simulated"] for call in seen] == [False, True]


# ─── key inventory plumbing ──────────────────────────────────────────────────


class _SelectorSpy:
    """Remembers how the service built its selector."""

    built = []

    def __init__(self, **kwargs):
        type(self).built.append(kwargs)

    def discover_keys(self, project_ids, **kwargs):
        return AttributeKeyRead((), _metadata())


@pytest.mark.parametrize("hide", [True, False])
def test_the_key_inventory_hands_the_rule_to_its_selector(monkeypatch, hide):
    _SelectorSpy.built = []
    monkeypatch.setattr(query_service_module, "AttributeReadSelector", _SelectorSpy)
    service = AnalyticsQueryService()

    service.get_span_attribute_keys_ch_for_projects([HOLDING], hide_simulated=hide)
    service.get_span_attribute_keys_ch(HOLDING, hide_simulated=hide)

    assert [kwargs["hide_simulated"] for kwargs in _SelectorSpy.built] == [hide, hide]


def test_the_key_inventory_is_not_narrowed_unless_asked(monkeypatch):
    _SelectorSpy.built = []
    monkeypatch.setattr(query_service_module, "AttributeReadSelector", _SelectorSpy)
    service = AnalyticsQueryService()

    service.get_span_attribute_keys_ch_for_projects([HOLDING])
    service.get_span_attribute_keys_ch(HOLDING)

    assert [kwargs["hide_simulated"] for kwargs in _SelectorSpy.built] == [False, False]


@pytest.mark.parametrize("hide", [True, False])
def test_the_project_key_lookup_hands_the_rule_to_the_inventory(monkeypatch, hide):
    asked = []

    class _Inventory:
        def get_span_attribute_keys_ch_for_projects(self, project_ids, **kwargs):
            asked.append(kwargs)
            return []

    monkeypatch.setattr(lookups_module, "is_clickhouse_enabled", lambda: True)
    monkeypatch.setattr(lookups_module, "AnalyticsQueryService", _Inventory)

    lookups_module.list_attribute_keys_for_project(HOLDING, hide_simulated=hide)
    lookups_module.list_attribute_keys_for_project(HOLDING)

    assert [call["hide_simulated"] for call in asked] == [hide, False]


# ─── metrics catalog ─────────────────────────────────────────────────────────


class _Cache:
    def __init__(self):
        self.store = {}
        self.timeouts = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, timeout=None):
        self.store[key] = value
        self.timeouts[key] = timeout


@pytest.fixture(autouse=True)
def kept_answers(monkeypatch):
    """A cache of its own for the answers the label walk and the picker keep.

    The real cache lives as long as the process, so a walk's answer kept by one
    test would be served to the next one that asks about the same project.
    """
    cache = _Cache()
    monkeypatch.setattr(helper_module, "django_cache", cache)
    monkeypatch.setattr(trace_view, "django_cache", cache)
    return cache


def _direct_catalog_pg_read(monkeypatch):
    """Run each catalog Postgres read in place and remember which family asked."""

    families = []

    def run(_deadline, family, read):
        families.append(family)
        return read()

    monkeypatch.setattr(catalog_module, "_run_metrics_catalog_pg_read", run)
    return families


def _catalog(monkeypatch, holds, *, prime=None, direct_pg=True, **kwargs):
    cache = _Cache()
    cache.store.update(prime or {})
    built = []
    resolved = []
    families = _direct_catalog_pg_read(monkeypatch) if direct_pg else []
    monkeypatch.setattr(catalog_module, "cache", cache)
    monkeypatch.setattr(catalog_module, "_can_use_metrics_catalog_cache", lambda: True)
    monkeypatch.setattr(catalog_module, "holding_projects", holds)
    monkeypatch.setattr(
        catalog_module,
        "_resolve_metrics_catalog_project_scope",
        lambda *_args, **_kwargs: resolved.append(1) or ([HOLDING], True),
    )
    monkeypatch.setattr(
        catalog_module,
        "build_metrics_catalog",
        lambda *_args, **build_kwargs: built.append(build_kwargs) or [],
    )
    result = catalog_module.get_cached_metrics_catalog(
        SimpleNamespace(id=WORKSPACE),
        project_ids_param=HOLDING,
        deadline=SimpleNamespace(remaining_ms=lambda *_args, **_kwargs: 1_000),
        **kwargs,
    )
    return SimpleNamespace(
        cache=cache, built=built, resolved=resolved, families=families, result=result
    )


_PLAIN_CATALOG_KEY = f"dashboard:metrics_catalog:v6:{WORKSPACE}:{HOLDING}::0:1::"
_HIDING_CATALOG_KEY = f"{_PLAIN_CATALOG_KEY}:hide_simulated"


def test_a_catalog_that_hides_is_cached_apart_from_one_that_does_not(monkeypatch):
    hiding = _catalog(monkeypatch, _HoldsAmong(HOLDING))
    plain = _catalog(monkeypatch, _HoldsAmong())

    assert list(plain.cache.store) == [_PLAIN_CATALOG_KEY]
    assert list(hiding.cache.store) == [_HIDING_CATALOG_KEY]
    assert hiding.built[0]["_hides_simulated"] is True
    assert plain.built[0]["_hides_simulated"] is False


def test_a_catalog_without_attribute_keys_or_eval_usage_does_not_ask_or_change_its_key(
    monkeypatch,
):
    holds = _HoldsAmong(HOLDING)

    catalog = _catalog(
        monkeypatch, holds, include_custom_attributes=False, category="system_metric"
    )

    assert holds.asked == []
    assert catalog.resolved == []
    assert list(catalog.cache.store) == [
        f"dashboard:metrics_catalog:v6:{WORKSPACE}:{HOLDING}::0:0:system_metric:"
    ]
    assert catalog.built[0]["_hides_simulated"] is None
    assert catalog.built[0]["_resolved_project_scope"] is None


@pytest.mark.parametrize("held", [True, False])
def test_a_cache_hit_reads_neither_the_scope_nor_the_holding_flag(monkeypatch, held):
    # The plain entry was built while the project did not hold, so it carries no
    # key of a marked trace; serving it must cost no Postgres read at all.
    holds = _HoldsAmong(*([HOLDING] if held else []))
    cached = [{"name": "cached"}]

    catalog = _catalog(monkeypatch, holds, prime={_PLAIN_CATALOG_KEY: cached})

    assert catalog.result is cached
    assert catalog.resolved == []
    assert catalog.families == []
    assert holds.asked == []
    assert catalog.built == []


def test_a_hiding_request_that_misses_the_plain_entry_is_served_from_its_own(
    monkeypatch,
):
    cached = [{"name": "cached for a holding project"}]

    catalog = _catalog(
        monkeypatch, _HoldsAmong(HOLDING), prime={_HIDING_CATALOG_KEY: cached}
    )

    assert catalog.result is cached
    assert catalog.built == []
    assert list(catalog.cache.store) == [_HIDING_CATALOG_KEY]


@pytest.mark.parametrize("held", [True, False])
def test_the_holding_read_runs_inside_the_catalogs_read_wrapper(monkeypatch, held):
    holds = _HoldsAmong(*([HOLDING] if held else []))

    catalog = _catalog(monkeypatch, holds)

    assert catalog.families == ["project_scope"]
    assert holds.asked == [[HOLDING]]
    assert catalog.resolved == [1]
    # The scope is read once and handed on, so the builder does not read it again.
    assert catalog.built[0]["_resolved_project_scope"] == ([HOLDING], True)


def test_a_database_error_in_the_holding_read_is_the_catalogs_unavailable_error(
    monkeypatch,
):
    def fails(_project_ids):
        raise DatabaseError("connection reset")

    with pytest.raises(catalog_module.MetricsCatalogUnavailable) as caught:
        _catalog(monkeypatch, fails, direct_pg=False)

    assert caught.value.family == "project_scope"


class _KeyInventory:
    asked = []

    def get_span_attribute_keys_ch_for_projects(self, project_ids, **kwargs):
        type(self).asked.append(kwargs)
        return [{"key": "customer.plan", "type": "string"}]


def _build_catalog(monkeypatch, holds, **kwargs):
    _KeyInventory.asked = []
    _build_catalog.families = _direct_catalog_pg_read(monkeypatch)
    monkeypatch.setattr(catalog_module, "holding_projects", holds)
    monkeypatch.setattr(catalog_module, "V2AnalyticsQueryService", _KeyInventory)
    catalog_module.build_metrics_catalog(
        SimpleNamespace(id=WORKSPACE, organization_id="org"),
        project_ids_param=HOLDING,
        category="custom_attribute",
        source="traces",
        deadline=SimpleNamespace(remaining_ms=lambda *_args, **_kwargs: 1_000),
        _resolved_project_scope=([HOLDING], True),
        **kwargs,
    )
    return _KeyInventory.asked


def test_the_catalog_asks_once_and_hands_the_answer_to_the_key_inventory(monkeypatch):
    holds = _HoldsAmong(HOLDING)

    asked = _build_catalog(monkeypatch, holds)

    assert holds.asked == [[HOLDING]]
    assert _build_catalog.families == ["project_scope"]
    assert [call["hide_simulated"] for call in asked] == [True]


def test_the_catalog_does_not_hide_for_a_project_that_does_not_hold(monkeypatch):
    asked = _build_catalog(monkeypatch, _HoldsAmong())

    assert [call["hide_simulated"] for call in asked] == [False]


def test_a_catalog_given_an_answer_does_not_ask_again(monkeypatch):
    holds = _HoldsAmong(HOLDING)

    asked = _build_catalog(monkeypatch, holds, _hides_simulated=True)

    assert holds.asked == []
    assert [call["hide_simulated"] for call in asked] == [True]


# ─── dashboard filter values ─────────────────────────────────────────────────


class _ProjectScope:
    """Bounded QuerySet double for the project authorization reads."""

    def __init__(self, project_ids):
        self.project_ids = tuple(project_ids)
        self.requested = None

    def filter(self, **kwargs):
        requested = kwargs.get("id__in")
        if requested is not None:
            self.requested = {str(value) for value in requested}
        return self

    def order_by(self, *_args):
        return self

    def values_list(self, *_args, **_kwargs):
        return [
            project_id
            for project_id in self.project_ids
            if self.requested is None or project_id in self.requested
        ]


def _wire_dashboard(monkeypatch, *, holds, cursor_state=None):
    """Patch everything the filter-value view reaches outside the readers."""

    deadline = Mock()
    deadline.remaining_ms.return_value = 3_900
    decoded = []
    bound = []

    def decode(token, *, query, **_kwargs):
        decoded.append(query)
        return cursor_state, None

    def load_seen(reference, *, binding, **_kwargs):
        bound.append(binding["query"])
        return AttributeCursorSeenState(digests=(), state_id=None)

    monkeypatch.setattr(
        dashboard_view, "ReadDeadline", SimpleNamespace(start=lambda _ms: deadline)
    )
    monkeypatch.setattr(
        dashboard_view,
        "project_queryset_for_request",
        lambda _request: _ProjectScope([HOLDING, PLAIN]),
    )
    monkeypatch.setattr(
        dashboard_view, "_run_filter_value_pg_read", lambda _deadline, read: read()
    )
    monkeypatch.setattr(
        dashboard_view,
        "cursor_scope_for_request",
        lambda *_args, **_kwargs: {"principal": "unit"},
    )
    monkeypatch.setattr(dashboard_view, "decode_native_list_cursor", decode)
    monkeypatch.setattr(dashboard_view, "load_attribute_cursor_seen_state", load_seen)
    monkeypatch.setattr(dashboard_view, "V2AnalyticsQueryService", lambda: object())
    monkeypatch.setattr(dashboard_view, "holding_projects", holds)
    return decoded, bound


def _filter_values(**params):
    request = SimpleNamespace(
        validated_query_data={
            "source": "traces",
            "search": "",
            **params,
        },
        workspace=SimpleNamespace(id=WORKSPACE),
    )
    return dashboard_view.DashboardViewSet.filter_values.__wrapped__(
        dashboard_view.DashboardViewSet(), request
    )


def _system_page(retained):
    return FilterValueCursorPageRead(
        values=("customer-ended-call",),
        query_window_start=retained,
        query_window_end=retained + timedelta(minutes=5),
        has_more=False,
        next_segment_end=retained,
        next_segment_start=None,
        next_value_after=None,
        seen_value_digests=(),
        browse_status="exhausted",
    )


_SYSTEM_PARAMS = {
    "metric_name": "ended_reason",
    "metric_type": "system_metric",
    "page_size": 20,
}
_BASE_SYSTEM_QUERY = {
    "metric_name": "ended_reason",
    "metric_type": "system_metric",
    "source": "traces",
    "search": "",
}


@pytest.mark.parametrize(("project", "hides"), [(HOLDING, True), (PLAIN, False)])
def test_the_system_picker_decides_once_and_passes_it_to_the_reader(
    monkeypatch, project, hides
):
    holds = _HoldsAmong(HOLDING)
    _wire_dashboard(monkeypatch, holds=holds)
    reads = []
    monkeypatch.setattr(
        dashboard_view,
        "read_span_system_filter_value_cursor_page",
        lambda *_args, **kwargs: reads.append(kwargs) or _system_page(NOW),
    )
    monkeypatch.setattr(
        dashboard_view,
        "AttributeReadSelector",
        lambda **_kwargs: Mock(retained_window_start=lambda *_a, **_k: NOW),
    )

    response = _filter_values(project_ids=[project], **_SYSTEM_PARAMS)

    assert response.status_code == 200
    assert [read["hide_simulated"] for read in reads] == [hides]
    assert holds.asked == [[project]]


@pytest.mark.parametrize(("project", "hides"), [(HOLDING, True), (PLAIN, False)])
def test_a_system_picker_walk_is_signed_apart_by_the_rule(monkeypatch, project, hides):
    cursor_state = SimpleNamespace(
        order=(NOW, NOW - timedelta(minutes=5), "", ()),
        window_start=NOW - timedelta(days=30),
        window_end=NOW,
        seen_rows=0,
    )
    decoded, bound = _wire_dashboard(
        monkeypatch, holds=_HoldsAmong(HOLDING), cursor_state=cursor_state
    )
    monkeypatch.setattr(
        dashboard_view,
        "read_span_system_filter_value_cursor_page",
        lambda *_args, **_kwargs: _system_page(NOW),
    )

    response = _filter_values(project_ids=[project], cursor="opaque", **_SYSTEM_PARAMS)

    # The signed query gains the rule only when it is set, so a walk begun under
    # the other answer fails to decode and a plain project's cursors are as before.
    expected = {**_BASE_SYSTEM_QUERY, "project_ids": [project]}
    if hides:
        expected["hide_simulated"] = True
    assert response.status_code == 200
    assert decoded == [expected]
    assert bound == [expected]


@pytest.mark.parametrize(("project", "hides"), [(HOLDING, True), (PLAIN, False)])
def test_the_system_picker_of_a_workspace_batch_decides_for_its_own_projects(
    monkeypatch, project, hides
):
    holds = _HoldsAmong(HOLDING)
    _wire_dashboard(monkeypatch, holds=holds)
    monkeypatch.setattr(
        dashboard_view,
        "project_queryset_for_request",
        lambda _request: _ProjectScope([project]),
    )
    reads = []
    monkeypatch.setattr(
        dashboard_view,
        "read_span_system_filter_value_cursor_page",
        lambda *_args, **kwargs: reads.append(kwargs) or _system_page(NOW),
    )

    response = _filter_values(project_ids=[], **_SYSTEM_PARAMS)

    assert response.status_code == 200
    assert [read["hide_simulated"] for read in reads] == [hides]
    assert holds.asked == [[project]]


@pytest.mark.parametrize(("project", "hides"), [(HOLDING, True), (PLAIN, False)])
def test_the_legacy_system_picker_passes_the_rule_to_its_reader(
    monkeypatch, project, hides
):
    holds = _HoldsAmong(HOLDING)
    _wire_dashboard(monkeypatch, holds=holds)
    reads = []
    monkeypatch.setattr(
        dashboard_view,
        "read_span_system_filter_values",
        lambda *_args, **kwargs: (
            reads.append(kwargs) or FilterValueRead(("a",), True, None, NOW, NOW)
        ),
    )

    response = _filter_values(
        project_ids=[project],
        metric_name="ended_reason",
        metric_type="system_metric",
    )

    assert response.status_code == 200
    assert [read["hide_simulated"] for read in reads] == [hides]
    assert holds.asked == [[project]]


def _value_page():
    return AttributeValueCursorPageRead(
        rows=(),
        metadata=_metadata(),
        has_more=False,
        next_segment_end=NOW,
        next_before_identity=None,
        next_resume_identity=None,
        next_resume_member_offset=0,
        seen_value_digests=(),
    )


_ATTRIBUTE_PARAMS = {
    "metric_name": "customer.plan",
    "metric_type": "custom_attribute",
}


@pytest.mark.parametrize(("project", "hides"), [(HOLDING, True), (PLAIN, False)])
def test_the_attribute_picker_hands_its_selector_the_rule_and_signs_the_walk(
    monkeypatch, project, hides
):
    holds = _HoldsAmong(HOLDING)
    _, bound = _wire_dashboard(monkeypatch, holds=holds)
    selector = Mock()
    selector.retained_window_start.return_value = NOW - timedelta(days=30)
    selector.read_value_cursor_page.return_value = _value_page()
    built = []
    monkeypatch.setattr(
        dashboard_view,
        "AttributeReadSelector",
        lambda **kwargs: built.append(kwargs) or selector,
    )

    response = _filter_values(project_ids=[project], page_size=20, **_ATTRIBUTE_PARAMS)

    assert response.status_code == 200
    assert [kwargs["hide_simulated"] for kwargs in built] == [hides]
    assert holds.asked == [[project]]
    # Signed into the query, and so the seen-state binding, only when set.
    assert [("hide_simulated" in query) for query in bound] == [hides]


@pytest.mark.parametrize(("project", "hides"), [(HOLDING, True), (PLAIN, False)])
def test_a_resumed_attribute_picker_walk_is_decoded_under_the_rule(
    monkeypatch, project, hides
):
    cursor_state = SimpleNamespace(
        order=(NOW, (), (), 0, ()),
        window_start=NOW - timedelta(days=30),
        window_end=NOW,
        scan_slice_start=None,
        scan_slice_end=None,
        seen_rows=0,
    )
    decoded, _ = _wire_dashboard(
        monkeypatch, holds=_HoldsAmong(HOLDING), cursor_state=cursor_state
    )
    selector = Mock()
    selector.read_value_cursor_page.return_value = _value_page()
    built = []
    monkeypatch.setattr(
        dashboard_view,
        "AttributeReadSelector",
        lambda **kwargs: built.append(kwargs) or selector,
    )

    response = _filter_values(
        project_ids=[project], page_size=20, cursor="opaque", **_ATTRIBUTE_PARAMS
    )

    assert response.status_code == 200
    assert [kwargs["hide_simulated"] for kwargs in built] == [hides]
    assert [query.get("hide_simulated") for query in decoded] == [
        True if hides else None
    ]


@pytest.mark.parametrize(("project", "hides"), [(HOLDING, True), (PLAIN, False)])
def test_the_legacy_attribute_picker_hands_its_selector_the_rule(
    monkeypatch, project, hides
):
    holds = _HoldsAmong(HOLDING)
    _wire_dashboard(monkeypatch, holds=holds)
    selector = Mock()
    selector.read_values.return_value = AttributeValueRead((), _metadata())
    built = []
    monkeypatch.setattr(
        dashboard_view,
        "AttributeReadSelector",
        lambda **kwargs: built.append(kwargs) or selector,
    )

    response = _filter_values(project_ids=[project], **_ATTRIBUTE_PARAMS)

    assert response.status_code == 200
    assert [kwargs["hide_simulated"] for kwargs in built] == [hides]
    assert holds.asked == [[project]]


def test_a_scope_with_a_holding_project_hides_for_every_project_of_it(monkeypatch):
    # One decision per request over its projects, like the other readers of a
    # project list; an id that is not a uuid names no holding project.
    holds = _HoldsAmong(HOLDING)
    monkeypatch.setattr(dashboard_view, "holding_projects", holds)

    assert dashboard_view._scope_holds_simulated([PLAIN, HOLDING, "p1"]) is True
    assert dashboard_view._scope_holds_simulated([PLAIN]) is False
    assert holds.asked == [[PLAIN, HOLDING], [PLAIN]]


def test_a_scope_with_a_holding_project_reads_the_flag_inside_the_pickers_wrapper(
    monkeypatch,
):
    # The decision is one more Postgres read of this request, so it runs under
    # the request's own read policy and wall like the others.
    inside = []
    holds = _HoldsAmong(HOLDING)

    def asking(project_ids):
        inside.append(state["inside"])
        return holds(project_ids)

    state = {"inside": False}

    def wrapper(_deadline, read):
        state["inside"] = True
        try:
            return read()
        finally:
            state["inside"] = False

    _wire_dashboard(monkeypatch, holds=asking)
    monkeypatch.setattr(dashboard_view, "_run_filter_value_pg_read", wrapper)
    monkeypatch.setattr(
        dashboard_view,
        "read_span_system_filter_value_cursor_page",
        lambda *_args, **_kwargs: _system_page(NOW),
    )
    monkeypatch.setattr(
        dashboard_view,
        "AttributeReadSelector",
        lambda **_kwargs: Mock(retained_window_start=lambda *_a, **_k: NOW),
    )

    response = _filter_values(project_ids=[HOLDING], **_SYSTEM_PARAMS)

    assert response.status_code == 200
    assert inside == [True]


# ─── the collector's observed values ─────────────────────────────────────────


def _catalog_value_request(project):
    return (
        SimpleNamespace(workspace=SimpleNamespace(id=WORKSPACE)),
        {
            "property_id": "custom_attribute:customer.plan",
            "_property_kind": "custom_attribute",
            "page_size": 10,
            "project_ids": [project],
        },
    )


class _CatalogReader:
    """``PropertyCatalogValueReader`` double: remembers that it was built."""

    built = 0

    def __init__(self, **_kwargs):
        type(self).built += 1

    def read_page(self, **_kwargs):
        return "observed page"


def _wire_catalog_values(monkeypatch, holds, project):
    _CatalogReader.built = 0
    monkeypatch.setattr(dashboard_view, "holding_projects", holds)
    monkeypatch.setattr(
        dashboard_view, "_run_filter_value_pg_read", lambda _deadline, read: read()
    )
    monkeypatch.setattr(
        dashboard_view,
        "resolve_property_catalog_project_scope",
        lambda *_args, **_kwargs: [project],
    )
    monkeypatch.setattr(
        dashboard_view,
        "cursor_scope_for_request",
        lambda *_args, **_kwargs: {"principal": "unit"},
    )
    monkeypatch.setattr(dashboard_view, "PropertyCatalogValueReader", _CatalogReader)


def test_a_holding_scope_is_not_answered_from_the_collectors_observed_values(
    monkeypatch, settings
):
    # The collector's catalog knows nothing of the marks, so a value only
    # simulated calls carry would be offered; the established span readers hide.
    settings.PROPERTY_CATALOG_DATABASE = "test_index"
    holds = _HoldsAmong(HOLDING)
    _wire_catalog_values(monkeypatch, holds, HOLDING)
    request, params = _catalog_value_request(HOLDING)

    with pytest.raises(PropertyCatalogValueNotReady):
        dashboard_view._read_property_catalog_value_page(
            request, params, deadline=Mock()
        )

    assert holds.asked == [[HOLDING]]
    assert _CatalogReader.built == 0


def test_a_scope_that_does_not_hold_keeps_the_collectors_observed_values(
    monkeypatch, settings
):
    settings.PROPERTY_CATALOG_DATABASE = "test_index"
    holds = _HoldsAmong(HOLDING)
    _wire_catalog_values(monkeypatch, holds, PLAIN)
    request, params = _catalog_value_request(PLAIN)

    page = dashboard_view._read_property_catalog_value_page(
        request, params, deadline=Mock()
    )

    assert page == "observed page"
    assert holds.asked == [[PLAIN]]
    assert _CatalogReader.built == 1


def test_a_database_error_in_the_holding_read_makes_the_observed_values_unavailable(
    monkeypatch, settings
):
    settings.PROPERTY_CATALOG_DATABASE = "test_index"

    def fails(_project_ids):
        raise DatabaseError("connection reset")

    _wire_catalog_values(monkeypatch, fails, HOLDING)
    request, params = _catalog_value_request(HOLDING)

    with pytest.raises(PropertyCatalogValueUnavailable):
        dashboard_view._read_property_catalog_value_page(
            request, params, deadline=Mock()
        )

    assert _CatalogReader.built == 0


def test_a_holding_scopes_property_values_fall_through_to_the_hiding_reader(
    monkeypatch, settings
):
    settings.PROPERTY_CATALOG_DATABASE = "test_index"
    holds = _HoldsAmong(HOLDING)
    _wire_dashboard(monkeypatch, holds=holds)
    _wire_catalog_values(monkeypatch, holds, HOLDING)
    # ``_wire_catalog_values`` replaces the wrapper and scope stand-ins with the
    # same ones; the reader must never be built for this scope.
    selector = Mock()
    selector.retained_window_start.return_value = NOW - timedelta(days=30)
    selector.read_value_cursor_page.return_value = _value_page()
    built = []
    monkeypatch.setattr(
        dashboard_view,
        "AttributeReadSelector",
        lambda **kwargs: built.append(kwargs) or selector,
    )

    response = _filter_values(
        project_ids=[HOLDING],
        page_size=20,
        property_id="custom_attribute:customer.plan",
        _property_kind="custom_attribute",
        **_ATTRIBUTE_PARAMS,
    )

    assert response.status_code == 200
    assert _CatalogReader.built == 0
    assert [kwargs["hide_simulated"] for kwargs in built] == [True]


def _batched_attribute_picker(monkeypatch, project):
    holds = _HoldsAmong(HOLDING)
    _wire_dashboard(monkeypatch, holds=holds)
    monkeypatch.setattr(
        dashboard_view,
        "project_queryset_for_request",
        lambda _request: _ProjectScope([project]),
    )
    selector = Mock()
    selector.read_value_cursor_page.return_value = _value_page()
    built = []
    monkeypatch.setattr(
        dashboard_view,
        "AttributeReadSelector",
        lambda **kwargs: built.append(kwargs) or selector,
    )

    response = _filter_values(project_ids=[], page_size=20, **_ATTRIBUTE_PARAMS)
    return response, built, holds


@pytest.mark.parametrize(("project", "hides"), [(HOLDING, True), (PLAIN, False)])
def test_the_attribute_picker_of_a_workspace_batch_decides_for_its_own_projects(
    monkeypatch, project, hides
):
    response, built, holds = _batched_attribute_picker(monkeypatch, project)

    assert response.status_code == 200
    assert [kwargs["hide_simulated"] for kwargs in built] == [hides]
    assert holds.asked == [[project]]


class _Rows:
    """Queryset double for the legacy inventory's authorization read."""

    def __init__(self, ids):
        self.ids = list(ids)

    def filter(self, **_kwargs):
        return self

    def values_list(self, *_args, **_kwargs):
        return self.ids

    def values(self, *_args, **_kwargs):
        return []

    def count(self):
        return len(self.ids)


@pytest.mark.parametrize(
    ("projects", "hides"),
    [([HOLDING], True), ([PLAIN], False), ([PLAIN, HOLDING], True)],
)
def test_the_legacy_inventory_decides_once_for_all_its_projects(
    monkeypatch, projects, hides
):
    import tracer.models.trace_annotation as annotation_module

    holds = _HoldsAmong(HOLDING)
    monkeypatch.setattr(dashboard_view, "holding_projects", holds)
    monkeypatch.setattr(
        dashboard_view, "Project", SimpleNamespace(objects=_Rows(projects))
    )
    monkeypatch.setattr(
        dashboard_view,
        "CustomEvalConfig",
        SimpleNamespace(no_workspace_objects=_Rows([])),
    )
    monkeypatch.setattr(
        annotation_module,
        "AnnotationLabel",
        SimpleNamespace(no_workspace_objects=_Rows([])),
    )
    asked = []

    class _Inventory:
        def get_span_attribute_keys_ch(self, project_id, **kwargs):
            asked.append((project_id, kwargs["hide_simulated"]))
            return []

    monkeypatch.setattr(dashboard_view, "AnalyticsQueryService", _Inventory)
    request = SimpleNamespace(
        query_params={"project_ids": ",".join(projects)},
        workspace=SimpleNamespace(id=WORKSPACE),
    )

    response = dashboard_view.DashboardViewSet()._metrics_observability_legacy(request)

    assert response.status_code == 200
    assert asked == [(project, hides) for project in projects]
    assert len(holds.asked) == 1


# ─── smart filter grounding ──────────────────────────────────────────────────


@pytest.mark.parametrize("hides", [True, False])
def test_the_grounding_reads_pass_the_decision_they_are_given(monkeypatch, hides):
    system_reads = []
    selectors = []

    class _Selector:
        def __init__(self, **kwargs):
            selectors.append(kwargs)

        def read_values(self, *_args, **_kwargs):
            return AttributeValueRead((), _metadata())

    monkeypatch.setattr(
        "tracer.services.clickhouse.filter_value_reads.read_span_system_filter_values",
        lambda *_args, **kwargs: (
            system_reads.append(kwargs)
            or FilterValueRead(("gpt-4o",), True, None, NOW, NOW)
        ),
    )
    monkeypatch.setattr(
        "tracer.services.clickhouse.attribute_reads.AttributeReadSelector", _Selector
    )
    monkeypatch.setattr(
        "tracer.services.clickhouse.v2.query_service.V2AnalyticsQueryService",
        lambda: object(),
    )

    ai_filter_view._fetch_trace_field_values(
        [HOLDING],
        "model",
        "system_metric",
        search_query="gpt",
        hide_simulated=hides,
    )
    ai_filter_view._fetch_trace_field_values(
        [HOLDING],
        "customer.plan",
        "custom_attribute",
        search_query="pro",
        hide_simulated=hides,
    )

    assert [read["hide_simulated"] for read in system_reads] == [hides]
    assert [kwargs["hide_simulated"] for kwargs in selectors] == [hides]


def test_the_grounding_reads_do_not_hide_unless_told_to(monkeypatch):
    system_reads = []
    monkeypatch.setattr(
        "tracer.services.clickhouse.filter_value_reads.read_span_system_filter_values",
        lambda *_args, **kwargs: (
            system_reads.append(kwargs)
            or FilterValueRead(("gpt-4o",), True, None, NOW, NOW)
        ),
    )
    monkeypatch.setattr(
        "tracer.services.clickhouse.v2.query_service.V2AnalyticsQueryService",
        lambda: object(),
    )

    ai_filter_view._fetch_trace_field_values(
        [HOLDING], "model", "system_metric", search_query="gpt"
    )

    assert [read["hide_simulated"] for read in system_reads] == [False]


_SMART_SCHEMA = [
    {
        "field": "status",
        "property_id": "system_attribute:traces:status",
        "label": "Status",
        "type": "string",
        "category": "system",
    }
]


def _smart_request(project):
    request = APIRequestFactory().post(
        "/model-hub/ai-filter/",
        {
            "mode": "smart",
            "source": "traces",
            "query": "show failed calls",
            "project_id": project,
            "schema": _SMART_SCHEMA,
        },
        format="json",
    )
    request.workspace = Mock()
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    return request


@pytest.mark.parametrize(("project", "hides"), [(HOLDING, True), (PLAIN, False)])
def test_a_smart_request_decides_once_inside_its_postgres_bound(
    monkeypatch, project, hides
):
    events = []
    holds = _HoldsAmong(HOLDING)

    def asking(project_ids):
        events.append("holding")
        return holds(project_ids)

    @contextmanager
    def bounded(_deadline):
        events.append("enter")
        yield
        events.append("exit")

    fetches = []

    def agent(_query, _schema, fetch_values, **_kwargs):
        # The model asks for values more than once in one request.
        fetch_values("system_attribute:traces:status", search_query="fail")
        fetch_values("system_attribute:traces:status", search_query="done")
        return []

    monkeypatch.setattr(ai_filter_view, "holding_projects", asking)
    monkeypatch.setattr(ai_filter_view, "_bounded_ai_filter_postgres", bounded)
    monkeypatch.setattr(
        ai_filter_view, "_resolve_project_ids", lambda *_args: [project]
    )
    monkeypatch.setattr(
        ai_filter_view,
        "_authorize_smart_property_schema",
        lambda *_a, **_k: _SMART_SCHEMA,
    )
    monkeypatch.setattr(
        ai_filter_view,
        "_fetch_trace_field_values",
        lambda *_args, **kwargs: fetches.append(kwargs) or [],
    )
    monkeypatch.setattr(ai_filter_view, "_run_smart_agent", agent)

    response = ai_filter_view.AIFilterView.as_view()(_smart_request(project))

    assert response.status_code == 200
    assert holds.asked == [[project]]
    assert events == ["enter", "holding", "exit"]
    assert [call["hide_simulated"] for call in fetches] == [hides, hides]


# ─── attribute pickers of the observation span and span attribute views ─────


def _authenticated_get(path, data, **user):
    request = APIRequestFactory().get(path, data)
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True, **user))
    return request


def _spy_on_selector(monkeypatch, method, result):
    """Answer a selector method and remember the rule each selector was built with."""

    seen = []

    def answer(self, *_args, **_kwargs):
        seen.append(self._hide_simulated)
        return result

    monkeypatch.setattr(AttributeReadSelector, method, answer)
    return seen


@pytest.mark.parametrize(("project", "hides"), [(HOLDING, True), (PLAIN, False)])
@pytest.mark.parametrize(
    "endpoint", ["get_span_attributes_list", "get_eval_attributes_list"]
)
def test_the_observation_span_attribute_pickers_decide_once_for_the_project(
    monkeypatch, project, hides, endpoint
):
    holds = _Holds(HOLDING)
    monkeypatch.setattr(observation_span_module, "project_hides_simulated", holds)
    monkeypatch.setattr(
        observation_span_module.ObservationSpanView,
        "_attribute_project_for_request",
        staticmethod(lambda _request, _project_id: True),
    )
    seen = _spy_on_selector(
        monkeypatch,
        "discover_keys",
        AttributeKeyRead((AttributeKeyRow("customer.plan", "string", 1),), _metadata()),
    )
    data = {"filters": json.dumps({"project_id": project})}
    if endpoint == "get_eval_attributes_list":
        data["row_type"] = "spans"
    request = _authenticated_get("/tracer/observation-span/x/", data)

    response = observation_span_module.ObservationSpanView.as_view({"get": endpoint})(
        request
    )

    assert response.status_code == 200
    assert seen == [hides]
    assert holds.asked == [project]


@pytest.mark.parametrize(
    ("endpoint", "message"),
    [
        ("get_span_attributes_list", "Span attributes could not be loaded"),
        ("get_eval_attributes_list", "Evaluation attributes could not be loaded"),
    ],
)
def test_a_database_error_in_the_decision_is_answered_like_any_other_failure(
    monkeypatch, endpoint, message
):
    def fails(_project_id):
        raise DatabaseError("connection reset")

    monkeypatch.setattr(observation_span_module, "project_hides_simulated", fails)
    monkeypatch.setattr(
        observation_span_module.ObservationSpanView,
        "_attribute_project_for_request",
        staticmethod(lambda _request, _project_id: True),
    )
    data = {"filters": json.dumps({"project_id": HOLDING})}
    if endpoint == "get_eval_attributes_list":
        data["row_type"] = "spans"
    request = _authenticated_get("/tracer/observation-span/x/", data)

    response = observation_span_module.ObservationSpanView.as_view({"get": endpoint})(
        request
    )

    assert response.status_code == 500
    assert message in json.dumps(response.data)
    assert "connection reset" not in json.dumps(response.data)


def _scope_the_project(monkeypatch, holds):
    monkeypatch.setattr(span_attributes_module, "project_hides_simulated", holds)
    monkeypatch.setattr(
        span_attributes_module,
        "_project_is_in_request_scope",
        lambda _request, _project_id: True,
    )


def _key_page(window_end, *, digest):
    return AttributeKeyCursorPageRead(
        (AttributeKeyRow("alpha", "string", 1),),
        _metadata(),
        True,
        "continuation",
        window_end,
        (HOLDING, "trace", "span", SPAN_TIME),
        None,
        0,
        (digest,),
        window_end - timedelta(hours=12),
    )


@pytest.mark.parametrize(("project", "hides"), [(HOLDING, True), (PLAIN, False)])
def test_a_key_walk_is_signed_apart_by_the_rule_and_its_selector_hides(
    monkeypatch, project, hides
):
    holds = _Holds(HOLDING)
    _scope_the_project(monkeypatch, holds)
    seen = []

    def read_page(self, project_ids, **kwargs):
        seen.append(self._hide_simulated)
        return _key_page(
            kwargs["window_end"], digest=attribute_key_cursor_digest("alpha")
        )

    monkeypatch.setattr(AttributeReadSelector, "read_key_cursor_page", read_page)
    request = _authenticated_get(
        "/api/traces/span-attribute-keys/", {"project_id": project, "page_size": 10}
    )

    response = span_attributes_module.SpanAttributeKeysView.as_view()(request)

    assert response.status_code == 200
    assert seen == [hides]
    assert holds.asked == [project]
    cursor = response.data["next_cursor"]
    base_query = {"project_id": project, "mode": "recent_attribute_keys"}
    scope = cursor_scope_for_request(
        SimpleNamespace(
            user=SimpleNamespace(), auth=None, organization=None, workspace=None
        ),
        project_ids=[project],
    )

    def decode(query):
        return decode_list_cursor(
            cursor,
            resource="span_attribute_keys",
            scope=scope,
            query=query,
            page_size=10,
        )

    # The cursor opens only under the query it was signed with: the plain one
    # for a project that does not hold, and the one naming the rule for a
    # project that does. A cursor never resumes under the other answer.
    signed = {**base_query, "hide_simulated": True} if hides else base_query
    other = base_query if hides else {**base_query, "hide_simulated": True}
    assert decode(signed).order
    with pytest.raises(ListCursorError):
        decode(other)


@pytest.mark.parametrize(("project", "hides"), [(HOLDING, True), (PLAIN, False)])
def test_the_compatibility_key_lookup_and_value_sample_decide_for_their_project(
    monkeypatch, project, hides
):
    holds = _Holds(HOLDING)
    _scope_the_project(monkeypatch, holds)
    keys_seen = _spy_on_selector(
        monkeypatch, "discover_keys", AttributeKeyRead((), _metadata())
    )
    values_seen = _spy_on_selector(
        monkeypatch, "read_values", AttributeValueRead((), _metadata())
    )

    keys = span_attributes_module.SpanAttributeKeysView.as_view()(
        _authenticated_get("/api/traces/span-attribute-keys/", {"project_id": project})
    )
    values = span_attributes_module.SpanAttributeValuesView.as_view()(
        _authenticated_get(
            "/api/traces/span-attribute-values/",
            {"project_id": project, "key": "customer.plan"},
        )
    )

    assert keys.status_code == 200
    assert values.status_code == 200
    assert keys_seen == [hides]
    assert values_seen == [hides]


@pytest.mark.parametrize(("project", "hides"), [(HOLDING, True), (PLAIN, False)])
def test_the_detail_snapshot_is_keyed_apart_by_the_rule(monkeypatch, project, hides):
    holds = _Holds(HOLDING)
    _scope_the_project(monkeypatch, holds)
    identities = []
    monkeypatch.setattr(
        span_attributes_module,
        "read_or_schedule_exact_snapshot",
        lambda namespace, identity, **_kwargs: identities.append(identity) or {},
    )
    request = _authenticated_get(
        "/api/traces/span-attribute-detail/",
        {"project_id": project, "key": "customer.plan"},
        organization=SimpleNamespace(id="org"),
        workspace=SimpleNamespace(id=WORKSPACE),
    )

    response = span_attributes_module.SpanAttributeDetailView.as_view()(request)

    assert response.status_code == 200
    # In the identity only when set, so a snapshot stored before the project
    # held is never served to a request that hides.
    plain_identity = {
        "organization_id": "org",
        "workspace_id": WORKSPACE,
        "project_id": project,
        "attribute_key": "customer.plan",
        "horizon_days": 365,
    }
    assert identities == [
        {**plain_identity, "hide_simulated": True} if hides else plain_identity
    ]
    assert holds.asked == [project]


@pytest.mark.parametrize("hides", [True, False])
def test_a_workspace_key_walk_decides_for_each_batch_of_projects(monkeypatch, hides):
    holds = _HoldsAmong(*([HOLDING] if hides else []))
    monkeypatch.setattr(span_attributes_module, "holding_projects", holds)
    monkeypatch.setattr(
        span_attributes_module,
        "_workspace_project_batch",
        lambda _request, **_kwargs: ((HOLDING,), False),
    )
    monkeypatch.setattr(
        span_attributes_module,
        "_run_span_attribute_pg_read",
        lambda _deadline, read: read(),
    )
    seen = []

    def read_page(self, project_ids, **kwargs):
        seen.append(self._hide_simulated)
        return _key_page(
            kwargs["window_end"],
            digest=attribute_key_type_cursor_digest("alpha", "string"),
        )

    monkeypatch.setattr(AttributeReadSelector, "read_key_cursor_page", read_page)
    request = _authenticated_get(
        "/api/traces/span-attribute-keys/",
        {"workspace_scope": True, "page_size": 10},
    )

    response = span_attributes_module.SpanAttributeKeysView.as_view()(request)

    assert response.status_code == 200
    assert seen == [hides]
    assert holds.asked == [[HOLDING]]


@pytest.mark.parametrize("workspace_scope", [False, True])
def test_the_key_walk_reads_the_holding_flag_inside_the_views_read_wrapper(
    monkeypatch, workspace_scope
):
    # The decision is a Postgres read of this request, so it runs under the
    # view's own read policy and wall.
    state = {"inside": False}
    inside = []

    def wrapper(_deadline, read):
        state["inside"] = True
        try:
            return read()
        finally:
            state["inside"] = False

    def one_project(project_id):
        inside.append(state["inside"])
        return False

    def among(project_ids):
        inside.append(state["inside"])
        return frozenset()

    _scope_the_project(monkeypatch, one_project)
    monkeypatch.setattr(span_attributes_module, "holding_projects", among)
    monkeypatch.setattr(span_attributes_module, "_run_span_attribute_pg_read", wrapper)
    monkeypatch.setattr(
        span_attributes_module,
        "_workspace_project_batch",
        lambda _request, **_kwargs: ((HOLDING,), False),
    )
    monkeypatch.setattr(
        AttributeReadSelector,
        "read_key_cursor_page",
        lambda self, project_ids, **kwargs: _key_page(
            kwargs["window_end"],
            digest=(
                attribute_key_type_cursor_digest("alpha", "string")
                if workspace_scope
                else attribute_key_cursor_digest("alpha")
            ),
        ),
    )
    params = {"workspace_scope": True} if workspace_scope else {"project_id": HOLDING}
    request = _authenticated_get(
        "/api/traces/span-attribute-keys/", {**params, "page_size": 10}
    )

    response = span_attributes_module.SpanAttributeKeysView.as_view()(request)

    assert response.status_code == 200
    assert inside == [True]


# ─── Postgres key reader behind the project eval attribute tools ─────────────


def _span_attribute_sql(monkeypatch, **kwargs):
    seen = []
    monkeypatch.setattr(
        SQL_query_handler,
        "execute_query",
        classmethod(
            lambda cls, query, params=None: seen.append((query, list(params))) or []
        ),
    )
    SQL_query_handler.get_span_attributes_for_project(HOLDING, **kwargs)
    (call,) = seen
    return call


def _squashed(sql):
    return " ".join(sql.split())


def test_the_postgres_key_reader_leaves_out_spans_that_carry_either_mark(monkeypatch):
    query, params = _span_attribute_sql(monkeypatch, hide_simulated=True)
    plain_query, plain_params = _span_attribute_sql(monkeypatch)

    # The mark test is the module's own text, appended as it is: the reader
    # builds none, and binds only the project.
    assert f"AND {postgres_span_exclusion_sql()}" in _squashed(query)
    assert params == [HOLDING]
    # Hiding only narrows the statement it would otherwise send.
    assert query.startswith(plain_query)
    assert plain_params == [HOLDING]


def test_the_postgres_key_reader_sends_today_s_statement_when_not_hiding(monkeypatch):
    default = _span_attribute_sql(monkeypatch)
    explicit = _span_attribute_sql(monkeypatch, hide_simulated=False)

    assert default == explicit
    assert "jsonb_exists" not in default[0]
    assert SIMULATION_MARK_KEY not in default[0]
    assert default[1] == [HOLDING]


class _Stop(Exception):
    """Ends a tool run once it has read the project's attribute keys."""


def _eval_attribute_tool_run(monkeypatch, tool_module, run, project):
    from uuid import UUID

    import model_hub.models.evals_metric as templates_module
    import tracer.models.custom_eval_config as configs_module
    import tracer.models.project as projects_module

    class _Missing(Exception):
        pass

    holds = _Holds(HOLDING)
    reads = []
    monkeypatch.setattr(tool_module, "project_hides_simulated", holds)
    monkeypatch.setattr(
        projects_module,
        "Project",
        SimpleNamespace(
            DoesNotExist=_Missing,
            objects=SimpleNamespace(get=lambda **_kwargs: SimpleNamespace(name="p")),
        ),
    )
    monkeypatch.setattr(
        templates_module,
        "EvalTemplate",
        SimpleNamespace(
            DoesNotExist=_Missing,
            no_workspace_objects=SimpleNamespace(
                get=lambda *_args, **_kwargs: SimpleNamespace(config={})
            ),
        ),
    )
    monkeypatch.setattr(
        configs_module,
        "CustomEvalConfig",
        SimpleNamespace(
            objects=SimpleNamespace(
                filter=lambda **_kwargs: SimpleNamespace(exists=lambda: False)
            )
        ),
    )

    def read_keys(project_id, **kwargs):
        reads.append((project_id, kwargs))
        raise _Stop

    monkeypatch.setattr(
        SQL_query_handler, "get_span_attributes_for_project", staticmethod(read_keys)
    )
    context = SimpleNamespace(organization=object())
    with pytest.raises(_Stop):
        run(UUID(project), context)
    return holds, reads


@pytest.mark.parametrize(("project", "hides"), [(HOLDING, True), (PLAIN, False)])
@pytest.mark.parametrize("tool", ["listing", "config"])
def test_the_eval_attribute_tools_decide_for_their_project_and_pass_it_on(
    monkeypatch, project, hides, tool
):
    import ai_tools.tools.tracing.create_custom_eval_config as config_module
    import ai_tools.tools.tracing.get_project_eval_attributes as listing_module

    if tool == "listing":
        module = listing_module

        def run(project_id, context):
            listing_module.GetProjectEvalAttributesTool().execute(
                listing_module.GetProjectEvalAttributesInput(project_id=project_id),
                context,
            )
    else:
        module = config_module

        def run(project_id, context):
            config_module.CreateCustomEvalConfigTool().execute(
                config_module.CreateCustomEvalConfigInput(
                    project_id=project_id,
                    eval_template_id=project_id,
                    name="Accuracy",
                ),
                context,
            )

    holds, reads = _eval_attribute_tool_run(monkeypatch, module, run, project)

    assert holds.asked == [project]
    assert reads == [(project, {"hide_simulated": hides})]


# ─── cluster RCA project key fallback ────────────────────────────────────────


@pytest.mark.parametrize(("project", "hides"), [(HOLDING, True), (PLAIN, False)])
def test_the_cluster_rca_project_key_listing_decides_and_passes_it_on(
    monkeypatch, project, hides
):
    from ee.agenthub.cluster_rca import agent as agent_module

    holds = _Holds(HOLDING)
    reads = []
    monkeypatch.setattr(agent_module, "project_hides_simulated", holds)
    monkeypatch.setattr(
        agent_module,
        "list_attribute_keys_for_project",
        lambda project_id, **kwargs: reads.append((project_id, kwargs)) or [],
    )

    agent_module.ClusterAnalysisAgent._list_attribute_keys(
        SimpleNamespace(project_id=project), {}, 10
    )

    assert holds.asked == [project]
    assert reads == [(project, {"hide_simulated": hides})]


@pytest.mark.parametrize(("project", "hides"), [(HOLDING, True), (PLAIN, False)])
def test_the_cluster_rca_prompt_keys_decide_and_pass_it_on(monkeypatch, project, hides):
    from ee.agenthub.cluster_rca import context_builder

    holds = _Holds(HOLDING)
    reads = []
    monkeypatch.setattr(context_builder, "project_hides_simulated", holds)
    monkeypatch.setattr(
        context_builder,
        "list_attribute_keys_for_project",
        lambda project_id, **kwargs: reads.append((project_id, kwargs)) or [],
    )

    context_builder.build_project_schema_context(project)

    assert holds.asked == [project]
    assert reads == [(project, {"hide_simulated": hides})]


# ─── eval-name picker ────────────────────────────────────────────────────────


def _flat(sql):
    return " ".join(sql.split())


EVAL_CONFIGS = ["cfg-a", "cfg-b"]
EVAL_TABLE = "tracer_eval_logger_v2"
# What the candidate read has always sent for a project that does not hold.
EVAL_NAMES_TODAY = (
    "SELECT DISTINCT toString(custom_eval_config_id) AS config_id "
    f"FROM {EVAL_TABLE} WHERE is_deleted = 0 "
    "AND custom_eval_config_id IN %(config_ids)s"
)
# A config stays only if one of its eval rows is on a trace the marked spans do
# not name: a negative test, so a row with no trace id stays too. Written out
# whole because a membership written the wrong way round carries the same text.
EVAL_NAMES_HIDING = _flat(
    f"{EVAL_NAMES_TODAY} "
    "AND ifNull(toString(trace_id), '') NOT IN ("
    "SELECT trace_id FROM spans "
    "WHERE project_id IN %(project_ids)s AND is_deleted = 0 "
    "AND trace_id IN ("
    f"SELECT DISTINCT toString(trace_id) FROM {EVAL_TABLE} "
    "WHERE is_deleted = 0 AND custom_eval_config_id IN %(config_ids)s) "
    f"AND NOT ({UNMARKED}))"
)


def _eval_name_service(settings):
    """A real service whose statements are recorded instead of sent."""

    settings.CH25_EVAL_LOGGER_TABLE = EVAL_TABLE
    service = AnalyticsQueryService()
    sent = []

    def record(query, params=None, **_kwargs):
        sent.append((_flat(query), dict(params)))
        return SimpleNamespace(data=[{"config_id": "cfg-a"}])

    service.execute_ch_query = record
    return service, sent


def test_a_holding_projects_eval_names_leave_marked_traces_out(settings):
    service, sent = _eval_name_service(settings)

    ids = service.get_eval_config_ids_for_candidates_ch(
        EVAL_CONFIGS, window_days=None, hide_simulated=True, project_ids=[HOLDING]
    )

    assert ids == ["cfg-a"]
    assert sent == [
        (
            EVAL_NAMES_HIDING,
            {"config_ids": tuple(EVAL_CONFIGS), "project_ids": (HOLDING,)},
        )
    ]


def test_the_project_facade_hands_its_own_project_to_the_eval_name_read(settings):
    service, sent = _eval_name_service(settings)

    service.get_eval_config_ids_with_data_ch(
        HOLDING,
        candidate_config_ids=EVAL_CONFIGS,
        window_days=None,
        hide_simulated=True,
    )

    assert sent == [
        (
            EVAL_NAMES_HIDING,
            {"config_ids": tuple(EVAL_CONFIGS), "project_ids": (HOLDING,)},
        )
    ]


def test_eval_names_of_a_project_that_does_not_hold_are_exactly_what_they_were(
    settings,
):
    service, sent = _eval_name_service(settings)

    service.get_eval_config_ids_for_candidates_ch(EVAL_CONFIGS, window_days=None)
    service.get_eval_config_ids_for_candidates_ch(
        EVAL_CONFIGS, window_days=None, hide_simulated=False, project_ids=[PLAIN]
    )
    service.get_eval_config_ids_with_data_ch(
        PLAIN, candidate_config_ids=EVAL_CONFIGS, window_days=None
    )
    service.get_eval_config_ids_with_data_ch(
        PLAIN,
        candidate_config_ids=EVAL_CONFIGS,
        window_days=None,
        hide_simulated=False,
    )

    assert sent == [(EVAL_NAMES_TODAY, {"config_ids": tuple(EVAL_CONFIGS)})] * 4
    assert _carries_no_mark(sent[0][0])
    assert "spans" not in sent[0][0]


def test_the_eval_name_window_bounds_the_rows_and_the_traces_they_name(settings):
    service, sent = _eval_name_service(settings)
    window = "created_at >= now() - toIntervalDay(%(window_days)s)"

    service.get_eval_config_ids_for_candidates_ch(
        EVAL_CONFIGS, window_days=30, hide_simulated=True, project_ids=[HOLDING]
    )
    service.get_eval_config_ids_for_candidates_ch(EVAL_CONFIGS, window_days=30)

    hiding, plain = sent
    assert hiding[0].count(window) == 2
    assert plain[0].count(window) == 1
    assert hiding[1]["window_days"] == plain[1]["window_days"] == 30


@pytest.mark.parametrize("project_ids", [None, [], [""]])
def test_eval_names_cannot_hide_without_the_projects_to_look_in(settings, project_ids):
    service, sent = _eval_name_service(settings)

    with pytest.raises(ValueError, match="project ids"):
        service.get_eval_config_ids_for_candidates_ch(
            EVAL_CONFIGS, hide_simulated=True, project_ids=project_ids
        )

    assert sent == []


def _eval_names_request(monkeypatch, project, holds, *, configs=("config-1",)):
    project_scope = MagicMock()
    project_scope.filter.return_value.first.return_value = SimpleNamespace(
        trace_type="observe"
    )
    config_manager = MagicMock()
    config_manager.filter.return_value.values_list.return_value = list(configs)
    analytics = MagicMock()
    analytics.get_eval_config_ids_with_data_ch.return_value = []
    monkeypatch.setattr(
        trace_view, "_project_queryset_for_request", lambda _request: project_scope
    )
    monkeypatch.setattr(trace_view.CustomEvalConfig, "objects", config_manager)
    monkeypatch.setattr(trace_view, "V2AnalyticsQueryService", lambda: analytics)
    monkeypatch.setattr(trace_view, "project_hides_simulated", holds)
    request = SimpleNamespace(query_params={"project_id": project})
    view = trace_view.TraceView()
    view.request = request
    response = unwrap(trace_view.TraceView.get_eval_names)(view, request)
    return response, analytics


@pytest.mark.parametrize(("project", "hides"), [(HOLDING, True), (PLAIN, False)])
def test_the_eval_name_picker_decides_once_and_passes_it_to_the_read(
    monkeypatch, project, hides
):
    holds = _Holds(HOLDING)

    response, analytics = _eval_names_request(monkeypatch, project, holds)

    assert response.status_code == 200
    assert holds.asked == [project]
    analytics.get_eval_config_ids_with_data_ch.assert_called_once_with(
        project,
        candidate_config_ids=["config-1"],
        window_days=None,
        hide_simulated=hides,
    )


def test_the_eval_name_picker_of_a_project_without_configs_reads_nothing(monkeypatch):
    holds = _Holds(HOLDING)

    response, analytics = _eval_names_request(monkeypatch, HOLDING, holds, configs=())

    assert response.status_code == 200
    assert holds.asked == []
    analytics.get_eval_config_ids_with_data_ch.assert_not_called()


def test_the_hiding_eval_name_read_has_a_server_side_cap_and_the_plain_one_does_not(
    settings,
):
    settings.CH25_EVAL_LOGGER_TABLE = EVAL_TABLE
    service = AnalyticsQueryService()
    kwargs_sent = []

    def record(query, params=None, **kwargs):
        kwargs_sent.append(kwargs)
        return SimpleNamespace(data=[])

    service.execute_ch_query = record

    service.get_eval_config_ids_for_candidates_ch(
        EVAL_CONFIGS,
        timeout_ms=3000,
        window_days=None,
        hide_simulated=True,
        project_ids=[HOLDING],
    )
    service.get_eval_config_ids_for_candidates_ch(
        EVAL_CONFIGS, timeout_ms=3000, window_days=None
    )

    hiding, plain = kwargs_sent
    assert hiding == {"timeout_ms": 3000, "server_execution_cap_ms": 3000}
    # A read that hides nothing is exactly the call it always was.
    assert plain == {"timeout_ms": 3000}


def test_the_eval_names_of_a_holding_project_are_kept_for_a_short_time(
    monkeypatch, kept_answers
):
    holds = _Holds(HOLDING)

    response, first = _eval_names_request(monkeypatch, HOLDING, holds)
    again, second = _eval_names_request(monkeypatch, HOLDING, holds)

    assert response.status_code == again.status_code == 200
    first.get_eval_config_ids_with_data_ch.assert_called_once()
    # The second request is answered from what the first one kept.
    second.get_eval_config_ids_with_data_ch.assert_not_called()
    [key] = kept_answers.store
    assert key.startswith(f"eval_names_with_data:{HOLDING}:")
    assert key.endswith(":hide_simulated")
    assert kept_answers.timeouts[key] == 120


def test_the_eval_names_of_a_project_that_does_not_hold_are_not_kept(
    monkeypatch, kept_answers
):
    holds = _Holds(HOLDING)

    _, first = _eval_names_request(monkeypatch, PLAIN, holds)
    _, second = _eval_names_request(monkeypatch, PLAIN, holds)

    first.get_eval_config_ids_with_data_ch.assert_called_once()
    second.get_eval_config_ids_with_data_ch.assert_called_once()
    assert kept_answers.store == {}


def _v2_eval_name_service(settings):
    """The production service, whose statements are recorded instead of sent."""
    from tracer.services.clickhouse.v2.query_service import V2AnalyticsQueryService

    settings.CH25_EVAL_LOGGER_TABLE = EVAL_TABLE
    # ``__init__`` would open the pooled ClickHouse client; nothing here sends.
    service = V2AnalyticsQueryService.__new__(V2AnalyticsQueryService)
    sent = []

    def record(query, params=None, **_kwargs):
        sent.append((_flat(query), dict(params)))
        return SimpleNamespace(data=[{"config_id": "cfg-a"}])

    service.execute_ch_query = record
    return service, sent


def test_the_production_service_hands_the_rule_and_the_projects_to_the_eval_name_read(
    settings,
):
    service, sent = _v2_eval_name_service(settings)

    candidates = service.get_eval_config_ids_for_candidates_ch(
        EVAL_CONFIGS, window_days=None, hide_simulated=True, project_ids=[HOLDING]
    )
    facade = service.get_eval_config_ids_with_data_ch(
        HOLDING,
        candidate_config_ids=EVAL_CONFIGS,
        window_days=None,
        hide_simulated=True,
    )

    assert candidates == facade == ["cfg-a"]
    hiding = (
        EVAL_NAMES_HIDING,
        {"config_ids": tuple(EVAL_CONFIGS), "project_ids": (HOLDING,)},
    )
    assert sent == [hiding, hiding]


def test_the_production_service_sends_todays_eval_name_statement_when_not_hiding(
    settings,
):
    service, sent = _v2_eval_name_service(settings)

    service.get_eval_config_ids_for_candidates_ch(EVAL_CONFIGS, window_days=None)
    service.get_eval_config_ids_with_data_ch(
        PLAIN, candidate_config_ids=EVAL_CONFIGS, window_days=None
    )

    assert sent == [(EVAL_NAMES_TODAY, {"config_ids": tuple(EVAL_CONFIGS)})] * 2


# ─── metrics catalog: which evals have data ──────────────────────────────────


class _CatalogStop(BaseException):
    """Ends a catalog build once it has asked which eval configs have data.

    Not an ``Exception``: the catalog turns those into a fallback to every
    configured eval, which would carry the build on into the database.
    """


class _ConfigRows:
    def __init__(self, ids):
        self.ids = list(ids)

    def filter(self, **_kwargs):
        return self

    def exclude(self, **kwargs):
        return _ConfigRows(
            [config for config in self.ids if config not in kwargs["id__in"]]
        )

    def distinct(self):
        return self

    def order_by(self, *_args):
        return self

    def values_list(self, *_args, **_kwargs):
        return list(self.ids)


class _EvalUsage:
    asked = []

    def get_eval_config_ids_for_candidates_ch(self, config_ids, **kwargs):
        type(self).asked.append((config_ids, kwargs))
        raise _CatalogStop


def _eval_usage_read(monkeypatch, holds, **kwargs):
    _EvalUsage.asked = []
    families = _direct_catalog_pg_read(monkeypatch)
    monkeypatch.setattr(catalog_module, "holding_projects", holds)
    monkeypatch.setattr(catalog_module, "V2AnalyticsQueryService", _EvalUsage)
    monkeypatch.setattr(
        catalog_module,
        "CustomEvalConfig",
        SimpleNamespace(objects=_ConfigRows(["config-1"])),
    )
    with pytest.raises(_CatalogStop):
        catalog_module.build_metrics_catalog(
            SimpleNamespace(id=WORKSPACE, organization_id="org"),
            project_ids_param=HOLDING,
            category="eval_metric",
            source="all",
            deadline=SimpleNamespace(remaining_ms=lambda *_args, **_kwargs: 1_000),
            _resolved_project_scope=([HOLDING], True),
            **kwargs,
        )
    return _EvalUsage.asked, families


@pytest.mark.parametrize(("holding", "hides"), [(HOLDING, True), (PLAIN, False)])
def test_the_catalog_asks_once_and_hands_the_answer_to_the_eval_usage_read(
    monkeypatch, holding, hides
):
    holds = _HoldsAmong(holding)

    asked, families = _eval_usage_read(monkeypatch, holds)

    assert holds.asked == [[HOLDING]]
    assert families == ["project_scope", "eval_metrics"]
    assert len(asked) == 1
    config_ids, kwargs = asked[0]
    assert config_ids == ["config-1"]
    assert kwargs["hide_simulated"] is hides
    assert kwargs["project_ids"] == [HOLDING]


def test_a_catalog_given_an_answer_does_not_ask_again_for_eval_usage(monkeypatch):
    holds = _HoldsAmong(HOLDING)

    asked, _ = _eval_usage_read(monkeypatch, holds, _hides_simulated=True)

    assert holds.asked == []
    assert asked[0][1]["hide_simulated"] is True


class _TemplateQuery:
    """Ends a catalog build at the template query, keeping what it was asked."""

    asked = []

    def filter(self, **kwargs):
        type(self).asked.append(kwargs)
        raise _CatalogStop


class _UsageByRows:
    """Analytics double: which configs have eval rows, and which have unmarked ones."""

    asked = []
    with_rows = ["config-1"]
    unmarked = []
    fails = False

    def get_eval_config_ids_for_candidates_ch(self, config_ids, **kwargs):
        type(self).asked.append(kwargs)
        if type(self).fails:
            raise RuntimeError("the eval usage read failed")
        return list(
            type(self).unmarked
            if kwargs.get("hide_simulated")
            else type(self).with_rows
        )


def _eval_fallback_read(monkeypatch, holds, *, with_rows, unmarked=(), fails=False):
    _UsageByRows.asked, _UsageByRows.fails = [], fails
    _UsageByRows.with_rows, _UsageByRows.unmarked = list(with_rows), list(unmarked)
    _TemplateQuery.asked = []
    _direct_catalog_pg_read(monkeypatch)
    monkeypatch.setattr(catalog_module, "holding_projects", holds)
    monkeypatch.setattr(catalog_module, "V2AnalyticsQueryService", _UsageByRows)
    monkeypatch.setattr(
        catalog_module,
        "CustomEvalConfig",
        SimpleNamespace(objects=_ConfigRows(["config-1", "config-2"])),
    )
    monkeypatch.setattr(
        "model_hub.models.evals_metric.EvalTemplate",
        SimpleNamespace(
            no_workspace_objects=_TemplateQuery(), objects=_TemplateQuery()
        ),
    )
    catalog_module.build_metrics_catalog(
        SimpleNamespace(id=WORKSPACE, organization_id="org"),
        project_ids_param=HOLDING,
        category="eval_metric",
        source="all",
        deadline=SimpleNamespace(remaining_ms=lambda *_args, **_kwargs: 1_000),
        _resolved_project_scope=([HOLDING], True),
    )


def test_the_fallback_of_a_holding_scope_leaves_out_a_config_with_only_marked_rows(
    monkeypatch,
):
    # config-1 has eval rows, all on marked traces; config-2 has never run. The
    # usage read finds nothing unmarked, so the list falls back to the configured
    # evals, which must not bring config-1 back.
    with pytest.raises(_CatalogStop):
        _eval_fallback_read(
            monkeypatch, _HoldsAmong(HOLDING), with_rows=["config-1"], unmarked=[]
        )

    assert [call.get("hide_simulated") for call in _UsageByRows.asked] == [True, None]
    assert _TemplateQuery.asked[-1]["id__in"] == ["config-2"]


def test_the_fallback_of_a_scope_that_does_not_hold_lists_every_configured_eval(
    monkeypatch,
):
    with pytest.raises(_CatalogStop):
        _eval_fallback_read(monkeypatch, _HoldsAmong(PLAIN), with_rows=[], unmarked=[])

    # One usage read, as it always was, and the whole configured set.
    assert len(_UsageByRows.asked) == 1
    assert _UsageByRows.asked[0]["hide_simulated"] is False
    assert _TemplateQuery.asked[-1]["id__in"] == ["config-1", "config-2"]


def test_a_holding_scope_whose_eval_usage_cannot_be_read_is_unavailable(monkeypatch):
    with pytest.raises(catalog_module.MetricsCatalogUnavailable) as caught:
        _eval_fallback_read(
            monkeypatch, _HoldsAmong(HOLDING), with_rows=[], unmarked=[], fails=True
        )

    assert caught.value.family == "eval_metrics"
    assert _TemplateQuery.asked == []


_EVAL_CATALOG_KEY = (
    f"dashboard:metrics_catalog:v6:{WORKSPACE}:{HOLDING}::0:1:eval_metric:"
)


def test_a_catalog_of_evals_is_cached_apart_when_its_scope_holds(monkeypatch):
    hiding = _catalog(monkeypatch, _HoldsAmong(HOLDING), category="eval_metric")
    plain = _catalog(monkeypatch, _HoldsAmong(), category="eval_metric")

    assert list(hiding.cache.store) == [f"{_EVAL_CATALOG_KEY}:hide_simulated"]
    assert list(plain.cache.store) == [_EVAL_CATALOG_KEY]
    assert hiding.built[0]["_hides_simulated"] is True
    assert plain.built[0]["_hides_simulated"] is False


def test_a_catalog_of_evals_that_reads_no_usage_does_not_ask(monkeypatch):
    holds = _HoldsAmong(HOLDING)

    catalog = _catalog(monkeypatch, holds, category="eval_metric", per_eval_config=True)

    assert holds.asked == []
    assert catalog.built[0]["_hides_simulated"] is None
    assert list(catalog.cache.store) == [
        f"dashboard:metrics_catalog:v6:{WORKSPACE}:{HOLDING}::1:1:eval_metric:"
    ]


# ─── annotation label list ───────────────────────────────────────────────────

MARKED_TRACES = {"trace-m1", "trace-m2"}
# (score id, label, trace, span): a score sits on a trace or on a span.
SCORES = [
    (1, "label-marked-only", "trace-m1", None),
    (2, "label-marked-only", "trace-m2", None),
    (3, "label-mixed", "trace-m1", None),
    (4, "label-mixed", "trace-ok", None),
    (5, "label-span-ok", None, "span-ok"),
    (6, "label-span-marked", None, "span-m"),
    (7, "label-orphan", None, "span-gone"),
]
SPAN_TRACES = {"span-ok": "trace-ok", "span-m": "trace-m1"}
SCORED_LABELS = [
    "label-marked-only",
    "label-mixed",
    "label-span-ok",
    "label-span-marked",
    "label-orphan",
]


class _Labels(list):
    def distinct(self):
        return _Labels(dict.fromkeys(self))


class _ScoreRows:
    """Score queryset double: the filters the label walk uses, over tuples.

    A row is (id, label, trace, span). A score with neither is a session score.
    """

    def __init__(self, rows, seen):
        self.rows = rows
        self.seen = seen

    def filter(self, *_conditions, **kwargs):
        rows = self.rows
        if "trace_id__isnull" in kwargs:
            rows = [row for row in rows if row[2] is None and row[3] is None]
        elif "tracer_project_id" in kwargs:
            # The label walk's own scope: a score on a trace or on a span.
            rows = [row for row in rows if row[2] is not None or row[3] is not None]
        if "label_id__in" in kwargs:
            rows = [row for row in rows if row[1] in kwargs["label_id__in"]]
        if "id__gt" in kwargs:
            rows = [row for row in rows if row[0] > kwargs["id__gt"]]
        if "tracer_project_id" in kwargs:
            self.seen.append(kwargs["tracer_project_id"])
        return _ScoreRows(rows, self.seen)

    def order_by(self, *_fields):
        return _ScoreRows(sorted(self.rows), self.seen)

    def values_list(self, *_fields, flat=False):
        if flat:
            return _Labels(row[1] for row in self.rows)
        return list(self.rows)

    @property
    def query(self):
        # Enough of a queryset for ``Exists`` to wrap it; nothing compiles it.
        return SimpleNamespace(clone=lambda: SimpleNamespace())


def _label_world(monkeypatch, *, scores=SCORES, labels=SCORED_LABELS):
    """Replace every store the label list reads; count each read it makes."""

    world = SimpleNamespace(
        label_reads=[],
        score_reads=[],
        mark_reads=[],
        span_reads=[],
        label_filters=[],
        result=object(),
        holds=_HoldsAmong(HOLDING),
    )

    class Source:
        @staticmethod
        def _trace_span_scope():
            return Q()

        def label_ids_for_project(self, project_id):
            world.label_reads.append(project_id)
            return list(labels)

    def marked(*, project_id, trace_ids, **_kwargs):
        world.mark_reads.append((project_id, list(trace_ids)))
        return frozenset(trace for trace in trace_ids if trace in MARKED_TRACES)

    def traces_of_spans(project_id, span_ids):
        world.span_reads.append((project_id, list(span_ids)))
        return {span: SPAN_TRACES[span] for span in span_ids if span in SPAN_TRACES}

    def label_filter(*args, **kwargs):
        world.label_filters.append((args, kwargs))
        return SimpleNamespace(distinct=lambda: world.result)

    monkeypatch.setattr(
        "tracer.services.annotation_label_source.AnnotationLabelScoresProjectPG",
        Source,
    )
    monkeypatch.setattr(
        "model_hub.models.score.Score",
        SimpleNamespace(
            no_workspace_objects=_ScoreRows(list(scores), world.score_reads)
        ),
    )
    monkeypatch.setattr(visibility_module, "marked_trace_ids", marked)
    monkeypatch.setattr(helper_module, "_trace_ids_of_spans", traces_of_spans)
    monkeypatch.setattr(helper_module, "holding_projects", world.holds)
    monkeypatch.setattr(helper_module.AnnotationsLabels.objects, "filter", label_filter)
    return world


def _offered(world):
    """The project owner and the labels found through scores, as the query got them."""

    (condition,), kwargs = world.label_filters[-1]
    assert kwargs == {"deleted": False}
    owner = dict(condition.children)
    return owner, set(owner["id__in"])


def test_a_holding_project_drops_a_label_whose_only_scores_are_on_marked_traces(
    monkeypatch,
):
    world = _label_world(monkeypatch)

    result = helper_module.get_annotation_labels_for_project(
        HOLDING, hide_simulated=True
    )

    owner, found = _offered(world)
    assert result is world.result
    # One unmarked score keeps a label, whether it sits on a trace or on a span.
    assert found == {"label-mixed", "label-span-ok", "label-orphan"}
    # A label attached to the project directly is not found through scores at
    # all: its own clause is still there, whatever its scores are.
    assert owner["project_id"] == HOLDING
    assert world.score_reads and set(world.score_reads) == {HOLDING}


def test_a_score_whose_span_has_no_live_row_cannot_be_shown_to_be_marked(monkeypatch):
    world = _label_world(monkeypatch, scores=[SCORES[-1]], labels=["label-orphan"])

    helper_module.get_annotation_labels_for_project(HOLDING, hide_simulated=True)

    assert _offered(world)[1] == {"label-orphan"}
    assert world.span_reads == [(HOLDING, ["span-gone"])]


def test_the_label_walk_reads_each_batch_once_and_stops_when_every_label_is_decided(
    monkeypatch,
):
    monkeypatch.setattr(helper_module, "_MARKED_SCORE_BATCH", 2)
    scores = [
        (1, "label-late", "trace-m1", None),
        (2, "label-late", "trace-m2", None),
        (3, "label-late", "trace-ok", None),
        (4, "label-never-read", "trace-ok", None),
    ]
    world = _label_world(monkeypatch, scores=scores, labels=["label-late"])

    helper_module.get_annotation_labels_for_project(HOLDING, hide_simulated=True)

    assert _offered(world)[1] == {"label-late"}
    assert world.mark_reads == [
        (HOLDING, ["trace-m1", "trace-m2"]),
        (HOLDING, ["trace-ok"]),
    ]


def test_a_label_walk_that_cannot_finish_fails_instead_of_dropping_the_label(
    monkeypatch,
):
    monkeypatch.setattr(helper_module, "_MARKED_SCORE_BATCH", 2)
    monkeypatch.setattr(helper_module, "_MARKED_SCORE_MAX_BATCHES", 1)
    scores = [
        (1, "label-long", "trace-m1", None),
        (2, "label-long", "trace-m2", None),
        (3, "label-long", "trace-ok", None),
    ]
    world = _label_world(monkeypatch, scores=scores, labels=["label-long"])

    with pytest.raises(MarkedLookupFailed, match="No answer was guessed"):
        helper_module.get_annotation_labels_for_project(HOLDING, hide_simulated=True)

    assert world.label_filters == []


def test_a_label_walk_that_cannot_finish_does_not_claim_every_score_was_simulated(
    monkeypatch,
):
    monkeypatch.setattr(helper_module, "_MARKED_SCORE_BATCH", 2)
    monkeypatch.setattr(helper_module, "_MARKED_SCORE_MAX_BATCHES", 1)
    # The first label is decided by an unmarked score inside the batch the walk
    # read, so the scores it read were not all on simulated traces.
    scores = [
        (1, "label-kept", "trace-ok", None),
        (2, "label-long", "trace-m1", None),
        (3, "label-long", "trace-m2", None),
    ]
    _label_world(monkeypatch, scores=scores, labels=["label-kept", "label-long"])

    with pytest.raises(MarkedLookupFailed) as caught:
        helper_module.get_annotation_labels_for_project(HOLDING, hide_simulated=True)

    message = str(caught.value)
    assert "none of the labels still in question" in message
    assert "every one was on a simulated trace" not in message


def test_a_failed_mark_lookup_fails_the_label_list(monkeypatch):
    world = _label_world(monkeypatch)

    def fails(**_kwargs):
        raise MarkedLookupFailed("the lookup timed out")

    monkeypatch.setattr(visibility_module, "marked_trace_ids", fails)

    with pytest.raises(MarkedLookupFailed, match="timed out"):
        helper_module.get_annotation_labels_for_project(HOLDING, hide_simulated=True)

    assert world.label_filters == []


def test_the_label_list_of_several_projects_checks_each_one(monkeypatch):
    world = _label_world(monkeypatch)
    checked = []
    monkeypatch.setattr(
        helper_module,
        "_label_ids_with_an_unmarked_score",
        lambda project_id, label_ids, *, holding: (
            checked.append((project_id, holding)) or ["label-mixed"]
        ),
    )

    helper_module.get_annotation_labels_for_project(
        None, project_ids=[HOLDING, PLAIN], hide_simulated=True
    )

    owner, found = _offered(world)
    # Which projects hold is asked once for the whole list and handed to each.
    assert checked == [(HOLDING, {HOLDING}), (PLAIN, {HOLDING})]
    assert world.holds.asked == [[HOLDING, PLAIN]]
    assert owner["project_id__in"] == [HOLDING, PLAIN]
    assert found == {"label-mixed"}


def test_the_label_list_of_a_project_that_does_not_hold_is_what_it_was(monkeypatch):
    plain = _label_world(monkeypatch)
    helper_module.get_annotation_labels_for_project(PLAIN)
    explicit = _label_world(monkeypatch)
    helper_module.get_annotation_labels_for_project(PLAIN, hide_simulated=False)

    for world in (plain, explicit):
        owner, found = _offered(world)
        assert owner["project_id"] == PLAIN
        assert found == set(SCORED_LABELS)
        # One read of the labels with scores, and no read of any score, mark or
        # span: the query count is the one it always was.
        assert world.label_reads == [PLAIN]
        assert world.score_reads == []
        assert world.mark_reads == []
        assert world.span_reads == []
        assert len(world.label_filters) == 1


def test_the_span_trace_lookup_is_one_bounded_statement_inside_the_project(
    monkeypatch,
):
    import tracer.services.clickhouse.v2.query_service as v2_service_module

    sent = []

    class Service:
        def execute_ch_query(self, query, params, **kwargs):
            sent.append((_flat(query), params, kwargs))
            return SimpleNamespace(data=[{"id": "span-1", "trace_id": "trace-1"}])

    monkeypatch.setattr(v2_service_module, "V2AnalyticsQueryService", Service)

    found = helper_module._trace_ids_of_spans(
        HOLDING, ["span-1", "span-2", "span-1", ""]
    )

    assert found == {"span-1": "trace-1"}
    [(query, params, kwargs)] = sent
    assert (
        "PREWHERE project_id = toUUID(%(project_id)s) AND id IN %(span_ids)s" in query
    )
    assert "HAVING argMax(is_deleted, _version) = 0" in query
    assert params == {"project_id": HOLDING, "span_ids": ("span-1", "span-2")}
    assert kwargs == {
        "server_execution_cap_ms": visibility_module.MARKED_LOOKUP_TIMEOUT_MS
    }
    assert helper_module._trace_ids_of_spans(HOLDING, ["", None]) == {}
    assert len(sent) == 1


def test_a_failed_span_trace_lookup_is_a_failed_mark_lookup(monkeypatch):
    import tracer.services.clickhouse.v2.query_service as v2_service_module

    class Service:
        def execute_ch_query(self, *_args, **_kwargs):
            raise RuntimeError("connection reset")

    monkeypatch.setattr(v2_service_module, "V2AnalyticsQueryService", Service)

    with pytest.raises(MarkedLookupFailed) as caught:
        helper_module._trace_ids_of_spans(HOLDING, ["span-1"])

    assert isinstance(caught.value.__cause__, RuntimeError)


def test_a_label_list_of_a_scope_walks_only_the_projects_that_hold(
    monkeypatch, kept_answers
):
    world = _label_world(monkeypatch)

    helper_module.get_annotation_labels_for_project(
        None, project_ids=[HOLDING, PLAIN], hide_simulated=True
    )

    # The project that does not hold has no marked trace: none of its scores,
    # traces or spans is read, and nothing is kept for it.
    assert set(world.score_reads) == {HOLDING}
    assert {project for project, _ in world.mark_reads} == {HOLDING}
    assert [key.split(":")[1] for key in kept_answers.store] == [HOLDING]


def test_a_label_list_asks_a_single_project_whether_it_holds_before_it_walks(
    monkeypatch,
):
    world = _label_world(monkeypatch)

    kept = helper_module._label_ids_with_an_unmarked_score(PLAIN, SCORED_LABELS)

    assert kept == SCORED_LABELS
    assert world.holds.asked == [[PLAIN]]
    assert world.score_reads == []
    assert world.mark_reads == []


def test_the_answer_of_a_label_walk_is_kept_per_project_and_hiding_decision(
    monkeypatch, kept_answers
):
    world = _label_world(monkeypatch)

    first = helper_module.get_annotation_labels_for_project(
        HOLDING, hide_simulated=True
    )
    reads = (len(world.score_reads), len(world.mark_reads), len(world.span_reads))
    second = helper_module.get_annotation_labels_for_project(
        HOLDING, hide_simulated=True
    )

    assert first is second is world.result
    assert reads[0] and reads[1]
    # The second list page of a holding project reads no score, mark or span.
    assert (
        len(world.score_reads),
        len(world.mark_reads),
        len(world.span_reads),
    ) == reads
    [key] = kept_answers.store
    assert key.startswith(f"annotation_labels_unmarked:{HOLDING}:")
    assert key.endswith(":hide_simulated")
    assert kept_answers.timeouts[key] == 120
    assert set(kept_answers.store[key]) == {
        "label-mixed",
        "label-span-ok",
        "label-orphan",
    }


def test_a_label_walk_that_failed_keeps_nothing(monkeypatch, kept_answers):
    _label_world(monkeypatch)

    def fails(**_kwargs):
        raise MarkedLookupFailed("the lookup timed out")

    monkeypatch.setattr(visibility_module, "marked_trace_ids", fails)

    with pytest.raises(MarkedLookupFailed):
        helper_module.get_annotation_labels_for_project(HOLDING, hide_simulated=True)

    assert kept_answers.store == {}


# A label scored on a session keeps its place whatever its trace scores are:
# a session is never simulated. These scores sit on no trace and no span.
SESSION_SCORES = [
    *SCORES,
    (8, "label-session", "trace-m1", None),
    (9, "label-session", "trace-m2", None),
    (10, "label-session", None, None),
    (11, "label-session-only", None, None),
]
# Labels with a trace or span score; "label-session-only" has none.
TRACE_SCORED_LABELS = [*SCORED_LABELS, "label-session"]


SESSION_WORLD = {"scores": SESSION_SCORES, "labels": TRACE_SCORED_LABELS}


def test_a_list_view_keeps_a_label_with_a_session_score_without_a_lookup(monkeypatch):
    world = _label_world(monkeypatch, **SESSION_WORLD)

    helper_module.get_annotation_labels_for_project(HOLDING, hide_simulated=True)

    _, found = _offered(world)
    assert "label-session" in found
    assert "label-marked-only" not in found
    # Its marked trace scores never reached the lookup of marked traces.
    assert [traces for _, traces in world.mark_reads] == [
        ["trace-m1", "trace-m2", "trace-m1", "trace-ok", "trace-ok", "trace-m1"]
    ]


def test_a_list_view_of_several_projects_keeps_a_label_with_a_session_score(
    monkeypatch,
):
    world = _label_world(monkeypatch, **SESSION_WORLD)

    helper_module.get_annotation_labels_for_project(
        None, project_ids=[HOLDING], hide_simulated=True
    )

    assert "label-session" in _offered(world)[1]


def test_the_paged_catalog_keeps_a_label_with_a_session_score(monkeypatch):
    _, _, query, _ = _wire_label_page(
        monkeypatch, _HoldsAmong(HOLDING), **SESSION_WORLD
    )

    _build_label_page(HOLDING, include_custom_attributes=False)

    owner = dict(query.filters[-1][0].children)
    assert "label-session" in owner["id__in"]
    assert "label-marked-only" not in owner["id__in"]


def test_the_unpaged_catalog_keeps_a_label_with_a_session_score(monkeypatch):
    _, _, query = _wire_label_catalog(
        monkeypatch, _HoldsAmong(HOLDING), **SESSION_WORLD
    )

    with pytest.raises(_CatalogStop):
        _build_label_catalog(HOLDING)

    owner = dict(query.filters[-1][0].children)
    assert "label-session" in owner["id__in"]
    assert "label-marked-only" not in owner["id__in"]


def test_the_picker_keeps_a_label_with_a_session_score_and_one_with_nothing_else(
    monkeypatch,
):
    _, labels, _ = _picker_label_condition(
        monkeypatch, _HoldsAmong(HOLDING), HOLDING, **SESSION_WORLD
    )

    owner, _ = _kept_by(labels)
    # One with trace scores and a session score, and one scored only on a
    # session, which no trace-scoped list would name at all.
    assert {"label-session", "label-session-only"} <= owner["id__in"]
    assert "label-marked-only" not in owner["id__in"]


# ─── callers of the label list ───────────────────────────────────────────────


@pytest.mark.parametrize("project", [HOLDING, PLAIN])
@pytest.mark.parametrize(
    ("resolve", "call", "resolver"),
    [
        ("resolve_filtered_trace_ids", {}, "_resolve_trace_ids_clickhouse"),
        (
            "resolve_filtered_trace_ids",
            {"is_voice_call": True},
            "_resolve_voice_call_ids_clickhouse",
        ),
        ("resolve_filtered_span_ids", {}, "_resolve_span_ids_clickhouse"),
    ],
    ids=["trace", "voice", "span"],
)
def test_a_bulk_selection_decides_once_for_its_labels_and_its_resolver(
    monkeypatch, project, resolve, call, resolver
):
    holds = _Holds(HOLDING)
    label_reads = []
    resolved = []
    monkeypatch.setattr(bulk_selection_module, "project_hides_simulated", holds)
    monkeypatch.setattr(
        bulk_selection_module,
        "Project",
        SimpleNamespace(
            objects=SimpleNamespace(get=lambda **_kwargs: SimpleNamespace(id=project))
        ),
    )
    monkeypatch.setattr(
        bulk_selection_module, "_project_matches_workspace", lambda *_args: True
    )
    monkeypatch.setattr(
        bulk_selection_module,
        "get_annotation_labels_for_project",
        lambda *_args, **kwargs: label_reads.append(kwargs) or [],
    )
    monkeypatch.setattr(
        bulk_selection_module,
        resolver,
        lambda **kwargs: resolved.append(kwargs) or "resolved",
    )

    getattr(bulk_selection_module, resolve)(
        project_id=project, filters=[], organization=object(), **call
    )

    hides = project == HOLDING
    assert holds.asked == [project]
    assert label_reads == [{"hide_simulated": hides}]
    assert [kwargs["hide_simulated"] for kwargs in resolved] == [hides]


# Every call of the label list in the list views, found in the source. A view
# that lists marked traces or spans decides once and hands the decision to its
# label list and its builder. The calls left alone read the legacy Postgres
# span tables or only look a row up by its position; a new call has to be
# decided here.
_LABEL_LIST = "get_annotation_labels_for_project"
_VIEWS = Path(trace_view.__file__).parent
_DECIDED_CALLERS = {
    "trace.py": {
        "TraceView._list_traces_of_session_clickhouse",
        "TraceView._list_voice_calls_clickhouse",
        "TraceView._list_traces_clickhouse",
    },
    "observation_span.py": {
        "ObservationSpanView._list_spans_clickhouse",
        "ObservationSpanView._list_spans_non_observe_clickhouse",
    },
}
_LEFT_ALONE_CALLERS = {
    "trace.py": {"TraceView.get_trace_id_by_index"},
    "observation_span.py": {
        "ObservationSpanView.get_trace_id_by_index_spans_as_base",
        "ObservationSpanView.get_trace_id_by_index_spans_as_observe",
    },
}


def _label_list_calls(path, name=_LABEL_LIST):
    """``{qualname: (call, function)}`` for each call of the named label read."""

    found = {}

    def walk(node, stack):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                walk(child, [*stack, child])
                continue
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == name
            ):
                found[".".join(item.name for item in stack)] = (child, stack[-1])
            walk(child, stack)

    walk(ast.parse(path.read_text(encoding="utf-8")), [])
    return found


@pytest.mark.parametrize("view", sorted(_DECIDED_CALLERS))
def test_every_label_list_in_a_list_view_gets_the_decision_made_once(view):
    calls = _label_list_calls(_VIEWS / view)

    assert set(calls) == _DECIDED_CALLERS[view] | _LEFT_ALONE_CALLERS[view]
    for qualname in _DECIDED_CALLERS[view]:
        call, function = calls[qualname]
        passed = {keyword.arg: keyword.value for keyword in call.keywords}
        assert isinstance(passed.get("hide_simulated"), ast.Name), qualname
        assert passed["hide_simulated"].id == "hide_simulated", qualname
        decisions = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Assign)
            and [ast.unparse(target) for target in node.targets] == ["hide_simulated"]
        ]
        assert len(decisions) == 1, f"{qualname} decides more than once"
        assert decisions[0].lineno < call.lineno, qualname
        # Decided from the holding flag, never fixed.
        assert "hides_simulated(" in ast.unparse(decisions[0]) or (
            "holding_projects(" in ast.unparse(decisions[0])
        ), qualname
    for qualname in _LEFT_ALONE_CALLERS[view]:
        assert not calls[qualname][0].keywords, qualname


# ─── metrics catalog: which labels have scores ───────────────────────────────

_LABEL_CATALOG_KEY = (
    f"dashboard:metrics_catalog:v6:{WORKSPACE}:{HOLDING}::0:1:annotation_metric:"
)


class _LabelQuery:
    """The catalog's label queryset double: remembers each filter, then stops."""

    def __init__(self):
        self.filters = []

    def filter(self, *conditions, **_kwargs):
        self.filters.append(conditions)
        return self

    def distinct(self):
        return self

    def order_by(self, *_fields):
        return self

    def values(self, *_fields):
        raise _CatalogStop


def _wire_label_catalog(monkeypatch, holds, **world):
    import tracer.services.annotation_label_source as label_source_module

    world = _label_world(monkeypatch, **world)
    families = _direct_catalog_pg_read(monkeypatch)
    query = _LabelQuery()
    monkeypatch.setattr(catalog_module, "holding_projects", holds)
    monkeypatch.setattr(
        catalog_module,
        "AnnotationLabelScoresProjectPG",
        label_source_module.AnnotationLabelScoresProjectPG,
    )
    monkeypatch.setattr(
        "model_hub.models.develop_annotations.AnnotationsLabels",
        SimpleNamespace(no_workspace_objects=query),
    )
    return world, families, query


def _build_label_catalog(project, **kwargs):
    catalog_module.build_metrics_catalog(
        SimpleNamespace(id=WORKSPACE, organization="org"),
        project_ids_param=project,
        category="annotation_metric",
        source="traces",
        deadline=SimpleNamespace(remaining_ms=lambda *_args, **_kwargs: 1_000),
        _resolved_project_scope=([project], True),
        **kwargs,
    )


def _label_usage_read(monkeypatch, holds, project, **kwargs):
    world, families, query = _wire_label_catalog(monkeypatch, holds)
    with pytest.raises(_CatalogStop):
        _build_label_catalog(project, **kwargs)
    return world, families, dict(query.filters[-1][0].children)


def test_a_holding_scope_lists_no_label_whose_only_scores_are_on_marked_traces(
    monkeypatch,
):
    holds = _HoldsAmong(HOLDING)

    world, families, owner = _label_usage_read(monkeypatch, holds, HOLDING)

    assert holds.asked == [[HOLDING]]
    assert families == ["project_scope", "annotation_metrics"]
    assert owner["id__in"] == {"label-mixed", "label-span-ok", "label-orphan"}
    # A label attached to the project directly is still listed.
    assert owner["project_id__in"] == [HOLDING]
    assert world.mark_reads


def test_a_scope_that_does_not_hold_lists_the_labels_it_always_did(monkeypatch):
    holds = _HoldsAmong(HOLDING)

    world, families, owner = _label_usage_read(monkeypatch, holds, PLAIN)

    assert holds.asked == [[PLAIN]]
    assert families == ["project_scope", "annotation_metrics"]
    assert owner["id__in"] == set(SCORED_LABELS)
    assert owner["project_id__in"] == [PLAIN]
    assert world.label_reads == [PLAIN]
    # No score, mark or span is read: the query count is the one it always was.
    assert world.score_reads == []
    assert world.mark_reads == []
    assert world.span_reads == []


def test_a_catalog_given_an_answer_does_not_ask_again_for_labels(monkeypatch):
    holds = _HoldsAmong(HOLDING)

    _, families, owner = _label_usage_read(
        monkeypatch, holds, HOLDING, _hides_simulated=True
    )

    assert holds.asked == []
    assert families == ["annotation_metrics"]
    assert "label-marked-only" not in owner["id__in"]


def test_a_catalog_that_cannot_tell_which_traces_are_marked_fails_instead_of_listing(
    monkeypatch,
):
    def fails(**_kwargs):
        raise MarkedLookupFailed("the lookup timed out")

    _, _, query = _wire_label_catalog(monkeypatch, _HoldsAmong(HOLDING))
    monkeypatch.setattr(visibility_module, "marked_trace_ids", fails)

    with pytest.raises(catalog_module.MetricsCatalogUnavailable) as caught:
        _build_label_catalog(HOLDING)

    assert caught.value.family == "annotation_metrics"
    assert isinstance(caught.value.__cause__, MarkedLookupFailed)
    assert query.filters == []


def test_a_catalog_of_labels_is_cached_apart_when_its_scope_holds(monkeypatch):
    hiding = _catalog(monkeypatch, _HoldsAmong(HOLDING), category="annotation_metric")
    plain = _catalog(monkeypatch, _HoldsAmong(), category="annotation_metric")

    assert list(hiding.cache.store) == [f"{_LABEL_CATALOG_KEY}:hide_simulated"]
    assert list(plain.cache.store) == [_LABEL_CATALOG_KEY]
    assert hiding.built[0]["_hides_simulated"] is True
    assert plain.built[0]["_hides_simulated"] is False


class _ExistsMarker(Q):
    """Stands for the EXISTS subquery of the paged label read, which a double
    queryset cannot be handed to the real ``Exists``."""

    def __init__(self, subquery):
        super().__init__(("exists_subquery", subquery))


def _wire_label_page(monkeypatch, holds, **world):
    world, families, query = _wire_label_catalog(monkeypatch, holds, **world)
    built = []
    monkeypatch.setattr("django.db.models.Exists", _ExistsMarker)
    monkeypatch.setattr(
        catalog_module,
        "_catalog_family_requested",
        lambda *, family_category, **_kwargs: (
            family_category in ("annotation_metric", "custom_attribute")
        ),
    )
    monkeypatch.setattr(
        catalog_module,
        "_resolve_metrics_catalog_project_scope",
        lambda _workspace, project_ids_param, **_kwargs: ([project_ids_param], True),
    )
    monkeypatch.setattr(
        catalog_module,
        "_catalog_search_and_order",
        lambda queryset, **_kwargs: queryset,
    )
    monkeypatch.setattr(
        catalog_module,
        "_queryset_catalog_family",
        lambda **kwargs: kwargs["queryset"],
    )
    monkeypatch.setattr(
        catalog_module, "_run_metrics_catalog_pg_snapshot", lambda _d, read: read()
    )
    monkeypatch.setattr(
        catalog_module, "_paginate_catalog_families", lambda *_a, **_k: ([], 0, False)
    )
    monkeypatch.setattr(
        catalog_module,
        "build_metrics_catalog",
        lambda *_args, **kwargs: built.append(kwargs) or [],
    )
    return world, families, query, built


def _build_label_page(project, **kwargs):
    return catalog_module.build_metrics_catalog_page(
        SimpleNamespace(id=WORKSPACE, organization="org"),
        page=1,
        page_size=10,
        project_ids_param=project,
        deadline=SimpleNamespace(remaining_ms=lambda *_args, **_kwargs: 1_000),
        **kwargs,
    )


def test_a_holding_scope_pages_over_the_labels_with_an_unmarked_score(monkeypatch):
    holds = _HoldsAmong(HOLDING)
    world, families, query, _ = _wire_label_page(monkeypatch, holds)

    _build_label_page(HOLDING, include_custom_attributes=False)

    owner = dict(query.filters[-1][0].children)
    assert holds.asked == [[HOLDING]]
    assert families == ["project_scope", "annotation_metrics"]
    assert owner["id__in"] == {"label-mixed", "label-span-ok", "label-orphan"}
    assert owner["project_id__in"] == [HOLDING]
    # The count and the page rows come from the one queryset, so no EXISTS
    # subquery is left beside the id set.
    assert "exists_subquery" not in owner
    assert world.mark_reads


def test_a_scope_that_does_not_hold_pages_over_the_same_subquery_as_before(
    monkeypatch,
):
    holds = _HoldsAmong(HOLDING)
    world, families, query, _ = _wire_label_page(monkeypatch, holds)

    _build_label_page(PLAIN, include_custom_attributes=False)

    owner = dict(query.filters[-1][0].children)
    assert holds.asked == [[PLAIN]]
    assert families == ["project_scope"]
    assert owner["project_id__in"] == [PLAIN]
    assert "exists_subquery" in owner
    assert "id__in" not in owner
    assert world.label_reads == []
    assert world.mark_reads == []
    assert world.span_reads == []


def test_a_page_that_cannot_tell_which_traces_are_marked_fails_instead_of_listing(
    monkeypatch,
):
    def fails(**_kwargs):
        raise MarkedLookupFailed("the lookup timed out")

    _, _, query, _ = _wire_label_page(monkeypatch, _HoldsAmong(HOLDING))
    monkeypatch.setattr(visibility_module, "marked_trace_ids", fails)

    with pytest.raises(catalog_module.MetricsCatalogUnavailable) as caught:
        _build_label_page(HOLDING, include_custom_attributes=False)

    assert caught.value.family == "annotation_metrics"
    assert isinstance(caught.value.__cause__, MarkedLookupFailed)
    # Only the base label query was built; the scope filter never was.
    assert len(query.filters) == 1


def test_a_page_that_lists_labels_and_attribute_keys_asks_which_projects_hold_once(
    monkeypatch,
):
    holds = _HoldsAmong(HOLDING)
    _, _, _, built = _wire_label_page(monkeypatch, holds)

    _build_label_page(HOLDING)

    assert holds.asked == [[HOLDING]]
    assert [call["_hides_simulated"] for call in built] == [True]


# ─── annotation labels in the cursor-mode property picker ────────────────────

ORGANIZATION = "00000000-0000-4000-8000-000000000001"


def _picker_scope(*projects):
    return {
        "organization_id": ORGANIZATION,
        "workspace_id": WORKSPACE,
        "project_ids": tuple(projects),
        "agent_definition_id": "",
        "dataset_id": "",
    }


def _picker_label_condition(
    monkeypatch, holds, *projects, query=None, build=1, marked=None, **world
):
    """The condition the picker keeps a label by, as its label query got it."""
    from tracer.services.clickhouse.v2.property_catalog import (
        source_adapters as adapters_module,
    )

    world = _label_world(monkeypatch, **world)
    if marked is not None:
        monkeypatch.setattr(visibility_module, "marked_trace_ids", marked)
    labels = _LabelQuery()
    monkeypatch.setattr(adapters_module, "holding_projects", holds)
    monkeypatch.setattr(
        "model_hub.models.develop_annotations.AnnotationsLabels",
        SimpleNamespace(no_workspace_objects=labels),
    )
    source = adapters_module.CurrentDefinitionSource(
        SimpleNamespace(remaining_ms=lambda *_args, **_kwargs: 1_000)
    )
    monkeypatch.setattr(source, "_read", lambda read: read())
    query = query or {"category": "annotation_metric", "source": "traces"}
    for _ in range(build):
        list(source._families(_picker_scope(*projects), query))
    return world, labels, source


def _kept_by(labels):
    (condition,) = labels.filters[-1]
    return (
        dict(child for child in condition.children if isinstance(child, tuple)),
        [child for child in condition.children if isinstance(child, Exists)],
    )


def test_a_holding_scope_lists_no_label_in_the_picker_whose_only_scores_are_marked(
    monkeypatch,
):
    holds = _HoldsAmong(HOLDING)

    world, labels, _ = _picker_label_condition(monkeypatch, holds, HOLDING)

    owner, subqueries = _kept_by(labels)
    assert holds.asked == [[HOLDING]]
    assert owner["id__in"] == {"label-mixed", "label-span-ok", "label-orphan"}
    # A label attached to the project directly is still listed, and no EXISTS
    # subquery is left beside the id set, which would list every scored label.
    assert owner["project_id__in"] == (HOLDING,)
    assert subqueries == []
    assert world.mark_reads


def test_a_scope_that_does_not_hold_lists_the_picker_labels_it_always_did(
    monkeypatch,
):
    holds = _HoldsAmong(HOLDING)

    world, labels, _ = _picker_label_condition(monkeypatch, holds, PLAIN)

    owner, subqueries = _kept_by(labels)
    assert holds.asked == [[PLAIN]]
    assert owner == {"project_id__in": (PLAIN,)}
    assert len(subqueries) == 1
    # No score, mark or span is read: the query count is the one it always was.
    assert world.label_reads == []
    assert world.score_reads == []
    assert world.mark_reads == []
    assert world.span_reads == []


def test_a_picker_read_that_drops_the_label_family_does_not_ask_or_walk(monkeypatch):
    holds = _HoldsAmong(HOLDING)

    world, _, _ = _picker_label_condition(
        monkeypatch,
        holds,
        HOLDING,
        query={"category": "system_metric", "source": "traces"},
    )

    assert holds.asked == []
    assert world.score_reads == []
    assert world.mark_reads == []


def test_a_picker_asks_and_walks_once_for_its_counts_and_its_page(monkeypatch):
    holds = _HoldsAmong(HOLDING)

    world, _, _ = _picker_label_condition(monkeypatch, holds, HOLDING, build=2)

    assert holds.asked == [[HOLDING]]
    assert len(world.label_reads) == 1


def test_a_picker_that_cannot_tell_which_traces_are_marked_is_unavailable(monkeypatch):
    from tracer.services.clickhouse.v2.property_catalog.reader import (
        PropertyCatalogUnavailable,
    )

    def fails(**_kwargs):
        raise MarkedLookupFailed("the lookup timed out")

    with pytest.raises(PropertyCatalogUnavailable) as caught:
        _picker_label_condition(
            monkeypatch, _HoldsAmong(HOLDING), HOLDING, marked=fails
        )

    assert caught.value.reason == "marked_lookup_failed"
    assert isinstance(caught.value.__cause__, MarkedLookupFailed)


# ─── annotation labels of several projects ───────────────────────────────────

OWNED_LABEL = SimpleNamespace(id="label-owned", project_id=HOLDING)


def _labels_by_project_world(monkeypatch, by_project):
    """The labels of several projects: what each reads and what the query is asked."""

    world = _label_world(monkeypatch)
    universe = [
        SimpleNamespace(id=label, project_id=None)
        for label in dict.fromkeys(
            label for labels in by_project.values() for label in labels
        )
    ] + [OWNED_LABEL]
    world.project_reads = []

    class Source:
        @staticmethod
        def _trace_span_scope():
            return Q()

        def label_ids_by_project(self, project_ids):
            world.project_reads.append(list(project_ids))
            return {project: list(by_project[project]) for project in project_ids}

    def label_filter(condition, **kwargs):
        world.label_filters.append(((condition,), kwargs))
        owner = dict(condition.children)
        kept = [
            label
            for label in universe
            if label.id in owner["id__in"]
            or label.project_id in owner["project_id__in"]
        ]
        return SimpleNamespace(distinct=lambda: kept)

    monkeypatch.setattr(
        "tracer.services.annotation_label_source.AnnotationLabelScoresProjectPG",
        Source,
    )
    monkeypatch.setattr(helper_module.AnnotationsLabels.objects, "filter", label_filter)
    return world


def _ids_by_project(result):
    return {
        project: sorted(label.id for label in labels)
        for project, labels in result.items()
    }


_TWO_PROJECTS = {
    HOLDING: ["label-marked-only", "label-mixed"],
    PLAIN: ["label-span-ok", "label-marked-only"],
}


def test_a_hiding_read_of_several_projects_drops_marked_only_labels_in_each(
    monkeypatch,
):
    world = _labels_by_project_world(monkeypatch, _TWO_PROJECTS)

    result = helper_module.get_annotation_labels_by_project(
        [HOLDING, PLAIN], hide_simulated=True
    )

    # A label with an unmarked score stays under its own project; the label of
    # only marked scores goes from the project that holds; a label the project
    # owns stays whatever its scores are. A project that does not hold has no
    # marked trace, so it keeps every label and no score of it is read.
    assert _ids_by_project(result) == {
        HOLDING: ["label-mixed", "label-owned"],
        PLAIN: ["label-marked-only", "label-span-ok"],
    }
    assert world.project_reads == [[HOLDING, PLAIN]]
    assert {project for project, _ in world.mark_reads} == {HOLDING}
    assert set(world.score_reads) == {HOLDING}
    assert len(world.label_filters) == 1


@pytest.mark.parametrize("passed", [{}, {"hide_simulated": False}])
def test_a_read_of_several_projects_that_do_not_hold_is_what_it_was(
    monkeypatch, passed
):
    world = _labels_by_project_world(monkeypatch, _TWO_PROJECTS)

    result = helper_module.get_annotation_labels_by_project([HOLDING, PLAIN], **passed)

    assert _ids_by_project(result) == {
        HOLDING: ["label-marked-only", "label-mixed", "label-owned"],
        PLAIN: ["label-marked-only", "label-span-ok"],
    }
    # One read of the labels with scores and one of the labels: no read of any
    # score, mark or span.
    assert world.project_reads == [[HOLDING, PLAIN]]
    assert world.score_reads == []
    assert world.mark_reads == []
    assert world.span_reads == []
    assert len(world.label_filters) == 1


def test_a_failed_mark_lookup_fails_the_read_of_several_projects(monkeypatch):
    world = _labels_by_project_world(monkeypatch, _TWO_PROJECTS)

    def fails(**_kwargs):
        raise MarkedLookupFailed("the lookup timed out")

    monkeypatch.setattr(visibility_module, "marked_trace_ids", fails)

    with pytest.raises(MarkedLookupFailed, match="timed out"):
        helper_module.get_annotation_labels_by_project(
            [HOLDING, PLAIN], hide_simulated=True
        )

    assert world.label_filters == []


_LABELS_BY_PROJECT = "get_annotation_labels_by_project"
# Listing views read the labels of every project of an organization-wide read
# with the decision they made for the request. The session list is not hidden
# this week and keeps its call as it was.
_DECIDED_BY_PROJECT = {
    "trace.py": {"TraceView._list_traces_of_session_clickhouse"},
    "observation_span.py": {"ObservationSpanView._list_spans_clickhouse"},
}
_BY_PROJECT_LEFT_ALONE = {"trace_session.py": {"TraceSessionView._select_session_page"}}


@pytest.mark.parametrize(
    "view", sorted({*_DECIDED_BY_PROJECT, *_BY_PROJECT_LEFT_ALONE})
)
def test_every_read_of_several_projects_in_a_list_view_gets_the_decision(view):
    calls = _label_list_calls(_VIEWS / view, _LABELS_BY_PROJECT)
    decided = _DECIDED_BY_PROJECT.get(view, set())
    left_alone = _BY_PROJECT_LEFT_ALONE.get(view, set())

    assert set(calls) == decided | left_alone
    for qualname in decided:
        call, function = calls[qualname]
        passed = {keyword.arg: keyword.value for keyword in call.keywords}
        assert isinstance(passed.get("hide_simulated"), ast.Name), qualname
        assert passed["hide_simulated"].id == "hide_simulated", qualname
        decisions = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Assign)
            and [ast.unparse(target) for target in node.targets] == ["hide_simulated"]
        ]
        assert len(decisions) == 1, f"{qualname} decides more than once"
        assert decisions[0].lineno < call.lineno, qualname
        assert "hides_simulated(" in ast.unparse(decisions[0]) or (
            "holding_projects(" in ast.unparse(decisions[0])
        ), qualname
    for qualname in left_alone:
        assert not calls[qualname][0].keywords, qualname


@pytest.mark.parametrize("hides", [True, False])
def test_a_dashboard_hands_its_decision_to_the_labels_it_binds(monkeypatch, hides):
    asked = []
    monkeypatch.setattr(
        dashboard_view, "_run_filter_value_pg_read", lambda _deadline, read: read()
    )
    monkeypatch.setattr(
        dashboard_view,
        "get_annotation_labels_by_project",
        lambda project_ids, **kwargs: asked.append(kwargs) or {HOLDING: []},
    )
    config = {
        "project_ids": [HOLDING],
        "filters": [{"metric_type": "system_metric", "metric_name": "has_annotation"}],
        "metrics": [],
    }

    bound = dashboard_view._bind_dashboard_annotation_completeness(
        config,
        SimpleNamespace(organization="organization"),
        deadline=None,
        allow_metadata_read=True,
        hide_simulated=hides,
    )

    assert asked == [{"organization": "organization", "hide_simulated": hides}]
    assert bound["annotation_label_ids_by_project"] == {HOLDING: []}


def test_a_dashboard_decides_before_it_binds_labels_and_passes_the_decision():
    path = Path(dashboard_view.__file__)
    calls = _label_list_calls(path, "_bind_dashboard_annotation_completeness")

    assert list(calls) == ["DashboardWidgetViewSet._execute_ch_query_config"]
    call, function = calls["DashboardWidgetViewSet._execute_ch_query_config"]
    passed = {keyword.arg: keyword.value for keyword in call.keywords}
    assert isinstance(passed.get("hide_simulated"), ast.Name)
    assert passed["hide_simulated"].id == "hide_simulated"
    decisions = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Assign)
        and [ast.unparse(target) for target in node.targets] == ["hide_simulated"]
    ]
    assert len(decisions) == 1
    assert decisions[0].lineno < call.lineno
    assert "_scope_holds_simulated(" in ast.unparse(decisions[0])


# ─── the label sets that complete a has_annotation filter ────────────────────

_HAS_ANNOTATION = {
    "column_id": "has_annotation",
    "filter_config": {"filter_op": "equals", "filter_value": True},
}
_GRAPH_READS = Path(exact_reads_module.__file__)
_ROW_RESOLVER = Path(row_resolver_module.__file__)
_GRAPH_DISPATCH = Path(graph_dispatch_module.__file__)


def _label_set_readers(monkeypatch, module):
    """Replace the label list the completeness set is built from; record its calls."""

    calls = []

    def labels(project_id, **kwargs):
        calls.append((project_id, kwargs))
        return [SimpleNamespace(id="label-b"), SimpleNamespace(id="label-a")]

    monkeypatch.setattr(module, "get_annotation_labels_for_project", labels)
    return calls


@pytest.mark.parametrize(
    ("hide", "sent"),
    [
        ({"hide_simulated": True}, {"hide_simulated": True}),
        ({}, {}),
        ({"hide_simulated": False}, {}),
    ],
    ids=["holding", "omitted", "plain"],
)
def test_a_graph_label_set_is_built_from_the_labels_its_list_shows(
    monkeypatch, hide, sent
):
    calls = _label_set_readers(monkeypatch, exact_reads_module)

    labels = exact_reads_module._annotation_label_ids_for_filters(
        HOLDING, [_HAS_ANNOTATION], **hide
    )

    assert labels == ("label-a", "label-b")
    # A project that does not hold makes the very call it made before.
    assert calls == [(HOLDING, sent)]


def test_a_graph_label_set_reads_nothing_without_a_completeness_filter(monkeypatch):
    calls = _label_set_readers(monkeypatch, exact_reads_module)

    assert (
        exact_reads_module._annotation_label_ids_for_filters(
            HOLDING, [], hide_simulated=True
        )
        is None
    )
    assert calls == []


def test_a_graph_label_set_that_cannot_be_decided_fails_the_refresh(monkeypatch):
    def fails(*_args, **_kwargs):
        raise MarkedLookupFailed("the lookup timed out")

    monkeypatch.setattr(exact_reads_module, "get_annotation_labels_for_project", fails)

    with pytest.raises(
        exact_reads_module.ExactGraphReadError, match="temporarily unavailable"
    ):
        exact_reads_module._annotation_label_ids_for_filters(
            HOLDING, [_HAS_ANNOTATION], hide_simulated=True
        )


@pytest.mark.parametrize(
    ("hide", "sent"), [(True, {"hide_simulated": True}), (False, {})]
)
def test_a_membership_filter_builds_its_label_set_under_the_decision(
    monkeypatch, hide, sent
):
    asked = []
    monkeypatch.setattr(
        exact_reads_module,
        "_annotation_label_ids_for_filters",
        lambda project_id, filters, **kwargs: asked.append(kwargs) or ("label-a",),
    )

    exact_reads_module._compile_membership_filter(
        project_id=HOLDING,
        filters=[_HAS_ANNOTATION],
        observe_type="trace",
        hide_simulated=hide,
    )

    assert asked == [sent]


@pytest.mark.parametrize(
    ("hide", "sent"),
    [
        ({"hide_simulated": True}, {"hide_simulated": True}),
        ({}, {}),
        ({"hide_simulated": False}, {}),
    ],
    ids=["holding", "omitted", "plain"],
)
def test_an_eval_task_label_set_is_built_from_the_labels_its_list_shows(
    monkeypatch, hide, sent
):
    calls = _label_set_readers(monkeypatch, helper_module)

    labels = row_resolver_module._annotation_label_ids_for_filters(
        HOLDING, [_HAS_ANNOTATION], **hide
    )

    assert labels == ("label-b", "label-a")
    assert calls == [(HOLDING, sent)]


def test_an_eval_task_label_set_that_cannot_be_decided_stops_the_selection(
    monkeypatch,
):
    def fails(*_args, **_kwargs):
        raise MarkedLookupFailed("the lookup timed out")

    monkeypatch.setattr(helper_module, "get_annotation_labels_for_project", fails)

    with pytest.raises(row_resolver_module.EvalTaskReadBudgetExceeded):
        row_resolver_module._annotation_label_ids_for_filters(
            HOLDING, [_HAS_ANNOTATION], hide_simulated=True
        )


# Every build of a completeness label set, found in the source. A reader that
# hides hands its own decision to the build, in the one spelling that adds
# nothing for a project that does not hold. The session and user charts are not
# hidden this week and build their sets as before.
_LABEL_SET = "_annotation_label_ids_for_filters"
_DECIDED_LABEL_SETS = {
    _GRAPH_READS: {
        "read_exact_system_graph",
        "read_exact_agent_graph",
        "read_exact_all_system_metrics",
        "read_exact_eval_graph",
        "_compile_membership_filter",
        "read_exact_annotation_graph",
    },
    _GRAPH_DISPATCH: {"_fetch_direct_raw_system_metric_graph"},
    _ROW_RESOLVER: {
        "_resolve_continuous_rows",
        "_resolve_bounded_historical_span_ids",
    },
}
_UNHIDDEN_LABEL_SETS = {
    _GRAPH_READS: {"_session_membership_plan", "_user_filter_clauses"},
    _GRAPH_DISPATCH: set(),
    _ROW_RESOLVER: set(),
}


def _passes_the_decision(call):
    splats = [keyword.value for keyword in call.keywords if keyword.arg is None]
    return [
        ast.unparse(splat)
        for splat in splats
        if isinstance(splat, ast.Call)
        and isinstance(splat.func, ast.Name)
        and splat.func.id == "_hide_simulated_kwargs"
    ] == ["_hide_simulated_kwargs(hide_simulated)"]


@pytest.mark.parametrize(
    "path", sorted(_DECIDED_LABEL_SETS, key=str), ids=lambda p: p.name
)
def test_every_build_of_a_label_set_gets_the_decision_of_its_read(path):
    calls = _label_list_calls(path, _LABEL_SET)
    decided = _DECIDED_LABEL_SETS[path]
    unhidden = _UNHIDDEN_LABEL_SETS[path]

    assert set(calls) == decided | unhidden
    for qualname in decided:
        call, function = calls[qualname]
        assert _passes_the_decision(call), qualname
        names = {
            argument.arg for argument in function.args.args + function.args.kwonlyargs
        }
        assert "hide_simulated" in names, qualname
    for qualname in unhidden:
        assert not calls[qualname][0].keywords, qualname


# ─── a label list that cannot be decided answers unavailable ─────────────────

# Each view that reads the labels of a holding project can be stopped by
# MarkedLookupFailed, which is a read that failed and not a bad request.
_ANSWERS_UNAVAILABLE = [
    ("trace.py", "TraceView", "list_traces"),
    ("trace.py", "TraceView", "list_traces_of_session"),
    ("trace.py", "TraceView", "list_voice_calls"),
    ("observation_span.py", "ObservationSpanView", "list_spans"),
    ("observation_span.py", "ObservationSpanView", "list_spans_observe"),
    ("dashboard.py", "DashboardWidgetViewSet", "_execute_ch_query_config"),
]


@pytest.mark.parametrize(
    ("view", "class_name", "method"),
    _ANSWERS_UNAVAILABLE,
    ids=[method for _, _, method in _ANSWERS_UNAVAILABLE],
)
def test_a_failed_mark_lookup_answers_unavailable_in_every_view_that_reads_labels(
    view, class_name, method
):
    tree = ast.parse((_VIEWS / view).read_text(encoding="utf-8"))
    function = next(
        node
        for cls in tree.body
        if isinstance(cls, ast.ClassDef) and cls.name == class_name
        for node in cls.body
        if isinstance(node, ast.FunctionDef) and node.name == method
    )

    handlers = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.ExceptHandler)
        and "MarkedLookupFailed" in ast.unparse(node)
    ]

    assert len(handlers) == 1
    [handler] = handlers
    if "MarkedLookupFailed" in ast.unparse(handler.type):
        statements = handler.body
    else:
        # ``except Exception`` that tests the type before it chooses the answer.
        statements = [
            statement
            for node in ast.walk(handler)
            if isinstance(node, ast.If)
            and "MarkedLookupFailed" in ast.unparse(node.test)
            for statement in node.body
        ]
    answer = "\n".join(ast.unparse(statement) for statement in statements)
    # The same answer a ClickHouse read that ran out of time gets.
    assert "HTTP_503_SERVICE_UNAVAILABLE" in answer
    assert "HTTP_500" not in answer
    assert "bad_request" not in answer
