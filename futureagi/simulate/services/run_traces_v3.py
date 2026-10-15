"""The traces a code agent's own process exported during one run.

A run of a ``source`` agent whose observability is on marks every span its
process exports with the job id as a resource attribute. This read lists the
run's traces by that mark, scoped to the agent's project and to the job's own
time window, newest first, one page at a time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog
from django.utils import timezone

from simulate.models import HostedHarnessJob, TestExecution
from simulate.services.agent_setup import AgentNotFound, agent_summary
from tracer.services.clickhouse.client import get_clickhouse_client
from tracer.services.clickhouse.v2.query_builders import columns as cols

logger = structlog.get_logger(__name__)

RUN_MARK_KEY = "fi.simulation.job_id"
RESOURCE_ATTRS_COLUMN = "resource_attrs"
READ_TIMEOUT_MS = 2000
TRACED_AGENT_KIND = "source"


@dataclass(frozen=True)
class RunTrace:
    trace_id: str
    name: str
    start_time: datetime
    status: str
    latency_ms: float | None


@dataclass(frozen=True)
class RunTracesPage:
    count: int
    traces: list[RunTrace]


OBSERVABILITY_UNAVAILABLE = "unavailable"
OBSERVABILITY_OFF = "off"
OBSERVABILITY_ON = "on"


@dataclass(frozen=True)
class RunTracesSource:
    job: HostedHarnessJob
    project_id: str


@dataclass(frozen=True)
class RunTracesState:
    observability: str
    agent_id: str | None = None
    source: RunTracesSource | None = None


def run_traces_state(execution: TestExecution) -> RunTracesState:
    """Whether this run's traces can exist, and where to read them when they can."""
    unavailable = RunTracesState(observability=OBSERVABILITY_UNAVAILABLE)
    job = HostedHarnessJob.no_workspace_objects.filter(
        test_execution=execution, deleted=False
    ).first()
    if job is None:
        return unavailable
    agent = execution.agent_definition or execution.run_test.agent_definition
    if agent is None:
        return unavailable
    try:
        summary = agent_summary(agent.id)
    except AgentNotFound:
        return unavailable
    if summary.kind != TRACED_AGENT_KIND:
        return unavailable
    row = agent.observability_provider
    if row is None or row.deleted or not row.enabled or row.project.deleted:
        return RunTracesState(observability=OBSERVABILITY_OFF, agent_id=str(agent.id))
    return RunTracesState(
        observability=OBSERVABILITY_ON,
        agent_id=str(agent.id),
        source=RunTracesSource(job=job, project_id=str(row.project_id)),
    )


def _run_filter_sql() -> str:
    mark = cols.json_path(RESOURCE_ATTRS_COLUMN, RUN_MARK_KEY, "String")
    return (
        "FROM spans "
        "WHERE project_id = %(project_id)s "
        "AND start_time >= %(window_start)s "
        "AND start_time <= %(window_end)s "
        f"AND {mark} = %(job_id)s "
    )


def run_traces_count_sql() -> str:
    return "SELECT uniqExact(trace_id) " + _run_filter_sql()


def run_traces_sql() -> str:
    root_first = "(parent_span_id != '', start_time)"
    return (
        "SELECT trace_id, "
        f"argMin(name, {root_first}) AS root_name, "
        "min(start_time) AS started_at, "
        f"argMin(status, {root_first}) AS root_status, "
        f"argMin(latency_ms, {root_first}) AS root_latency_ms "
        + _run_filter_sql()
        + "GROUP BY trace_id "
        "ORDER BY started_at DESC, trace_id "
        "LIMIT %(limit)s OFFSET %(offset)s"
    )


def run_traces_params(source: RunTracesSource) -> dict[str, Any]:
    job = source.job
    return {
        "project_id": source.project_id,
        "job_id": str(job.id),
        "window_start": job.created_at,
        "window_end": job.terminal_at or timezone.now(),
    }


def read_run_traces(
    source: RunTracesSource, *, page: int, page_size: int
) -> RunTracesPage:
    client = get_clickhouse_client()
    params = run_traces_params(source)
    count_rows, _columns, _elapsed_ms = client.execute_read(
        run_traces_count_sql(), params, timeout_ms=READ_TIMEOUT_MS
    )
    count = int(count_rows[0][0]) if count_rows else 0
    if count == 0 or (page - 1) * page_size >= count:
        return RunTracesPage(count=count, traces=[])
    rows, _columns, _elapsed_ms = client.execute_read(
        run_traces_sql(),
        {**params, "limit": page_size, "offset": (page - 1) * page_size},
        timeout_ms=READ_TIMEOUT_MS,
    )
    return RunTracesPage(
        count=count,
        traces=[
            RunTrace(
                trace_id=str(trace_id),
                name=name or "",
                start_time=start_time,
                status=status or "",
                latency_ms=latency_ms,
            )
            for trace_id, name, start_time, status, latency_ms in rows
        ],
    )
