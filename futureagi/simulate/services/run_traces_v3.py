"""The traces a code agent's own process exported during one run.

A run of a ``source`` agent marks every span its process exports with the job id
as a resource attribute, and every span of a scenario with that call's id. The
collector stores them in the agent's hidden simulation project. This read lists
the run's traces by the job mark, scoped to that project and to the job's own
time window, newest first.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.utils import timezone

from simulate.models import CallExecution, HostedHarnessJob, TestExecution
from simulate.services.agent_setup import AgentNotFound, agent_summary
from tracer.models.project import Project
from tracer.services.clickhouse.client import get_clickhouse_client
from tracer.services.clickhouse.v2.query_builders import columns as cols

RUN_MARK_KEY = "fi.simulation.job_id"
CALL_MARK_KEY = "fi.simulator.call_execution_id"
READ_TIMEOUT_MS = 2000
MAX_TRACES = 200
TRACED_AGENT_KIND = "source"


@dataclass(frozen=True)
class RunTrace:
    trace_id: str
    name: str | None
    start_time: datetime | None
    status: str | None
    latency_ms: int | None
    call_execution_id: str | None


@dataclass(frozen=True)
class RunTracesSource:
    job: HostedHarnessJob
    project_id: str


def simulation_project_id(agent) -> str | None:
    project = Project.with_simulation.filter(
        simulation_agent=agent, deleted=False
    ).first()
    return str(project.id) if project else None


def run_traces_source(execution: TestExecution) -> RunTracesSource | None:
    job = HostedHarnessJob.no_workspace_objects.filter(
        test_execution=execution, deleted=False
    ).first()
    if job is None:
        return None
    agent = execution.agent_definition or execution.run_test.agent_definition
    if agent is None:
        return None
    try:
        summary = agent_summary(agent.id)
    except AgentNotFound:
        return None
    if summary.kind != TRACED_AGENT_KIND:
        return None
    project_id = simulation_project_id(agent)
    if project_id is None:
        return None
    return RunTracesSource(job=job, project_id=project_id)


def is_call_of_run(execution: TestExecution, call_execution_id: str) -> bool:
    return CallExecution.objects.filter(
        id=call_execution_id, test_execution=execution
    ).exists()


def run_traces_sql(*, by_call: bool = False) -> str:
    job_mark = cols.json_path(cols.RESOURCE_ATTRS, RUN_MARK_KEY, "String")
    call_mark = f"{cols.ATTRS_STRING}['{CALL_MARK_KEY}']"
    has_call_mark = f"mapContains({cols.ATTRS_STRING}, '{CALL_MARK_KEY}')"
    is_root = "parent_span_id = ''"
    having = "HAVING call_execution_id = %(call_execution_id)s " if by_call else ""
    return (
        "SELECT trace_id, "
        f"countIf({is_root}) > 0 AS has_root, "
        f"argMinIf(name, start_time, {is_root}) AS root_name, "
        f"minIf(start_time, {is_root}) AS root_start_time, "
        f"argMinIf(status, start_time, {is_root}) AS root_status, "
        f"argMinIf(latency_ms, start_time, {is_root}) AS root_latency_ms, "
        f"argMinIf({call_mark}, start_time, {has_call_mark}) AS call_execution_id "
        "FROM spans FINAL "
        "WHERE project_id = %(project_id)s "
        "AND start_time >= %(window_start)s "
        "AND start_time <= %(window_end)s "
        f"AND {job_mark} = %(job_id)s "
        "GROUP BY trace_id "
        f"{having}"
        "ORDER BY has_root ASC, root_start_time DESC, trace_id "
        "LIMIT %(limit)s"
    )


def run_traces_params(
    source: RunTracesSource, call_execution_id: str | None = None
) -> dict[str, Any]:
    job = source.job
    params = {
        "project_id": source.project_id,
        "job_id": str(job.id),
        "window_start": job.created_at,
        "window_end": job.terminal_at or timezone.now(),
        "limit": MAX_TRACES,
    }
    if call_execution_id is not None:
        params["call_execution_id"] = call_execution_id
    return params


def read_run_traces(
    source: RunTracesSource, *, call_execution_id: str | None = None
) -> list[RunTrace]:
    rows, _columns, _elapsed_ms = get_clickhouse_client().execute_read(
        run_traces_sql(by_call=call_execution_id is not None),
        run_traces_params(source, call_execution_id),
        timeout_ms=READ_TIMEOUT_MS,
    )
    return [
        RunTrace(
            trace_id=str(trace_id),
            name=name if has_root else None,
            start_time=start_time if has_root else None,
            status=status if has_root else None,
            latency_ms=latency_ms if has_root else None,
            call_execution_id=call_id or None,
        )
        for trace_id, has_root, name, start_time, status, latency_ms, call_id in rows
    ]
