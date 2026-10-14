"""Hiding simulated traces and spans in the list builders and filter compilers.

Nothing here touches Postgres or ClickHouse: each case builds a statement and
reads its text and parameters. The expected text always comes from the
visibility module, so these tests never spell a mark key themselves.
"""

from __future__ import annotations

import copy
import re
from datetime import datetime, timedelta
from typing import Any

import pytest

from tracer.services.clickhouse.query_builders.latest_filter_predicates import (
    compile_exact_graph_filter_predicates,
    compile_trace_filter_plans,
    latest_span_membership_source_sql,
    simulation_hide_plan,
)
from tracer.services.clickhouse.query_builders.span_list import SpanListQueryBuilder
from tracer.services.clickhouse.query_builders.trace_list import TraceListQueryBuilder
from tracer.services.clickhouse.query_builders.voice_call_list import (
    VOICE_CALL_ROOT_FILTER,
    VOICE_CALL_SIMULATOR_EXCLUSION_FILTER,
)
from tracer.services.clickhouse.v2.query_builders.filters import rewrite_v1_sql_to_v2
from tracer.services.clickhouse.v2.query_builders.span_list import (
    SpanListQueryBuilderV2,
)
from tracer.services.clickhouse.v2.query_builders.trace_list import (
    TraceListQueryBuilderV2,
)
from tracer.services.clickhouse.v2.query_builders.voice_call_list import (
    VoiceCallListQueryBuilderV2,
)
from tracer.services.simulation_visibility import resource_mark_sql, row_exclusion_sql

PROJECT_ID = "00000000-0000-4000-8000-0000000000a1"
OTHER_PROJECT_ID = "00000000-0000-4000-8000-0000000000a2"
WINDOW_START = datetime(2026, 1, 1)
WINDOW_END = datetime(2026, 1, 2)
SLICE_START = WINDOW_END - timedelta(minutes=5)

# What a legacy-token builder writes before its rewrite boundary, what a v2
# builder sends after it, and what the graph compiler writes directly.
LEGACY_UNMARKED = row_exclusion_sql(map_column="span_attr_str")
UNMARKED = rewrite_v1_sql_to_v2(LEGACY_UNMARKED)
CH25_UNMARKED = row_exclusion_sql(map_column="attrs_string")
ROOT_GUARD = "(parent_span_id IS NULL OR parent_span_id = '')"
# The latest-state value a hiding plan computes; a row is kept when it is 1.
HIDE_ALIAS = simulation_hide_plan(scope="root").aggregates[0].rsplit(" AS ", 1)[1]


