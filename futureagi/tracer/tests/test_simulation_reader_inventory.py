"""The record of every reader of the span tables, decided one by one.

A simulated run's traces live in the customer's own project and must not show
in any view of it. Every function or SQL template that can read a span
therefore has to say how it leaves a marked trace out, or why it need not.
``FILED`` is that decision, one entry per reader, under the bucket that covers
it. A reader nobody filed fails the first case below, so a new reader cannot
ship until someone has decided.

A reader is the source of a function (the functions and classes nested in it
count on their own), or of a module-level template, that matches
``READS_A_TABLE`` or builds the hourly rollup source. A file is never filed as
a whole: one file mixes readers that hide, readers that need not, and readers
of another store.

Nothing here opens a Postgres or ClickHouse connection. The scan reads source
files, and the chart cases run each entry point against a recording analytics
double, with the few Postgres reads a chart makes replaced where they happen.
"""

from __future__ import annotations

import ast
import io
import os
import re
import tokenize
import uuid
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import NamedTuple
from unittest import mock

import pytest
from django.core.cache import cache

from tracer.services.clickhouse import exact_graph_reads as exact
from tracer.services.clickhouse import graph_dispatch as dispatch
from tracer.services.clickhouse.exact_attribute_detail import (
    read_exact_attribute_detail,
)
from tracer.services.clickhouse.query_service import QueryResult
from tracer.services.clickhouse.v2 import query_service as v2_query_service
from tracer.services.simulation_visibility import (
    RESOURCE_MARK_KEY,
    SIMULATION_MARK_KEY,
    row_exclusion_sql,
)
from tracer.tests._graph_cost_stub import affordable_scan_estimate
from tracer.utils import graphs_optimized
from tracer.views.dashboard import DashboardWidgetViewSet

pytestmark = pytest.mark.unit

FUTUREAGI = Path(__file__).resolve().parents[2]

# A FROM clause over the spans table, over the rollup of trace counts, or over a
# template (``FROM {...}``) whose table only the reader knows, which is why a
# person names it. Whatever builds the hourly rollup source reads one as well.
READS_A_TABLE = re.compile(r"FROM\s+(\{[^}]*\}|spans\b|trace_count_rollup\b)")
HOURLY_ROLLUP_SOURCE = "hourly_aggregate_state_source("
SPAN_TABLES = ("spans", "trace_count_rollup", "span_metrics_hourly", "hourly_aggregate")

# The open-source tree has no ``ee`` package, so its readers cannot be missing there.
ENTERPRISE_PREFIX = "ee."

# ---------------------------------------------------------------------------
# The buckets
# ---------------------------------------------------------------------------

# Hands the request's filter list to a compiler, or draws a chart: it hides
# marked traces through the keyword-only ``hide_simulated`` argument that its
# view computes once per request.
LIST_OR_CHART = "list or chart reader"
# Has no filter list to carry the rule, so it decides for its own project, or
# for the projects it spans, and adds the row test itself.
PROJECT_WIDE = "project-wide reader"
# A chart's cost probe or seed lookup: it reads the request's filters only and
# returns nothing a user sees, and the statement that returns the data carries
# the rule itself. Not a chart read, so it is not held to the chart rule.
CHART_CACHE = "chart cost probe or seed lookup"
# A dropdown a user sees (a filter value, an attribute key or value, the metrics
# catalog): the view decides once for its request, and the reader leaves marked
# spans out through its own row test, or a view refuses a holding scope first.
PICKER = "filter or catalog picker"
# Reads the ids it is given. Opening a marked trace, span or session by id must
# keep working, and an id read moves no count.
BY_ID = "read by id"
# No project view reaches it: session and user views (a marked trace has no
# session or end-user identity), operator tooling, code nothing calls, and a
# reader a view refuses for a holding project before it calls it.
NOT_REACHED = "not reached"
# Counts or names marked spans and returns no call content, and either no user
# sees the result (deployment telemetry) or the owner accepted the exception.
COUNTS_AND_NAMES = "counts or names, no call content"
# A reader a user sees that still shows marked traces. Each reason starts with
# who has to act: a ruling that is pending, or work that is scheduled.
KNOWN_GAP = "known gap, not hidden yet"
KNOWN_GAP_REASONS = ("PENDING OWNER:", "SCHEDULED:")
# Matched only through a ``FROM {...}`` template over a table that holds no
# span; its filing names that table.
NO_SPAN_TABLE = "reads no span table"
# A production call now has child spans under its top span, so a reader that
# adds up every span of a trace or a project shows new numbers. These readers
# are filed beside their hiding bucket so the list of them is known.
CHILDREN_ERROR_FEED_LATENCY = "children counted: error-feed latency"
CHILDREN_MONITOR_RESPONSE_TIME = "children counted: monitor SPAN_RESPONSE_TIME"
CHILDREN_MONITOR_FUNCTION_ERRORS = (
    "children counted: monitor ERROR_RATES_FOR_FUNCTION_CALLING"
)
CHILDREN_PROJECT_CHART = "children counted: project chart, unfiltered"
CHILDREN_SPAN_EVAL_TASKS = "children counted: span-level eval tasks"
CHILDREN_VOICE_EVAL_CELLS = "children counted: voice list eval cells"
CHILDREN_ATTRIBUTE_PICKERS = "children counted: attribute pickers and key catalog"
CHILDREN_TRACE_FILTERS = (
    "children counted: trace filters on type, name, status and values; span list"
)

BUCKETS = (
    LIST_OR_CHART,
    PROJECT_WIDE,
    CHART_CACHE,
    PICKER,
    BY_ID,
    NOT_REACHED,
    COUNTS_AND_NAMES,
    KNOWN_GAP,
    NO_SPAN_TABLE,
    CHILDREN_ERROR_FEED_LATENCY,
    CHILDREN_MONITOR_RESPONSE_TIME,
    CHILDREN_MONITOR_FUNCTION_ERRORS,
    CHILDREN_PROJECT_CHART,
    CHILDREN_SPAN_EVAL_TASKS,
    CHILDREN_VOICE_EVAL_CELLS,
    CHILDREN_ATTRIBUTE_PICKERS,
    CHILDREN_TRACE_FILTERS,
)


class Filing(NamedTuple):
    buckets: tuple[str, ...]
    why: str
    table: str | None


def filed(*buckets: str, why: str, table: str | None = None) -> Filing:
    return Filing(buckets, why, table)


_ANNOTATION_CELLS = filed(
    NO_SPAN_TABLE,
    why="annotation cells of an already filtered page",
    table="model_hub_score",
)
_ATTRIBUTE_PICKER = filed(
    PICKER,
    CHILDREN_ATTRIBUTE_PICKERS,
    why="attribute key and value picker template: the selector adds the row test to its candidate sample and frontier, and reads a marked span as deleted in the latest-state replay, when the request hides",
)
_CANDIDATE_PROBE = filed(
    LIST_OR_CHART,
    why="candidate-only probe: returns a superset that every path sends through the classifier",
)
_CDC_LAG_CHECK = filed(
    NO_SPAN_TABLE,
    why="health check reads the lag of the change-data-capture tables",
    table="tracer_trace, trace_session and tracer_eval_logger",
)
_CONTINUOUS_CANDIDATES = filed(
    LIST_OR_CHART,
    why="continuous eval task candidates: an arrival superset that the row resolver replays through a builder carrying hide_simulated",
)
_DASHBOARD_SIDE_TABLE_STATEMENT = filed(
    PROJECT_WIDE,
    why="dashboard eval or annotation statement over a side table: the spans join is a LEFT JOIN, so _marked_traces_hidden_clause adds a negative trace-id membership against the marked spans when hide_simulated is true",
)
_DASHBOARD_SOURCE = filed(
    PROJECT_WIDE,
    why="dashboard traces source: the base clauses of _build_where_clauses carry the conjunct",
)
_DASHBOARD_V2_SOURCE = filed(
    PROJECT_WIDE, why="dashboard v2 sources: carry the mark columns the conjunct reads"
)
_DEAD_ANNOTATION_BUILDER = filed(
    NO_SPAN_TABLE,
    why="dead code: only the dead get_annotation_graph_data builds it",
    table="model_hub_score",
)
_DEAD_ERROR_ANALYSIS = filed(
    NOT_REACHED, why="dead code: the builder is only exported by its package"
)
_DEAD_SPAN_READER = filed(
    NOT_REACHED,
    why="dead code: no production caller; the live previous and next reads are list readers",
)
_DEAD_TRACE_GRAPH = filed(
    NOT_REACHED,
    why="dead code: reached only from _fetch_trace_system_metric_graph and _finite_trace_span_ids, which nothing calls",
)
_DICTIONARY_DDL = filed(
    NO_SPAN_TABLE,
    why="dictionary DDL over replicated Postgres tables",
    table="the replicated Postgres table each dictionary names",
)
_EMBEDDING_STORE = filed(
    NO_SPAN_TABLE,
    why="embedding store reads",
    table="vector store tables named by the caller",
)
_ERROR_EMBEDDINGS = filed(
    NO_SPAN_TABLE,
    why="error clustering reads its own embeddings",
    table="error_embeddings",
)
_EVAL_CELLS = filed(
    NO_SPAN_TABLE,
    why="eval cells of an already filtered page",
    table="tracer_eval_logger",
)
_EVAL_CENTROIDS = filed(
    NO_SPAN_TABLE, why="eval clustering reads its centroids", table="cluster_centroids"
)
_EVAL_CHART_MEMBERSHIP = filed(
    LIST_OR_CHART,
    why="eval chart membership and the negative scan that leaves marked traces' eval rows out",
)
_EVAL_CHART_ROWS = filed(
    NO_SPAN_TABLE,
    why="eval chart statement over eval rows; the span membership is the filter fragment",
    table="tracer_eval_logger",
)
_EVAL_FILTER_LEAF = filed(
    NO_SPAN_TABLE, why="eval filter leaf", table="tracer_eval_logger"
)
_EVAL_LOG_BACKFILL = filed(
    NO_SPAN_TABLE,
    why="operator backfill of eval log columns",
    table="tracer_eval_logger",
)
_EVAL_MONITOR = filed(
    NO_SPAN_TABLE,
    PROJECT_WIDE,
    why="eval monitor: reads eval rows whose spans come through _bounded_spans_subquery, which carries the conjunct",
    table="tracer_eval_logger",
)
_EVAL_ROWS_OF_A_CONFIG = filed(
    NO_SPAN_TABLE,
    why="eval rows of the config or traces it is given",
    table="tracer_eval_logger",
)
_EXACT_CHART_STATEMENT = filed(
    LIST_OR_CHART,
    why="exact project chart statement: the plan carries the predicate when hide_simulated is true",
)
_FILTER_LEAF = filed(
    LIST_OR_CHART,
    why="filter-leaf subquery of a list or chart reader: the predicate is the builder's, never a filter",
)
_FILTER_VALUE_PICKER = filed(
    PICKER,
    why="SQL of the system filter-value pickers (read_span_system_filter_value_cursor_page and read_span_system_filter_values, called by the dashboard filter-value endpoints and the AI filter view): with hide_simulated the spans read carries row_unmarked and only a span whose newest version is unmarked is offered",
)
_GROUPING_STORE = filed(
    NO_SPAN_TABLE,
    why="grouping reads and verifies its own feature rows",
    table="f6_grouping_features and f6_grouping_buckets",
)
_HOURLY_EVAL_STATEMENT = filed(
    NO_SPAN_TABLE,
    why="eval chart statement over the hourly eval aggregate",
    table="eval_metrics_hourly",
)
_LEGACY_PAGE_STATEMENT = filed(
    LIST_OR_CHART,
    why="legacy page statement: carries the row test when hide_simulated is true",
)
_MIGRATION_PARITY_CHECK = filed(
    NOT_REACHED,
    why="operator parity check of the Postgres to ClickHouse migration; no project view",
)
_MONITOR_STATEMENT = filed(
    PROJECT_WIDE,
    CHILDREN_MONITOR_RESPONSE_TIME,
    CHILDREN_MONITOR_FUNCTION_ERRORS,
    why="monitor statement over _spans_base_where; a monitor metric adds up every span, children included",
)
_OBSERVED_CATALOG = filed(
    NO_SPAN_TABLE,
    KNOWN_GAP,
    CHILDREN_ATTRIBUTE_PICKERS,
    why="PENDING OWNER: the collector's observed key catalog (and the AI filter's schema keys) still lists keys that only marked spans carry; how it hides is not ruled",
    table="observed_attribute_keys",
)
_OUTBOX_BOOKKEEPING = filed(
    NO_SPAN_TABLE,
    why="outbox change-data-capture bookkeeping in Postgres",
    table="the outbox, state and dead-letter tables",
)
_OUTBOX_CHECK = filed(
    NO_SPAN_TABLE,
    why="outbox change-data-capture checks the tables it replicates",
    table="the replicated model tables it is given",
)
_PROJECT_CHART_STATEMENT = filed(
    LIST_OR_CHART,
    CHILDREN_PROJECT_CHART,
    why="project chart statement: aggregate states for a project that does not hide, raw rows with the conjunct when it hides",
)
_PROJECT_WIDE_READ = filed(
    PROJECT_WIDE,
    why="project-wide reader without a filter list: the conjunct is added when the project hides",
)
_READS_THE_IDS_IT_IS_GIVEN = filed(
    BY_ID,
    why="reads the ids it is given: opening a marked trace by id must keep working",
)
_RECORD_UPSERT = filed(
    NO_SPAN_TABLE,
    why="record upsert: reads the row it is about to update",
    table="events and the other model_hub tables named by the caller",
)
_ROOT_CAUSE_AGENT_READ = filed(
    BY_ID, why="the root-cause agent reads the traces or the session it is given"
)
_ROOT_VERIFIER = filed(
    LIST_OR_CHART,
    why="root verifier of the exact chart lane: judges the live root and carries the row test",
)
_SCAN_CENTROIDS = filed(
    NO_SPAN_TABLE, why="scan clustering reads its centroids", table="cluster_centroids"
)
_SEED_PAGE = filed(
    LIST_OR_CHART,
    why="seed page: carries the plan, and the classifier stays the authority",
)
_SESSION_OR_USER = filed(
    NOT_REACHED,
    why="session or user reader, outside the hiding rule: a marked trace carries no session or end-user identity",
)
_SESSION_TASK_READ = filed(
    NOT_REACHED, why="session eval task candidates and the session id remap"
)
_SESSION_USER_SURVIVOR_MAP = filed(
    NO_SPAN_TABLE,
    why="survivor map of renamed session and user ids",
    table="trace_session_id_remap and end_user_id_remap",
)
_SPAN_LIST_SOURCE = filed(
    LIST_OR_CHART,
    why="latest-state source of the span list: packs the columns the row test reads when hide_simulated is true",
)
_STATEMENT_BY_IDS = filed(
    LIST_OR_CHART, why="statement for the ids of a page the classifier already filtered"
)
_SURVIVOR_MAP = filed(
    NO_SPAN_TABLE,
    why="survivor map of renamed ids",
    table="end_user_id_remap and trace_session_id_remap",
)
_TELEMETRY_COUNTS = filed(
    COUNTS_AND_NAMES,
    why="deployment telemetry sent to us, which no user sees: it keeps counting (owner ruling)",
)
_TRACE_DETAIL_BY_ID = filed(BY_ID, why="trace detail by trace id")
_TRACE_INPUT_VECTORS = filed(
    NO_SPAN_TABLE,
    why="scan clustering reads the stored trace input vectors",
    table="trace_input_embeddings",
)
_TRACE_PROJECT_SCOPE = filed(
    NO_SPAN_TABLE,
    why="project scope of traces read from the trace table",
    table="traces",
)

