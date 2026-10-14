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

import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.db import DatabaseError
from rest_framework.test import APIRequestFactory, force_authenticate

import tracer.services.clickhouse.exact_attribute_detail as detail_module
import tracer.services.clickhouse.query_service as query_service_module
import tracer.services.clickhouse.span_attribute_lookups as lookups_module
import tracer.services.dashboard_metrics_catalog as catalog_module
import tracer.views.observation_span as observation_span_module
import tracer.views.span_attributes as span_attributes_module
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

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, timeout=None):
        self.store[key] = value


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


def test_a_catalog_without_attribute_keys_does_not_ask_or_change_its_key(monkeypatch):
    holds = _HoldsAmong(HOLDING)

    catalog = _catalog(monkeypatch, holds, include_custom_attributes=False)

    assert holds.asked == []
    assert catalog.resolved == []
    assert list(catalog.cache.store) == [
        f"dashboard:metrics_catalog:v6:{WORKSPACE}:{HOLDING}::0:0::"
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