def _flat(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


def _time_filter(
    start: datetime = WINDOW_START, end: datetime = WINDOW_END
) -> dict[str, Any]:
    return {
        "column_id": "created_at",
        "filter_config": {
            "filter_type": "datetime",
            "filter_op": "between",
            "filter_value": [start.isoformat(), end.isoformat()],
        },
    }


def _attribute_filter(key: str, value: str) -> dict[str, Any]:
    return {
        "column_id": key,
        "filter_config": {
            "col_type": "SPAN_ATTRIBUTE",
            "filter_type": "text",
            "filter_op": "equals",
            "filter_value": value,
        },
    }


def _system_filter(key: str, value: str) -> dict[str, Any]:
    return {
        "column_id": key,
        "filter_config": {
            "col_type": "SYSTEM_METRIC",
            "filter_type": "text",
            "filter_op": "equals",
            "filter_value": value,
        },
    }


# ---------------------------------------------------------------------------
# Builders: one entry per statement that selects, counts or classifies rows
# ---------------------------------------------------------------------------


def _trace(**extra: Any) -> TraceListQueryBuilderV2:
    return TraceListQueryBuilderV2(
        project_id=PROJECT_ID, filters=[_time_filter()], **extra
    )


def _trace_filtered(**extra: Any) -> TraceListQueryBuilderV2:
    return TraceListQueryBuilderV2(
        project_id=PROJECT_ID,
        filters=[_time_filter(), _attribute_filter("final_status", "Rejected")],
        **extra,
    )


def _trace_org(**extra: Any) -> TraceListQueryBuilderV2:
    return TraceListQueryBuilderV2(
        project_id=None,
        project_ids=[PROJECT_ID, OTHER_PROJECT_ID],
        filters=[_time_filter()],
        **extra,
    )


def _span(**extra: Any) -> SpanListQueryBuilderV2:
    return SpanListQueryBuilderV2(
        project_id=PROJECT_ID, filters=[_time_filter()], **extra
    )


def _span_org(**extra: Any) -> SpanListQueryBuilderV2:
    return SpanListQueryBuilderV2(
        project_id=None,
        project_ids=[PROJECT_ID, OTHER_PROJECT_ID],
        filters=[_time_filter()],
        **extra,
    )


def _voice(**extra: Any) -> VoiceCallListQueryBuilderV2:
    return VoiceCallListQueryBuilderV2(
        project_id=PROJECT_ID, filters=[_time_filter()], **extra
    )


def _graph_builder(**extra: Any) -> TraceListQueryBuilderV2:
    return TraceListQueryBuilderV2(
        project_id=PROJECT_ID,
        filters=[_time_filter(), _attribute_filter("final_status", "Rejected")],
        page_number=0,
        page_size=200,
        bounded_internal_scan=True,
        bounded_identity_only=True,
        bounded_bulk_scan=True,
        bounded_include_filter_witnesses=False,
        bounded_global_span_witnesses=True,
        **extra,
    )


def _seed_window() -> dict[str, Any]:
    return {"slice_start": SLICE_START, "slice_end": WINDOW_END, "limit": 50}


STATEMENTS = {
    "trace.build": (_trace, lambda b: b.build()),
    "trace.build_id_query": (_trace, lambda b: b.build_id_query()),
    "trace.build_count_query": (_trace, lambda b: b.build_count_query()),
    "trace.classifier": (
        _trace_filtered,
        lambda b: b.build_filter_match_query(["trace-a"]),
    ),
    "trace.ordered_seed": (
        _trace_filtered,
        lambda b: b.build_filter_ordered_seed_page(**_seed_window()),
    ),
    "trace.root_seed": (_trace, lambda b: b.build_filter_seed_page(**_seed_window())),
    "trace.root_verifier": (
        _trace,
        lambda b: b.build_exact_graph_root_membership_query(
            candidate_trace_ids=["trace-a"],
            request_start=WINDOW_START,
            request_end=WINDOW_END,
        ),
    ),
    "trace.org.build_count_query": (_trace_org, lambda b: b.build_count_query()),
    "trace.org.classifier": (
        _trace_org,
        lambda b: b.build_filter_match_query(["trace-a"]),
    ),
    "span.build": (_span, lambda b: b.build()),
    "span.build_id_query": (_span, lambda b: b.build_id_query()),
    "span.build_count_query": (_span, lambda b: b.build_count_query()),
    "span.classifier": (_span, lambda b: b.build_filter_match_query(["span-a"])),
    "span.seed": (_span, lambda b: b.build_filter_seed_page(**_seed_window())),
    "span.org.build": (_span_org, lambda b: b.build()),
    "span.org.classifier": (
        _span_org,
        lambda b: b.build_filter_match_query(["span-a"]),
    ),
    "voice.build": (_voice, lambda b: b.build()),
    "voice.build_id_query": (_voice, lambda b: b.build_id_query()),
    "voice.build_count_query": (_voice, lambda b: b.build_count_query()),
    "voice.classifier": (_voice, lambda b: b.build_filter_match_query(["trace-a"])),
    "voice.ordered_seed": (
        _voice,
        lambda b: b.build_filter_ordered_seed_page(**_seed_window()),
    ),
}


@pytest.mark.unit
@pytest.mark.parametrize("label", sorted(STATEMENTS))
def test_hiding_puts_the_unmarked_row_text_in_the_statement(label):
    make, call = STATEMENTS[label]

    sql, _ = call(make(hide_simulated=True))

    flat = _flat(sql)
    assert UNMARKED in flat
    # The resource key rides in the same test; a plan or row predicate that
    # lost it would hide a span-marked trace and show a resource-marked one.
    assert resource_mark_sql(schema="v2") in flat
    # The test is true for an unmarked row, so negating it would show only the
    # marked rows and hide everything else.
    assert f"NOT {UNMARKED}" not in flat


# How each statement keeps an unmarked row: as one more AND on the rows it
# reads, or as the latest-state value a hiding plan computes and compares with
# 1. The live-root verifiers keep it in a flag of their own, checked further on.
KEEPS_BY_ROW_TEST = (
    "trace.build",
    "trace.build_id_query",
    "trace.build_count_query",
    "trace.ordered_seed",
    "trace.root_seed",
    "trace.org.build_count_query",
    "span.build",
    "span.build_id_query",
    "span.build_count_query",
    "span.seed",
    "span.org.build",
    "voice.build",
    "voice.build_id_query",
    "voice.build_count_query",
    "voice.ordered_seed",
)
KEEPS_BY_HIDE_PLAN = (
    "trace.classifier",
    "trace.org.classifier",
    "span.classifier",
    "span.org.classifier",
    "voice.classifier",
)


@pytest.mark.unit
def test_every_statement_has_the_way_it_keeps_unmarked_rows_checked():
    checked = [*KEEPS_BY_ROW_TEST, *KEEPS_BY_HIDE_PLAN, "trace.root_verifier"]

    assert sorted(checked) == sorted(STATEMENTS)


@pytest.mark.unit
@pytest.mark.parametrize("label", KEEPS_BY_ROW_TEST)
def test_hiding_adds_the_unmarked_row_test_as_one_more_and(label):
    make, call = STATEMENTS[label]

    sql, _ = call(make(hide_simulated=True))

    assert f"AND {UNMARKED}" in _flat(sql)


@pytest.mark.unit
@pytest.mark.parametrize("label", KEEPS_BY_HIDE_PLAN)
def test_hiding_keeps_the_rows_whose_latest_unmarked_value_is_one(label):
    make, call = STATEMENTS[label]

    sql, _ = call(make(hide_simulated=True))

    flat = _flat(sql)
    assert f"{HIDE_ALIAS} = 1" in flat
    assert f"{HIDE_ALIAS} = 0" not in flat


@pytest.mark.unit
@pytest.mark.parametrize("label", sorted(STATEMENTS))
def test_omitting_the_argument_and_passing_false_send_the_same_statement(label):
    make, call = STATEMENTS[label]

    omitted_sql, omitted_params = call(make())
    false_sql, false_params = call(make(hide_simulated=False))

    assert false_sql == omitted_sql
    assert false_params == omitted_params
    assert UNMARKED not in _flat(false_sql)
    assert resource_mark_sql(schema="v2") not in _flat(false_sql)


@pytest.mark.unit
@pytest.mark.parametrize("label", sorted(STATEMENTS))
def test_hiding_adds_no_parameter(label):
    make, call = STATEMENTS[label]

    _, hidden_params = call(make(hide_simulated=True))
    _, plain_params = call(make())

    assert hidden_params == plain_params


@pytest.mark.unit
def test_the_unfiltered_trace_count_stops_reading_the_rollup_when_hiding():
    plain_sql, _ = _trace().build_count_query()
    false_sql, _ = _trace(hide_simulated=False).build_count_query()
    hidden_sql, hidden_params = _trace(hide_simulated=True).build_count_query()

    assert "trace_count_rollup" in plain_sql
    assert "trace_count_rollup" in false_sql
    assert "trace_count_rollup" not in hidden_sql
    assert UNMARKED in _flat(hidden_sql)
    # The raw count takes its window from the params, which a caller that
    # counts before building a page has not set.
    assert hidden_params["start_date"] == WINDOW_START
    assert hidden_params["end_date"] == WINDOW_END


@pytest.mark.unit
def test_a_filtered_trace_count_reads_spans_with_or_without_hiding():
    filters = [_time_filter(), _attribute_filter("final_status", "Rejected")]

    plain_sql, _ = TraceListQueryBuilderV2(
        project_id=PROJECT_ID, filters=filters
    ).build_count_query()
    hidden_sql, _ = TraceListQueryBuilderV2(
        project_id=PROJECT_ID, filters=filters, hide_simulated=True
    ).build_count_query()

    assert "trace_count_rollup" not in plain_sql
    assert "trace_count_rollup" not in hidden_sql


# ---------------------------------------------------------------------------
# The predicate is never a filter, a plan the decisions see, or a cursor field
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_request_filter_list_is_not_touched_by_hiding():
    filters = [_time_filter(), _attribute_filter("final_status", "Rejected")]
    before = copy.deepcopy(filters)

    trace = TraceListQueryBuilderV2(
        project_id=PROJECT_ID, filters=filters, hide_simulated=True
    )
    span = SpanListQueryBuilderV2(
        project_id=PROJECT_ID, filters=filters, hide_simulated=True
    )
    voice = VoiceCallListQueryBuilderV2(
        project_id=PROJECT_ID, filters=filters, hide_simulated=True
    )
    trace.build_filter_match_query(["trace-a"])
    span.build_filter_match_query(["span-a"])
    voice.build_filter_match_query(["trace-a"])

    assert filters == before
    assert trace.filters == before
    assert trace._bounded_filters() == before
    assert span.filters == before
    assert voice.filters == before
    delegate = voice._bounded_delegate()
    assert LEGACY_UNMARKED not in repr(delegate.filters)
    assert delegate.hide_simulated is True


@pytest.mark.unit
def test_the_plans_that_shape_a_read_do_not_include_the_hiding_plan():
    filters = [_time_filter(), _attribute_filter("final_status", "Rejected")]
    plain = TraceListQueryBuilder(project_id=PROJECT_ID, filters=filters)
    hidden = TraceListQueryBuilder(
        project_id=PROJECT_ID, filters=filters, hide_simulated=True
    )

    plain_plans = plain._partition_trace_filter_plans(plain._bounded_filters())
    hidden_plans = hidden._partition_trace_filter_plans(hidden._bounded_filters())

    assert hidden_plans == plain_plans
    for plan in hidden_plans[0]:
        text = " ".join([*plan.aggregates, plan.predicate, plan.seed_predicate])
        assert LEGACY_UNMARKED not in text


REQUEST_SHAPES = {
    "time_only_long": [_time_filter()],
    "time_only_short": [_time_filter(end=WINDOW_START + timedelta(minutes=30))],
    "attribute": [_time_filter(), _attribute_filter("final_status", "Rejected")],
    "call_type": [_time_filter(), _system_filter("call_type", "inbound")],
    "native_column": [_time_filter(), _system_filter("model", "gpt-4o")],
}

TRACE_DECISIONS = (
    "requires_cursor_for_long_filtered_read",
    "recommended_filter_classify_batch_size",
    "recommended_filter_seed_batch_size",
    "recommended_filter_cursor_seed_batch_size",
    "filter_seed_proves_result_order",
    "requires_unindexed_graph_sample_slice",
    "supports_bounded_filter_scan",
    "bounded_filter_degraded_error_code",
)


@pytest.mark.unit
@pytest.mark.parametrize("shape", sorted(REQUEST_SHAPES))
def test_trace_request_shape_decisions_answer_the_same_when_hiding(shape):
    filters = REQUEST_SHAPES[shape]
    plain = TraceListQueryBuilderV2(project_id=PROJECT_ID, filters=filters)
    hidden = TraceListQueryBuilderV2(
        project_id=PROJECT_ID, filters=filters, hide_simulated=True
    )

    for decision in TRACE_DECISIONS:
        assert getattr(hidden, decision)() == getattr(plain, decision)(), decision


SPAN_DECISIONS = (
    "requires_cursor_for_long_filtered_read",
    "recommended_filter_classify_batch_size",
    "recommended_filter_seed_batch_size",
    "recommended_filter_cursor_seed_batch_size",
    "recommended_filter_unindexed_micro_seed_width",
    "filter_seed_width_policy",
    "supports_filter_population_time_discovery",
    "supports_filter_anchor_probe",
    "requires_unindexed_graph_sample_slice",
    "supports_bounded_filter_scan",
    "bounded_filter_degraded_error_code",
)


@pytest.mark.unit
@pytest.mark.parametrize("anchor_probe", [False, True])
@pytest.mark.parametrize("shape", sorted(REQUEST_SHAPES))
def test_span_request_shape_decisions_answer_the_same_when_hiding(shape, anchor_probe):
    filters = REQUEST_SHAPES[shape]
    # Most of these decisions count or inspect the plans compiled from the
    # request, so a hiding plan that joined them would change an answer: the
    # call-type micro-seed counts plans against leaves, and a probing builder
    # would take the hiding row test for an indexed anchor on a time-only list.
    plain = SpanListQueryBuilderV2(
        project_id=PROJECT_ID, filters=filters, bounded_anchor_probe=anchor_probe
    )
    hidden = SpanListQueryBuilderV2(
        project_id=PROJECT_ID,
        filters=filters,
        bounded_anchor_probe=anchor_probe,
        hide_simulated=True,
    )

    for decision in SPAN_DECISIONS:
        assert getattr(hidden, decision)() == getattr(plain, decision)(), decision


@pytest.mark.unit
@pytest.mark.parametrize("shape", sorted(REQUEST_SHAPES))
def test_voice_request_shape_decisions_answer_the_same_when_hiding(shape):
    filters = REQUEST_SHAPES[shape]
    plain = VoiceCallListQueryBuilderV2(project_id=PROJECT_ID, filters=filters)
    hidden = VoiceCallListQueryBuilderV2(
        project_id=PROJECT_ID, filters=filters, hide_simulated=True
    )

    for decision in (
        "recommended_filter_classify_batch_size",
        "recommended_filter_seed_batch_size",
        "recommended_filter_cursor_seed_batch_size",
        "filter_seed_proves_result_order",
        "supports_bounded_filter_scan",
        "bounded_filter_degraded_error_code",
        "supports_filter_exact_zero_probe",
    ):
        assert getattr(hidden, decision)() == getattr(plain, decision)(), decision


# ---------------------------------------------------------------------------
# Trace grain: the live root decides, through a root-scope plan
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_root_hiding_plan_is_the_shared_row_test_and_the_compiler_adds_none():
    filters = [_time_filter(), _attribute_filter("final_status", "Rejected")]

    # The compiler compiles the request's leaves and nothing else: the list
    # builders add the hiding plan themselves, after the request plans.
    assert all(
        plan.seed_predicate != LEGACY_UNMARKED
        for plan in compile_trace_filter_plans(filters)
    )
    with pytest.raises(TypeError):
        compile_trace_filter_plans(filters, hide_simulated=True)

    plan = simulation_hide_plan(scope="root")
    alias = plan.aggregates[0].rsplit(" AS ", 1)[1]
    assert plan.scope == "root"
    assert plan.seed_predicate == LEGACY_UNMARKED
    assert (
        plan.aggregates[0] == f"argMax({LEGACY_UNMARKED}, _peerdb_version) AS {alias}"
    )
    # The aggregate is true for an unmarked row, so only 1 keeps it.
    assert plan.predicate == f"{alias} = 1"
    assert resource_mark_sql(schema="v1") in plan.aggregates[0]
    assert resource_mark_sql(schema="v1") in plan.seed_predicate
    assert plan.params == {}


@pytest.mark.unit
def test_the_membership_plans_are_the_request_plans_and_one_root_hiding_plan(
    monkeypatch,
):
    filters = [_time_filter(), _attribute_filter("final_status", "Rejected")]
    plain = TraceListQueryBuilder(project_id=PROJECT_ID, filters=filters)
    hidden = TraceListQueryBuilder(
        project_id=PROJECT_ID, filters=filters, hide_simulated=True
    )
    request_plans, request_residual = plain._partition_trace_filter_plans(filters)
    asked: list[list[dict[str, Any]]] = []
    partition = hidden._partition_trace_filter_plans

    def watching_partition(items: list[dict[str, Any]]):
        asked.append(items)
        return partition(items)

    # A list surface that overrides its partition keeps its filter semantics
    # when it hides, so hiding must be built on the instance's own partition.
    monkeypatch.setattr(hidden, "_partition_trace_filter_plans", watching_partition)

    plans, residual = hidden._membership_trace_filter_plans(filters)

    assert plain._membership_trace_filter_plans(filters) == (
        request_plans,
        request_residual,
    )
    assert asked == [filters]
    assert residual == request_residual
    assert plans[:-1] == request_plans
    assert plans[-1] == simulation_hide_plan(scope="root")


@pytest.mark.unit
def test_the_classifier_judges_a_trace_by_its_root_plan():
    plan = simulation_hide_plan(scope="root")
    alias = plan.aggregates[0].rsplit(" AS ", 1)[1]

    sql, _ = TraceListQueryBuilder(
        project_id=PROJECT_ID, filters=[_time_filter()], hide_simulated=True
    ).build_filter_match_query(["trace-a"])

    flat = _flat(sql)
    assert plan.aggregates[0] in flat
    assert f"argMaxIf(tuple({alias})" in flat
    assert plan.predicate in flat.split("HAVING", 1)[1]


@pytest.mark.unit
def test_a_marked_voice_call_is_left_out_by_the_voice_classifier_and_seed():
    plan = simulation_hide_plan(scope="root")

    classifier, classifier_params = VoiceCallListQueryBuilderV2(
        project_id=PROJECT_ID, filters=[_time_filter()], hide_simulated=True
    ).build_filter_match_query(["trace-a"])
    seed, _ = VoiceCallListQueryBuilderV2(
        project_id=PROJECT_ID, filters=[_time_filter()], hide_simulated=True
    ).build_filter_ordered_seed_page(**_seed_window())

    assert plan.predicate in _flat(classifier)
    assert UNMARKED in _flat(seed)
    # The conversation-root invariant stays beside it.
    assert VOICE_CALL_ROOT_FILTER["filter_config"]["filter_value"] in (
        classifier_params.values()
    )


ROOT_VERIFIERS = {
    "membership": lambda b: b.build_exact_graph_root_membership_query(
        candidate_trace_ids=["trace-a"],
        request_start=WINDOW_START,
        request_end=WINDOW_END,
    ),
    "partition": lambda b: b.build_exact_graph_latest_root_partition(
        partition_start=WINDOW_START,
        partition_end=WINDOW_START + timedelta(hours=2),
        request_start=WINDOW_START,
        request_end=WINDOW_END,
    ),
}


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(ROOT_VERIFIERS))
def test_a_root_verifier_keeps_only_a_live_root_that_is_unmarked(name):
    call = ROOT_VERIFIERS[name]

    plain_sql, plain_params = call(_graph_builder())
    false_sql, false_params = call(_graph_builder(hide_simulated=False))
    hidden_sql, hidden_params = call(_graph_builder(hide_simulated=True))

    # An empty statement would mean the builder fell out of the lane under test.
    assert plain_sql
    assert (false_sql, false_params) == (plain_sql, plain_params)
    assert "latest_unmarked_root" not in plain_sql
    flat = _flat(hidden_sql)
    assert UNMARKED in flat
    assert "AND latest_unmarked_root = 1" in flat
    assert "latest_unmarked_root = 0" not in flat
    assert hidden_params == plain_params


