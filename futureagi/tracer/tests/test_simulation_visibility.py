"""The module every reader asks which projects hide simulated traces.

The tests that need ClickHouse seed the real ``spans`` table the way
``test_voice_list_simulator_raw_log`` does and run the module's SQL text against
it; the rest need only Postgres, or nothing.
"""

from __future__ import annotations

import ast
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from django.db import connection
from structlog.testing import capture_logs

from accounts.models.workspace import Workspace
from model_hub.models.ai_model import AIModel
from tracer.models.observability_provider import (
    ObservabilityProvider,
    ProviderChoices,
)
from tracer.models.project import Project
from tracer.services.clickhouse.v2.query_builders.filters import (
    rewrite_v1_sql_to_v2,
)
from tracer.services.simulation_visibility import (
    HOLDING_FLAG,
    MARKED_LOOKUP_TIMEOUT_MS,
    RESOURCE_MARK_KEY,
    SIMULATION_MARK_KEY,
    HoldingNotRecorded,
    MarkedLookupFailed,
    hide_simulated,
    holding_projects,
    marked_trace_ids,
    record_holding,
    resource_mark_sql,
    row_exclusion_sql,
)
from tracer.tests._ch_seed import seed_ch_spans

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Written out in full so a change to the module cannot move both sides of a
# comparison.
_V2_EXCLUSION = (
    "NOT (mapContains(attrs_string, 'fi.simulator.call_execution_id') "
    "OR resource_attrs.fi.simulation.origin.:String IS NOT NULL)"
)
_V1_EXCLUSION = (
    "NOT (mapContains(span_attr_str, 'fi.simulator.call_execution_id') "
    "OR JSONHas(resource_attributes_raw, 'fi.simulation.origin'))"
)
_V1_EXCLUSION_AFTER_REWRITER = (
    "NOT (mapContains(attrs_string, 'fi.simulator.call_execution_id') "
    "OR (resource_attrs.fi.simulation.origin.:String IS NOT NULL))"
)
_RECORD_HOLDING_STATEMENT = (
    "UPDATE tracer_observability_provider SET metadata = "
    "(CASE WHEN jsonb_typeof(metadata) = 'object' THEN metadata "
    "ELSE '{}'::jsonb END) || '{\"holds_simulated\": true}'::jsonb "
    "WHERE project_id = %(project_id)s"
)


def _project(organization, workspace):
    return Project.no_workspace_objects.create(
        name=f"visibility-{uuid.uuid4().hex[:10]}",
        organization=organization,
        workspace=workspace,
        model_type=AIModel.ModelTypes.GENERATIVE_LLM,
        trace_type="observe",
    )


def _provider(project, organization, workspace, metadata=None):
    return ObservabilityProvider.no_workspace_objects.create(
        project=project,
        provider=ProviderChoices.VAPI,
        organization=organization,
        workspace=workspace,
        metadata={} if metadata is None else metadata,
    )


def _metadata(provider):
    return ObservabilityProvider._base_manager.values_list("metadata", flat=True).get(
        pk=provider.pk
    )


def _set_metadata_literal(provider, literal):
    """Store ``literal`` (JSON text, or None for SQL NULL) in the row's column."""
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE tracer_observability_provider SET metadata = %s::jsonb "
            "WHERE id = %s",
            [literal, str(provider.id)],
        )


def _statements():
    """Record every statement the connection runs, as (sql, params)."""
    seen = []

    def spy(execute, sql, params, many, context):
        seen.append((sql, params))
        return execute(sql, params, many, context)

    return seen, connection.execute_wrapper(spy)


# ─── The text ─────────────────────────────────────────────────────────────────


def test_row_exclusion_sql_text():
    assert row_exclusion_sql(map_column="attrs_string") == _V2_EXCLUSION
    assert row_exclusion_sql(map_column="span_attr_str") == _V1_EXCLUSION
    for other in ("attrs_number", "span_attributes_raw", "", "ATTRS_STRING"):
        with pytest.raises(
            ValueError, match="map_column must be attrs_string or span_attr_str"
        ):
            row_exclusion_sql(map_column=other)
    with pytest.raises(ValueError, match="schema must be v1 or v2"):
        resource_mark_sql(schema="v3")