# Every entry is decided: a reader that still shows marked traces is filed under
# the known-gap bucket with who has to act, never as undecided.
_FILED_BY_MODULE: dict[str, dict[str, Filing]] = {
    "agentic_eval.core.database.ch_vector": {
        "ClickHouseVectorDB.fetch_all_vectors": _EMBEDDING_STORE,
        "ClickHouseVectorDB.fetch_vector_by_id": _EMBEDDING_STORE,
        "ClickHouseVectorDB.get_num_vectors": _EMBEDDING_STORE,
        "ClickHouseVectorDB.get_random_examples": _EMBEDDING_STORE,
        "ClickHouseVectorDB.vector_similarity_search": _EMBEDDING_STORE,
        "ClickHouseVectorDB.vector_similarity_search_with_threshold": _EMBEDDING_STORE,
    },
    "ee.agenthub.traceerroragent.error_cluster": {
        "ErrorEmbeddingClusterer.cluster_project.<locals>._fetch_project_embeddings": filed(
            NO_SPAN_TABLE,
            why="error clustering reads the embeddings it stored",
            table="error_embeddings",
        ),
    },
    "ee.usage.migrations.0015_backfill_plan_and_wallet_credits": {
        "migrate_wallet_to_credits": filed(
            NO_SPAN_TABLE,
            why="data migration between billing tables",
            table="credit balance and organization subscription tables",
        ),
    },
    "model_hub.management.commands.backfill_score_tracer_project": {
        "_project_match_query": filed(
            NOT_REACHED,
            why="one-shot operator backfill: finds which project owns a score's trace or span, writes the score's project column and lists nothing",
        ),
    },
    "model_hub.management.commands.gt_roundtrip_test": {
        "Command._live_row_count": filed(
            NO_SPAN_TABLE,
            why="operator round trip of the ground truth store",
            table="ground_truths",
        ),
    },
    "model_hub.management.commands.migrate_ch_vector_tables": {
        "Command._migrate_one_table": filed(
            NO_SPAN_TABLE,
            why="operator copy of the vector tables",
            table="vector tables named on the command line",
        ),
    },
    "model_hub.management.commands.migrate_legacy_default_tables_to_replicated": {
        "Command._migrate_one_table": filed(
            NO_SPAN_TABLE,
            why="operator copy of the clustering, embedding and log tables",
            table="cluster_centroids, trace_input_embeddings, error_embeddings, llm_logs and events",
        ),
    },
    "model_hub.management.commands.migrate_m2m_to_fk": {
        "Command.handle": filed(
            NO_SPAN_TABLE,
            why="operator migration between Postgres relation tables",
            table="the many-to-many table being migrated",
        ),
    },
    "model_hub.selectors.eval_usage": {
        "_latest_usage_slice": filed(
            NO_SPAN_TABLE,
            KNOWN_GAP,
            why="SCHEDULED: the eval usage page reads the API call log through the project scope of each run's trace and lists the evals of marked traces; the owner put it in next week's work",
            table="usage_apicalllog",
        ),
    },
    "model_hub.utils.clickhouse": {
        "create_empty_conversation": _RECORD_UPSERT,
        "update_eval_record": _RECORD_UPSERT,
        "upsert_record": _RECORD_UPSERT,
    },
    "model_hub.views.separate_evals": {
        "_fetch_eval_metric_buckets": filed(
            NO_SPAN_TABLE,
            KNOWN_GAP,
            why="SCHEDULED: next week, beside the eval usage page. The legacy template chart over the API call log counts every successful run of the template in the organization, runs on marked traces included; it has no project scope, and no web page shows it (EvalDetailView is not routed), so only API callers see it",
            table="usage_apicalllog",
        ),
    },
    "simulate.services.preview_pagination": {
        "_read_revision_state": filed(
            NO_SPAN_TABLE,
            why="preview paging fences on the revision of a run's rows",
            table="simulate_run_test, simulate_test_execution and simulate_call_execution",
        ),
    },
    "simulate.services.run_results_v3_queries": {
        "_RunCallsTable.as_sql": filed(
            NO_SPAN_TABLE,
            why="run results read call execution rows",
            table="the simulate call execution table",
        ),
    },
    "tfc.deployment_telemetry.ch_counts": {
        "count_spans": _TELEMETRY_COUNTS,
        "count_traces": _TELEMETRY_COUNTS,
        "ingesting_project_ids": _TELEMETRY_COUNTS,
    },
    "tracer.management.commands.backfill_eval_score": {
        "_AFFECTED_COUNT": filed(
            NO_SPAN_TABLE,
            why="operator backfill of an eval score column",
            table="usage_apicalllog",
        ),
    },
    "tracer.management.commands.recreate_clickhouse_mv": {
        "Command._run_backfill": filed(
            NO_SPAN_TABLE,
            why="operator backfill after recreating a view: reads the manifest's source table",
            table="tracer_eval_logger",
        ),
    },
    "tracer.queries.error_analysis": {
        "TraceErrorAnalysisDB._embedding_exists_for_detail": filed(
            NO_SPAN_TABLE,
            why="error analysis checks its own embeddings",
            table="error_embeddings",
        ),
    },
    "tracer.queries.error_clustering": {
        "ErrorClusteringDB.get_unclustered_embeddings": _ERROR_EMBEDDINGS,
        "ErrorClusteringDB.mark_embeddings_as_clustered": _ERROR_EMBEDDINGS,
    },
    "tracer.queries.eval_clustering": {
        "assign_to_cluster": _EVAL_CENTROIDS,
        "find_nearest_centroid": _EVAL_CENTROIDS,
    },
    "tracer.queries.scan_clustering": {
        "_absorb_centroid": _SCAN_CENTROIDS,
        "assign_to_cluster": _SCAN_CENTROIDS,
        "find_nearest_centroid": _SCAN_CENTROIDS,
        "find_success_trace_baseline": _TRACE_INPUT_VECTORS,
        "get_cluster_trace_embeddings": _TRACE_INPUT_VECTORS,
        "merge_duplicate_clusters": _SCAN_CENTROIDS,
    },
    "tracer.selectors.eval_tasks.continuous_candidates": {
        "_expand_changed_span_identity_batch": _CONTINUOUS_CANDIDATES,
        "_expand_end_user_id_batch": _CONTINUOUS_CANDIDATES,
        "_expand_relation_ref_batch": _CONTINUOUS_CANDIDATES,
        "_read_changed_remap_ids": filed(
            NO_SPAN_TABLE,
            why="changed id remaps",
            table="trace_session_id_remap and end_user_id_remap",
        ),
        "_read_changed_span_identities": _CONTINUOUS_CANDIDATES,
        "_read_relation_refs_paged": filed(
            NO_SPAN_TABLE,
            why="changed score and eval refs, expanded to spans by the sibling readers",
            table="model_hub_score and tracer_eval_logger",
        ),
        "_read_root_id_batch": _CONTINUOUS_CANDIDATES,
        "_read_workflow_changed_spans": _CONTINUOUS_CANDIDATES,
    },
    "tracer.selectors.eval_tasks.row_resolver": {
        "_build_sample_query": filed(
            LIST_OR_CHART,
            CHILDREN_SPAN_EVAL_TASKS,
            why="sampled historical eval task: its builder carries hide_simulated; a span-level task selects every span, children included",
        ),
    },
    "tracer.services.annotation_label_source": {
        "_CH_PROJECT_SCOPE": filed(
            NOT_REACHED,
            why="legacy compatibility reader of annotation label ids, kept for rollback tooling and not used by the direct-write APIs",
        ),
    },
    "tracer.services.clickhouse.attribute_reads": {
        "_CANDIDATE_SQL": _ATTRIBUTE_PICKER,
        "_LATEST_BROWSE_SQL": _ATTRIBUTE_PICKER,
        "_LATEST_CARDINALITY_SQL": _ATTRIBUTE_PICKER,
        "_LATEST_JSON_TARGET_SQL": _ATTRIBUTE_PICKER,
        "_LATEST_TARGET_SQL": _ATTRIBUTE_PICKER,
        "_LATEST_TYPED_BROWSE_SQL": _ATTRIBUTE_PICKER,
        "_LATEST_TYPED_TARGET_SQL": _ATTRIBUTE_PICKER,
        "_LATEST_TYPED_VERSION_SQL": _ATTRIBUTE_PICKER,
        "_ORDERED_CANDIDATE_SQL": _ATTRIBUTE_PICKER,
        "_ORDERED_STRATIFIED_CANDIDATE_SQL": _ATTRIBUTE_PICKER,
        "_ORDERED_TYPED_VALUE_CANDIDATE_SQL": _ATTRIBUTE_PICKER,
        "_STRATIFIED_CANDIDATE_SQL": _ATTRIBUTE_PICKER,
        "_TYPED_VALUE_CANDIDATE_SQL": _ATTRIBUTE_PICKER,
        "_VALUE_CURSOR_SEARCHED_RAW_SINGLE_LANE_SQL": _ATTRIBUTE_PICKER,
        "_VALUE_CURSOR_SEARCHED_RAW_SUPERSET_SQL": _ATTRIBUTE_PICKER,
    },
    "tracer.services.clickhouse.client": {
        "ClickHouseClient.check_replication_lag": _CDC_LAG_CHECK,
        "ClickHouseClient.get_table_row_count": filed(
            NOT_REACHED,
            why="dead code: no caller, and the table is the caller's argument",
        ),
    },
    "tracer.services.clickhouse.cluster_rca_spans": {
        "aggregate_span_field": _ROOT_CAUSE_AGENT_READ,
        "aggregate_trace_field": _ROOT_CAUSE_AGENT_READ,
        "distinct_sessions": _ROOT_CAUSE_AGENT_READ,
        "error_messages_in_traces": _ROOT_CAUSE_AGENT_READ,
        "list_spans_in_traces": _ROOT_CAUSE_AGENT_READ,
        "search_spans_in_traces": _ROOT_CAUSE_AGENT_READ,
        "search_trace_ids": _ROOT_CAUSE_AGENT_READ,
        "timeline_trace_counts": _ROOT_CAUSE_AGENT_READ,
        "trace_roots": _ROOT_CAUSE_AGENT_READ,
        "traces_in_session": _ROOT_CAUSE_AGENT_READ,
    },
    "tracer.services.clickhouse.consistency": {
        "ConsistencyChecker.check_row_counts": filed(
            NO_SPAN_TABLE,
            why="health check compares Postgres with the change-data-capture tables",
            table="tracer_trace, trace_session and tracer_eval_logger",
        ),
        "ConsistencyChecker.get_cdc_lag": _CDC_LAG_CHECK,
    },
    "tracer.services.clickhouse.exact_attribute_detail": {
        "EXACT_ATTRIBUTE_DETAIL_SQL": filed(
            PICKER,
            why="attribute catalog detail statement: _detail_sql reads a marked span as deleted, so it adds nothing to a count, a value or a statistic when hide_simulated is true",
        ),
    },
    "tracer.services.clickhouse.exact_graph_reads": {
        "_eval_partition_trace_ids_sql": filed(
            NO_SPAN_TABLE,
            why="trace ids of the eval chart's partition",
            table="tracer_eval_logger",
        ),
        "_finite_survivor_map_ctes": _SESSION_OR_USER,
        "_latest_session_rows_sql": _SESSION_OR_USER,
        "_session_aggregate_source_sql": _SESSION_OR_USER,
        "_session_numeric_absence_probe_sql": _SESSION_OR_USER,
        "_user_aggregate_source_sql": filed(
            NO_SPAN_TABLE, why="user chart eval source", table="tracer_eval_logger"
        ),
        "session_graph_root_estimate_sql": _SESSION_OR_USER,
    },
    "tracer.services.clickhouse.filter_value_reads": {
        "_hiding_spans_source": filed(
            PICKER,
            why="the hiding itself: a spans read that carries each row's mark test as row_unmarked for the filter-value statements",
        ),
        "_latest_span_value_cte": _FILTER_VALUE_PICKER,
        "_latest_voice_value_cte": _FILTER_VALUE_PICKER,
        "read_session_message_filter_values": filed(
            NOT_REACHED, why="session message filter values"
        ),
    },
    "tracer.services.clickhouse.graph_dispatch": {
        "_finite_eval_rows": filed(
            NO_SPAN_TABLE,
            why="dead code: reached only from _finite_eval_graph, which nothing calls",
            table="tracer_eval_logger",
        ),
        "_finite_trace_span_candidates": _DEAD_TRACE_GRAPH,
        "_finite_trace_span_ids": _DEAD_TRACE_GRAPH,
        "_select_raw_trace_seed_candidate": filed(
            CHART_CACHE,
            why="seed candidate of the raw chart lane: reads the request's filters only, and the statement that carries the predicate is the chart's own",
        ),
        "_trace_system_metric_query": _DEAD_TRACE_GRAPH,
    },
    "tracer.services.clickhouse.graph_read_cost": {
        "_explain_raw_graph_scan": filed(
            CHART_CACHE,
            why="cost probe of a chart read: asks the index for a row estimate, returns no span data",
        ),
        "estimate_user_graph_scan_rows": filed(
            NOT_REACHED, why="cost probe of the user chart"
        ),
    },
    "tracer.services.clickhouse.oss_cdc_bootstrap": {
        "_definitions": filed(
            NO_SPAN_TABLE,
            why="install-time DDL text for the change-data-capture views: reads nothing",
            table="none: it rewrites DDL text",
        ),
    },
    "tracer.services.clickhouse.oss_native_bootstrap": {
        "_inspect": filed(
            NOT_REACHED,
            why="install-time bootstrap probes whether a table is empty before it changes anything; no project view",
        ),
    },
    "tracer.services.clickhouse.oss_outbox_cdc": {
        "_ch_has_live_rows": _OUTBOX_CHECK,
        "_ch_live_among_sql": _OUTBOX_CHECK,
        "_ch_live_page_sql": _OUTBOX_CHECK,
        "_ch_max_version": _OUTBOX_CHECK,
        "_due_reconciles": _OUTBOX_BOOKKEEPING,
        "_outbox_head": _OUTBOX_BOOKKEEPING,
        "_pending_snapshots": _OUTBOX_BOOKKEEPING,
        "_probe": _OUTBOX_CHECK,
        "drain": _OUTBOX_BOOKKEEPING,
        "drain_batch": _OUTBOX_BOOKKEEPING,
        "load_floor": _OUTBOX_BOOKKEEPING,
        "reconcile": _OUTBOX_BOOKKEEPING,
        "reconcile_table": _OUTBOX_CHECK,
        "snapshot_page": _OUTBOX_BOOKKEEPING,
        "status": _OUTBOX_BOOKKEEPING,
    },
    "tracer.services.clickhouse.query_builders.agent_graph": {
        "AgentGraphQueryBuilder.build": filed(
            LIST_OR_CHART,
            why="Agent Graph statement: carries the predicate through hide_simulated, which the cache identity also holds",
        ),
    },
    "tracer.services.clickhouse.query_builders.annotation_graph": {
        "AnnotationGraphQueryBuilder._build_bool_query": _DEAD_ANNOTATION_BUILDER,
        "AnnotationGraphQueryBuilder._build_float_query": _DEAD_ANNOTATION_BUILDER,
        "AnnotationGraphQueryBuilder._build_str_list_query": _DEAD_ANNOTATION_BUILDER,
        "AnnotationGraphQueryBuilder._build_text_query": _DEAD_ANNOTATION_BUILDER,
        "AnnotationGraphQueryBuilder._filtered_spans_subquery": filed(
            NOT_REACHED,
            why="dead code: only the dead get_annotation_graph_data builds it; the live annotation chart is read_exact_annotation_graph",
        ),
    },
    "tracer.services.clickhouse.query_builders.dashboard": {
        "DashboardQueryBuilder._annotation_presence_relation": _DASHBOARD_SOURCE,
        "DashboardQueryBuilder._build_annotation_companion_relation": filed(
            PROJECT_WIDE,
            why="annotation breakdown LEFT JOIN over the caller's spans population, which carries the base clauses",
        ),
        "DashboardQueryBuilder._build_annotation_metric_query": _DASHBOARD_SIDE_TABLE_STATEMENT,
        "DashboardQueryBuilder._build_custom_attr_query": _DASHBOARD_SOURCE,
        "DashboardQueryBuilder._build_eval_metric_query": _DASHBOARD_SIDE_TABLE_STATEMENT,
        "DashboardQueryBuilder._build_subquery_filters": _DASHBOARD_SOURCE,
        "DashboardQueryBuilder._build_system_metric_query": _DASHBOARD_SOURCE,
        "DashboardQueryBuilder._eval_presence_relation": filed(
            NO_SPAN_TABLE,
            why="has-eval presence over eval rows and the trace table",
            table="tracer_eval_logger and traces",
        ),
        "DashboardQueryBuilder._marked_traces_hidden_clause": filed(
            PROJECT_WIDE,
            why="the hiding itself: the negative trace-id membership the eval and annotation statements add, read from the spans of the traces they can count with no time bound",
        ),
        "DashboardQueryBuilder._spans_source": _DASHBOARD_SOURCE,
        "_resolved_spans_source": _DASHBOARD_SOURCE,
        "_touched_survivor_map_subquery": _SURVIVOR_MAP,
    },
    "tracer.services.clickhouse.query_builders.error_analysis": {
        "ErrorAnalysisQueryBuilder._build_breakdown": _DEAD_ERROR_ANALYSIS,
        "ErrorAnalysisQueryBuilder._build_summary": _DEAD_ERROR_ANALYSIS,
        "ErrorAnalysisQueryBuilder._build_time_series": _DEAD_ERROR_ANALYSIS,
    },
    "tracer.services.clickhouse.query_builders.eval_metrics": {
        "EvalMetricsQueryBuilder._build_choices_query": _EVAL_CHART_ROWS,
        "EvalMetricsQueryBuilder._build_pass_fail_agg": _HOURLY_EVAL_STATEMENT,
        "EvalMetricsQueryBuilder._build_pass_fail_raw": _EVAL_CHART_ROWS,
        "EvalMetricsQueryBuilder._build_score_agg": _HOURLY_EVAL_STATEMENT,
        "EvalMetricsQueryBuilder._build_score_raw": _EVAL_CHART_ROWS,
        "EvalMetricsQueryBuilder._filter_fragment": filed(
            NOT_REACHED,
            why="overridden: the eval chart builds only EvalMetricsQueryBuilderV2 (exact_graph_reads.py), whose _filter_fragment replaces this one and carries the predicate",
        ),
    },
    "tracer.services.clickhouse.query_builders.exact_graph_predicates": {
        "_compile_has_eval_filter": filed(
            NO_SPAN_TABLE,
            why="has-eval filter leaf of the exact chart compiler",
            table="tracer_eval_logger",
        ),
    },
    "tracer.services.clickhouse.query_builders.filters": {
        "ClickHouseFilterBuilder.__init__": _FILTER_LEAF,
        "ClickHouseFilterBuilder._build_enduser_string_subquery": _FILTER_LEAF,
        "ClickHouseFilterBuilder._build_eval_condition.<locals>.eval_value_subquery": _EVAL_FILTER_LEAF,
        "ClickHouseFilterBuilder._build_has_eval_condition": _EVAL_FILTER_LEAF,
        "ClickHouseFilterBuilder._build_native_trace_tags_condition": filed(
            NO_SPAN_TABLE, why="trace tag filter leaf", table="tracer_trace"
        ),
        "ClickHouseFilterBuilder._build_system_metric_condition": _FILTER_LEAF,
        "ClickHouseFilterBuilder._candidate_trace_filter": _FILTER_LEAF,
        "ClickHouseFilterBuilder._enduser_dimension_id_subquery": filed(
            NO_SPAN_TABLE, why="end-user filter leaf", table="tracer_enduser"
        ),
        "ClickHouseFilterBuilder._scope_span_attr_inner": _FILTER_LEAF,
        "ClickHouseFilterBuilder._scoped_spans_subquery": _FILTER_LEAF,
        "ClickHouseFilterBuilder._score_side_candidate_filter": _FILTER_LEAF,
    },
    "tracer.services.clickhouse.query_builders.hourly_aggregate_states": {
        "hourly_aggregate_state_source": filed(
            LIST_OR_CHART,
            CHILDREN_PROJECT_CHART,
            why="hourly aggregate states count every physical row and carry no attribute; a hiding project never reads them (accepted difference)",
        ),
    },
    "tracer.services.clickhouse.query_builders.latest_filter_predicates": {
        "latest_span_membership_source_sql": filed(
            LIST_OR_CHART,
            why="membership source the graph compiler's trace clause reads; the compiler passes the marked-span predicate through it",
        ),
    },
    "tracer.services.clickhouse.query_builders.monitor_metrics": {
        "MonitorMetricsQueryBuilder._bounded_spans_subquery": filed(
            PROJECT_WIDE, why="the conjunct of the eval monitors' span membership"
        ),
        "MonitorMetricsQueryBuilder._build_eval_metric_value_query": _EVAL_MONITOR,
        "MonitorMetricsQueryBuilder._build_eval_stats_query": _EVAL_MONITOR,
        "MonitorMetricsQueryBuilder._build_eval_time_series_query": _EVAL_MONITOR,
        "MonitorMetricsQueryBuilder._eval_rows_source": _EVAL_MONITOR,
        "MonitorMetricsQueryBuilder.build_historical_stats_query": _MONITOR_STATEMENT,
        "MonitorMetricsQueryBuilder.build_metric_value_query": _MONITOR_STATEMENT,
        "MonitorMetricsQueryBuilder.build_time_series_query": _MONITOR_STATEMENT,
    },
    "tracer.services.clickhouse.query_builders.session_analytics": {
        "SessionAnalyticsQueryBuilder.build_first_last_message_query": _SESSION_OR_USER,
        "SessionAnalyticsQueryBuilder.build_session_metrics_query": _SESSION_OR_USER,
        "SessionAnalyticsQueryBuilder.build_session_navigation_query": _SESSION_OR_USER,
        "SessionAnalyticsQueryBuilder.build_user_stats_query": _SESSION_OR_USER,
    },
    "tracer.services.clickhouse.query_builders.session_list": {
        "SessionListQueryBuilder._bounded_eval_membership_ctes": filed(
            NO_SPAN_TABLE,
            why="session list eval membership",
            table="tracer_eval_logger",
        ),
        "SessionListQueryBuilder._build_session_user_membership_clause": _SESSION_OR_USER,
        "SessionListQueryBuilder._build_user_presence_clause": _SESSION_OR_USER,
        "SessionListQueryBuilder._candidate_session_ctes": _SESSION_OR_USER,
        "SessionListQueryBuilder._seed_witness_identity_gate": _SESSION_OR_USER,
        "SessionListQueryBuilder._session_from_where": _SESSION_OR_USER,
        "SessionListQueryBuilder.build_filter_anchor_probe": _SESSION_OR_USER,
        "SessionListQueryBuilder.build_filter_seed_page": _SESSION_OR_USER,
        "SessionListQueryBuilder.build_page_hydration_query": _SESSION_OR_USER,
    },
    "tracer.services.clickhouse.query_builders.session_time_series": {
        "SessionRollupTimeSeriesQueryBuilder._rollup_session_source": _SESSION_OR_USER,
        "SessionTimeSeriesQueryBuilder.build": _SESSION_OR_USER,
    },
    "tracer.services.clickhouse.query_builders.span_list": {
        "SpanListQueryBuilder._build_filter_match_query": filed(
            LIST_OR_CHART,
            why="classifier: the plan that carries the predicate joins the request's plans",
        ),
        "SpanListQueryBuilder.build": filed(
            LIST_OR_CHART,
            CHILDREN_TRACE_FILTERS,
            why="legacy page statement: carries the row test when hide_simulated is true",
        ),
        "SpanListQueryBuilder.build_annotation_query": _ANNOTATION_CELLS,
        "SpanListQueryBuilder.build_content_query": _STATEMENT_BY_IDS,
        "SpanListQueryBuilder.build_count_query": _LEGACY_PAGE_STATEMENT,
        "SpanListQueryBuilder.build_eval_query": _EVAL_CELLS,
        "SpanListQueryBuilder.build_filter_anchor_probe": _CANDIDATE_PROBE,
        "SpanListQueryBuilder.build_filter_seed_page": _SEED_PAGE,
        "SpanListQueryBuilder.build_id_query": _LEGACY_PAGE_STATEMENT,
    },
    "tracer.services.clickhouse.query_builders.time_series": {
        "TimeSeriesQueryBuilder._build_agg_query": _PROJECT_CHART_STATEMENT,
        "TimeSeriesQueryBuilder._build_exact_raw_query": _EXACT_CHART_STATEMENT,
        "TimeSeriesQueryBuilder._build_raw_query": _PROJECT_CHART_STATEMENT,
        "TimeSeriesQueryBuilder._exact_latest_scalar_source": _EXACT_CHART_STATEMENT,
        "TimeSeriesQueryBuilder._raw_scalar_source": _EXACT_CHART_STATEMENT,
        "TimeSeriesQueryBuilder.build_exact_span_partition": _EXACT_CHART_STATEMENT,
        "TimeSeriesQueryBuilder.build_exact_trace_contribution_batch": _EXACT_CHART_STATEMENT,
        "plan_hiding_simulated": filed(
            LIST_OR_CHART,
            why="adds the predicate to a chart plan: the hiding itself, not a reader",
        ),
    },
    "tracer.services.clickhouse.query_builders.trace_list": {
        "TraceListQueryBuilder._build_global_typed_map_witness_probe": _CANDIDATE_PROBE,
        "TraceListQueryBuilder.build": _LEGACY_PAGE_STATEMENT,
        "TraceListQueryBuilder.build_annotation_query": _ANNOTATION_CELLS,
        "TraceListQueryBuilder.build_content_query": _STATEMENT_BY_IDS,
        "TraceListQueryBuilder.build_count_query": _LEGACY_PAGE_STATEMENT,
        "TraceListQueryBuilder.build_eval_query": _EVAL_CELLS,
        "TraceListQueryBuilder.build_exact_graph_latest_anchor_partition": _CANDIDATE_PROBE,
        "TraceListQueryBuilder.build_exact_graph_latest_root_partition": _ROOT_VERIFIER,
        "TraceListQueryBuilder.build_exact_graph_root_membership_query": _ROOT_VERIFIER,
        "TraceListQueryBuilder.build_filter_anchor_probe": _CANDIDATE_PROBE,
        "TraceListQueryBuilder.build_filter_candidate_witness_probe": _CANDIDATE_PROBE,
        "TraceListQueryBuilder.build_filter_exact_zero_probe": _CANDIDATE_PROBE,
        "TraceListQueryBuilder.build_filter_match_query": filed(
            LIST_OR_CHART,
            CHILDREN_TRACE_FILTERS,
            why="classifier: the plan that carries the predicate joins the request's plans",
        ),
        "TraceListQueryBuilder.build_filter_ordered_seed_page": _SEED_PAGE,
        "TraceListQueryBuilder.build_filter_page_hydration_query": _STATEMENT_BY_IDS,
        "TraceListQueryBuilder.build_filter_root_time_discovery_query": _CANDIDATE_PROBE,
        "TraceListQueryBuilder.build_filter_seed_page": _SEED_PAGE,
        "TraceListQueryBuilder.build_id_query": _LEGACY_PAGE_STATEMENT,
        "TraceListQueryBuilder.build_span_attributes_query": _STATEMENT_BY_IDS,
        "TraceListQueryBuilder.build_span_count_query": _STATEMENT_BY_IDS,
        "TraceListQueryBuilder.build_user_id_query": _STATEMENT_BY_IDS,
    },
    "tracer.services.clickhouse.query_builders.user_list": {
        "UserListQueryBuilder._build_candidate_first": _SESSION_OR_USER,
        "UserListQueryBuilder._candidate_page_ctes": _SESSION_OR_USER,
        "UserListQueryBuilder._finite_user_activity_ctes.<locals>.common_prefix": _SESSION_OR_USER,
        "UserListQueryBuilder.build_attribute_user_candidates_query": _SESSION_OR_USER,
        "UserListQueryBuilder.build_eval_query": filed(
            NO_SPAN_TABLE, why="user list eval cells", table="tracer_eval_logger"
        ),
        "UserListQueryBuilder.build_matching_activity_existence_query": _SESSION_OR_USER,
        "UserListQueryBuilder.build_matching_activity_instant_query": _SESSION_OR_USER,
        "UserListQueryBuilder.build_matching_activity_presence_query": _SESSION_OR_USER,
        "UserListQueryBuilder.build_matching_activity_slice_query": _SESSION_OR_USER,
        "UserListQueryBuilder.build_physical_user_presence_query": _SESSION_OR_USER,
        "UserListQueryBuilder.build_relation_filter_user_query": _SESSION_OR_USER,
        "_touched_survivor_map_subquery": _SURVIVOR_MAP,
    },
    "tracer.services.clickhouse.query_builders.user_time_series": {
        "UserTimeSeriesQueryBuilder.build": _SESSION_OR_USER,
    },
    "tracer.services.clickhouse.query_builders.voice_call_list": {
        "VoiceCallListQueryBuilder._apply_bounded_voice_constraints": filed(
            LIST_OR_CHART,
            why="adds the voice-only constraints to the trace classifier, which carries the plan",
        ),
        "VoiceCallListQueryBuilder.build": _LEGACY_PAGE_STATEMENT,
        "VoiceCallListQueryBuilder.build_annotation_query": _ANNOTATION_CELLS,
        "VoiceCallListQueryBuilder.build_child_spans_query": _STATEMENT_BY_IDS,
        "VoiceCallListQueryBuilder.build_content_query": _STATEMENT_BY_IDS,
        "VoiceCallListQueryBuilder.build_count_query": _LEGACY_PAGE_STATEMENT,
        "VoiceCallListQueryBuilder.build_eval_query": filed(
            NO_SPAN_TABLE,
            CHILDREN_VOICE_EVAL_CELLS,
            why="voice list eval cells average every eval row of the trace, children included",
            table="tracer_eval_logger",
        ),
        "VoiceCallListQueryBuilder.build_filter_exact_zero_probe": filed(
            LIST_OR_CHART,
            why="compatibility probe that never ends a page; it carries no row test and the classifier hides",
        ),
        "VoiceCallListQueryBuilder.build_id_query": _LEGACY_PAGE_STATEMENT,
    },
    "tracer.services.clickhouse.query_service": {
        "AnalyticsQueryService._eval_config_ids_query": filed(
            NO_SPAN_TABLE, why="eval config discovery", table="tracer_eval_logger"
        ),
        "AnalyticsQueryService.get_children_eval_metrics_ch": _EVAL_ROWS_OF_A_CONFIG,
        "AnalyticsQueryService.get_eval_config_ids_for_candidates_ch": filed(
            PICKER,
            why="eval-name picker and metrics catalog: with hide_simulated a config is kept only if it has an eval row whose trace is not among the marked spans' traces; a row with no trace id counts as unmarked",
        ),
        "AnalyticsQueryService.get_eval_config_ids_with_data_ch": filed(
            PICKER,
            NOT_REACHED,
            why="the fast path hands hide_simulated to the candidate read; only the eval-name picker sets it, and the non-observe span list passes none; the spans trace-join branch has no caller, because every caller passes candidate_config_ids",
        ),
        "AnalyticsQueryService.get_eval_detail_ch": filed(
            BY_ID, why="eval detail of the span or trace it is given"
        ),
        "AnalyticsQueryService.get_span_trace_map": filed(
            BY_ID, why="span to trace map of the traces it is given"
        ),
        "AnalyticsQueryService.get_trace_eval_scores_ch": _EVAL_ROWS_OF_A_CONFIG,
    },
    "tracer.services.clickhouse.schema": {
        "COLUMN_DICT": _DICTIONARY_DDL,
        "DATASET_DICT": _DICTIONARY_DDL,
        "ENDUSER_DICT": _DICTIONARY_DDL,
        "PROMPT_DICT": _DICTIONARY_DDL,
        "PROMPT_LABEL_DICT": _DICTIONARY_DDL,
        "SIMULATE_AGENT_DICT": _DICTIONARY_DDL,
        "SIMULATE_RUN_TEST_DICT": _DICTIONARY_DDL,
        "SIMULATE_SCENARIO_DICT": _DICTIONARY_DDL,
        "SIMULATE_TEST_EXECUTION_DICT": _DICTIONARY_DDL,
        "SIMULATE_VERSION_DICT": _DICTIONARY_DDL,
        "SPAN_METRICS_HOURLY_MV": filed(
            NOT_REACHED,
            why="legacy DDL of the hourly rollup view: a write-path definition, not a reader; the rollup's readers are filed as list or chart readers",
        ),
    },
    "tracer.services.clickhouse.span_attribute_lookups": {
        "aggregate_attribute_over_traces": _READS_THE_IDS_IT_IS_GIVEN,
        "list_attribute_keys_for_traces": filed(
            BY_ID,
            CHILDREN_ATTRIBUTE_PICKERS,
            why="keys across the trace ids it is given; only the root-cause agent calls it, for the traces of one cluster. The project-wide listing (list_attribute_keys_for_project) hides through the selector",
        ),
        "list_attributes_for_trace": _READS_THE_IDS_IT_IS_GIVEN,
        "scoped_trace_ids": _READS_THE_IDS_IT_IS_GIVEN,
        "span_id_by_provider_log_id": _READS_THE_IDS_IT_IS_GIVEN,
        "spans_by_eval_attribute_call_execution_ids": _READS_THE_IDS_IT_IS_GIVEN,
        "trace_ids_with_simulator_call_execution_id": _READS_THE_IDS_IT_IS_GIVEN,
    },
    "tracer.services.clickhouse.trace_project_scope": {
        "latest_live_project_traces_sql": _TRACE_PROJECT_SCOPE,
        "latest_live_trace_projects_sql": _TRACE_PROJECT_SCOPE,
    },
    "tracer.services.clickhouse.v2.backfill": {
        "_process_window": filed(
            NOT_REACHED,
            why="operator Postgres to ClickHouse span backfill: writes spans and checks counts; no project view",
        ),
    },
    "tracer.services.clickhouse.v2.id_remap_sql": {
        "bounded_survivor_map_subquery": _SESSION_USER_SURVIVOR_MAP,
        "survivor_map_subquery": _SESSION_USER_SURVIVOR_MAP,
    },
    "tracer.services.clickhouse.v2.physical_span_detail_reads": {
        "build_physical_span_detail_query": filed(
            BY_ID, why="one physical span by its full reference"
        ),
    },
    "tracer.services.clickhouse.v2.property_catalog.reader": {
        "PropertyCatalogReader._category_counts": _OBSERVED_CATALOG,
        "PropertyCatalogReader._observed_keys": _OBSERVED_CATALOG,
    },
    "tracer.services.clickhouse.v2.property_catalog.value_reader": {
        "PropertyCatalogValueReader.read_page": filed(
            NOT_REACHED,
            NO_SPAN_TABLE,
            CHILDREN_ATTRIBUTE_PICKERS,
            why="the collector's observed values: the view answers a holding scope not-ready before it builds this reader (pinned in GUARDED_AT_THE_VIEW)",
            table="observed_attribute_keys and observed_attribute_values",
        ),
    },
    "tracer.services.clickhouse.v2.query_builders.columns": {
        "json_path": filed(
            NO_SPAN_TABLE,
            why="text helper for the v2 path of a JSON attribute: reads nothing, spans appear only in its docstring",
            table="none: the match is the docstring",
        ),
    },
    "tracer.services.clickhouse.v2.query_builders.dashboard": {
        "DashboardQueryBuilderV2._build_custom_attr_query": _DASHBOARD_V2_SOURCE,
        "DashboardQueryBuilderV2._exact_filter_replay_source": _DASHBOARD_V2_SOURCE,
        "DashboardQueryBuilderV2._spans_source": _DASHBOARD_V2_SOURCE,
        "DashboardQueryBuilderV2.build_raw_metric_group_query": filed(
            PROJECT_WIDE,
            why="dashboard raw group: built on the compatible group, which takes the base clauses; no production caller",
        ),
    },
    "tracer.services.clickhouse.v2.query_builders.eval_metrics": {
        "EvalMetricsQueryBuilderV2._filter_fragment": _EVAL_CHART_MEMBERSHIP,
        "EvalMetricsQueryBuilderV2._hidden_rows_fragment": _EVAL_CHART_MEMBERSHIP,
    },
    "tracer.services.clickhouse.v2.query_builders.session_analytics": {
        "SessionAnalyticsQueryBuilderV2.build_session_metrics_query": _SESSION_OR_USER,
        "SessionAnalyticsQueryBuilderV2.build_session_navigation_query": _SESSION_OR_USER,
        "SessionAnalyticsQueryBuilderV2.build_user_stats_query": _SESSION_OR_USER,
    },
    "tracer.services.clickhouse.v2.query_builders.session_detail": {
        "_LIVE_SESSION_SPANS": filed(BY_ID, why="session detail by session id"),
    },
    "tracer.services.clickhouse.v2.query_builders.session_list": {
        "SessionListQueryBuilderV2.build_candidate_slice_density_probe_query": _SESSION_OR_USER,
    },
    "tracer.services.clickhouse.v2.query_builders.span_list": {
        "SpanListQueryBuilderV2._latest_state_source_sql": _SPAN_LIST_SOURCE,
        "SpanListQueryBuilderV2._latest_window_source_sql": _SPAN_LIST_SOURCE,
        "SpanListQueryBuilderV2.build_content_query": _STATEMENT_BY_IDS,
        "SpanListQueryBuilderV2.build_filter_population_time_discovery_query": _CANDIDATE_PROBE,
        "SpanListQueryBuilderV2.build_filter_seed_density_probe_query": _CANDIDATE_PROBE,
    },
    "tracer.services.clickhouse.v2.query_builders.trace_list": {
        "_TraceListQueryBuilderV2Core._filter_classifier_coordinate_predicate": _CANDIDATE_PROBE,
        "_TraceListQueryBuilderV2Core._scalar_candidate_conjunction_gate": _CANDIDATE_PROBE,
        "_TraceListQueryBuilderV2Core._scalar_candidate_root_population_predicate": _CANDIDATE_PROBE,
        "_TraceListQueryBuilderV2Core.build_count_query": filed(
            LIST_OR_CHART,
            why="pagination count: reads trace_count_rollup only while the project does not hide; a hiding project counts live roots instead",
        ),
        "_TraceListQueryBuilderV2Core.build_filter_seed_density_probe_query": _CANDIDATE_PROBE,
        "_TraceListQueryBuilderV2Core.build_span_attributes_query": _STATEMENT_BY_IDS,
        "_TraceListQueryBuilderV2Core.build_user_id_query": _STATEMENT_BY_IDS,
        "_TraceRootReplayV2._root_replay_query": _STATEMENT_BY_IDS,
    },
    "tracer.services.clickhouse.v2.query_builders.user_time_series": {
        "UserDetailTimeSeriesQueryBuilderV2.build": _SESSION_OR_USER,
        "_latest_spans_argmax_cte": _SESSION_OR_USER,
        "_touched_survivor_map_subquery": _SURVIVOR_MAP,
        "latest_physical_span_rows_sql": _SESSION_OR_USER,
    },
    "tracer.services.clickhouse.v2.span_reader": {
        "CHSpanReader._session_filter_remap": _SESSION_TASK_READ,
        "CHSpanReader.aggregate_by_end_user": filed(
            NOT_REACHED, why="user-scoped rollup of the session tasks"
        ),
        "CHSpanReader.aggregate_by_session_ids": _READS_THE_IDS_IT_IS_GIVEN,
        "CHSpanReader.aggregate_by_trace_ids": _READS_THE_IDS_IT_IS_GIVEN,
        "CHSpanReader.aggregate_window_with_filters": _DEAD_SPAN_READER,
        "CHSpanReader.child_tree_spans_by_trace_ids": _READS_THE_IDS_IT_IS_GIVEN,
        "CHSpanReader.count_by_project": _DEAD_SPAN_READER,
        "CHSpanReader.count_by_trace": _READS_THE_IDS_IT_IS_GIVEN,
        "CHSpanReader.count_with_filters": filed(
            PROJECT_WIDE,
            why="project-wide count; the conjunct is added only when it is called without trace_ids",
        ),
        "CHSpanReader.distinct_end_users_by_trace_ids": _READS_THE_IDS_IT_IS_GIVEN,
        "CHSpanReader.distinct_session_ids_with_filters": _SESSION_TASK_READ,
        "CHSpanReader.export_fields_by_ids": _READS_THE_IDS_IT_IS_GIVEN,
        "CHSpanReader.first_span_by_type": _READS_THE_IDS_IT_IS_GIVEN,
        "CHSpanReader.get": _READS_THE_IDS_IT_IS_GIVEN,
        "CHSpanReader.has_root_spans_of_type": _DEAD_SPAN_READER,
        "CHSpanReader.ids_by_project": _PROJECT_WIDE_READ,
        "CHSpanReader.list_by_ids": _READS_THE_IDS_IT_IS_GIVEN,
        "CHSpanReader.list_by_parent": _READS_THE_IDS_IT_IS_GIVEN,
        "CHSpanReader.list_by_project": filed(
            NOT_REACHED,
            why="lists every span of one experiment project version for the custom eval config view; experiment projects receive no marked trace",
        ),
        "CHSpanReader.list_by_session": _READS_THE_IDS_IT_IS_GIVEN,
        "CHSpanReader.list_by_trace": _READS_THE_IDS_IT_IS_GIVEN,
        "CHSpanReader.list_by_trace_ids": _READS_THE_IDS_IT_IS_GIVEN,
        "CHSpanReader.list_root_spans_by_trace_ids": _READS_THE_IDS_IT_IS_GIVEN,
        "CHSpanReader.newest_span_projects": _READS_THE_IDS_IT_IS_GIVEN,
        "CHSpanReader.newest_trace_projects": _READS_THE_IDS_IT_IS_GIVEN,
        "CHSpanReader.per_project_group_by_name": _PROJECT_WIDE_READ,
        "CHSpanReader.per_project_version_metric_aggregate": _DEAD_SPAN_READER,
        "CHSpanReader.per_trace_aggregate": _READS_THE_IDS_IT_IS_GIVEN,
        "CHSpanReader.per_trace_root_span_start_times": _READS_THE_IDS_IT_IS_GIVEN,
        "CHSpanReader.prev_next_span_by_start_time": _DEAD_SPAN_READER,
        "CHSpanReader.prev_next_trace_by_start_time": _DEAD_SPAN_READER,
        "CHSpanReader.recent_root_trace_ids_by_project": _PROJECT_WIDE_READ,
        "CHSpanReader.root_ids_by_project": _PROJECT_WIDE_READ,
        "CHSpanReader.root_ids_by_trace_ids": _READS_THE_IDS_IT_IS_GIVEN,
        "CHSpanReader.root_trace_candidates": filed(
            NOT_REACHED,
            why="feeds the unscheduled trace scanner sweep, which the hiding rule leaves alone",
        ),
        "CHSpanReader.roots_by_trace_ids": _READS_THE_IDS_IT_IS_GIVEN,
        "CHSpanReader.scope_by_ids": _READS_THE_IDS_IT_IS_GIVEN,
        "CHSpanReader.session_aggregate": _READS_THE_IDS_IT_IS_GIVEN,
        "CHSpanReader.session_trace_ids": _READS_THE_IDS_IT_IS_GIVEN,
        "CHSpanReader.time_bucket_aggregate": _DEAD_SPAN_READER,
        "CHSpanReader.time_bucket_aggregate_with_filters": _DEAD_SPAN_READER,
        "CHSpanReader.totals_by_trace_ids": filed(
            BY_ID,
            CHILDREN_ERROR_FEED_LATENCY,
            why="error-feed latency sums every span of the given traces, children included",
        ),
        "CHSpanReader.trace_aggregate": _READS_THE_IDS_IT_IS_GIVEN,
        "CHSpanReader.trace_aggregate_with_stddev": _READS_THE_IDS_IT_IS_GIVEN,
        "CHSpanReader.trace_ids_for_span_ids": _READS_THE_IDS_IT_IS_GIVEN,
        "CHSpanReader.trace_session_ids_by_trace_ids": _READS_THE_IDS_IT_IS_GIVEN,
        "_dedup_sql": filed(
            BY_ID,
            why="dedup wrapper of the span reads that name their trace or span ids; the caller supplies the where clause",
        ),
    },
    "tracer.services.clickhouse.v2.trace_detail_reads": {
        "TraceDetailReadBuilder.build_annotation_query": filed(
            NO_SPAN_TABLE, why="scores of a trace opened by id", table="model_hub_score"
        ),
        "TraceDetailReadBuilder.build_content_query": _TRACE_DETAIL_BY_ID,
        "TraceDetailReadBuilder.build_eval_query": filed(
            NO_SPAN_TABLE,
            why="eval rows of a trace opened by id",
            table="tracer_eval_logger",
        ),
        "TraceDetailReadBuilder.build_identity_query": _TRACE_DETAIL_BY_ID,
        "TraceDetailReadBuilder.build_span_anchor_query": _TRACE_DETAIL_BY_ID,
    },
    "tracer.services.clickhouse.v2.trace_session_dict_reader": {
        "session_exists": filed(
            NO_SPAN_TABLE,
            why="session existence is read from the session table",
            table="trace_sessions",
        ),
    },
    "tracer.services.clickhouse.v2.validate": {
        "_DEFAULT_QUERIES": _MIGRATION_PARITY_CHECK,
        "layer_a_counts": _MIGRATION_PARITY_CHECK,
        "layer_b_deep_equal": _MIGRATION_PARITY_CHECK,
    },
    "tracer.services.eval_tasks.backfill": {
        "_backfill_hashes": _EVAL_LOG_BACKFILL,
        "_backfill_status": _EVAL_LOG_BACKFILL,
        "backfill_blank_completed_status": _EVAL_LOG_BACKFILL,
    },
    "tracer.services.grouping.feature_store": {
        "GroupingFeatureStore.candidate_occurrences": _GROUPING_STORE,
        "GroupingFeatureStore.read_vectors": _GROUPING_STORE,
        "GroupingFeatureStore.write_and_verify": _GROUPING_STORE,
    },
    "tracer.services.simulation_visibility": {
        "_marked_trace_ids_sql": filed(
            BY_ID,
            why="the mark lookup: reads the trace ids it is given to say which are marked, and is how the trace investigator hides",
        ),
    },
    "tracer.services.users_list_manager": {
        "_users_attr_enrichment_query": _SESSION_OR_USER,
    },
    "tracer.utils.helper": {
        "_trace_ids_of_spans": filed(
            BY_ID,
            why="the trace of each scored span it is given: the annotation label list asks which traces are marked, so this lookup has to see a marked span",
        ),
    },
    "tracer.views.dashboard": {
        "_read_dashboard_rollup_fast_path": filed(
            PROJECT_WIDE,
            why="dashboard fast path: answers bounded_shape_unavailable for a holding project, so it reads no rollup for it",
        ),
    },
    "tracer.views.project": {
        "ProjectView.list_projects": filed(
            KNOWN_GAP,
            why="PENDING OWNER: the project list's 30-day volume and last-active, read from trace_count_rollup; it will hide, and the design is not ruled",
        ),
    },
    "tracer.views.trace": {
        "_has_voice_conversation_roots": filed(
            COUNTS_AND_NAMES,
            why="accepted exception (owner ruling): reads spans only to choose the voice CSV shape, a yes or no bit with no call content",
        ),
    },
    "tracer.views.trace_session": {
        "TraceSessionView._fetch_end_user_info": filed(
            NOT_REACHED, why="session list end-user enrichment"
        ),
        "TraceSessionView.eval_logs": filed(
            NO_SPAN_TABLE, why="session eval logs", table="tracer_eval_logger"
        ),
    },
}