# ---------------------------------------------------------------------------
# Span grain: the row predicate
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_span_classifier_judges_each_span_by_its_own_plan():
    plan = simulation_hide_plan(scope="span")

    sql, _ = SpanListQueryBuilder(
        project_id=PROJECT_ID, filters=[_time_filter()], hide_simulated=True
    ).build_filter_match_query(["span-a"])

    flat = _flat(sql)
    assert plan.aggregates[0] in flat
    assert plan.predicate in flat


@pytest.mark.unit
def test_the_span_list_projects_the_columns_the_row_predicate_reads():
    plain_sql, _ = _span().build()
    hidden_sql, _ = _span(hide_simulated=True).build()

    def packed_columns(sql: str) -> str:
        match = re.search(r"argMax\(tuple\(([^()]*)\), _version\)", sql)
        assert match is not None
        return match.group(1)

    assert "resource_attrs" not in packed_columns(plain_sql)
    assert "resource_attrs" in packed_columns(hidden_sql)
    assert "attrs_string" in packed_columns(hidden_sql)


# ---------------------------------------------------------------------------
# The exact-graph compiler
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("observe_type", ["trace", "span"])
def test_the_graph_compiler_without_hiding_is_unchanged(observe_type):
    leaves = [VOICE_CALL_ROOT_FILTER, VOICE_CALL_SIMULATOR_EXCLUSION_FILTER]

    omitted = compile_exact_graph_filter_predicates(
        leaves, project_id=PROJECT_ID, observe_type=observe_type
    )
    false = compile_exact_graph_filter_predicates(
        leaves,
        project_id=PROJECT_ID,
        observe_type=observe_type,
        hide_simulated=False,
    )

    assert false == omitted
    assert CH25_UNMARKED not in _flat(omitted[0])
    assert resource_mark_sql(schema="v2") not in _flat(omitted[0])