def _code_strings(source):
    """String values in a Python module, leaving out comments and docstrings."""
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstrings.add(id(first.value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                yield node.value


def _spells_a_mark(path, keys):
    text = path.read_text(encoding="utf-8", errors="replace")
    if not any(key in text for key in keys):
        return False
    if path.suffix == ".go":
        code = [ln for ln in text.splitlines() if not ln.lstrip().startswith("//")]
        return any(key in ln for ln in code for key in keys)
    try:
        return any(key in value for value in _code_strings(text) for key in keys)
    except SyntaxError:
        return True


def test_key_is_spelled_in_one_module():
    # The collector writes the resource mark and cannot import Python, so its
    # converter spells the key itself. No other file may.
    collector_converter = "fi-collector/exporter/clickhouse25exporter/converter.go"
    keys = (SIMULATION_MARK_KEY, RESOURCE_MARK_KEY)
    owner = "futureagi/tracer/services/simulation_visibility.py"
    skipped_dirs = {
        ".git",
        ".venv",
        ".uv-cache",
        "venv",
        "node_modules",
        "__pycache__",
        "migrations",
        "tests",
        "htmlcov",
    }
    found = set()
    for root_name, extension in (("futureagi", ".py"), ("fi-collector", ".go")):
        root = _REPO_ROOT / root_name
        if not root.is_dir():
            continue
        for current, dirs, files in os.walk(root):
            dirs[:] = [name for name in dirs if name not in skipped_dirs]
            for name in files:
                if not name.endswith(extension) or name == "conftest.py":
                    continue
                if name.startswith("test_") or name.endswith("_test.go"):
                    continue
                path = Path(current) / name
                if _spells_a_mark(path, keys):
                    found.add(path.relative_to(_REPO_ROOT).as_posix())
    assert found - {collector_converter} == {owner}


# ─── The flag ─────────────────────────────────────────────────────────────────


def test_holding_is_the_flag_on_a_provider_row(organization, workspace, user):
    other_workspace = Workspace.no_workspace_objects.create(
        name=f"visibility-ws-{uuid.uuid4().hex[:8]}",
        organization=organization,
        is_active=True,
        created_by=user,
    )

    def project_with(metadata, *, ws=workspace, deleted=False, row=True):
        project = Project.no_workspace_objects.create(
            name=f"visibility-{uuid.uuid4().hex[:10]}",
            organization=organization,
            workspace=ws,
            model_type=AIModel.ModelTypes.GENERATIVE_LLM,
            trace_type="observe",
        )
        if row:
            provider = ObservabilityProvider.no_workspace_objects.create(
                project=project,
                provider=ProviderChoices.VAPI,
                organization=organization,
                workspace=ws,
                metadata=metadata,
            )
            if deleted:
                provider.delete()
        return str(project.id)

    holding = project_with({"assistant_id": "a", HOLDING_FLAG: True})
    # The row is soft-deleted and sits in a workspace the request does not see;
    # the project still received marked spans once.
    soft_deleted = project_with({HOLDING_FLAG: True}, deleted=True)
    other_ws = project_with({HOLDING_FLAG: True}, ws=other_workspace)
    quiet = [
        project_with({"assistant_id": "b"}),
        project_with({}),
        project_with({HOLDING_FLAG: False}),
        project_with({HOLDING_FLAG: "true"}),
        project_with({HOLDING_FLAG: 1}),
        project_with(None),
        project_with("x"),
        project_with([HOLDING_FLAG]),
        project_with(None, row=False),
    ]

    assert holding_projects([holding, soft_deleted, other_ws, *quiet]) == {
        holding,
        soft_deleted,
        other_ws,
    }
    assert holding_projects(quiet) == frozenset()
    assert holding_projects([]) == frozenset()
    assert holding_projects(iter([holding])) == {holding}

    assert hide_simulated(holding) is True
    assert hide_simulated(soft_deleted) is True
    assert hide_simulated(other_ws) is True
    assert not any(hide_simulated(project_id) for project_id in quiet)
    # The answer does not depend on how the caller spells the id.
    assert hide_simulated(uuid.UUID(holding)) is True
    assert hide_simulated(holding.upper()) is True
    assert hide_simulated(str(uuid.uuid4())) is False


def test_no_clickhouse_read_in_holding_projects(
    organization, workspace, monkeypatch, django_assert_num_queries
):
    import clickhouse_connect

    from tracer.services.clickhouse import client as ch_client
    from tracer.services.clickhouse import query_service
    from tracer.services.clickhouse.v2 import query_service as v2_query_service

    def refuse(*args, **kwargs):
        raise AssertionError("holding_projects must not touch ClickHouse")

    monkeypatch.setattr(ch_client.ClickHouseClient, "_get_client", refuse)
    monkeypatch.setattr(ch_client, "get_clickhouse_client", refuse)
    monkeypatch.setattr(query_service.AnalyticsQueryService, "execute_ch_query", refuse)
    monkeypatch.setattr(v2_query_service, "get_v2_query_client", refuse)
    monkeypatch.setattr(clickhouse_connect, "get_client", refuse)

    project = _project(organization, workspace)
    _provider(project, organization, workspace, {HOLDING_FLAG: True})
    project_id = str(project.id)

    with django_assert_num_queries(1):
        assert holding_projects([project_id]) == {project_id}
    with django_assert_num_queries(1):
        assert hide_simulated(project_id) is True
    # Nothing to ask means nothing is sent anywhere.
    with django_assert_num_queries(0):
        assert holding_projects([]) == frozenset()


def test_record_holding_is_one_idempotent_update(organization, workspace):
    project = _project(organization, workspace)
    kept = {"assistant_id": "a", "nested": {"k": [1, 2]}}
    row = _provider(project, organization, workspace, dict(kept))
    deleted_row = _provider(project, organization, workspace, {"old": True})
    deleted_row.delete()
    untouched = _provider(_project(organization, workspace), organization, workspace)
    project_id = str(project.id)

    for _ in range(2):
        seen, wrapper = _statements()
        with capture_logs() as logs, wrapper:
            record_holding(project_id)
        # One statement, with the id as a parameter and nothing else.
        assert seen == [(_RECORD_HOLDING_STATEMENT, {"project_id": project_id})]
        # Once per call, not once per row it reached.
        assert [log["event"] for log in logs] == ["simulation_holding_recorded"]
        assert logs[0]["project_id"] == project_id
        # The owner's other keys survive; both rows of the project hold.
        assert _metadata(row) == {**kept, HOLDING_FLAG: True}
        assert _metadata(deleted_row) == {"old": True, HOLDING_FLAG: True}
        assert _metadata(untouched) == {}

    assert holding_projects([project_id]) == {project_id}


@pytest.mark.parametrize(
    "stored",
    [None, "null", '"x"', '["a", {"b": 1}]', "7", "true"],
    ids=["sql_null", "json_null", "string", "array", "number", "boolean"],
)
def test_record_holding_over_non_object_metadata(organization, workspace, stored):
    project = _project(organization, workspace)
    row = _provider(project, organization, workspace)
    _set_metadata_literal(row, stored)

    record_holding(str(project.id))

    assert _metadata(row) == {HOLDING_FLAG: True}
    assert holding_projects([str(project.id)]) == {str(project.id)}


def test_record_holding_without_row_logs_and_raises(organization, workspace):
    no_row = _project(organization, workspace)
    other = _provider(_project(organization, workspace), organization, workspace)

    for project_id in (str(no_row.id), str(uuid.uuid4())):
        with capture_logs() as logs:
            with pytest.raises(HoldingNotRecorded):
                record_holding(project_id)
        assert logs == [
            {
                "event": "simulation_holding_no_provider_row",
                "log_level": "error",
                "project_id": project_id,
            }
        ]

    assert _metadata(other) == {}


# ─── The marks on real span rows ─────────────────────────────────────────────


def _span(project, trace_id, span_id, start, *, parent=None, attrs=None, resource=None):
    return {
        "id": span_id,
        "trace_id": trace_id,
        "project_id": str(project.id),
        "org_id": str(project.organization_id),
        "parent_span_id": parent,
        "name": "root" if parent is None else "child",
        "observation_type": "chain",
        "status": "OK",
        "start_time": start,
        "end_time": start + timedelta(seconds=1),
        "latency_ms": 1000,
        "span_attributes": attrs or {},
        "resource_attributes": resource or {},
        "created_at": start,
        "updated_at": start,
    }


def _trace(project, start, *, attrs=None, resource=None):
    """A root and two children, each carrying the given marks."""
    trace_id = str(uuid.uuid4())
    ids = [uuid.uuid4().hex[:16] for _ in range(3)]
    rows = [
        _span(
            project,
            trace_id,
            ids[index],
            start + timedelta(seconds=index),
            parent=None if index == 0 else ids[0],
            attrs=attrs,
            resource=resource,
        )
        for index in range(3)
    ]
    return trace_id, ids, rows


def _start():
    return (datetime.now(UTC) - timedelta(hours=2)).replace(microsecond=0)


def _run(sql, **params):
    from tracer.services.clickhouse.v2.query_service import V2AnalyticsQueryService

    return V2AnalyticsQueryService().execute_ch_query(sql, params).data


def _kept(project, predicate):
    rows = _run(
        "SELECT id, trace_id FROM spans "
        "WHERE project_id = toUUID(%(project_id)s) AND " + predicate,
        project_id=str(project.id),
    )
    return {row["id"] for row in rows}, {row["trace_id"] for row in rows}


@pytest.mark.integration
def test_key_presence_not_value(organization, workspace):
    project = _project(organization, workspace)
    start = _start()
    empty_value, _, rows_a = _trace(project, start, attrs={SIMULATION_MARK_KEY: ""})
    some_value, _, rows_b = _trace(
        project, start, attrs={SIMULATION_MARK_KEY: "call-1"}
    )
    empty_resource, _, rows_c = _trace(project, start, resource={RESOURCE_MARK_KEY: ""})
    plain, _, rows_d = _trace(project, start, attrs={"other": "x"})
    # A sibling key under the same prefix is not the mark.
    sibling, _, rows_e = _trace(
        project, start, resource={"fi.simulation.job_id": "job-1"}
    )
    seed_ch_spans([*rows_a, *rows_b, *rows_c, *rows_d, *rows_e])

    asked = [empty_value, some_value, empty_resource, plain, sibling]
    assert marked_trace_ids(project_id=str(project.id), trace_ids=asked) == {
        empty_value,
        some_value,
        empty_resource,
    }
    _, kept_traces = _kept(project, row_exclusion_sql(map_column="attrs_string"))
    assert kept_traces == {plain, sibling}


@pytest.mark.integration
def test_row_exclusion_sql_text_per_map_column(organization, workspace):
    assert row_exclusion_sql(map_column="attrs_string") == _V2_EXCLUSION
    assert row_exclusion_sql(map_column="span_attr_str") == _V1_EXCLUSION
    # The legacy text reaches the current table through the rewriter, which
    # gives the resource term its path test.
    rewritten = rewrite_v1_sql_to_v2(row_exclusion_sql(map_column="span_attr_str"))
    assert rewritten == _V1_EXCLUSION_AFTER_REWRITER

    project = _project(organization, workspace)
    start = _start()
    production, _, rows_p = _trace(project, start)
    span_marked, _, rows_s = _trace(project, start, attrs={SIMULATION_MARK_KEY: "c"})
    resource_marked, _, rows_r = _trace(
        project, start, resource={RESOURCE_MARK_KEY: "simulation"}
    )
    seed_ch_spans([*rows_p, *rows_s, *rows_r])

    # Both texts parse against the table and keep exactly the production trace.
    for predicate in (_V2_EXCLUSION, rewritten):
        _, kept_traces = _kept(project, predicate)
        assert kept_traces == {production}
        assert span_marked not in kept_traces
        assert resource_marked not in kept_traces


@pytest.mark.integration
def test_predicate_hides_a_resource_marked_span_and_trace(organization, workspace):
    project = _project(organization, workspace)
    start = _start()
    production, production_ids, rows_p = _trace(project, start)
    resource_marked, _, rows_r = _trace(
        project, start, resource={RESOURCE_MARK_KEY: "simulation"}
    )
    # A resource-marked trace whose spans carry no span key at all.
    assert all(SIMULATION_MARK_KEY not in row["span_attributes"] for row in rows_r)
    span_marked, _, rows_s = _trace(project, start, attrs={SIMULATION_MARK_KEY: "c"})
    seed_ch_spans([*rows_p, *rows_r, *rows_s])

    # The current table's text, and the legacy text after the rewriter that
    # turns it into the same path test.
    for predicate in (
        row_exclusion_sql(map_column="attrs_string"),
        rewrite_v1_sql_to_v2(row_exclusion_sql(map_column="span_attr_str")),
    ):
        kept_spans, kept_traces = _kept(project, predicate)
        # Span grain: only the production spans remain. Trace grain: no span of
        # a marked trace remains.
        assert kept_spans == set(production_ids)
        assert kept_traces == {production}

    asked = [production, resource_marked, span_marked]
    assert marked_trace_ids(project_id=str(project.id), trace_ids=asked) == {
        resource_marked,
        span_marked,
    }


@pytest.mark.integration
def test_marked_trace_ids_answers_for_live_spans_of_the_asked_project(
    organization, workspace
):
    project = _project(organization, workspace)
    other_project = _project(organization, workspace)
    start = _start()
    later = start + timedelta(hours=1)

    marked, _, rows_marked = _trace(project, start, attrs={SIMULATION_MARK_KEY: "c"})
    deleted, _, rows_deleted = _trace(project, start, attrs={SIMULATION_MARK_KEY: "c"})
    unmarked_later, _, rows_unmarked = _trace(
        project, start, attrs={SIMULATION_MARK_KEY: "c"}
    )
    mixed, _, rows_mixed = _trace(project, start)
    rows_mixed[2]["span_attributes"] = {SIMULATION_MARK_KEY: "c"}
    other_projects_trace, _, rows_other = _trace(
        other_project, start, attrs={SIMULATION_MARK_KEY: "c"}
    )
    _, _, rows_not_asked = _trace(project, start, attrs={SIMULATION_MARK_KEY: "c"})

    # Versions are decided by created_at: the newest version of a span is the
    # live one, and a newest version that is deleted or carries no mark removes
    # the mark.
    seed_ch_spans(
        [
            *rows_marked,
            *rows_deleted,
            *rows_unmarked,
            *rows_mixed,
            *rows_other,
            *rows_not_asked,
        ],
        version_from_created_at=True,
    )
    newer = []
    for row in rows_deleted:
        newer.append({**row, "deleted": True, "created_at": later, "updated_at": later})
    for row in rows_unmarked:
        newer.append(
            {**row, "span_attributes": {}, "created_at": later, "updated_at": later}
        )
    seed_ch_spans(newer, version_from_created_at=True)

    asked = [marked, deleted, unmarked_later, mixed, other_projects_trace]
    # A trace marked on only one of its spans is marked. The other project's
    # trace is not this project's, and the one nobody asked about is not named.
    assert marked_trace_ids(project_id=str(project.id), trace_ids=asked) == {
        marked,
        mixed,
    }
    assert marked_trace_ids(project_id=str(other_project.id), trace_ids=asked) == {
        other_projects_trace
    }


# ─── The lookup's contract, with ClickHouse replaced ─────────────────────────


class _FakeService:
    """Stands in for the ClickHouse service and records what it is asked."""

    def __init__(self, *, rows=(), error=None):
        self.rows = list(rows)
        self.error = error
        self.calls = []

    def __call__(self):
        return self

    def execute_ch_query(self, query, params=None, **kwargs):
        self.calls.append((query, params, kwargs))
        if self.error is not None:
            raise self.error
        return type("Result", (), {"data": self.rows})()


@pytest.fixture
def fake_service(monkeypatch):
    def install(**kwargs):
        service = _FakeService(**kwargs)
        monkeypatch.setattr(
            "tracer.services.clickhouse.v2.query_service.V2AnalyticsQueryService",
            service,
        )
        return service

    return install


def test_marked_trace_ids_asks_one_read_bounded_by_the_timeout(fake_service):
    service = fake_service(rows=[{"trace_id": "t1"}, {"trace_id": "t3"}])
    project_id = str(uuid.uuid4())

    answer = marked_trace_ids(project_id=project_id, trace_ids=["t1", "t2", "t3"])

    assert answer == frozenset({"t1", "t3"})
    assert MARKED_LOOKUP_TIMEOUT_MS == 2000
    [(query, params, kwargs)] = service.calls
    assert params == {"project_id": project_id, "trace_ids": ("t1", "t2", "t3")}
    # The server stops the statement, not just the caller's wait.
    assert kwargs["server_execution_cap_ms"] == MARKED_LOOKUP_TIMEOUT_MS
    # Both marks are tested, on live rows, in this project only.
    assert "mapContains(attrs_string, 'fi.simulator.call_execution_id')" in query
    assert "resource_attrs.fi.simulation.origin.:String IS NOT NULL" in query
    assert "argMax(is_deleted, _version)" in query
    assert "project_id = toUUID(%(project_id)s)" in query

    marked_trace_ids(project_id=project_id, trace_ids=["t1"], timeout_ms=750)
    assert service.calls[-1][2]["server_execution_cap_ms"] == 750


def test_marked_trace_ids_raises_on_a_clickhouse_error(fake_service):
    error = RuntimeError("connection reset")
    fake_service(error=error)

    with pytest.raises(MarkedLookupFailed) as raised:
        marked_trace_ids(project_id=str(uuid.uuid4()), trace_ids=["t1"])

    assert raised.value.__cause__ is error


def test_marked_trace_ids_raises_past_the_timeout(fake_service):
    from tracer.services.clickhouse.read_budget import ReadDeadlineExceeded

    fake_service(error=ReadDeadlineExceeded("ClickHouse statement exceeded its cap"))

    with pytest.raises(MarkedLookupFailed):
        marked_trace_ids(project_id=str(uuid.uuid4()), trace_ids=["t1"], timeout_ms=1)


def test_marked_trace_ids_with_nothing_to_ask_reads_nothing(fake_service):
    service = fake_service(error=AssertionError("must not read"))

    assert marked_trace_ids(project_id=str(uuid.uuid4()), trace_ids=[]) == frozenset()
    assert marked_trace_ids(project_id=str(uuid.uuid4()), trace_ids=["", None]) == (
        frozenset()
    )
    assert service.calls == []
