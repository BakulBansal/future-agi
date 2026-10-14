"""Which projects hide simulated traces, and the SQL that hides them.

A simulated run lands its spans in the customer's own project. Its spans carry
one of two marks: a span attribute written for a converted voice call, or a
resource attribute the process of a code agent's run exports on every span. A
trace with any live span carrying either mark is a marked trace.

A project that has ever received a marked trace is "holding": one flag on its
observability provider row, set before the first marked export, so no reader can
see a marked span before the flag exists. Readers hide marked traces for holding
projects only; every other project keeps today's SQL byte for byte.

This module is the only Python that spells either mark key or builds a test for
it. Readers import the SQL text from here so the key, the column each schema
stores it in and the way it is tested cannot drift apart. The collector's Go code
that writes the resource mark cannot import it and is the one other place.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from typing import Literal

import structlog
from django.db import connection

from tracer.services.clickhouse.v2.query_builders import columns as cols

logger = structlog.get_logger(__name__)

# Both keys are matched case-sensitively, as ClickHouse map keys and JSON paths are.
SIMULATION_MARK_KEY = "fi.simulator.call_execution_id"
HOLDING_FLAG = "holds_simulated"
RESOURCE_MARK_KEY = "fi.simulation.origin"

# A lookup runs once per holding candidate while a request waits for it, so it
# gets a short fixed budget instead of the interactive read wall.
MARKED_LOOKUP_TIMEOUT_MS = 2000

# One statement, so the flag is either set or it is not. Merging with ``||`` keeps
# every other key the row's owner stored. The CASE is there because a hand-written
# PATCH can leave ``metadata`` null, a string or an array, and ``||`` over those
# would drop the flag or wrap it in an array: the row must hold either way.
_RECORD_HOLDING_SQL = (
    "UPDATE tracer_observability_provider "
    "SET metadata = (CASE WHEN jsonb_typeof(metadata) = 'object' "
    "THEN metadata ELSE '{}'::jsonb END) || "
    f"'{json.dumps({HOLDING_FLAG: True})}'::jsonb "
    "WHERE project_id = %(project_id)s"
)

# The schema follows the map column: the legacy spans table names its string map
# ``span_attr_str``, the current one ``attrs_string``.
_SCHEMA_OF_MAP_COLUMN: dict[str, Literal["v1", "v2"]] = {
    cols.ATTRS_STRING: "v2",
    "span_attr_str": "v1",
}


class HoldingNotRecorded(Exception):
    """The project has no provider row to carry the holding flag."""


class MarkedLookupFailed(Exception):
    """Which traces are marked could not be answered, so nothing may be guessed."""


def record_holding(project_id: str) -> None:
    """Flag the project's provider row as holding simulated traces.

    Idempotent. A database error propagates. When no row matches the marked spans
    must not be exported into a project that cannot hold, so it raises.
    """
    with connection.cursor() as cursor:
        cursor.execute(_RECORD_HOLDING_SQL, {"project_id": str(project_id)})
        matched = cursor.rowcount
    if matched:
        logger.info("simulation_holding_recorded", project_id=str(project_id))
        return
    logger.error("simulation_holding_no_provider_row", project_id=str(project_id))
    raise HoldingNotRecorded(
        f"Project {project_id} has no observability provider row, so it cannot "
        "record that it holds simulated traces. Simulated spans must not be "
        "exported to it until a provider row exists."
    )


def holding_projects(project_ids: Iterable[str]) -> frozenset[str]:
    """The given projects whose provider row carries the holding flag.

    One indexed Postgres read, no ClickHouse probe and no cache: a flag that was
    set before the first marked export is the whole answer, and a database error
    propagates to the reader as any database error does. The read ignores soft
    deletion and the request's workspace on purpose; a deleted or other-workspace
    row still means the project once received marked spans. Ids come back as
    canonical lowercase UUID text.
    """
    ids = list(project_ids)
    if not ids:
        return frozenset()
    # Imported here so a query builder can import the SQL text above without
    # loading the model layer.
    from tracer.models.observability_provider import ObservabilityProvider

    holding = (
        # ``_base_manager`` skips the soft-delete and workspace filters that
        # ``objects`` and ``all_objects`` apply.
        ObservabilityProvider._base_manager.filter(
            project_id__in=ids, **{f"metadata__{HOLDING_FLAG}": True}
        )
        .order_by()
        .values_list("project_id", flat=True)
        .distinct()
    )
    return frozenset(str(project_id) for project_id in holding)


def hide_simulated(project_id: str) -> bool:
    """Whether readers of this project must leave out marked traces."""
    # The database answers for the one id it was asked about, so the comparison
    # never depends on how the caller spelled the UUID (case, braces, UUID object).
    return bool(holding_projects((project_id,)))


def resource_mark_sql(*, schema: Literal["v1", "v2"]) -> str:
    """The test for the resource mark on a span row, in the given schema's SQL."""
    if schema == "v1":
        return f"JSONHas(resource_attributes_raw, '{RESOURCE_MARK_KEY}')"
    if schema == "v2":
        # The text ``columns.json_path`` writes: ClickHouse flattens dotted
        # resource keys into a path at write time.
        path = cols.json_path(cols.RESOURCE_ATTRS, RESOURCE_MARK_KEY, "String")
        return f"{path} IS NOT NULL"
    raise ValueError("schema must be v1 or v2")