# The predicate each membership selector in a graph clause judges a span by,
# in clause order: the selector is the only place a clause names a row test.
MEMBERSHIP_PREDICATE = re.compile(r"toUInt8\((.*?)\)\) AS membership_state")


def _membership_predicates(sql: str) -> list[str]:
    return MEMBERSHIP_PREDICATE.findall(_flat(sql))


def _trace_graph_hide_clause(predicate: str) -> str:
    """The whole trace-graph hiding clause, judging spans by ``predicate``."""

    return _flat(f"""
        (
            trace_id NOT IN (
                SELECT DISTINCT trace_id
                FROM ({latest_span_membership_source_sql(predicate=predicate)})
                WHERE matched
            )
        )""")


@pytest.mark.unit
@pytest.mark.parametrize("observe_type", ["trace", "span"])
def test_the_graph_compiler_adds_no_clause_for_an_empty_request_when_not_hiding(
    observe_type,
):
    omitted = compile_exact_graph_filter_predicates(
        [], project_id=PROJECT_ID, observe_type=observe_type
    )
    false = compile_exact_graph_filter_predicates(
        [], project_id=PROJECT_ID, observe_type=observe_type, hide_simulated=False
    )

    assert false == omitted
    assert "membership_state" not in omitted[0]
    assert CH25_UNMARKED not in _flat(omitted[0])