# ``module:qualname`` -> how the reader is filed. Written out per module above so
# that a module's name is read once; the key is the full name the scan reports.
FILED: dict[str, Filing] = {
    f"{module}:{qualname}": filing
    for module, readers in _FILED_BY_MODULE.items()
    for qualname, filing in readers.items()
}

# ---------------------------------------------------------------------------
# Finding the readers
# ---------------------------------------------------------------------------

_NEVER_WALKED = {"node_modules", "__pycache__", "site-packages", "venv"}


def reads_a_table(text):
    return bool(READS_A_TABLE.search(text)) or HOURLY_ROLLUP_SOURCE in text


def is_test_file(relative):
    return (
        "tests" in relative.parts[:-1]
        or relative.name.startswith("test_")
        or relative.name == "conftest.py"
    )


def non_test_python_files():
    """Every non-test ``.py`` under ``futureagi/``, relative to it."""

    found = []
    for directory, subdirectories, names in os.walk(FUTUREAGI):
        subdirectories[:] = sorted(
            name
            for name in subdirectories
            if name not in _NEVER_WALKED and not name.startswith(".")
        )
        for name in sorted(names):
            if name.endswith(".py"):
                relative = (Path(directory) / name).relative_to(FUTUREAGI)
                if not is_test_file(relative):
                    found.append(relative)
    return found


