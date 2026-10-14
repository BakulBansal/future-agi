"""Every reader of a holding project leaves out the marked traces, on real rows.

Each case seeds the real ``spans`` table the way the voice-list raw-log test
does and flags the project's provider row through the real ``record_holding``.
Three projects hold the same trace set, so what a reader answers is compared
with a project that never had the marked traces rather than with a number the
test computed:

* ``h`` holds, with every trace;
* ``rt`` holds, with the production traces only (the answer for a reader that
  judges a whole trace);
* ``rs`` holds, with the production traces and the two unmarked children of
  the trace whose root is marked (the answer for a reader that judges one
  span);
* ``t`` has a provider row that does not hold, with every trace (the marked
  traces must show there, or a case proves nothing).

The traces: ``p_*`` are production; ``s`` carries the span mark on every span;
``r`` carries the resource mark on every span; ``l`` is a rootless trace whose
children carry the resource mark; ``m`` has a marked root and two unmarked
children. Marked traces are heavier (latency, tokens, errors), so a leak moves
a number.

Every test here has database access (the module mark), so the real holding
decision runs.
"""

from __future__ import annotations

import json
import os
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.core import signing
from django.db import connection
from django.test.utils import CaptureQueriesContext

from tracer.services.simulation_visibility import (
    RESOURCE_MARK_KEY,
    SIMULATION_MARK_KEY,
    hide_simulated,
    holding_projects,
    marked_trace_ids,
    record_holding,
    row_exclusion_sql,
)
from tracer.tests.integration.test_simulation_default_parity import (  # noqa: F401
    DASHBOARD_LATENCY,
    DASHBOARD_SCORE,
    READERS,
    REGION_BREAKDOWN,
    STATUS_FILTER,
    TENANT_FILTER,
    Reader,
    TraceSpec,
    _chart_body,
    clickhouse_must_be_reachable,
    dashboard_config,
    env_factory_context,
    production_specs,
    relative_base,
    request_filters,
    seed_world,
)

pytestmark = [pytest.mark.integration, pytest.mark.django_db]


@pytest.fixture
def env_factory(ch_schema, ch_routes_on, monkeypatch):
    with env_factory_context(ch_schema, monkeypatch) as make:
        yield make


@pytest.fixture
def env(env_factory):
    return env_factory()


RESOURCE_ATTRS = {"service.name": "agent-under-test", RESOURCE_MARK_KEY: "simulation"}
TIMINGS_FILE_VARIABLE = "HIDE_READER_TIMINGS_FILE"
LARGE_TRACES = int(os.environ.get("HIDE_READER_LARGE_TRACES", "3000"))
TIMING_BUDGET_SECONDS = float(os.environ.get("HIDE_READER_TIMING_BUDGET_S", "30"))

# What a reader must not name, by what it names.
TRACE_LEAKS = frozenset({"s", "r", "m", "l"})
SPAN_LEAKS = frozenset(
    {f"{trace}/{index}" for trace in ("s", "r") for index in range(3)}
    | {"l/1", "l/2", "m/0"}
)
# A reader that judges a span still names the marked-root trace through its
# unmarked children.
LEAK_OVERRIDES = {
    "span_neighbours": frozenset({"s", "r", "l"}),
    "reader_group_by_name": frozenset({"s", "r", "l"}),
    "reader_root_ids": frozenset({"s/0", "r/0", "m/0"}),
}


def full_specs() -> list[TraceSpec]:
    # Each marked trace has a tier of its own, so a count of tiers moves too.
    heavy = {"weight": 50, "errors": 2}
    return [
        TraceSpec("p_old", 10),
        TraceSpec(
            "s", 15, span_attrs={SIMULATION_MARK_KEY: "call-s", "tier": "s"}, **heavy
        ),
        TraceSpec("p_mid", 20, errors=1),
        TraceSpec(
            "r", 25, resource_attrs=RESOURCE_ATTRS, span_attrs={"tier": "r"}, **heavy
        ),
        TraceSpec(
            "l",
            27,
            resource_attrs=RESOURCE_ATTRS,
            span_attrs={"tier": "l"},
            rootless=True,
            **heavy,
        ),
        TraceSpec(
            "m",
            30,
            root_attrs={SIMULATION_MARK_KEY: "call-m"},
            span_attrs={"tier": "m"},
            **heavy,
        ),
        TraceSpec("p_new", 35),
    ]


def span_reference_specs() -> list[TraceSpec]:
    """What a span reader sees: the children of the marked root stay."""
    return [
        *production_specs(),
        TraceSpec(
            "m", 30, span_attrs={"tier": "m"}, rootless=True, weight=50, errors=2
        ),
    ]


def leaks_of(reader: Reader) -> frozenset[str]:
    labels = LEAK_OVERRIDES.get(
        reader.name, TRACE_LEAKS if reader.grain == "trace" else SPAN_LEAKS
    )
    return frozenset(f"0/{label}" for label in labels)


def holding_world(
    env, name, specs, *, kind, base, extras=False, holds=True, **world_options
):
    world = seed_world(
        env, name, specs, kind=kind, base=base, extras=extras, **world_options
    )
    if holds:
        record_holding(world.project_id)
    return world