@pytest.mark.unit
def test_the_graph_compiler_hides_a_trace_in_which_any_live_span_is_marked():
    leaves = [VOICE_CALL_ROOT_FILTER, VOICE_CALL_SIMULATOR_EXCLUSION_FILTER]

    plain_sql, plain_params = compile_exact_graph_filter_predicates(
        leaves, project_id=PROJECT_ID, observe_type="trace"
    )
    sql, params = compile_exact_graph_filter_predicates(
        leaves, project_id=PROJECT_ID, observe_type="trace", hide_simulated=True
    )

    flat = _flat(sql)
    # One NOT IN clause for the number rule, one more for the mark.
    assert (
        flat.count("trace_id NOT IN") == _flat(plain_sql).count("trace_id NOT IN") + 1
    )
    # The request's own clauses come first and are left as they were.
    assert _membership_predicates(sql)[:-1] == _membership_predicates(plain_sql)
    assert _membership_predicates(sql)[-1] == f"NOT ({CH25_UNMARKED})"
    assert resource_mark_sql(schema="v2") in flat
    assert params == plain_params


@pytest.mark.unit
def test_a_trace_graph_leaves_out_a_rootless_trace_whose_children_carry_the_resource_mark():
    sql, _ = compile_exact_graph_filter_predicates(
        [], project_id=PROJECT_ID, observe_type="trace", hide_simulated=True
    )

    # A trace whose root never reached the collector has only its children to
    # be judged by, so the clause must not ask for a root.
    assert "parent_span_id" not in _flat(sql)
    assert _membership_predicates(sql) == [f"NOT ({CH25_UNMARKED})"]
    # The resource term is the only mark a code agent's spans carry.
    assert resource_mark_sql(schema="v2") in _membership_predicates(sql)[0]