def module_name(relative):
    parts = list(relative.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _blocks(node):
    for field in ("body", "orelse", "finalbody"):
        block = getattr(node, field, None)
        if isinstance(block, list):
            yield block
    for handler in getattr(node, "handlers", ()):
        yield handler.body
    for case in getattr(node, "cases", ()):
        yield case.body


def _own_text(node, lines):
    """The source of ``node`` without the functions and classes nested in it."""

    nested = set()
    for child in ast.walk(node):
        if child is not node and isinstance(
            child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            nested.update(range(child.lineno, child.end_lineno + 1))
    return "".join(
        lines[number - 1]
        for number in range(node.lineno, node.end_lineno + 1)
        if number not in nested
    )


def readers_in(source, module):
    """``{"module:qualname": text}`` for each function or template that reads."""

    lines = source.splitlines(keepends=True)
    found = {}

    def walk(body, prefix, inside_function):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualname = prefix + node.name
                text = _own_text(node, lines)
                if reads_a_table(text):
                    found[f"{module}:{qualname}"] = text
                walk(node.body, qualname + ".<locals>.", True)
            elif isinstance(node, ast.ClassDef):
                walk(node.body, prefix + node.name + ".", inside_function)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                text = "".join(lines[node.lineno - 1 : node.end_lineno])
                if not inside_function and reads_a_table(text):
                    target = (
                        node.targets[0] if isinstance(node, ast.Assign) else node.target
                    )
                    found[f"{module}:{prefix}{ast.unparse(target)}"] = text
            else:
                for block in _blocks(node):
                    walk(block, prefix, inside_function)

    walk(ast.parse(source).body, "", False)
    return found


def scan_readers():
    """Every reader the rule matches, and how many files were looked at."""

    readers = {}
    files = non_test_python_files()
    for relative in files:
        source = (FUTUREAGI / relative).read_text(encoding="utf-8")
        # Only a file that mentions a table can hold a reader: skipping the rest
        # keeps the parse to the few files that matter.
        if reads_a_table(source):
            readers.update(readers_in(source, module_name(relative)))
    return readers, len(files)


def without_prose(source):
    """``source`` with comments and docstrings blanked, so line numbers hold."""

    lines = source.splitlines(keepends=True)
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            (row, start), (_, end) = token.start, token.end
            line = lines[row - 1]
            lines[row - 1] = line[:start] + " " * (end - start) + line[end:]
    for node in ast.walk(ast.parse(source)):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                line = lines[first.lineno - 1]
                indent = line[: len(line) - len(line.lstrip())]
                if first.col_offset != len(indent):
                    continue
                # A body that was only a docstring stays a valid body.
                lines[first.lineno - 1] = indent + "pass\n"
                for row in range(first.lineno + 1, first.end_lineno + 1):
                    lines[row - 1] = "\n"
    return "".join(lines)


# ---------------------------------------------------------------------------
# The inventory
# ---------------------------------------------------------------------------

SAMPLE_SOURCE = '''
"""Prose that says FROM spans is not a template."""
TEMPLATE = """SELECT 1 FROM spans WHERE 1"""
NOT_A_READER = "SELECT 1"


class Reader:
    ATTRIBUTE = "SELECT 1 FROM {table}"

    def reads(self):
        return f"SELECT 1 FROM {self.table}"

    def outer(self):
        def inner():
            return "SELECT 1 FROM spans"

        return inner

    def only_prose(self):
        """Mentions FROM spans without reading anything."""
        return 1


def rollup():
    return hourly_aggregate_state_source(1)
'''


def test_the_scan_applies_the_rule_to_functions_and_templates():
    # The inventory is only as good as its scan: a scan that found nothing, or
    # that credited a nested function to its parent, would let readers go unfiled.
    found = readers_in(SAMPLE_SOURCE, "sample")

    assert set(found) == {
        "sample:TEMPLATE",
        "sample:Reader.ATTRIBUTE",
        "sample:Reader.reads",
        "sample:Reader.outer.<locals>.inner",
        "sample:Reader.only_prose",
        "sample:rollup",
    }
    assert set(readers_in(without_prose(SAMPLE_SOURCE), "sample")) == (
        set(found) - {"sample:Reader.only_prose"}
    )


# Readers a person set aside that the scan cannot see: they reach a span only
# through a builder, so no SQL text of their own matches. Each is held to the one
# fact that keeps the decision true.
SET_ASIDE = {
    "tracer.services.clickhouse.bounded_graph_reads:read_graph_candidates": (
        NOT_REACHED,
        "the session chart's candidate sample: it builds the list builders without "
        "hide_simulated because only the session chart calls it",
        {"tracer.services.clickhouse.session_graph:_fetch_system_metric_graph"},
    ),
}


def _set_aside_problems():
    problems = []
    modules = {module_name(relative): relative for relative in non_test_python_files()}
    for key, (bucket, _why, callers) in SET_ASIDE.items():
        module, qualname = key.split(":", 1)
        if bucket not in (NOT_REACHED, COUNTS_AND_NAMES):
            problems.append(f"{key} is set aside under a bucket that cannot allow it")
        if module not in modules:
            problems.append(f"{key} is set aside but its module is gone")
            continue
        tree = ast.parse((FUTUREAGI / modules[module]).read_text(encoding="utf-8"))
        node = dict(functions_in(tree)).get(qualname)
        if node is None:
            problems.append(f"{key} is set aside but is no longer defined")
            continue
        if hide_signature(node) is not None:
            problems.append(
                f"{key} gained a hide_simulated argument; it is no longer left alone"
            )
        found = callers_of(qualname.rsplit(".", 1)[-1])
        if found != callers:
            problems.append(
                f"{key} was set aside for {sorted(callers)} and is called from "
                f"{sorted(found)}"
            )
    return problems


# ---------------------------------------------------------------------------
# What a filing claims, held to the code
# ---------------------------------------------------------------------------

# A reader filed as carrying the rule must have it built somewhere. A filing is
# a person's word, so the cheap facts it depends on are checked: the module
# calls one of these, or hands the rule to the modules named below, or the
# reader's own text names what it adds.
# ``_unmarked_row_sql(`` is a substring of ``simulation_unmarked_row_sql(`` on
# purpose: a module that builds the row test through either spelling hides. What
# the substring cannot show is that a list builder still adds its plan, which is
# why ``MUST_CALL`` pins that call per module.
HIDING_CALLS = (
    "row_exclusion_sql(",
    "postgres_span_exclusion_sql(",
    "resource_mark_sql(",
    "plan_hiding_simulated(",
    "_unmarked_row_sql(",
    "simulation_hide_plan(",
    "simulation_unmarked_row_sql(",
)
CARRIES_THE_RULE = (LIST_OR_CHART, PROJECT_WIDE, PICKER)
_BUILDERS = "tracer.services.clickhouse.query_builders"
_LISTS = (f"{_BUILDERS}.span_list", f"{_BUILDERS}.trace_list")
_QUERY_SERVICE = "tracer.services.clickhouse.query_service"
# The call that makes a list builder hide, written the way its module spells it.
MUST_CALL = {
    f"{_BUILDERS}.trace_list": 'simulation_hide_plan(scope="root")',
    f"{_BUILDERS}.span_list": 'simulation_hide_plan(scope="span")',
}
# Modules whose readers carry the rule by handing ``hide_simulated`` to a
# builder: each module named must itself call a hiding function.
HANDS_THE_RULE_ON = {
    "tracer.selectors.eval_tasks.continuous_candidates": _LISTS,
    "tracer.selectors.eval_tasks.row_resolver": _LISTS,
    f"{_BUILDERS}.filters": _LISTS,
    f"{_BUILDERS}.hourly_aggregate_states": (f"{_BUILDERS}.time_series",),
    f"{_BUILDERS}.voice_call_list": (f"{_BUILDERS}.trace_list",),
    "tracer.services.clickhouse.v2.query_builders.span_list": (
        f"{_BUILDERS}.span_list",
    ),
    "tracer.services.clickhouse.v2.query_builders.trace_list": (
        f"{_BUILDERS}.trace_list",
    ),
}
# Readers held to their own text instead of their module's: the one place a
# module-wide call would not show that this reader still adds the rule.
OWN_TEXT_CARRIES = {
    "tracer.views.dashboard:_read_dashboard_rollup_fast_path": "hide_simulated",
    f"{_QUERY_SERVICE}:AnalyticsQueryService.get_eval_config_ids_for_candidates_ch": (
        "row_exclusion_sql("
    ),
    f"{_QUERY_SERVICE}:AnalyticsQueryService.get_eval_config_ids_with_data_ch": (
        "hide_simulated=hide_simulated"
    ),
    f"{_BUILDERS}.dashboard:DashboardQueryBuilder._build_eval_metric_query": (
        "_marked_traces_hidden_clause("
    ),
    f"{_BUILDERS}.dashboard:DashboardQueryBuilder._build_annotation_metric_query": (
        "_marked_traces_hidden_clause("
    ),
    f"{_BUILDERS}.dashboard:DashboardQueryBuilder._marked_traces_hidden_clause": (
        "row_exclusion_sql("
    ),
    "tracer.services.clickhouse.filter_value_reads:_hiding_spans_source": (
        "_unmarked_row_sql("
    ),
    "tracer.services.clickhouse.filter_value_reads:_latest_span_value_cte": (
        "latest_unmarked"
    ),
    "tracer.services.clickhouse.filter_value_reads:_latest_voice_value_cte": (
        "latest_unmarked"
    ),
}
# A reader no holding project reaches because its view refuses first: the view
# file and the refusal that must stay in it.
GUARDED_AT_THE_VIEW = {
    "tracer.services.clickhouse.v2.property_catalog.value_reader:"
    "PropertyCatalogValueReader.read_page": (
        "tracer/views/dashboard.py",
        'PropertyCatalogValueNotReady("simulated_traces")',
    ),
}
# A reader filed as read by id takes an id of what it reads. The ids that only
# scope a read to a tenant do not count.
SCOPE_IDS = {
    "project_id",
    "project_ids",
    "org_id",
    "organization_id",
    "workspace_id",
    "project_version_id",
}
BY_ID_WITHOUT_AN_ID = {
    "tracer.services.clickhouse.v2.span_reader:_dedup_sql": (
        "wraps reads that name their ids in the where clause the caller passes"
    ),
    "tracer.services.clickhouse.v2.trace_detail_reads:"
    "TraceDetailReadBuilder.build_content_query": "takes the trace identities",
    "tracer.services.clickhouse.v2.trace_detail_reads:"
    "TraceDetailReadBuilder.build_identity_query": (
        "the builder is made for the one trace it is given"
    ),
    "tracer.services.simulation_visibility:_marked_trace_ids_sql": (
        "the mark lookup: the caller binds the trace ids as a parameter"
    ),
}


def _filing_problems(readers):
    """What the filings claim that the code no longer shows, and unfinished ones."""

    problems = []
    modules = {module_name(relative): relative for relative in non_test_python_files()}
    sources, functions, codes = {}, {}, {}

    def source_of(module):
        if module not in sources:
            sources[module] = (FUTUREAGI / modules[module]).read_text(encoding="utf-8")
        return sources[module]

    def code_of(module):
        # What the module runs: a comment or a docstring that names a hiding
        # function, or the line that defines one, is not a call.
        if module not in codes:
            codes[module] = "".join(
                line
                for line in without_prose(source_of(module)).splitlines(keepends=True)
                if not line.lstrip().startswith(("def ", "async def "))
            )
        return codes[module]

    def function_of(module, qualname):
        if module not in functions:
            functions[module] = dict(functions_in(ast.parse(source_of(module))))
        return functions[module].get(qualname)

    def calls_a_hiding_function(module):
        return any(call in code_of(module) for call in HIDING_CALLS)

    for key, filing in sorted(FILED.items()):
        module, qualname = key.split(":", 1)
        if filing.why.upper().startswith("UNDECIDED"):
            problems.append(f"{key} is filed as undecided: the owner has to rule")
        if KNOWN_GAP in filing.buckets and not filing.why.startswith(KNOWN_GAP_REASONS):
            problems.append(
                f"{key} is a known gap and its reason starts with neither "
                f"{KNOWN_GAP_REASONS}"
            )
        if module not in modules:
            continue  # a stale filing, or ``ee`` in the open-source tree
        if set(filing.buckets) & set(CARRIES_THE_RULE):
            needle = OWN_TEXT_CARRIES.get(key)
            if needle is not None:
                if needle not in readers.get(key, ""):
                    problems.append(
                        f"{key} is filed as carrying the rule and its own text "
                        f"no longer has {needle!r}"
                    )
            elif not calls_a_hiding_function(module):
                handed_to = HANDS_THE_RULE_ON.get(module, ())
                if not handed_to:
                    problems.append(
                        f"{key} is filed as carrying the rule, but {module} calls "
                        f"none of {HIDING_CALLS} and hands it to no other module"
                    )
                for other in handed_to:
                    if other not in modules or not calls_a_hiding_function(other):
                        problems.append(
                            f"{key} is filed as carrying the rule through {other}, "
                            f"which calls none of {HIDING_CALLS}"
                        )
        if BY_ID in filing.buckets and key not in BY_ID_WITHOUT_AN_ID:
            node = function_of(module, qualname)
            if node is not None:  # a template has no parameters to look at
                arguments = node.args
                names = [
                    argument.arg
                    for argument in (
                        arguments.posonlyargs + arguments.args + arguments.kwonlyargs
                    )
                ]
                if not any(
                    name.endswith(("_id", "_ids")) and name not in SCOPE_IDS
                    for name in names
                ):
                    problems.append(
                        f"{key} is filed as read by id and takes no id of what it "
                        f"reads: {names}"
                    )
    for module, needle in MUST_CALL.items():
        if module not in modules:
            problems.append(f"{module} must call {needle} but the module is gone")
        elif needle not in code_of(module):
            problems.append(
                f"{module} no longer calls {needle}, the plan that hides marked "
                "traces from every statement of the builder"
            )
    for key in BY_ID_WITHOUT_AN_ID:
        if BY_ID not in getattr(FILED.get(key), "buckets", ()):
            problems.append(f"{key} is excused from taking an id but is not read by id")
    for key, (view, refusal) in GUARDED_AT_THE_VIEW.items():
        if key not in FILED:
            problems.append(f"{key} is guarded at the view but is not filed")
        if refusal not in (FUTUREAGI / view).read_text(encoding="utf-8"):
            problems.append(f"{key} is no longer refused for a holding scope in {view}")
    return problems


def test_every_spans_reading_function_is_filed():
    readers, scanned = scan_readers()

    assert scanned > 1000 and (
        "tracer.services.clickhouse.v2.span_reader:CHSpanReader.get" in readers
    ), "the scan did not reach the code it is meant to cover"

    enterprise_present = (FUTUREAGI / ENTERPRISE_PREFIX.rstrip(".")).is_dir()
    unfiled = sorted(set(readers) - set(FILED))
    stale = sorted(
        key
        for key in set(FILED) - set(readers)
        if enterprise_present or not key.startswith(ENTERPRISE_PREFIX)
    )
    assert not unfiled, (
        "these functions or templates read a span table and nobody filed them; "
        "decide whether each must leave marked traces out, then add it to FILED "
        "under the bucket that covers it, with a reason:\n" + "\n".join(unfiled)
    )
    assert not stale, (
        "these filings no longer match a reader; rename or remove them:\n"
        + "\n".join(stale)
    )

    unlisted = {
        key: filing.buckets
        for key, filing in FILED.items()
        if not filing.buckets or not set(filing.buckets) <= set(BUCKETS)
    }
    assert not unlisted, f"filed under a bucket that does not exist: {unlisted}"
    assert not [key for key, filing in FILED.items() if not filing.why.strip()]
    used = {bucket for filing in FILED.values() for bucket in filing.buckets}
    assert used == set(BUCKETS), (
        f"buckets nothing is filed under: {sorted(set(BUCKETS) - used)}"
    )
    set_aside = _set_aside_problems()
    assert not set_aside, "\n".join(set_aside)
    claims = _filing_problems(readers)
    assert not claims, "\n".join(claims)


def test_no_include_simulation_anywhere():
    # A request-level switch that brings marked traces back would let any caller
    # undo the hiding; none exists, and none is to be added by accident.
    needle = "include_simulation="
    offenders = [
        str(relative)
        for relative in non_test_python_files()
        if needle in (FUTUREAGI / relative).read_text(encoding="utf-8")
    ]

    assert not offenders, f"{needle!r} appears in: {offenders}"


def test_no_span_table_bucket_names_its_table():
    modules = {module_name(relative): relative for relative in non_test_python_files()}
    code = {}
    problems = []
    for key, filing in sorted(FILED.items()):
        if NO_SPAN_TABLE not in filing.buckets:
            if filing.table is not None:
                problems.append(f"{key} names a table but is not filed as reading none")
            continue
        named = (filing.table or "").strip()
        if not named:
            problems.append(f"{key} is filed as reading no span table and names none")
            continue
        if any(span_table in named.lower() for span_table in SPAN_TABLES):
            problems.append(f"{key} names {named!r}, which is a span table")
        module = key.split(":", 1)[0]
        if module not in modules:
            continue  # a stale filing; the first case reports it
        if module not in code:
            source = (FUTUREAGI / modules[module]).read_text(encoding="utf-8")
            code[module] = readers_in(without_prose(source), module)
        # The filing is a person's word; the code is checked against it. A
        # comment or a docstring that says "FROM spans" reads nothing.
        text = code[module].get(key, "")
        for match in READS_A_TABLE.finditer(text):
            read = match.group(1)
            if (
                read.startswith(("spans", "trace_count_rollup"))
                or "span" in read.lower()
            ):
                problems.append(f"{key} says it reads no span table but reads {read!r}")
        if HOURLY_ROLLUP_SOURCE in text:
            problems.append(f"{key} says it reads no span table but builds the rollup")

    assert not problems, "\n".join(problems)


# ---------------------------------------------------------------------------
# The chart entry points
# ---------------------------------------------------------------------------

DISPATCH = "tracer.services.clickhouse.graph_dispatch"
EXACT = "tracer.services.clickhouse.exact_graph_reads"
OPTIMIZED = "tracer.utils.graphs_optimized"

CHART_ENTRY_POINT = re.compile(
    r"^(fetch_\w+_graph_ch|fetch_\w+_chart_series_ch|fetch_all_system_metrics_ch"
    r"|get_\w+_graph_data|get_all_system_metrics|get_system_metric_data)$"
)
EXACT_READ = re.compile(r"^read_exact_")
# Entry points the name patterns above do not catch.
NAMED_ENTRY_POINTS = {
    "fetch_background_raw_system_metric_graph",
    "_read_direct_system_metrics",
}


def functions_in(tree):
    """``(qualname, node)`` for every function defined anywhere in ``tree``."""

    def walk(body, prefix):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield prefix + node.name, node
                yield from walk(node.body, prefix + node.name + ".<locals>.")
            elif isinstance(node, ast.ClassDef):
                yield from walk(node.body, prefix + node.name + ".")
            else:
                for block in _blocks(node):
                    yield from walk(block, prefix)

    yield from walk(tree.body, "")


def chart_entry_points():
    """Module-level chart entry points under ``tracer/``, and every exact read."""

    found = {}
    for relative in non_test_python_files():
        if relative.parts[0] != "tracer":
            continue
        text = (FUTUREAGI / relative).read_text(encoding="utf-8")
        if not re.search(r"def (fetch_|get_|read_exact_|_read_direct_)", text):
            continue
        in_clickhouse = relative.parts[:3] == ("tracer", "services", "clickhouse")
        module = module_name(relative)
        for qualname, node in functions_in(ast.parse(text)):
            if "." in qualname:
                continue  # methods and nested functions are not entry points
            if (
                CHART_ENTRY_POINT.match(node.name)
                or (in_clickhouse and EXACT_READ.match(node.name))
                or node.name in NAMED_ENTRY_POINTS
            ):
                found[f"{module}:{qualname}"] = node
    return found


def hide_signature(node):
    """None without the parameter, "ok" when keyword-only with the default False."""

    arguments = node.args
    for argument, default in zip(
        arguments.kwonlyargs, arguments.kw_defaults, strict=True
    ):
        if argument.arg == "hide_simulated":
            if isinstance(default, ast.Constant) and default.value is False:
                return "ok"
            return "keyword-only without the default False"
    if any(a.arg == "hide_simulated" for a in arguments.posonlyargs + arguments.args):
        return "positional"
    return None


def _called_names(function):
    """Names a function calls in its own body, nested functions left out."""

    names = set()
    pending = list(ast.iter_child_nodes(function))
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(node, ast.Call):
            target = node.func
            names.add(
                target.id
                if isinstance(target, ast.Name)
                else getattr(target, "attr", None)
            )
        pending.extend(ast.iter_child_nodes(node))
    return names


def callers_of(name):
    """``module:qualname`` of each non-test function that calls ``name``."""

    found = set()
    for relative in non_test_python_files():
        text = (FUTUREAGI / relative).read_text(encoding="utf-8")
        if name not in text:
            continue
        module = module_name(relative)
        for qualname, node in functions_in(ast.parse(text)):
            if name in _called_names(node):
                found.add(f"{module}:{qualname}")
    return found


def non_test_references(name):
    """Files that use ``name`` as a name, an attribute or an import."""

    users = []
    for relative in non_test_python_files():
        text = (FUTUREAGI / relative).read_text(encoding="utf-8")
        if name not in text:
            continue
        for node in ast.walk(ast.parse(text)):
            if (
                (isinstance(node, ast.Name) and node.id == name)
                or (isinstance(node, ast.Attribute) and node.attr == name)
                or (isinstance(node, ast.alias) and node.name == name)
            ):
                users.append(str(relative))
                break
    return users


# ----------------------------- the test doubles -----------------------------

PROJECT = "11111111-1111-4111-8111-111111111111"
ORG = "22222222-2222-4222-8222-222222222222"
OTHER_PROJECT = "77777777-7777-4777-8777-777777777777"
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
        pytest.fail("an inventory case reached a database or the network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(BaseDatabaseWrapper, "ensure_connection", forbidden)
    settings.DASHBOARD_TRACE_REPLICA_SHARD_CLUSTER = ""
    settings.DASHBOARD_ATTR_ROLLUP_ENABLED = False
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


class RecordingAnalytics:
    """Answers the cost probe with an affordable estimate and every read with no rows."""

    supports_per_query_read_settings = True

    def __init__(self):
        self.calls = []

    def execute_ch_query(self, query, params=None, **kwargs):
        self.calls.append((query, dict(params or {})))
        if "EXPLAIN ESTIMATE" in query:
            return affordable_scan_estimate(1_000)
        return SimpleNamespace(data=[], columns=COLUMNS, query_time_ms=1)

    @property
    def statements(self):
        """What the read itself sent, without the cost probes."""

        return [call for call in self.calls if "EXPLAIN ESTIMATE" not in call[0]]


def carries_the_rule(sql):
    """The row test of the span grain, or the plan that holds both marks."""

    flat = squash(sql)
    return squash(UNMARKED) in flat or (
        SIMULATION_MARK_KEY in flat and RESOURCE_MARK_KEY in flat
    )


def mentions_a_mark(calls):
    """Whether any statement, or any parameter it carries, names either mark."""

    text = " ".join(f"{sql} {sorted(params.items())!r}" for sql, params in calls)
    return SIMULATION_MARK_KEY in text or RESOURCE_MARK_KEY in text


@contextmanager
def no_snapshot():
    """Every cache probe misses, so the read runs its statement inline."""

    with mock.patch.object(
        dispatch, "read_or_schedule_exact_snapshot", lambda *a, **k: None
    ):
        yield


@contextmanager
def eval_config():
    """The one Postgres read of an eval chart, answered in memory."""

    config = SimpleNamespace(
        name="quality",
        eval_template=SimpleNamespace(config={"output": "SCORE"}, choices=[]),
    )
    with mock.patch.object(
        exact.CustomEvalConfig.objects,
        "select_related",
        lambda *_args: SimpleNamespace(get=lambda **_kwargs: config),
    ):
        yield


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


@contextmanager
def annotation_rows():
    """The label and Score reads of an annotation chart, answered in memory."""

    label = SimpleNamespace(name="quality", type="numeric")
    row = {
        "trace_id": TRACE,
        "observation_span_id": SPAN,
        "created_at": END - timedelta(hours=3),
        "value": {"rating": 4},
    }
    with (
        mock.patch.object(
            exact,
            "get_annotation_labels_for_project",
            lambda _project_id: SimpleNamespace(get=lambda **_kwargs: label),
        ),
        mock.patch.object(
            exact, "Score", SimpleNamespace(no_workspace_objects=_ScoreManager(row))
        ),
    ):
        yield


@contextmanager
def helper_analytics(analytics):
    """The analytics service the project chart helpers build for themselves."""

    with mock.patch.object(
        v2_query_service, "V2AnalyticsQueryService", lambda: analytics
    ):
        yield


# ----------------------- the readers that reach a statement -----------------------
# Each case runs one entry point with the analytics double it is given and the
# keyword arguments of the call: nothing, ``hide_simulated=False`` or ``True``.


def _system(observe_type):
    def run(analytics, **hide):
        with no_snapshot():
            dispatch.fetch_system_metric_graph_ch(
                analytics=analytics,
                project_id=PROJECT,
                filters=[window(), leaf("company_id", "company")],
                interval="day",
                metric_id="latency",
                observe_type=observe_type,
                **hide,
            )

    return run


def _background(analytics, **hide):
    dispatch.fetch_background_raw_system_metric_graph(
        analytics=analytics,
        project_id=PROJECT,
        filters=[window()],
        interval="day",
        metric_id="latency",
        observe_type="trace",
        **hide,
    )


def _all_metrics_ch(analytics, **hide):
    dispatch.fetch_all_system_metrics_ch(
        analytics=analytics,
        project_id=PROJECT,
        filters=[window()],
        interval="day",
        **hide,
    )


def _eval_ch(analytics, **hide):
    with eval_config():
        dispatch.fetch_eval_graph_ch(
            analytics=analytics,
            project_id=PROJECT,
            filters=[window()],
            interval="day",
            req_data_config={"id": CONFIG, "eval_output_type": "SCORE"},
            observe_type="trace",
            **hide,
        )


def _annotation_ch(observe_type):
    def run(analytics, **hide):
        with annotation_rows():
            dispatch.fetch_annotation_graph_ch(
                analytics=analytics,
                project_id=PROJECT,
                filters=[window(days=1)],
                interval="day",
                req_data_config={"id": LABEL, "output_type": "float"},
                observe_type=observe_type,
                **hide,
            )

    return run


def _exact_system(observe_type):
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

    return run


def _exact_all_metrics(analytics, **hide):
    exact.read_exact_all_system_metrics(
        analytics=analytics,
        project_id=PROJECT,
        filters=[window()],
        interval="day",
        **hide,
    )


def _exact_agent(analytics, **hide):
    exact.read_exact_agent_graph(
        analytics=analytics, project_id=PROJECT, filters=[window()], **hide
    )


def _exact_eval(analytics, **hide):
    with eval_config():
        exact.read_exact_eval_graph(
            analytics=analytics,
            project_id=PROJECT,
            filters=[window()],
            interval="day",
            req_data_config={"id": CONFIG, "eval_output_type": "SCORE"},
            observe_type="trace",
            all_series=False,
            **hide,
        )


def _exact_annotation(observe_type):
    def run(analytics, **hide):
        with annotation_rows():
            exact.read_exact_annotation_graph(
                analytics=analytics,
                project_id=PROJECT,
                filters=[window(days=1)],
                interval="day",
                req_data_config={"id": LABEL, "output_type": "float"},
                observe_type=observe_type,
                **hide,
            )

    return run


class _AttributeExecutor:
    """The attribute reader's executor, answering with the recording double."""

    def __init__(self, analytics):
        self.analytics = analytics

    def execute(self, sql, params=None, **kwargs):
        return self.analytics.execute_ch_query(sql, params, **kwargs)


def _exact_attribute_detail(analytics, **hide):
    # The window end is fixed so that two calls send the same parameters.
    read_exact_attribute_detail(
        project_id=PROJECT,
        attribute_key="agent.name",
        executor=_AttributeExecutor(analytics),
        window_end=END,
        **hide,
    )


def _direct_helper(analytics, **hide):
    with helper_analytics(analytics):
        graphs_optimized._read_direct_system_metrics(
            project_id=PROJECT, filters=[window()], interval="day", **hide
        )


def _all_metrics_helper(analytics, **hide):
    with helper_analytics(analytics):
        graphs_optimized.get_all_system_metrics(
            interval="day",
            filters=[window()],
            property="average",
            system_metric_filters={"project_id": PROJECT},
            **hide,
        )


def _one_metric_helper(analytics, **hide):
    with helper_analytics(analytics):
        graphs_optimized.get_system_metric_data(
            interval="day",
            filters=[window()],
            property="average",
            req_data_config={"id": "latency"},
            system_metric_filters={"project_id": PROJECT},
            observe_type="charts",
            **hide,
        )


# A hiding read must send the rule; a read that does not hide must send what it
# sent before, with no mention of either mark.
SENDS_A_STATEMENT = {
    f"{DISPATCH}:fetch_system_metric_graph_ch": (_system("trace"), _system("span")),
    f"{DISPATCH}:fetch_background_raw_system_metric_graph": (_background,),
    f"{DISPATCH}:fetch_all_system_metrics_ch": (_all_metrics_ch,),
    f"{DISPATCH}:fetch_eval_graph_ch": (_eval_ch,),
    f"{DISPATCH}:fetch_annotation_graph_ch": (
        _annotation_ch("trace"),
        _annotation_ch("span"),
    ),
    f"{EXACT}:read_exact_system_graph": (_exact_system("trace"), _exact_system("span")),
    f"{EXACT}:read_exact_all_system_metrics": (_exact_all_metrics,),
    f"{EXACT}:read_exact_agent_graph": (_exact_agent,),
    f"{EXACT}:read_exact_eval_graph": (_exact_eval,),
    f"{EXACT}:read_exact_annotation_graph": (
        _exact_annotation("trace"),
        _exact_annotation("span"),
    ),
    "tracer.services.clickhouse.exact_attribute_detail:read_exact_attribute_detail": (
        _exact_attribute_detail,
    ),
    f"{OPTIMIZED}:_read_direct_system_metrics": (_direct_helper,),
    f"{OPTIMIZED}:get_all_system_metrics": (_all_metrics_helper,),
    f"{OPTIMIZED}:get_system_metric_data": (_one_metric_helper,),
}


# ------------------- the readers that only hand over an identity -------------------
# These answer from the snapshot cache and send no statement of their own: the
# worker computes the snapshot from the identity. The rule reaches the worker
# only if the identity carries it.


def _fetch_system(**hide):
    dispatch.fetch_system_metric_graph_ch(
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
    dispatch.fetch_agent_graph_ch(
        project_id=PROJECT, filters=[window()], organization_id=ORG, **hide
    )


def _fetch_eval(**hide):
    dispatch.fetch_eval_graph_ch(
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
    dispatch.fetch_eval_chart_series_ch(
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
    dispatch.fetch_annotation_graph_ch(
        analytics=RecordingAnalytics(),
        project_id=PROJECT,
        filters=[window()],
        interval="day",
        req_data_config={"id": LABEL},
        observe_type="trace",
        organization_id=ORG,
        **hide,
    )


def _eval_helper(**hide):
    with helper_analytics(RecordingAnalytics()), eval_config():
        graphs_optimized.get_eval_graph_data(
            interval="day",
            filters=[window()],
            property="average",
            observe_type="charts",
            req_data_config={"id": CONFIG},
            eval_logger_filters={"project_id": PROJECT},
            **hide,
        )


# (what the entry point is, the namespace of the identity it hands over, the call)
HANDS_OVER_AN_IDENTITY = (
    ("fetch_system_metric_graph_ch", "observe-system-graph", _fetch_system),
    ("fetch_agent_graph_ch", "observe-agent-graph", _fetch_agent),
    ("fetch_eval_graph_ch", "observe-eval-graph", _fetch_eval),
    ("fetch_eval_chart_series_ch", "observe-eval-chart-series", _fetch_eval_series),
    ("fetch_annotation_graph_ch", "observe-annotation-graph", _fetch_annotation),
    ("get_eval_graph_data", "observe-eval-chart-series", _eval_helper),
)
# Entry points checked by their identity alone: they send no statement.
ANSWERS_FROM_THE_CACHE = {
    f"{DISPATCH}:fetch_agent_graph_ch",
    f"{DISPATCH}:fetch_eval_chart_series_ch",
    f"{OPTIMIZED}:get_eval_graph_data",
}

# The entry points no hiding argument reaches, each with the bucket that says why.
# Their signature must stay without the argument: one that gains it has to move
# to the cases above, where the rule is shown to reach its statement.
LEFT_ALONE = {
    f"{DISPATCH}:fetch_user_system_metric_graph_ch": (
        NOT_REACHED,
        "user chart",
    ),
    "tracer.services.clickhouse.session_graph:fetch_session_graph_ch": (
        NOT_REACHED,
        "session chart",
    ),
    f"{EXACT}:read_exact_user_system_graph": (NOT_REACHED, "user chart"),
    f"{EXACT}:read_exact_session_system_graph": (NOT_REACHED, "session chart"),
    f"{OPTIMIZED}:get_annotation_graph_data": (
        NOT_REACHED,
        "dead code: nothing calls it, the live annotation chart is "
        "fetch_annotation_graph_ch",
    ),
}
# Monitor charts have no argument either: the monitor builder asks for its own
# project, or for the projects it spans, and adds the row test itself.
DECIDES_IN_THE_BUILDER = {
    "tracer.utils.monitor_graphs:get_static_metric_graph_data",
    "tracer.utils.monitor_graphs:get_percentage_change_metric_graph_data",
}

# Every function that hands a chart identity to the snapshot cache.
IDENTITY_CALLERS = {
    f"{DISPATCH}:_schedule_unaffordable_graph_read",
    f"{DISPATCH}:fetch_system_metric_graph_ch",
    f"{DISPATCH}:fetch_agent_graph_ch",
    f"{DISPATCH}:fetch_eval_chart_series_ch",
    f"{DISPATCH}:fetch_user_system_metric_graph_ch",
}
# Every function that asks the snapshot cache directly, and why its identity is safe.
SNAPSHOT_READERS = {
    f"{DISPATCH}:_read_or_refresh_exact_graph": "the chart identities above",
    "tracer.views.dashboard:DashboardWidgetViewSet._execute_ch_query_config": (
        "the dashboard snapshot, checked below"
    ),
    "tracer.views.dashboard:_read_public_dashboard_query": (
        "takes the dashboard's identity from its caller"
    ),
    "tracer.services.clickhouse.session_graph:fetch_session_graph_ch": (
        "session chart, outside the hiding rule"
    ),
    "tracer.views.span_attributes:SpanAttributeDetailView.get": (
        "attribute catalog detail: the identity carries hide_simulated only when "
        "the project holds, and the refresh reads the rule from it"
    ),
    "model_hub.views.separate_evals:EvalUsageStatsView.get": (
        "eval usage over the API call log, no span table"
    ),
}


@contextmanager
def recorded_identities():
    """Each identity a chart read hands to the snapshot cache, with its namespace.

    A filtered eval or annotation chart reaches the cache only once it is handed
    to the background, so the cost gate is made to refuse it.
    """

    seen = []

    def record(namespace, identity, **kwargs):
        seen.append((namespace, dict(identity)))
        return kwargs["pending_payload"]

    with (
        mock.patch.object(dispatch, "read_or_schedule_exact_snapshot", record),
        mock.patch.object(
            dispatch,
            "_affordable_raw_log_membership_read",
            lambda **_: dispatch._GraphReadUnaffordable(estimated_rows=None),
        ),
    ):
        yield seen


class StubAnalytics:
    """Records every statement the dashboard view sends to ClickHouse."""

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


def dashboard_query(project_id):
    end = datetime(2026, 9, 1, tzinfo=UTC)
    return {
        "project_ids": [project_id],
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
                "source": "traces",
                "aggregation": "avg",
            }
        ],
        "filters": [],
        "breakdowns": [],
    }


def dashboard_snapshot_identity(project_id, holding):
    """The identity the dashboard hands to the snapshot cache for one project."""

    analytics = StubAnalytics()
    identities = []

    def cold_miss(_namespace, identity, **kwargs):
        identities.append(deepcopy(identity))
        payload = dict(kwargs["pending_payload"])
        payload.update(query_refreshing=False, query_refresh_failed=False)
        return payload

    def holding_among(project_ids):
        return frozenset(p for p in project_ids if p in holding)

    workspace = SimpleNamespace(id=uuid.uuid4(), organization_id=uuid.uuid4())
    with (
        mock.patch(
            "tracer.views.dashboard._materialize_dashboard_query_scope",
            side_effect=lambda config, *_args, **_kwargs: config,
        ),
        mock.patch(
            "tracer.views.dashboard._read_dashboard_rollup_fast_path",
            mock.MagicMock(return_value=None),
        ),
        mock.patch(
            "tracer.views.dashboard.read_or_schedule_exact_snapshot",
            side_effect=cold_miss,
        ),
        mock.patch(
            "tracer.views.dashboard._read_public_dashboard_query",
            mock.MagicMock(return_value={"query_status": "pending"}),
        ),
        mock.patch(
            "tracer.views.dashboard.V2AnalyticsQueryService",
            side_effect=lambda **_kwargs: analytics,
        ),
        mock.patch(
            "tracer.views.dashboard._project_queryset_for_dashboard_scope",
            return_value=mock.MagicMock(
                filter=mock.MagicMock(return_value=mock.MagicMock(count=lambda: 1)),
            ),
        ),
        mock.patch(
            "tracer.views.dashboard.Project.objects.filter",
            return_value=mock.MagicMock(
                values_list=mock.MagicMock(return_value=[]),
            ),
        ),
        mock.patch("tracer.views.dashboard.holding_projects", holding_among),
    ):
        DashboardWidgetViewSet()._execute_ch_query_config(
            dashboard_query(project_id),
            workspace,
            refresh=False,
            _exact_worker=False,
            cache_identity_override=None,
        )
    return identities


# ------------------------------- the checks -------------------------------


def _entry_point_problems(points):
    problems = []
    named = (
        set(SENDS_A_STATEMENT)
        | ANSWERS_FROM_THE_CACHE
        | set(LEFT_ALONE)
        | DECIDES_IN_THE_BUILDER
    )
    for key in sorted(set(points) - named):
        problems.append(
            f"{key} is a chart entry point nobody decided about: give it a "
            "hide_simulated argument and a case, or file it as left alone"
        )
    for key in sorted(named - set(points)):
        problems.append(f"{key} is named here but is no longer a chart entry point")
    for key, node in sorted(points.items()):
        shape = hide_signature(node)
        if key in SENDS_A_STATEMENT or key in ANSWERS_FROM_THE_CACHE:
            if shape != "ok":
                problems.append(
                    f"{key} must take hide_simulated keyword-only with the "
                    f"default False; it has: {shape}"
                )
        elif key in LEFT_ALONE or key in DECIDES_IN_THE_BUILDER:
            if shape is not None:
                problems.append(
                    f"{key} gained a hide_simulated argument; move it to the cases "
                    "that show the rule reaching its statement"
                )
    for key, (bucket, _why) in LEFT_ALONE.items():
        if bucket not in (NOT_REACHED, COUNTS_AND_NAMES):
            problems.append(f"{key} is left alone under a bucket that cannot allow it")
    return problems


def _statement_problems(key, cases):
    problems = []
    for case in cases:
        omitted, off, on = (
            RecordingAnalytics(),
            RecordingAnalytics(),
            RecordingAnalytics(),
        )
        try:
            case(omitted)
            case(off, hide_simulated=False)
            case(on, hide_simulated=True)
        except Exception as error:
            problems.append(f"{key} raised {error!r} before it could be checked")
            continue
        if not omitted.statements:
            problems.append(f"{key} sent no statement, so nothing could be checked")
        if off.statements != omitted.statements:
            problems.append(
                f"{key} sends one statement for hide_simulated=False and another "
                "with the argument omitted"
            )
        if mentions_a_mark(omitted.calls):
            problems.append(f"{key} names a mark when it was not asked to hide")
        if not any(carries_the_rule(sql) for sql, _params in on.statements):
            problems.append(
                f"{key} hides nothing when told to: no statement has the rule"
            )
    return problems


def _identity_problems():
    problems = []
    for name, namespace, fetch in HANDS_OVER_AN_IDENTITY:
        try:
            with recorded_identities() as seen:
                fetch()
                fetch(hide_simulated=False)
                fetch(hide_simulated=True)
        except Exception as error:
            problems.append(f"{name} raised {error!r} before its identity was read")
            continue
        if [seen_namespace for seen_namespace, _ in seen] != [namespace] * 3:
            problems.append(
                f"{name} handed over {[n for n, _ in seen]}, not {namespace} three times"
            )
            continue
        omitted, off, on = (identity for _, identity in seen)
        if "hide_simulated" in omitted:
            problems.append(
                f"{name} puts hide_simulated in an identity that does not hide"
            )
        if off != omitted:
            problems.append(
                f"{name} keys hide_simulated=False apart from the argument omitted"
            )
        if on != {**omitted, "hide_simulated": True}:
            problems.append(
                f"{name} does not add exactly hide_simulated=True when hiding"
            )
    return problems


def _dashboard_problems():
    holding = {PROJECT}
    try:
        held = dashboard_snapshot_identity(PROJECT, holding)
        unheld = dashboard_snapshot_identity(OTHER_PROJECT, holding)
    except Exception as error:
        return [f"the dashboard snapshot raised {error!r} before its identity was read"]
    problems = []
    if len(held) != 1 or len(unheld) != 1:
        return [
            f"the dashboard asked the snapshot cache {len(held)} and {len(unheld)} times"
        ]
    if held[0].get("hide_simulated") is not True:
        problems.append(
            "the dashboard snapshot of a holding project lacks hide_simulated"
        )
    if "hide_simulated" in unheld[0]:
        problems.append(
            "the dashboard snapshot of a project that does not hold has the key"
        )
    if "hide_simulated" in held[0].get("query_config", {}):
        problems.append(
            "the dashboard stores hide_simulated in the query, not the identity"
        )
    return problems


def _caller_problems():
    problems = []
    callers = callers_of("_read_or_refresh_exact_graph")
    for key in sorted(callers - IDENTITY_CALLERS):
        problems.append(f"{key} hands a chart identity to the cache and has no check")
    for key in sorted(IDENTITY_CALLERS - callers):
        problems.append(f"{key} no longer hands a chart identity to the cache")
    readers = callers_of("read_or_schedule_exact_snapshot")
    for key in sorted(readers - set(SNAPSHOT_READERS)):
        problems.append(
            f"{key} asks the snapshot cache and nobody decided its identity"
        )
    for key in sorted(set(SNAPSHOT_READERS) - readers):
        problems.append(f"{key} no longer asks the snapshot cache")
    return problems


def _dead_code_problems():
    problems = []
    for key, (_bucket, why) in LEFT_ALONE.items():
        if why.startswith("dead code"):
            name = key.split(":", 1)[1]
            users = non_test_references(name)
            if users:
                problems.append(f"{key} is filed as dead code but {users} use it")
    return problems


def test_every_chart_entry_point_and_exact_read_carries_the_predicate():
    problems = _entry_point_problems(chart_entry_points())
    for key, cases in SENDS_A_STATEMENT.items():
        problems += _statement_problems(key, cases)
    problems += _identity_problems()
    problems += _dashboard_problems()
    problems += _caller_problems()
    problems += _dead_code_problems()

    assert not problems, "\n".join(problems)