def make_comparison(env, kind, *, extras=False, base=None, **world_options):
    base = base or relative_base()
    options = {"kind": kind, "base": base, "extras": extras, **world_options}
    return SimpleNamespace(
        base=base,
        h=holding_world(env, "h", full_specs(), **options),
        t=holding_world(env, "t", full_specs(), holds=False, **options),
        rt=holding_world(env, "rt", production_specs(), **options),
        rs=holding_world(env, "rs", span_reference_specs(), **options),
    )


def reference_of(comparison, reader: Reader):
    return comparison.rt if reader.grain == "trace" else comparison.rs


def observe(reader: Reader, env, worlds, scope="single"):
    return reader.observe(reader.call(env, worlds, scope), worlds)


def has_signal(value) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return value > 0
    if isinstance(value, str):
        return False
    if isinstance(value, dict):
        return any(has_signal(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(has_signal(item) for item in value)
    return False


def record_timing(name: str, seconds: float, **details) -> None:
    line = {"read": name, "seconds": round(seconds, 4), **details}
    print(f"TIMING {json.dumps(line, sort_keys=True)}")
    path = os.environ.get(TIMINGS_FILE_VARIABLE)
    if path:
        with open(path, "a") as handle:
            handle.write(json.dumps(line, sort_keys=True) + "\n")


def timed(name: str, function, **details):
    started = time.perf_counter()
    result = function()
    elapsed = time.perf_counter() - started
    record_timing(name, elapsed, **details)
    assert elapsed <= TIMING_BUDGET_SECONDS, (
        f"{name} took {elapsed:.1f}s, over the {TIMING_BUDGET_SECONDS}s budget"
    )
    return result


CROSS_READER_NAMES = [name for name in READERS if name not in {"dashboard_fast_path"}]


def assert_answered(value, labels, what="the reference") -> None:
    """A comparison with an empty answer proves nothing: a build that hides
    everything would pass it."""
    if labels is not None:
        assert labels, f"{what} answered nothing"
    else:
        assert has_signal(value), f"{what} answered nothing"


def check_hides(env, reader: Reader, comparison) -> None:
    held_value, held_labels = observe(reader, env, [comparison.h])
    reference_value, reference_labels = observe(
        reader, env, [reference_of(comparison, reader)]
    )

    assert held_value == reference_value
    assert_answered(reference_value, reference_labels)
    if held_labels is not None:
        assert held_labels.isdisjoint(leaks_of(reader))
    if reader.control:
        plain_value, plain_labels = observe(reader, env, [comparison.t])
        assert plain_value != held_value
        if plain_labels is not None:
            assert plain_labels & leaks_of(reader), (
                "a project that does not hold must still show the marked traces"
            )


@pytest.mark.parametrize("name", CROSS_READER_NAMES)
def test_reader_leaves_out_marked_trace(env, name):
    reader = READERS[name]
    comparison = make_comparison(
        env, reader.kind, extras=reader.extras, **reader.world_options
    )
    check_hides(env, reader, comparison)


SEVERAL_PROJECT_READERS = [
    reader.name
    for reader in READERS.values()
    if "several" in reader.scopes and reader.name != "dashboard_fast_path"
]


# Either project may be the one that holds: a read that decided from the first
# project of its scope would pass with the holder first.
HOLDING_ORDERS = [
    pytest.param((True, False), id="holding_first"),
    pytest.param((False, True), id="holding_second"),
]


@pytest.mark.parametrize("holding_order", HOLDING_ORDERS)
@pytest.mark.parametrize("name", SEVERAL_PROJECT_READERS)
def test_org_scope_read_hides_when_any_project_holds(env_factory, name, holding_order):
    reader = READERS[name]
    base = relative_base()
    scoped = {}
    for label, specs, holding in (
        ("mixed", full_specs(), holding_order),
        (
            "reference",
            production_specs() if reader.grain == "trace" else span_reference_specs(),
            holding_order,
        ),
        ("open", full_specs(), (False, False)),
    ):
        scope_env = env_factory()
        worlds = [
            holding_world(
                scope_env,
                f"{label}{index}",
                specs,
                kind=reader.kind,
                base=base,
                holds=holds,
                **reader.world_options,
            )
            for index, holds in enumerate(holding)
        ]
        scoped[label] = (scope_env, worlds)

    def seen(label):
        scope_env, worlds = scoped[label]
        return reader.observe(reader.call(scope_env, worlds, "several"), worlds)

    mixed_value, mixed_labels = seen("mixed")
    reference_value, reference_labels = seen("reference")
    open_value, open_labels = seen("open")

    # One holding project makes the whole read hide, the plain project's own
    # marked traces included.
    assert mixed_value == reference_value
    assert_answered(reference_value, reference_labels)
    if mixed_labels is not None:
        marked = {f"{i}/{label}" for i in (0, 1) for label in _leaks(reader)}
        assert mixed_labels.isdisjoint(marked)
        assert open_labels & marked, "with nothing held the marked traces show"
    else:
        assert open_value != mixed_value


def _leaks(reader: Reader) -> frozenset[str]:
    return frozenset(label.split("/", 1)[1] for label in leaks_of(reader))


def _all_text(*values) -> str:
    return " ".join(json.dumps(value, default=str) for value in values)


def _cursor_payload(token: str):
    from django.conf import settings

    from tracer.services.clickhouse.list_cursor import CURSOR_SALT

    return signing.loads(token, key=settings.SECRET_KEY, salt=CURSOR_SALT)


def test_published_filters_and_cursors_never_carry_the_predicate(env):
    base = relative_base()
    world = holding_world(env, "h", full_specs(), kind="chain", base=base)
    params = {
        "project_id": world.project_id,
        "page_size": 1,
        "cursor_mode": True,
        "filters": json.dumps([world.window]),
    }
    pages, labels, cursors = [], set(), []
    for _ in range(8):
        response = env.client.get("/tracer/trace/list_traces_of_session/", params)
        assert response.status_code == 200, response.content[:500]
        result = response.json().get("result", response.json())
        pages.append(result)
        labels |= {world.label_of_trace[row["trace_id"]] for row in result["table"]}
        cursor = result["metadata"].get("next_cursor")
        if not cursor:
            break
        cursors.append(cursor)
        params = {key: value for key, value in params.items() if key != "cursor_mode"}
        params["cursor"] = cursor

    # Walking every page lists the production traces and no marked one.
    assert labels == {"p_old", "p_mid", "p_new"}
    assert cursors, "the walk never reached a second page"
    for cursor in cursors:
        text = json.dumps(_cursor_payload(cursor), default=str)
        assert SIMULATION_MARK_KEY not in text and RESOURCE_MARK_KEY not in text
        assert "hide_simulated" not in text
    published = _all_text(
        pages,
        READERS["trace_chart"].call(env, [world], "single"),
        READERS["span_chart"].call(env, [world], "single"),
        READERS["span_list"].call(env, [world], "single"),
    )
    assert SIMULATION_MARK_KEY not in published
    assert RESOURCE_MARK_KEY not in published


def _builder_decisions(builder):
    names = (
        "requires_cursor_for_long_filtered_read",
        "recommended_filter_classify_batch_size",
        "recommended_filter_seed_batch_size",
        "supports_bounded_filter_scan",
        "bounded_filter_degraded_error_code",
    )
    return {
        name: getattr(builder, name)()
        for name in names
        if callable(getattr(builder, name, None))
    }


def _request_shapes(world):
    shapes = ("time_only", "long_window", "attribute", "status")
    return {shape: request_filters(world, f"single.{shape}") for shape in shapes}


@pytest.mark.parametrize("shape", ["time_only", "long_window", "attribute", "status"])
@pytest.mark.parametrize("builder", ["trace", "span", "voice"])
def test_request_shape_decisions_unchanged_for_a_holding_project(env, builder, shape):
    from tracer.services.clickhouse.v2.query_builders.span_list import (
        SpanListQueryBuilderV2,
    )
    from tracer.services.clickhouse.v2.query_builders.trace_list import (
        TraceListQueryBuilderV2,
    )
    from tracer.services.clickhouse.v2.query_builders.voice_call_list import (
        VoiceCallListQueryBuilderV2,
    )

    world = holding_world(env, "h", full_specs(), kind="chain", base=relative_base())
    builder_class = {
        "trace": TraceListQueryBuilderV2,
        "span": SpanListQueryBuilderV2,
        "voice": VoiceCallListQueryBuilderV2,
    }[builder]
    filters = _request_shapes(world)[shape]
    holds = hide_simulated(world.project_id)
    assert holds is True

    built = {}
    for hide in (False, holds):
        instance = builder_class(
            project_id=world.project_id,
            filters=list(filters),
            page_number=0,
            page_size=30,
            eval_config_ids=[],
            annotation_label_ids=[],
            hide_simulated=hide,
        )
        built[hide] = instance
    # The cursor rule, the batch sizes and the read lane follow the request's
    # own filters; the rule is never an element of the filter list.
    assert _builder_decisions(built[True]) == _builder_decisions(built[False])
    assert built[True].filters == built[False].filters == list(filters)
    assert SIMULATION_MARK_KEY not in json.dumps(built[True].filters)
    assert RESOURCE_MARK_KEY not in json.dumps(built[True].filters)


# --- cached and background reads ----------------------------------------------


def _worker_identity(env, world, **extra):
    return {
        "project_id": world.project_id,
        "organization_id": str(env.organization.id),
        "workspace_id": str(env.workspace.id),
        "filters": [world.window],
        "interval": "hour",
        **extra,
    }


def _payload(env, namespace, world, **extra):
    from tracer.tasks import exact_aggregation

    return exact_aggregation._observe_payload(
        namespace, _worker_identity(env, world, **extra)
    )


def _names(payload) -> tuple:
    return tuple(
        sorted((node["name"], node["span_count"]) for node in payload["nodes"])
    )


def test_background_refresh_keeps_the_predicate(env):
    env.enable_background_worker()
    comparison = make_comparison(env, "chain", extras=True)
    h, rt, rs = comparison.h, comparison.rt, comparison.rs

    # The worker, scheduled through the real entry points: the snapshot it
    # stores is the one a reader is served.
    for name in ("agent_graph", "charts_eval"):
        reader = READERS[name]
        held, labels = observe(reader, env, [h])
        reference, reference_labels = observe(
            reader, env, [reference_of(comparison, reader)]
        )
        assert held == reference, name
        assert_answered(reference, reference_labels)
        if labels is not None:
            assert labels.isdisjoint(leaks_of(reader)), name
    assert {namespace for namespace, _ in env.refreshes} >= {
        "observe-agent-graph",
        "observe-eval-chart-series",
    }
    for namespace, identity in env.refreshes:
        if identity.get("project_id") in {h.project_id, rt.project_id, rs.project_id}:
            assert identity.get("hide_simulated") is True, namespace

    # The refresh of every namespace, given an identity with the key: the
    # statement of the interactive read of the same key.
    cases = {
        "observe-system-graph": (
            {"metric_id": "latency", "observe_type": "trace"},
            comparison.rt,
        ),
        "observe-all-system-graphs": ({}, comparison.rs),
        "observe-agent-graph": ({}, comparison.rt),
        "observe-eval-graph": (
            {"req_data_config": "eval", "observe_type": "trace"},
            comparison.rt,
        ),
        "observe-annotation-graph": (
            {"req_data_config": "label", "observe_type": "trace"},
            comparison.rt,
        ),
    }

    def extra_for(world, extra):
        extra = dict(extra)
        if extra.get("req_data_config") == "eval":
            extra["req_data_config"] = {"id": str(world.eval_config.id), "type": "EVAL"}
        if extra.get("req_data_config") == "label":
            extra["req_data_config"] = {
                "id": str(world.annotation_label.id),
                "type": "ANNOTATION",
            }
        return extra

    for namespace, (extra, reference_world) in cases.items():
        held = _payload(env, namespace, h, hide_simulated=True, **extra_for(h, extra))
        reference = _payload(
            env,
            namespace,
            reference_world,
            hide_simulated=True,
            **extra_for(reference_world, extra),
        )
        if namespace == "observe-agent-graph":
            answered = _names(reference)
            assert _names(held) == answered, namespace
        elif namespace == "observe-all-system-graphs":
            answered = _bundle(reference)
            assert _bundle(held) == answered, namespace
        else:
            answered = _points(reference)
            assert _points(held) == answered, namespace
        assert has_signal(answered), f"{namespace}: the reference answered nothing"

        # An identity without the key is computed without the predicate, as
        # its interactive read is, and never asks Postgres whether the
        # project holds.
        with CaptureQueriesContext(connection) as queries:
            unhidden = _payload(env, namespace, h, **extra_for(h, extra))
        assert not [
            q
            for q in queries.captured_queries
            if "tracer_observability_provider" in q["sql"]
        ], namespace
        if namespace == "observe-agent-graph":
            assert _names(unhidden) != _names(held), namespace
        elif namespace == "observe-all-system-graphs":
            assert _bundle(unhidden) != _bundle(held), namespace
        else:
            assert _points(unhidden) != _points(held), namespace

    # A dashboard refreshed on the worker writes under the key it was asked for.
    for world, reference_world in ((h, rs),):
        held = _scheduled_dashboard(env, [world], DASHBOARD_SCORE)
        reference = _scheduled_dashboard(env, [reference_world], DASHBOARD_SCORE)
        answered = READERS["dashboard_score"].observe(reference, [reference_world])
        assert READERS["dashboard_score"].observe(held, [world]) == answered
        assert_answered(*answered)
    dashboard_identities = [
        identity
        for namespace, identity in env.refreshes
        if namespace == "dashboard-query"
    ]
    assert dashboard_identities
    assert all(
        identity.get("hide_simulated") is True for identity in dashboard_identities
    )

    # The system, eval and annotation charts answer a cold read directly, so
    # each is sent to the background through its own entry point: the system
    # chart by a refresh request, the other two by a read the interactive wall
    # refuses. The all-metrics chart has no entry point that schedules it (its
    # fetch drops the refresh request), so the worker call above is the only
    # consumer of its identity.
    from tracer.services.clickhouse import graph_dispatch

    graph_url = "/tracer/trace/get_graph_methods/"
    before = len(env.refreshes)
    env.client.post(
        f"{graph_url}?refresh=true", _chart_body(h, "latency"), format="json"
    )
    env.monkeypatch.setattr(
        graph_dispatch,
        "_affordable_raw_log_membership_read",
        lambda **kwargs: graph_dispatch._GraphReadUnaffordable(estimated_rows=None),
    )
    env.client.post(
        graph_url, _chart_body(h, str(h.eval_config.id), "EVAL"), format="json"
    )
    env.client.post(
        graph_url,
        _chart_body(h, str(h.annotation_label.id), "ANNOTATION"),
        format="json",
    )
    scheduled = {
        namespace: identity
        for namespace, identity in env.refreshes[before:]
        if identity.get("project_id") == h.project_id
    }
    for namespace in (
        "observe-system-graph",
        "observe-eval-graph",
        "observe-annotation-graph",
    ):
        assert namespace in scheduled, f"{namespace} was not scheduled"
        assert scheduled[namespace].get("hide_simulated") is True, namespace


def _points(payload) -> tuple:
    from tracer.tests.integration.test_simulation_default_parity import _series_value

    return _series_value(payload)


def _bundle(payload) -> tuple:
    from tracer.tests.integration.test_simulation_default_parity import _bundle_value

    return _bundle_value(payload)


def _scheduled_dashboard(env, worlds, metric):
    """Ask for a refresh, then read what the worker stored."""
    body = dashboard_config(worlds, metric)
    payload = None
    for url in (
        "/tracer/dashboard/query/?refresh=true",
        "/tracer/dashboard/query/",
        "/tracer/dashboard/query/",
    ):
        response = env.client.post(url, body, format="json")
        assert response.status_code == 200, response.content[:500]
        payload = response.json().get("result", response.json())
        if payload.get("query_status", "complete") == "complete":
            return payload
    raise AssertionError(f"the dashboard snapshot never completed: {payload}")


def test_snapshot_from_before_holding_is_not_served(env):
    env.enable_background_worker()
    base = relative_base()
    world = seed_world(env, "x", full_specs(), kind="chain", base=base)
    reader = READERS["agent_graph"]

    before_value, before_labels = observe(reader, env, [world])
    assert before_labels & leaks_of(reader), "the project did not hold yet"
    scheduled = len(env.refreshes)
    # The same read again is served from the stored snapshot.
    observe(reader, env, [world])
    assert len(env.refreshes) == scheduled

    record_holding(world.project_id)
    after_value, after_labels = observe(reader, env, [world])

    # The stored snapshot has no key, so it is not served; a new one is made,
    # under the key, without the marked traces.
    assert len(env.refreshes) == scheduled + 1
    assert env.refreshes[-1][1].get("hide_simulated") is True
    assert after_labels.isdisjoint(leaks_of(reader))
    assert after_value != before_value


@pytest.mark.parametrize("name", ["charts_single", "charts_all", "charts_eval"])
def test_single_metric_and_eval_charts_hide(env, name):
    reader = READERS[name]
    check_hides(env, reader, make_comparison(env, "chain", extras=reader.extras))


# --- by id, and the agent id ----------------------------------------------------


def test_detail_by_id_returns_marked_trace(env):
    comparison = make_comparison(env, "voice")
    world = comparison.h
    for label in ("s", "r", "m"):
        trace_id = world.trace_ids[label]
        response = env.client.get(
            "/tracer/trace/voice_call_detail/",
            {"trace_id": trace_id, "project_id": world.project_id},
        )
        assert response.status_code == 200, (label, response.content[:500])
        body = response.json().get("result", response.json())
        assert str(body["trace_id"]) == trace_id


def agent_specs() -> list[TraceSpec]:
    heavy = {"weight": 50, "errors": 2}
    return [
        TraceSpec("p_agent", 10, root_attrs={"agent.id": "agent-1"}),
        TraceSpec(
            "s_connect",
            15,
            span_attrs={
                SIMULATION_MARK_KEY: "call-1",
                "agent.id": "agent-1",
                "run.mode": "connect_only",
            },
            **heavy,
        ),
        TraceSpec(
            "s_copy",
            20,
            span_attrs={
                SIMULATION_MARK_KEY: "call-2",
                "agent.id": "agent-2",
                "run.mode": "environment_backed",
            },
            **heavy,
        ),
        TraceSpec(
            "s_import",
            25,
            span_attrs={
                SIMULATION_MARK_KEY: "call-3",
                "agent.id": "agent-1",
                "run.mode": "provider_import",
            },
            **heavy,
        ),
    ]


@pytest.mark.parametrize("name", ["voice_list", "trace_list", "span_list"])
def test_mark_hides_whatever_the_agent_id(env, name):
    reader = READERS[name]
    kind = "voice" if reader.kind == "voice" else "chain"
    base = relative_base()
    held = holding_world(env, "h", agent_specs(), kind=kind, base=base)
    plain = holding_world(env, "t", agent_specs(), kind=kind, base=base, holds=False)

    _, held_labels = observe(reader, env, [held])
    _, plain_labels = observe(reader, env, [plain])

    shown = {label.split("/", 1)[1].split("/")[0] for label in held_labels}
    # The production call shares agent-1 with two marked calls: the mark, not
    # the agent, decides.
    assert shown == {"p_agent"}
    assert {"s_connect", "s_copy", "s_import"} <= {
        label.split("/", 1)[1].split("/")[0] for label in plain_labels
    }


def test_marked_root_over_unmarked_children(env):
    comparisons = {}
    results = {}
    for reader in READERS.values():
        if reader.extras or reader.name == "dashboard_fast_path":
            continue
        key = (reader.kind, tuple(sorted(reader.world_options.items())))
        if key not in comparisons:
            comparisons[key] = make_comparison(env, reader.kind, **reader.world_options)
        comparison = comparisons[key]
        results[reader.name] = tuple(
            observe(reader, env, [world])
            for world in (comparison.h, comparison.rt, comparison.rs)
        )

    def value(name, which):
        return results[name][which][0]

    def labels(name, which):
        return results[name][which][1]

    held, trace_ref, span_ref = 0, 1, 2

    # A reader that judges a whole trace leaves the trace out with its root;
    # a reader that judges one span keeps the two unmarked children.
    trace_label_readers = {
        "trace_list",
        "trace_list_by_version",
        "trace_csv",
        "voice_list",
        "voice_csv",
        "eval_task_traces",
        "eval_task_traces_unlimited",
        "eval_task_traces_continuous",
        "eval_task_voice",
        "eval_task_voice_unlimited",
        "eval_task_voice_continuous",
        "queue_traces",
        "queue_voice",
        "reader_recent_roots",
    }
    span_label_readers = {
        "span_list",
        "span_list_non_observe",
        "span_csv",
        "eval_task_spans",
        "eval_task_spans_unlimited",
        "eval_task_spans_continuous",
        "queue_spans",
        "reader_ids",
    }
    for name in trace_label_readers:
        assert "0/m" not in labels(name, held), name
    for name in span_label_readers:
        assert {"0/m/1", "0/m/2"} <= labels(name, held), name
        assert "0/m/0" not in labels(name, held), name

    # Numbers that add up every span (a monitor counts, sums and averages all
    # spans; the project chart and the span chart read the span grain; a
    # custom attribute is read from each span that has it) count the two
    # children: the answer is the one without the root, not the one without
    # the whole trace.
    for name in ("dashboard_score", "monitors", "project_chart", "span_chart"):
        assert value(name, held) == value(name, span_ref), name
        assert value(name, held) != value(name, trace_ref), name
    # The trace chart judges the trace by every span it has, so the marked root
    # takes the children with it, and the rootless copy of the trace is counted
    # in full.
    assert value("trace_chart", held) == value("trace_chart", trace_ref)
    assert value("trace_chart", held) != value("trace_chart", span_ref)
    # Dashboard latency averages root spans only (the builder restricts it to
    # a missing parent), and the children are not roots: with the marked root
    # hidden the answer is the production roots', whichever way the trace is
    # judged.
    assert has_signal(value("dashboard_latency", trace_ref))
    assert (
        value("dashboard_latency", held)
        == value("dashboard_latency", trace_ref)
        == value("dashboard_latency", span_ref)
    )


def test_rootless_marked_trace_stays_out_of_every_trace_grain_chart(env):
    base = relative_base()
    with_rootless = [
        *production_specs(),
        TraceSpec("l", 27, resource_attrs=RESOURCE_ATTRS, rootless=True, weight=50),
    ]
    held = holding_world(env, "h", with_rootless, kind="chain", base=base, extras=True)
    plain = holding_world(
        env, "t", with_rootless, kind="chain", base=base, extras=True, holds=False
    )
    reference = holding_world(
        env, "rt", production_specs(), kind="chain", base=base, extras=True
    )

    for name in (
        "trace_chart",
        "agent_graph",
        "eval_chart_trace",
        "annotation_chart_trace",
    ):
        reader = READERS[name]
        held_value, _ = observe(reader, env, [held])
        reference_value, reference_labels = observe(reader, env, [reference])
        assert held_value == reference_value, name
        assert_answered(reference_value, reference_labels)
    # The trace has no root, yet a read that does not hide counts its children.
    for name in ("trace_chart", "agent_graph"):
        reader = READERS[name]
        plain_value, _ = observe(reader, env, [plain])
        reference_value, _ = observe(reader, env, [reference])
        assert plain_value != reference_value, name


# --- the investigator ------------------------------------------------------------


def test_investigator_cancels_marked_jobs_and_claims_the_rest(env):
    from django.utils import timezone

    from tracer.models.trace_investigation import (
        TraceInvestigationJob,
        TraceInvestigationJobState,
    )
    from tracer.models.trace_scan import TraceScanConfig
    from tracer.services.trace_investigation import claim_due_investigations

    base = relative_base()
    held = holding_world(env, "h", full_specs(), kind="chain", base=base)
    plain = holding_world(env, "t", full_specs(), kind="chain", base=base, holds=False)
    engine = "omega-v1"
    first_due = timezone.now() - timedelta(days=1)
    for world in (held, plain):
        TraceScanConfig.no_workspace_objects.create(
            project=world.project,
            sampling_rate=1.0,
            enabled=True,
            scan_version=engine,
        )
        # The marked traces head the queue.
        for index, label in enumerate(("s", "r", "p_old", "p_mid")):
            TraceInvestigationJob.no_workspace_objects.create(
                organization=env.organization,
                workspace=env.workspace,
                project=world.project,
                trace_id=world.trace_ids[label],
                root_span_id="0123456789abcdef",
                root_end_time=first_due,
                not_before=first_due + timedelta(seconds=index),
            )

    claims = claim_due_investigations(
        worker_id="reader-test", engine_version=engine, limit=10
    )["claims"]
    by_project = {str(claim["project_id"]): str(claim["trace_id"]) for claim in claims}

    assert by_project[held.project_id] == held.trace_ids["p_old"]
    assert by_project[plain.project_id] == plain.trace_ids["s"]
    cancelled = TraceInvestigationJob.no_workspace_objects.filter(
        project=held.project, state=TraceInvestigationJobState.CANCELLED
    ).values_list("trace_id", flat=True)
    assert {str(i) for i in cancelled} == {held.trace_ids["s"], held.trace_ids["r"]}


# --- both schemas, the large fixture ----------------------------------------------


def _count(env, world, predicate: str) -> int:
    rows = (
        env.analytics()
        .execute_ch_query(
            "SELECT count() AS n FROM spans "
            "WHERE project_id = toUUID(%(project_id)s) AND is_deleted = 0 "
            f"AND {predicate}",
            {"project_id": world.project_id},
        )
        .data
    )
    return int(rows[0]["n"])


def test_resource_mark_text_parses_and_runs_on_both_schemas(env):
    from tracer.services.clickhouse.v2.query_builders.filters import (
        rewrite_v1_sql_to_v2,
    )

    world = holding_world(env, "h", full_specs(), kind="chain", base=relative_base())
    # What stays is every span of the production traces and the two children
    # of the trace whose root is marked.
    expected = sum(2 if spec.rootless else 3 for spec in span_reference_specs())
    current = row_exclusion_sql(map_column="attrs_string")
    legacy = rewrite_v1_sql_to_v2(row_exclusion_sql(map_column="span_attr_str"))
    for predicate in (current, legacy):
        assert _count(env, world, predicate) == expected
        assert (
            _count(env, world, f"NOT ({predicate})") == len(world.span_ids) - expected
        )
    assert RESOURCE_MARK_KEY in current


def test_trace_count_leaves_out_marked_traces_and_skips_the_rollup(env):
    from tracer.services.clickhouse.v2.query_builders.trace_list import (
        TraceListQueryBuilderV2,
    )

    comparison = make_comparison(env, "chain")

    def count(world, *, hide):
        builder = TraceListQueryBuilderV2(
            project_id=world.project_id,
            filters=[world.window],
            page_number=0,
            page_size=30,
            eval_config_ids=[],
            annotation_label_ids=[],
            hide_simulated=hide,
        )
        builder.build()
        sql, params = builder.build_count_query()
        rows = env.analytics().execute_ch_query(sql, params).data
        return sql, int(rows[0]["total"])

    held_sql, held_total = count(
        comparison.h, hide=hide_simulated(comparison.h.project_id)
    )
    reference_sql, reference_total = count(
        comparison.rt, hide=hide_simulated(comparison.rt.project_id)
    )
    plain_sql, _ = count(comparison.t, hide=hide_simulated(comparison.t.project_id))

    # Only the date window is asked for, so a project that holds nothing is
    # counted from the rollup, which has no attribute to leave a mark out by.
    assert "trace_count_rollup" in plain_sql
    assert "trace_count_rollup" not in held_sql
    assert "trace_count_rollup" not in reference_sql
    assert reference_total == len(production_specs())
    assert held_total == reference_total


def large_specs(count: int) -> list[TraceSpec]:
    specs = []
    for index in range(count):
        kwargs = {}
        if index % 5 == 0:
            kwargs["resource_attrs"] = RESOURCE_ATTRS
        elif index % 11 == 0:
            kwargs["span_attrs"] = {SIMULATION_MARK_KEY: f"call-{index}"}
        if kwargs:
            kwargs["weight"] = 50
        specs.append(TraceSpec(f"t{index}", 1 + index % 58, **kwargs))
    return specs


@pytest.mark.slow
def test_unindexed_json_read_is_timed_on_the_large_fixture(env):
    from tracer.services.clickhouse.v2.query_builders.filters import (
        rewrite_v1_sql_to_v2,
    )

    base = relative_base()
    specs = large_specs(LARGE_TRACES)
    world = holding_world(env, "big", specs, kind="chain", base=base)
    marked = sum(1 for spec in specs if spec.weight == 50)
    spans = len(world.span_ids)
    expected_kept = spans - 3 * marked
    current = row_exclusion_sql(map_column="attrs_string")
    legacy = rewrite_v1_sql_to_v2(row_exclusion_sql(map_column="span_attr_str"))

    for schema, predicate in (("v2", current), ("v1_rewritten", legacy)):
        kept = timed(
            f"exclusion_scan_{schema}",
            lambda predicate=predicate: _count(env, world, predicate),
            spans=spans,
        )
        assert kept == expected_kept

    # The lookup the investigator makes, over the biggest page it asks for.
    page = [world.trace_ids[f"t{i}"] for i in range(200)]
    found = timed(
        "marked_trace_ids_200",
        lambda: marked_trace_ids(project_id=world.project_id, trace_ids=page),
        spans=spans,
    )
    assert len(found) == sum(1 for i in range(200) if specs[i].weight == 50)

    for name in ("span_chart", "trace_chart", "project_chart"):
        reader = READERS[name]
        payload = timed(
            f"reader_{name}",
            lambda reader=reader: reader.call(env, [world], "single"),
            spans=spans,
        )
        assert has_signal(reader.observe(payload, [world])[0])


# --- the dashboard builder, every widget shape -------------------------------------

TIER_METRIC = {
    "id": "tier",
    "name": "tier",
    "type": "custom_attribute",
    "source": "traces",
    "aggregation": "count_distinct",
    "attribute_key": "tier",
    "attribute_type": "string",
}
USER_COUNT = {
    "id": "user_count",
    "name": "user_count",
    "type": "system_metric",
    "source": "traces",
    "aggregation": "count_distinct",
}


def dashboard_shapes(world):
    """(id, metric, filters, breakdowns, is replay, population is hidden)."""
    label = str(world.annotation_label.id) if world.annotation_label else ""
    config = str(world.eval_config.id) if world.eval_config else ""
    annotation_metric = {
        "id": "annotation",
        "name": label,
        "type": "annotation_metric",
        "label_id": label,
        "output_type": "numeric",
        "source": "traces",
        "aggregation": "avg",
    }
    eval_metric = {
        "id": "eval",
        "name": config,
        "type": "eval_metric",
        "config_id": config,
        "output_type": "SCORE",
        "source": "traces",
        "aggregation": "avg",
    }
    annotation_breakdown = {
        "name": label,
        "type": "annotation_metric",
        "label_id": label,
        "source": "traces",
    }
    return [
        (
            "latency_attribute_filter",
            DASHBOARD_LATENCY,
            [STATUS_FILTER],
            [],
            True,
            True,
        ),
        ("non_number_custom_metric", TIER_METRIC, [], [], True, True),
        ("number_plain", DASHBOARD_SCORE, [], [], False, True),
        ("number_filtered", DASHBOARD_SCORE, [TENANT_FILTER], [], False, True),
        ("number_broken_down", DASHBOARD_SCORE, [], [REGION_BREAKDOWN], False, True),
        (
            "number_filtered_broken_down",
            DASHBOARD_SCORE,
            [TENANT_FILTER],
            [REGION_BREAKDOWN],
            False,
            True,
        ),
        ("latency_unfiltered", DASHBOARD_LATENCY, [], [], False, True),
        # No span carries an end user, so the count is zero with or without the rule.
        ("user_count", USER_COUNT, [], [], False, False),
        (
            "latency_by_annotation",
            DASHBOARD_LATENCY,
            [],
            [annotation_breakdown],
            False,
            False,
        ),
        (
            "annotation_metric_with_span_filter",
            annotation_metric,
            [STATUS_FILTER],
            [],
            True,
            False,
        ),
        ("eval_metric_with_span_filter", eval_metric, [STATUS_FILTER], [], True, False),
    ]


def run_widget(env, world, shape, *, hide: bool):
    from tracer.views import dashboard as dashboard_view

    _, metric, filters, breakdowns, _, _ = shape
    config = dashboard_config(
        [world], metric, filters=list(filters), breakdowns=list(breakdowns)
    )
    serializer = dashboard_view.DashboardReadQuerySerializer(data=config)
    assert serializer.is_valid(), serializer.errors
    query_config = dashboard_view._normalize_dashboard_query_filters(
        serializer.validated_data
    )
    if hide:
        query_config["hide_simulated"] = True
    builder = dashboard_view.DashboardQueryBuilderV2(query_config)
    # The public read always runs on the latest physical state.
    builder._latest_state_spans_required = True
    sql, params = builder.build_metric_query(builder.metrics[0])
    started = time.perf_counter()
    rows = (
        env.analytics()
        .execute_ch_query(
            sql, params, settings=dict(dashboard_view._DASHBOARD_TRACE_READ_SETTINGS)
        )
        .data
    )
    elapsed = time.perf_counter() - started
    return rows, elapsed


def widget_value(rows) -> tuple:
    return tuple(
        sorted(
            (str(row.get("breakdown_value", "")), json.dumps(row.get("value")))
            for row in rows
        )
    )


@pytest.mark.parametrize(
    "shape_id",
    [
        shape[0]
        for shape in dashboard_shapes(
            SimpleNamespace(annotation_label=None, eval_config=None)
        )
    ],
)
def test_every_dashboard_widget_shape_runs_on_a_resource_marked_holding_project(
    env, shape_id
):
    comparison = make_comparison(env, "chain", extras=True)
    held_shape = next(s for s in dashboard_shapes(comparison.h) if s[0] == shape_id)
    reference_shape = next(
        s for s in dashboard_shapes(comparison.rs) if s[0] == shape_id
    )
    assert hide_simulated(comparison.h.project_id)

    held_rows, elapsed = run_widget(env, comparison.h, held_shape, hide=True)
    record_timing(f"dashboard_widget_{shape_id}", elapsed, replay=held_shape[4])
    reference_rows, _ = run_widget(env, comparison.rs, reference_shape, hide=True)

    if held_shape[5]:
        # The population is spans: the marked traces, both marks, are out.
        assert widget_value(held_rows) == widget_value(reference_rows)
        plain_rows, _ = run_widget(env, comparison.t, held_shape, hide=False)
        assert widget_value(plain_rows) != widget_value(held_rows)
    else:
        # The population is the eval or annotation rows; the statement has to
        # parse and run with the packed resource column, and it answers.
        assert isinstance(held_rows, list)


@pytest.mark.slow
@pytest.mark.parametrize(
    "shape_id", ["latency_attribute_filter", "non_number_custom_metric"]
)
def test_dashboard_replay_read_is_timed_on_the_large_fixture(env, shape_id):
    base = relative_base()
    world = holding_world(
        env, "big", large_specs(LARGE_TRACES), kind="chain", base=base
    )
    shape = next(s for s in dashboard_shapes(world) if s[0] == shape_id)
    rows, elapsed = run_widget(env, world, shape, hide=True)
    record_timing(f"dashboard_replay_{shape_id}", elapsed, spans=len(world.span_ids))
    assert rows is not None
    assert elapsed <= TIMING_BUDGET_SECONDS


def test_holding_decision_is_the_real_read(env):
    """The cases above stand on the real decision, not a stand-in."""
    world = holding_world(
        env, "h", production_specs(), kind="chain", base=relative_base()
    )
    other = seed_world(env, "o", production_specs(), kind="chain", base=relative_base())
    assert hide_simulated(world.project_id) is True
    assert hide_simulated(other.project_id) is False
    assert holding_projects([world.project_id, other.project_id]) == frozenset(
        {world.project_id}
    )


# --- cases that wait for the hosted-provider path -----------------------------------


@pytest.mark.skip(
    reason="needs contract 04's exporter and children under a production call; "
    "the hosted-provider path moved to next week"
)
def test_production_children_change_only_the_listed_numbers():
    raise AssertionError("not built this week")


@pytest.mark.skip(
    reason="needs contract 04's exporter; the hosted-provider path moved to next week"
)
def test_stored_call_hidden_from_the_ingest_that_marks_it():
    raise AssertionError("not built this week")


@pytest.mark.skip(
    reason="needs contract 04's exporter; the hosted-provider path moved to next week"
)
def test_holding_is_recorded_before_the_first_marked_export():
    raise AssertionError("not built this week")