@pytest.mark.unit
def test_a_trace_graph_leaves_out_a_trace_with_an_unmarked_root_and_a_marked_child():
    leaves = [VOICE_CALL_ROOT_FILTER]

    plain_sql, _ = compile_exact_graph_filter_predicates(
        leaves, project_id=PROJECT_ID, observe_type="trace"
    )
    sql, _ = compile_exact_graph_filter_predicates(
        leaves, project_id=PROJECT_ID, observe_type="trace", hide_simulated=True
    )

    kept = _membership_predicates(plain_sql)
    hidden = _membership_predicates(sql)
    # The request's root leaf is judged on the root; the mark clause is one more
    # and asks for no root, so a marked child decides alone however clean its
    # root is.
    assert kept
    assert all(ROOT_GUARD in predicate for predicate in kept)
    assert hidden[:-1] == kept
    assert hidden[-1] == f"NOT ({CH25_UNMARKED})"
    assert ROOT_GUARD not in hidden[-1]


@pytest.mark.unit
def test_the_trace_graph_clause_leaves_out_the_marked_traces_and_keeps_the_rest():
    sql, _ = compile_exact_graph_filter_predicates(
        [], project_id=PROJECT_ID, observe_type="trace", hide_simulated=True
    )
    flat = _flat(sql)

    # The whole clause, reading the same window and source as the number rule.
    assert flat == _trace_graph_hide_clause(f"NOT ({CH25_UNMARKED})")
    # NOT IN leaves the matched traces out; IN would keep only them.
    assert "trace_id IN" not in flat
    # The selector must match the spans that ARE marked. Handing it the unmarked
    # test would leave out every trace that has one clean span.
    assert f"toUInt8({CH25_UNMARKED})" not in flat


@pytest.mark.unit
def test_the_graph_compiler_hides_a_span_by_its_own_row():
    _, plain_params = compile_exact_graph_filter_predicates(
        [], project_id=PROJECT_ID, observe_type="span"
    )
    sql, params = compile_exact_graph_filter_predicates(
        [], project_id=PROJECT_ID, observe_type="span", hide_simulated=True
    )

    assert _flat(sql) == f"({CH25_UNMARKED})"
    assert "trace_id NOT IN" not in sql
    assert params == plain_params