def _marked_row_sql(map_column: str, schema: Literal["v1", "v2"]) -> str:
    """True for a span row carrying either mark. Only the key's presence counts."""
    return (
        f"mapContains({map_column}, '{SIMULATION_MARK_KEY}') "
        f"OR {resource_mark_sql(schema=schema)}"
    )


def row_exclusion_sql(*, map_column: str) -> str:
    """The predicate that leaves a marked span row out of a read.

    Always this exact text, so a reader's SQL for a holding project is the same
    everywhere and a rewriter for the other schema can match it.
    """
    schema = _SCHEMA_OF_MAP_COLUMN.get(map_column)
    if schema is None:
        raise ValueError("map_column must be attrs_string or span_attr_str")
    return f"NOT ({_marked_row_sql(map_column, schema)})"


def _marked_trace_ids_sql() -> str:
    # The table's sorting key is the identity ReplacingMergeTree collapses on, so
    # grouping by it keeps one row per live span. It is read from span_reader,
    # which pins it against the live table, and imported here because span_reader
    # imports this module for its own predicate.
    from tracer.services.clickhouse.v2.span_reader import _SORTING_KEY

    mark = _marked_row_sql(cols.ATTRS_STRING, "v2")
    # No FINAL: filtering a mutable column under FINAL can revive an older
    # version, and FINAL's cost follows the project's span volume rather than the
    # handful of ids asked about. Grouping by the sorting key and taking the
    # newest version's deletion flag and mark keeps the read on the trace-id
    # bloom index.
    return (
        "SELECT trace_id FROM ("
        " SELECT trace_id,"
        " argMax(is_deleted, _version) AS latest_is_deleted,"
        f" argMax(toUInt8(({mark})), _version) AS latest_marked"
        " FROM spans"
        " PREWHERE project_id = toUUID(%(project_id)s)"
        " AND trace_id IN %(trace_ids)s"
        f" GROUP BY {_SORTING_KEY}"
        ") WHERE latest_is_deleted = 0 AND latest_marked = 1"
        " GROUP BY trace_id"
    )


def marked_trace_ids(
    *,
    project_id: str,
    trace_ids: Sequence[str],
    timeout_ms: int = MARKED_LOOKUP_TIMEOUT_MS,
) -> frozenset[str]:
    """Which of the project's given traces have a live span carrying either mark.

    One ClickHouse read that the server stops at ``timeout_ms``. A timeout or any
    ClickHouse error raises ``MarkedLookupFailed``: answering with an empty or a
    partial set would let a marked trace through.
    """
    ids = tuple(dict.fromkeys(str(trace_id) for trace_id in trace_ids if trace_id))
    if not ids:
        return frozenset()
    # Imported here: the query service module reaches every query builder, and
    # the builders import this module.
    from tracer.services.clickhouse.v2.query_service import V2AnalyticsQueryService

    sql = _marked_trace_ids_sql()
    try:
        result = V2AnalyticsQueryService().execute_ch_query(
            sql,
            {"project_id": str(project_id), "trace_ids": ids},
            server_execution_cap_ms=timeout_ms,
        )
    except Exception as exc:
        # A server timeout, a dropped connection and a rejected statement all
        # leave the lookup without an answer; the type decides nothing here.
        raise MarkedLookupFailed(
            f"Could not tell which traces of project {project_id} are simulated: "
            f"the ClickHouse read failed or ran longer than {timeout_ms} ms. "
            "No answer was guessed; ask again."
        ) from exc
    return frozenset(str(row["trace_id"]) for row in result.data)
