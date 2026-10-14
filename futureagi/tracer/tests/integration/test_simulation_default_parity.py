"""A project that holds no marked trace reads exactly as it did before the change.

Every reader of the observability surface is run against a project that has
production traces only, and what it sends ClickHouse (statement text and
parameters) and what it answers is compared with a file recorded on the commit
before the change. A reader that adds one character to a statement for a
project that holds nothing fails here.

Recording happens once, on a checkout of the commit before the change
(``ea12cc3f4``). Copy this file into that checkout, then::

    git checkout ea12cc3f4
    SIMULATION_PARITY_RECORD=1 pytest \
        futureagi/tracer/tests/integration/test_simulation_default_parity.py

Recording refuses to run unless ``git rev-parse HEAD`` starts with
``RECORDED_COMMIT``, and unless ``git status`` shows no modified tracked file
other than this file and the ``fixtures/`` folder. While ``RECORDED_COMMIT`` is
``ea12cc3f4`` it also refuses when ``tracer.services.simulation_visibility``
exists, the module the change adds. Recorded on the changed tree, the
comparison would be the build against itself and could never fail.

Every case then writes ``fixtures/simulation_parity/<reader>.<scope>.json`` and
fails with "recorded": a shell that kept the variable set would otherwise
re-record and pass forever, so a recording run is never green. Copy the files
back and run without the variable to compare. The module imports nothing that
does not exist on that commit; the cases that need the holding flag import it
inside the test and do not run when recording.

The recorded files have a shelf life. The seeded day (``PARITY_BASE``) has to
stay inside the table's 90-day retention, so every case fails once the day is
``MAX_BASE_AGE`` (75 days) old, and any later change to what a reader sends
fails its case until it is recorded again. To re-record: set ``PARITY_BASE`` to
a recent day and, to describe the same readers, run the command above again on
``ea12cc3f4``. To describe the readers of a later commit instead, also set
``RECORDED_COMMIT`` to that commit and check it out; the module check is then
skipped, and edits to this file and the ``fixtures/`` folder do not stop the
run. A ClickHouse that is down
fails this module; it never skips it.

Ids are random per run, so every id the world knows is replaced by a name
(``<trace:a:p_mid>``) in statements and bodies before they are written or
compared. Timestamps may move with the clock between the recording and the
comparison; a timestamp is accepted when it is equal or has moved by exactly
the time between the two runs.

The reader catalogue (``READERS``) is also what the cross-reader tests import,
so both modules call every reader the same way.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import re
import subprocess
import threading
import uuid
from collections.abc import Callable, Iterator, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from django.conf import settings

pytestmark = [pytest.mark.integration, pytest.mark.django_db]

RECORD_VARIABLE = "SIMULATION_PARITY_RECORD"
RECORD = os.environ.get(RECORD_VARIABLE) == "1"
FIXTURES = Path(__file__).parent / "fixtures" / "simulation_parity"

# A fixed day, so windows and seeded times are the same on every run. Rows
# older than the table's retention are dropped by ClickHouse, so a recording
# older than this limit must be redone with a newer day.
PARITY_BASE = datetime(2026, 10, 12, 10, 0, tzinfo=UTC)
MAX_BASE_AGE = timedelta(days=75)
# The checkout the files are recorded on: the commit before the change.
RECORDED_COMMIT = "ea12cc3f4"
# How far a timestamp may differ from the recorded one beyond the clock's own
# movement between the two runs.
CLOCK_TOLERANCE = timedelta(minutes=5)

LOW_ATTRIBUTES = {
    "final_status": "settled",
    "tenant": "acme",
    "tier": "gold",
    "region": "eu",
}


def relative_base() -> datetime:
    """An hour-aligned time a day back, for tests that compare no recording."""
    return (datetime.now(UTC) - timedelta(days=1)).replace(
        minute=0, second=0, microsecond=0
    )


def window_filter(base: datetime, before: timedelta = timedelta(hours=1)) -> dict:
    return {
        "column_id": "created_at",
        "filter_config": {
            "col_type": "SYSTEM_METRIC",
            "filter_type": "datetime",
            "filter_op": "between",
            "filter_value": [
                (base - before).isoformat(),
                (base + timedelta(hours=2)).isoformat(),
            ],
        },
    }


# The shapes a list or chart request takes, beyond the date window alone: a
# window long enough to take the finite-scan route, one attribute leaf, one
# status leaf, and the second page of a cursor walk. Each reaches filtered
# branches that a bare window never does.
LONG_WINDOW = timedelta(days=14)
SHAPES_FILTERED = ("long_window", "attribute", "status")
SHAPES_LISTS = (*SHAPES_FILTERED, "page_two")

# Every seeded trace has a span that matches each leaf, so a filtered read
# answers the same traces as the bare window and a neighbour lookup still
# finds its trace.
ATTRIBUTE_LEAF = {
    "column_id": "tier",
    "filter_config": {
        "col_type": "SPAN_ATTRIBUTE",
        "filter_type": "text",
        "filter_op": "equals",
        "filter_value": "gold",
    },
}
STATUS_LEAF = {
    "column_id": "status",
    "filter_config": {
        "col_type": "SYSTEM_METRIC",
        "filter_type": "categorical",
        "filter_op": "equals",
        "filter_value": "OK",
    },
}


def split_scope(scope: str) -> tuple[str, str]:
    """``single.attribute`` is the single-project read with an attribute leaf."""
    base, _, shape = scope.partition(".")
    return base, shape or "time_only"


def request_filters(world, scope: str) -> list[dict]:
    _, shape = split_scope(scope)
    if shape == "long_window":
        return [window_filter(world.base, LONG_WINDOW)]
    if shape == "attribute":
        return [world.window, deepcopy(ATTRIBUTE_LEAF)]
    if shape == "status":
        return [world.window, deepcopy(STATUS_LEAF)]
    return [world.window]


# --- the seeded world --------------------------------------------------------


@dataclass(frozen=True)
class TraceSpec:
    """One trace of a world: a root and two children, or two children alone."""

    label: str
    minute: int
    span_attrs: dict = field(default_factory=dict)
    root_attrs: dict = field(default_factory=dict)
    resource_attrs: dict = field(default_factory=dict)
    rootless: bool = False
    weight: int = 1
    errors: int = 0
    number: str | None = None


def production_specs() -> list[TraceSpec]:
    return [
        TraceSpec("p_old", 10),
        TraceSpec("p_mid", 20, errors=1),
        TraceSpec("p_new", 35),
    ]


def parity_specs(kind: str) -> list[TraceSpec]:
    specs = production_specs()
    if kind == "voice":
        from tracer.services.clickhouse.query_builders.voice_call_list import (
            VAPI_PHONE_NUMBERS,
        )

        # Two calls dialled from a simulator number, which the caller-number
        # rule drops when asked to.
        specs += [
            TraceSpec("sim_a", 12, number=VAPI_PHONE_NUMBERS[0]),
            TraceSpec("sim_b", 14, number=VAPI_PHONE_NUMBERS[0]),
        ]
    return specs


@dataclass
class ProjectWorld:
    name: str
    kind: str
    base: datetime
    project: Any
    specs: dict[str, TraceSpec]
    trace_ids: dict[str, str]
    span_ids: dict[str, str]
    eval_config: Any = None
    annotation_label: Any = None
    version: Any = None

    @property
    def project_id(self) -> str:
        return str(self.project.id)

    @property
    def window(self) -> dict:
        return window_filter(self.base)

    @property
    def label_of_trace(self) -> dict[str, str]:
        return {value: key for key, value in self.trace_ids.items()}

    @property
    def label_of_span(self) -> dict[str, str]:
        return {value: key for key, value in self.span_ids.items()}


_ROLES = ("root", "llm", "tool")


def seed_world(
    env,
    name: str,
    specs: Sequence[TraceSpec],
    *,
    kind: str,
    base: datetime,
    extras: bool = False,
    provider_row: bool = True,
    trace_type: str = "observe",
    versioned: bool = False,
    arrival_from_created_at: bool = False,
) -> ProjectWorld:
    """Create one project and seed its traces into the real tables.

    ``versioned`` gives the project a version that every seeded trace and span
    belongs to, for the readers that are asked about a version, not a project.
    ``arrival_from_created_at`` stamps each span's arrival time with its own
    creation time instead of the insert time, for the readers that look only at
    what arrived inside a fixed past window.
    """
    from model_hub.models.ai_model import AIModel
    from tfc.middleware.workspace_context import set_workspace_context
    from tracer.models.observability_provider import (
        ObservabilityProvider,
        ProviderChoices,
    )
    from tracer.models.project import Project
    from tracer.models.project_version import ProjectVersion
    from tracer.tests._ch_seed import seed_ch_spans, seed_ch_traces

    set_workspace_context(
        workspace=env.workspace, organization=env.organization, user=env.user
    )
    project = Project.no_workspace_objects.create(
        name=f"{name}-{uuid.uuid4().hex[:8]}",
        organization=env.organization,
        workspace=env.workspace,
        model_type=AIModel.ModelTypes.GENERATIVE_LLM,
        trace_type=trace_type,
        metadata={},
    )
    version = (
        ProjectVersion.no_workspace_objects.create(
            project=project, name=f"{name} run", version="v1", metadata={}
        )
        if versioned
        else None
    )
    if provider_row:
        # A row that does not hold: the flag, not the row, decides.
        ObservabilityProvider.no_workspace_objects.create(
            project=project,
            provider=ProviderChoices.VAPI,
            organization=env.organization,
            workspace=env.workspace,
            metadata={},
        )

    root_type = "conversation" if kind == "voice" else "agent"
    rows, trace_rows = [], []
    trace_ids: dict[str, str] = {}
    span_ids: dict[str, str] = {}
    for spec in specs:
        trace_id = str(uuid.uuid4())
        trace_ids[spec.label] = trace_id
        root_id = uuid.uuid4().hex[:16]
        start = base + timedelta(minutes=spec.minute)
        for index, role in enumerate(_ROLES):
            if index == 0 and spec.rootless:
                continue
            span_id = root_id if index == 0 else uuid.uuid4().hex[:16]
            span_ids[f"{spec.label}/{index}"] = span_id
            started = start + timedelta(seconds=10 * index)
            latency = 100 * spec.weight + 10 * index
            attrs = {**LOW_ATTRIBUTES, "score": float(spec.weight), **spec.span_attrs}
            if index == 0:
                attrs.update(spec.root_attrs)
                if kind == "voice":
                    number = spec.number or "+15550000001"
                    attrs["raw_log"] = json.dumps({"customer": {"number": number}})
            rows.append(
                {
                    "id": span_id,
                    "trace_id": trace_id,
                    "project_id": str(project.id),
                    "org_id": str(env.organization.id),
                    "parent_span_id": None if index == 0 else root_id,
                    "name": f"{spec.label}:{role}",
                    "observation_type": root_type if index == 0 else role,
                    "status": "ERROR" if 0 < index <= spec.errors else "OK",
                    "start_time": started,
                    "end_time": started + timedelta(milliseconds=latency),
                    "latency_ms": latency,
                    "model": "gpt-test" if index == 1 else "",
                    "provider": "vapi" if kind == "voice" and index == 0 else "",
                    "prompt_tokens": 4 * spec.weight,
                    "completion_tokens": 6 * spec.weight,
                    "total_tokens": 10 * spec.weight,
                    "cost": 0.01 * spec.weight,
                    "span_attributes": attrs,
                    "resource_attributes": dict(spec.resource_attrs),
                    "created_at": started,
                    "updated_at": started,
                }
            )
            if version is not None:
                rows[-1]["project_version_id"] = str(version.id)
        trace_rows.append(
            {
                "id": trace_id,
                "project_id": str(project.id),
                "project_version_id": str(version.id) if version else None,
                "name": spec.label,
                "session_id": None,
                "external_id": None,
                "tags": "[]",
                "metadata": "{}",
                "input": "",
                "output": "",
                "error": "",
                "error_analysis_status": "PENDING",
                "created_at": start,
                "updated_at": start,
                "is_deleted": 0,
            }
        )
    seed_ch_spans(rows, version_from_created_at=arrival_from_created_at)
    seed_ch_traces(trace_rows)
    # Trace project scope is read through a dictionary that reloads on a timer.
    env.ch.command("SYSTEM RELOAD DICTIONARY trace_dict")

    world = ProjectWorld(
        name=name,
        kind=kind,
        base=base,
        project=project,
        specs={spec.label: spec for spec in specs},
        trace_ids=trace_ids,
        span_ids=span_ids,
        version=version,
    )
    if extras:
        _seed_extras(env, world, rows)
    return world


def _seed_extras(env, world: ProjectWorld, rows: list[dict]) -> None:
    """An eval config with a score on every span and an annotation per trace."""
    from model_hub.models.choices import (
        AnnotationTypeChoices,
        QueueItemSourceType,
        ScoreSource,
    )
    from model_hub.models.develop_annotations import AnnotationsLabels
    from model_hub.models.evals_metric import EvalTemplate
    from model_hub.models.score import Score
    from tracer.models.custom_eval_config import CustomEvalConfig

    project = world.project
    template = EvalTemplate.objects.create(
        name=f"template-{uuid.uuid4().hex[:8]}",
        description="reader test template",
        organization=env.organization,
        workspace=env.workspace,
        config={"type": "pass_fail", "criteria": "x"},
    )
    world.eval_config = CustomEvalConfig.objects.create(
        name=f"eval-{uuid.uuid4().hex[:8]}",
        project=project,
        eval_template=template,
        config={"threshold": 0.5},
        mapping={"input": "input", "output": "output"},
        filters={},
    )
    world.annotation_label = AnnotationsLabels.objects.create(
        name=f"label-{uuid.uuid4().hex[:8]}",
        type=AnnotationTypeChoices.NUMERIC.value,
        organization=env.organization,
        workspace=env.workspace,
        project=project,
        settings={"min": 0, "max": 1, "step_size": 0.1, "display_type": "slider"},
    )

    scored_at = world.base + timedelta(minutes=40)
    stamp = scored_at.strftime("%Y-%m-%d %H:%M:%S")
    label_of_trace = world.label_of_trace
    values = []
    for row in rows:
        weight = world.specs[label_of_trace[row["trace_id"]]].weight
        value = 0.2 if weight == 1 else 0.9
        values.append(
            f"('{uuid.uuid4()}', '{row['id']}', '{row['trace_id']}', "
            f"'{world.eval_config.id}', 'span', {value}, {int(value >= 0.5)}, "
            f"'{stamp}', '{stamp}')"
        )
    database = settings.CLICKHOUSE["CH_DATABASE"]
    for table in ("tracer_eval_logger", "tracer_eval_logger_v2"):
        env.ch.command(
            f"INSERT INTO {database}.{table} (id, observation_span_id, trace_id, "
            "custom_eval_config_id, target_type, output_float, output_bool, "
            f"created_at, updated_at) VALUES {', '.join(values)}"
        )

    def score(weight, **source):
        created = Score.objects.create(
            label=world.annotation_label,
            value={"value": 0.2 if weight == 1 else 0.9},
            organization=env.organization,
            workspace=env.workspace,
            score_source=ScoreSource.HUMAN.value,
            tracer_project_id=project.id,
            **source,
        )
        # created_at is set on insert; the chart windows need it near the spans.
        Score.no_workspace_objects.filter(id=created.id).update(created_at=scored_at)

    for label, trace_id in world.trace_ids.items():
        weight = world.specs[label].weight
        score(
            source_type=QueueItemSourceType.TRACE.value,
            trace_id=trace_id,
            weight=weight,
        )
    for row in rows:
        score(
            source_type=QueueItemSourceType.OBSERVATION_SPAN.value,
            observation_span_id=row["id"],
            weight=world.specs[label_of_trace[row["trace_id"]]].weight,
        )


# --- the environment ---------------------------------------------------------


@dataclass
class Env:
    client: Any
    organization: Any
    workspace: Any
    user: Any
    ch: Any
    monkeypatch: pytest.MonkeyPatch
    recorder: Any = None
    refreshes: list = field(default_factory=list)
    extra_symbols: dict = field(default_factory=dict)
    worker_enabled: bool = False

    def analytics(self):
        from tracer.services.clickhouse.v2.query_service import (
            V2AnalyticsQueryService,
        )

        return V2AnalyticsQueryService()

    def enable_background_worker(self) -> None:
        """Run every refresh a reader schedules at once, on the real worker code."""
        if self.worker_enabled:
            return
        from tracer.services import exact_aggregation_cache
        from tracer.tasks import exact_aggregation

        self.monkeypatch.setattr(
            exact_aggregation_cache,
            "_configured_exact_aggregation_task_queue",
            lambda: "exact_aggregation",
        )
        self.monkeypatch.setattr(
            exact_aggregation,
            "_exact_observe_analytics",
            lambda: contextlib.nullcontext(self.analytics()),
        )

        def apply_async(**call):
            kwargs = call["kwargs"]
            self.refreshes.append((kwargs["namespace"], kwargs["identity"]))
            exact_aggregation.refresh_exact_aggregation_snapshot._original_func(
                **kwargs
            )
            return SimpleNamespace(id=f"refresh-{len(self.refreshes)}")

        self.monkeypatch.setattr(
            exact_aggregation.refresh_exact_aggregation_snapshot,
            "apply_async",
            apply_async,
        )
        self.worker_enabled = True


def make_scope():
    """A private organisation, so an organisation-wide read sees only its projects."""
    from accounts.models.organization import Organization
    from accounts.models.organization_membership import OrganizationMembership
    from accounts.models.user import User
    from accounts.models.workspace import Workspace, WorkspaceMembership
    from conftest import WorkspaceAwareAPIClient
    from tfc.constants.levels import Level
    from tfc.constants.roles import OrganizationRoles
    from tfc.middleware.workspace_context import (
        clear_workspace_context,
        set_workspace_context,
    )

    clear_workspace_context()
    organization = Organization.objects.create(
        name=f"reader-org-{uuid.uuid4().hex[:8]}"
    )
    set_workspace_context(organization=organization)
    user = User.objects.create_user(
        email=f"reader-{uuid.uuid4().hex[:8]}@futureagi.com",
        password="testpassword123",
        name="Reader Test User",
        organization=organization,
        organization_role=OrganizationRoles.OWNER,
    )
    membership, _ = OrganizationMembership.no_workspace_objects.get_or_create(
        user=user,
        organization=organization,
        defaults={
            "role": OrganizationRoles.OWNER,
            "level": Level.OWNER,
            "is_active": True,
        },
    )
    workspace = Workspace.objects.create(
        name="Reader Test Workspace",
        organization=organization,
        is_default=True,
        is_active=True,
        created_by=user,
    )
    WorkspaceMembership.no_workspace_objects.get_or_create(
        user=user,
        workspace=workspace,
        defaults={
            "role": "Workspace Owner",
            "level": Level.OWNER,
            "is_active": True,
            "organization_membership": membership,
        },
    )
    set_workspace_context(workspace=workspace, organization=organization, user=user)
    client = WorkspaceAwareAPIClient()
    client.force_authenticate(user=user)
    client.set_workspace(workspace)
    return SimpleNamespace(
        organization=organization, user=user, workspace=workspace, client=client
    )


@contextlib.contextmanager
def env_factory_context(ch_schema, monkeypatch):
    """Hand out environments, each in an organisation of its own."""
    from django.core.cache import cache

    cache.clear()
    made = []

    def make() -> Env:
        scope = make_scope()
        made.append(scope)
        return Env(
            client=scope.client,
            organization=scope.organization,
            workspace=scope.workspace,
            user=scope.user,
            ch=ch_schema,
            monkeypatch=monkeypatch,
        )

    try:
        yield make
    finally:
        for scope in made:
            scope.client.stop_workspace_injection()
        cache.clear()


@pytest.fixture
def env_factory(ch_schema, ch_routes_on, monkeypatch):
    with env_factory_context(ch_schema, monkeypatch) as make:
        yield make


@pytest.fixture
def env(env_factory) -> Env:
    return env_factory()


# --- recording what ClickHouse is sent -----------------------------------------

_READ_STATEMENT = re.compile(
    r"\s*(?:--[^\n]*\n\s*)*(?:SELECT|WITH|EXPLAIN)\b", re.IGNORECASE
)


def _encode(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=str)
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


class StatementRecorder:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.statements: list[dict[str, Any]] = []

    def add(self, sql: Any, params: Any) -> None:
        text = sql if isinstance(sql, str) else str(sql)
        if not _READ_STATEMENT.match(text):
            return
        encoded = json.dumps(params or {}, default=_encode, sort_keys=True)
        with self._lock:
            self.statements.append({"sql": text, "params": json.loads(encoded)})


class _RecordingHttpClient:
    """Wraps the span reader's HTTP client; it is built by the reader itself."""

    def __init__(self, inner: Any, recorder: StatementRecorder) -> None:
        self._inner = inner
        self._recorder = recorder

    def query(self, sql, parameters=None, *args, **kwargs):
        self._recorder.add(sql, parameters)
        return self._inner.query(sql, parameters, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


@contextlib.contextmanager
def recording(env: Env) -> Iterator[StatementRecorder]:
    """Record every read the native driver is asked to run, whoever asks."""
    import clickhouse_driver

    driver = clickhouse_driver.Client
    original_execute, original_iter = driver.execute, driver.execute_iter
    recorder = StatementRecorder()

    def execute(self, query, params=None, *args, **kwargs):
        recorder.add(query, params)
        return original_execute(self, query, params, *args, **kwargs)

    def execute_iter(self, query, params=None, *args, **kwargs):
        recorder.add(query, params)
        return original_iter(self, query, params, *args, **kwargs)

    driver.execute, driver.execute_iter = execute, execute_iter
    env.recorder = recorder
    try:
        yield recorder
    finally:
        driver.execute, driver.execute_iter = original_execute, original_iter
        env.recorder = None


# --- calling readers -----------------------------------------------------------


def _check(response, what: str):
    body = getattr(response, "content", b"")
    assert response.status_code == 200, f"{what}: {response.status_code} {body[:600]!r}"
    return response


def _json(response, what: str):
    body = _check(response, what).json()
    return body.get("result", body) if isinstance(body, dict) else body


def _filters_param(world, scope: str = "single") -> str:
    return json.dumps(request_filters(world, scope))


def _continuation(first: dict, what: str) -> str:
    """The signed cursor a first page names for its second."""
    cursor = first.get("next_cursor") or (first.get("metadata") or {}).get(
        "next_cursor"
    )
    assert cursor, f"{what}: the first page named no continuation"
    return cursor


def _label_maps(worlds):
    traces, spans = {}, {}
    for index, world in enumerate(worlds):
        traces.update(
            {tid: f"{index}/{lab}" for tid, lab in world.label_of_trace.items()}
        )
        spans.update(
            {sid: f"{index}/{lab}" for sid, lab in world.label_of_span.items()}
        )
    return traces, spans


def _trace_labels(ids, worlds) -> frozenset[str]:
    traces, _ = _label_maps(worlds)
    return frozenset(traces.get(str(i), f"unknown:{i}") for i in ids)


def _span_labels(ids, worlds) -> frozenset[str]:
    _, spans = _label_maps(worlds)
    return frozenset(spans.get(str(i), f"unknown:{i}") for i in ids)


def _names_in_text(text: str, mapping: dict[str, str]) -> frozenset[str]:
    return frozenset(label for key, label in mapping.items() if key in text)


@dataclass(frozen=True)
class Reader:
    """One row of the contract's reader tables, and how to call and read it."""

    name: str
    kind: str
    grain: str
    call: Callable[[Env, list[ProjectWorld], str], Any]
    observe: Callable[[Any, list[ProjectWorld]], tuple[Any, frozenset[str] | None]]
    scopes: tuple[str, ...] = ("single",)
    rule_scopes: tuple[str, ...] = ()
    extras: bool = False
    unordered: bool = False
    control: bool = True
    # Request shapes beyond the date window alone, run as ``single.<shape>``.
    shapes: tuple[str, ...] = ()
    # Options of the seeded project, for readers that ask about a version.
    world_options: dict = field(default_factory=dict)


def _point(item: dict, field_name: str = "value") -> tuple:
    return (item.get("timestamp"), item.get(field_name), item.get("primary_traffic"))


def _series_value(payload) -> tuple:
    if isinstance(payload, list):
        return tuple(_series_value(item) for item in payload)
    return tuple(_point(item) for item in payload.get("data", []))


def _bundle_value(payload: dict) -> tuple:
    out = []
    for key in ("latency", "tokens", "cost", "traffic"):
        if key in payload:
            field_name = "traffic" if key == "traffic" else "value"
            out.append((key, tuple(_point(i, field_name) for i in payload[key])))
    return tuple(out)


def _series_observation(payload, worlds):
    return _series_value(payload), None


def _bundle_observation(payload, worlds):
    payload = payload.get("system_metrics", payload)
    return _bundle_value(payload), None


# ids and text readers


def _call_voice_list(env, worlds, scope):
    base, shape = split_scope(scope)
    world = worlds[0]
    url = "/tracer/trace/list_voice_calls/"
    body = {
        "project_id": world.project_id,
        "page_size": 1 if shape == "page_two" else 25,
        "cursor_mode": True,
        "filters": request_filters(world, scope),
    }
    if base in ("field_off", "field_on"):
        body["remove_simulation_calls"] = base == "field_on"
    first = _json(env.client.post(url, body, format="json"), "voice list")
    if shape != "page_two":
        return first
    body.pop("cursor_mode")
    body["cursor"] = _continuation(first, "voice list")
    return _json(env.client.post(url, body, format="json"), "voice list page two")


def _observe_voice_list(payload, worlds):
    labels = _trace_labels([row["trace_id"] for row in payload["results"]], worlds)
    # The count and the "more pages" flag are part of the answer: a marked call
    # left in "N calls" is a leak the rows alone do not show.
    value = (tuple(sorted(labels)), payload.get("count"), payload.get("has_more"))
    return value, labels


def _call_export(env, worlds, scope):
    world = worlds[0]
    response = env.client.get(
        "/tracer/trace/get_trace_export_data/",
        {"project_id": world.project_id, "filters": _filters_param(world, scope)},
    )
    return _check(response, "trace export").content.decode()


def _observe_trace_text(payload, worlds):
    traces, _ = _label_maps(worlds)
    labels = _names_in_text(payload, traces)
    return tuple(sorted(labels)), labels


def _chart_body(
    world, metric="latency", metric_type="SYSTEM_METRIC", *, filters=None, **extra
):
    return {
        "project_id": world.project_id,
        "interval": "hour",
        "property": "average",
        "req_data_config": {"id": metric, "type": metric_type},
        "filters": [world.window] if filters is None else filters,
        **extra,
    }


def _call_chart(url, metric="latency", **extra):
    def call(env, worlds, scope):
        base, _ = split_scope(scope)
        body = _chart_body(
            worlds[0], metric, filters=request_filters(worlds[0], scope), **extra
        )
        if base in ("field_off", "field_on"):
            body["remove_simulation_calls"] = base == "field_on"
        return _json(env.client.post(url, body, format="json"), f"chart {url}")

    return call


def _call_list(url, what):
    """A numbered list, or, for the page-two shape, the page a cursor names."""

    def call(env, worlds, scope):
        base, shape = split_scope(scope)
        params = {
            "page_number": 0,
            "page_size": 1 if shape == "page_two" else 100,
            "filters": _filters_param(worlds[0], scope),
        }
        if base == "single":
            params["project_id"] = worlds[0].project_id
        if shape == "page_two":
            params["cursor_mode"] = True
        first = _json(env.client.get(url, params), what)
        if shape != "page_two":
            return first
        # A cursor and a page number together are refused.
        del params["cursor_mode"], params["page_number"]
        params["cursor"] = _continuation(first, what)
        return _json(env.client.get(url, params), f"{what} page two")

    return call


def _list_meta(payload) -> tuple:
    meta = payload.get("metadata") or {}
    return meta.get("total_rows"), meta.get("has_more")


_call_trace_list = _call_list("/tracer/trace/list_traces_of_session/", "trace list")
_call_span_list = _call_list(
    "/tracer/observation-span/list_spans_observe/", "span list"
)


def _call_trace_list_by_version(env, worlds, scope):
    world = worlds[0]
    params = {
        "project_version_id": str(world.version.id),
        "page_number": 0,
        "page_size": 100,
        "filters": _filters_param(world, scope),
    }
    return _json(
        env.client.get("/tracer/trace/list_traces/", params), "trace list by version"
    )


def _call_span_list_by_version(env, worlds, scope):
    world = worlds[0]
    params = {
        "project_version_id": str(world.version.id),
        "page_number": 0,
        "page_size": 100,
        "filters": _filters_param(world, scope),
    }
    return _json(
        env.client.get("/tracer/observation-span/list_spans/", params),
        "span list by version",
    )


def _observe_trace_list(payload, worlds):
    labels = _trace_labels([row["trace_id"] for row in payload["table"]], worlds)
    return (tuple(sorted(labels)), *_list_meta(payload)), labels


def _observe_span_list(payload, worlds):
    labels = _span_labels([row["span_id"] for row in payload["table"]], worlds)
    return (tuple(sorted(labels)), *_list_meta(payload)), labels


def _call_span_export(env, worlds, scope):
    world = worlds[0]
    response = env.client.get(
        "/tracer/observation-span/get_spans_export_data/",
        {"project_id": world.project_id, "filters": _filters_param(world, scope)},
    )
    return _check(response, "span export").content.decode()


def _observe_span_text(payload, worlds):
    _, spans = _label_maps(worlds)
    labels = _names_in_text(payload, spans)
    return tuple(sorted(labels)), labels


def _call_trace_neighbours(env, worlds, scope):
    world = worlds[0]
    return _json(
        env.client.get(
            "/tracer/trace/get_trace_id_by_index_observe/",
            {
                "project_id": world.project_id,
                "trace_id": world.trace_ids["p_mid"],
                "filters": _filters_param(world, scope),
            },
        ),
        "trace neighbours",
    )


def _observe_neighbours(payload, worlds):
    previous, following = (
        payload.get("previous_trace_id"),
        payload.get("next_trace_id"),
    )
    traces, _ = _label_maps(worlds)
    labels = frozenset(
        traces.get(str(v), f"unknown:{v}") for v in (previous, following) if v
    )
    return (traces.get(str(previous)), traces.get(str(following))), labels


def _call_span_neighbours(env, worlds, scope):
    world = worlds[0]
    return _json(
        env.client.get(
            "/tracer/observation-span/get_trace_id_by_index_spans_as_observe/",
            {
                "project_id": world.project_id,
                "span_id": world.span_ids["p_mid/2"],
                "filters": _filters_param(world, scope),
            },
        ),
        "span neighbours",
    )


def _call_agent_graph(env, worlds, scope):
    env.enable_background_worker()
    world = worlds[0]
    params = {"project_id": world.project_id, "filters": _filters_param(world, scope)}
    payload = _json(env.client.get("/tracer/trace/agent_graph/", params), "agent graph")
    if payload.get("query_status") != "complete":
        payload = _json(
            env.client.get("/tracer/trace/agent_graph/", params), "agent graph again"
        )
    assert payload.get("query_status") == "complete", payload
    return payload


def _observe_agent_graph(payload, worlds):
    value = tuple(
        sorted(
            (node["name"], node["span_count"], node["total_tokens"])
            for node in payload["nodes"]
        )
    )
    labels = frozenset(f"0/{name.split(':', 1)[0]}" for name, _, _ in value)
    return value, labels


def _call_project_chart(env, worlds, scope):
    world = worlds[0]
    return _json(
        env.client.get(
            "/tracer/project/get_graph_data/",
            {
                "project_id": world.project_id,
                "interval": "hour",
                "filters": _filters_param(world, scope),
            },
        ),
        "project chart",
    )


def _call_charts(config_for):
    def call(env, worlds, scope):
        world = worlds[0]
        config = config_for(world)
        if config["type"] == "EVAL":
            env.enable_background_worker()
        response = env.client.get(
            "/tracer/charts/fetch_graph/",
            {
                "project_id": world.project_id,
                "interval": "hour",
                "property": "average",
                "req_data_config": json.dumps(config),
                "filters": _filters_param(world, scope),
            },
        )
        return _json(response, "charts view")

    return call


# eval tasks, queue selection, monitors, the span reader, dashboards


def _call_eval_task(row_type_name, *, unlimited=False, continuous=False):
    """Resolve a task's rows.

    ``unlimited`` leaves the row limit unset, which resolves through the sampled
    query instead of the bounded reader; ``continuous`` resolves the arrival
    delta between the task's start and a fixed ceiling.
    """

    def call(env, worlds, scope):
        from tracer.models.eval_task import EvalTask, EvalTaskStatus, RowType, RunType
        from tracer.selectors.eval_tasks.row_resolver import resolve_desired_rows

        world = worlds[0]
        start = (world.base - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        end = (world.base + timedelta(hours=2)).isoformat().replace("+00:00", "Z")
        task = EvalTask.objects.create(
            project=world.project,
            name="resolver-task",
            filters={"date_range": [start, end]},
            sampling_rate=100.0,
            spans_limit=None if unlimited else 1_000_000,
            run_type=RunType.CONTINUOUS if continuous else RunType.HISTORICAL,
            start_time=world.base - timedelta(hours=1) if continuous else None,
            status=EvalTaskStatus.PENDING,
            row_type=getattr(RowType, row_type_name),
        )
        env.extra_symbols[str(task.id)] = "<task>"
        ceiling = world.base + timedelta(hours=2) if continuous else None
        ids = [str(i) for i in resolve_desired_rows(task, ceiling=ceiling).matched_ids]
        # No arrival in the window ends the resolver before it builds the
        # query that judges the rows, which would leave that query untested.
        assert ids or not continuous, "the continuous task found no arrival"
        return ids

    return call


def _observe_trace_ids(payload, worlds):
    labels = _trace_labels(payload, worlds)
    return tuple(sorted(labels)), labels


def _observe_voice_task_ids(payload, worlds):
    # A voice task names a call by its conversation root span, not its trace.
    labels = frozenset(
        label.rsplit("/", 1)[0] for label in _span_labels(payload, worlds)
    )
    return tuple(sorted(labels)), labels


def _observe_span_ids(payload, worlds):
    labels = _span_labels(payload, worlds)
    return tuple(sorted(labels)), labels


def _call_queue(kind):
    def call(env, worlds, scope):
        from model_hub.services import bulk_selection

        world = worlds[0]
        common = {
            "project_id": world.project.id,
            "filters": request_filters(world, scope),
            "organization": env.organization,
            "workspace": env.workspace,
        }
        if kind == "spans":
            result = bulk_selection.resolve_filtered_span_ids(**common)
        else:
            extra = {}
            if kind == "voice":
                extra = {
                    "is_voice_call": True,
                    "remove_simulation_calls": split_scope(scope)[0] == "field_on",
                }
            result = bulk_selection.resolve_filtered_trace_ids(**common, **extra)
        return [str(i) for i in result.ids]

    return call


MONITOR_METRICS = ("count_of_errors", "token_usage", "span_response_time")


def _call_monitor(env, worlds, scope):
    from tracer.services.clickhouse.v2.query_builders.monitor_metrics import (
        MonitorMetricsQueryBuilderV2,
    )

    world = worlds[0]
    if split_scope(scope)[0] == "single":
        builder = MonitorMetricsQueryBuilderV2(project_id=world.project_id)
    else:
        builder = MonitorMetricsQueryBuilderV2(
            project_id=None, project_ids=[w.project_id for w in worlds]
        )
    start, end = world.base, world.base + timedelta(hours=1)
    out = {}
    for metric in MONITOR_METRICS:
        sql, params = builder.build_metric_value_query(metric, start, end)
        out[metric] = env.analytics().execute_ch_query(sql, params).data
    return out


def _observe_monitor(payload, worlds):
    return payload, None


def _span_reader_call(function):
    def call(env, worlds, scope):
        from tracer.services.clickhouse.v2 import get_reader

        world = worlds[0]
        with get_reader() as reader:
            if env.recorder is not None:
                reader._client = _RecordingHttpClient(reader._client, env.recorder)
            return function(reader, world)

    return call


def _window(world):
    return world.base, world.base + timedelta(hours=1)


def _observe_count(payload, worlds):
    return payload, None


def _observe_group_by_name(payload, worlds):
    value = tuple(sorted((row["name"], row["usage_count"]) for row in payload))
    labels = frozenset(f"0/{name.split(':', 1)[0]}" for name, _ in value)
    return value, labels


DASHBOARD_LATENCY = {
    "id": "latency",
    "name": "latency",
    "type": "system_metric",
    "source": "traces",
    "aggregation": "avg",
}
DASHBOARD_SCORE = {
    "id": "score",
    "name": "score",
    "type": "custom_attribute",
    "source": "traces",
    "aggregation": "avg",
    "attribute_key": "score",
    "attribute_type": "number",
}
TENANT_FILTER = {
    "column_id": "tenant",
    "filter_config": {
        "col_type": "SPAN_ATTRIBUTE",
        "filter_type": "text",
        "filter_op": "in",
        "filter_value": ["acme", "globex"],
        "attribute_value_types": ["string", "string"],
    },
}
STATUS_FILTER = {
    "column_id": "final_status",
    "filter_config": {
        "col_type": "SPAN_ATTRIBUTE",
        "filter_type": "text",
        "filter_op": "equals",
        "filter_value": "settled",
    },
}
REGION_BREAKDOWN = {
    "name": "region",
    "type": "custom_attribute",
    "source": "traces",
    "attribute_type": "string",
}


def dashboard_config(worlds, metric, **extra) -> dict:
    world = worlds[0]
    return {
        "project_ids": [w.project_id for w in worlds],
        "granularity": "day",
        "time_range": {
            "custom_start": (world.base - timedelta(hours=1)).isoformat(),
            "custom_end": (world.base + timedelta(hours=2)).isoformat(),
        },
        "metrics": [metric],
        "filters": [],
        "breakdowns": [],
        **extra,
    }


def _call_dashboard(metric, **config):
    def call(env, worlds, scope):
        payload = _json(
            env.client.post(
                "/tracer/dashboard/query/",
                dashboard_config(
                    worlds if scope == "several" else worlds[:1],
                    metric,
                    **deepcopy(config),
                ),
                format="json",
            ),
            "dashboard query",
        )
        assert payload.get("query_status", "complete") == "complete", payload
        return payload

    return call


def _observe_dashboard(payload, worlds):
    value = tuple(
        (
            metric["id"],
            tuple(
                (
                    series["name"],
                    tuple(point["value"] for point in series["data"]),
                )
                for series in metric["series"]
            ),
        )
        for metric in payload["metrics"]
    )
    return value, None


DASHBOARD_TOKENS = {
    "id": "tokens",
    "name": "tokens",
    "type": "system_metric",
    "source": "traces",
    "aggregation": "sum",
    "filters": [],
}


def _call_fast_path(env, worlds, scope):
    from tracer.views.dashboard import _read_dashboard_rollup_fast_path

    scoped = worlds if scope == "several" else worlds[:1]
    return _read_dashboard_rollup_fast_path(
        dashboard_config(scoped, deepcopy(DASHBOARD_TOKENS))
    )


def _observe_fast_path(payload, worlds):
    return json.dumps(payload, sort_keys=True, default=_encode), None


def _eval_config(world):
    return {"id": str(world.eval_config.id), "type": "EVAL"}


def _annotation_config(world):
    return {"id": str(world.annotation_label.id), "type": "ANNOTATION"}


def _chart_reader(name, url, grain, config_for):
    """A trace or span chart of an eval or an annotation label."""

    def call(env, worlds, scope):
        world = worlds[0]
        config = config_for(world)
        body = _chart_body(world, config["id"], config["type"])
        return _json(env.client.post(url, body, format="json"), name)

    return Reader(
        name=name,
        kind="chain",
        grain=grain,
        call=call,
        observe=_series_observation,
        extras=True,
        control=False,
    )


TRACE_VIEW = "/tracer/trace/get_graph_methods/"
SPAN_VIEW = "/tracer/observation-span/get_graph_methods/"

_READERS = [
    Reader(
        "voice_list",
        "voice",
        "trace",
        _call_voice_list,
        _observe_voice_list,
        rule_scopes=("field_off", "field_on"),
        shapes=SHAPES_LISTS,
    ),
    Reader(
        "voice_csv",
        "voice",
        "trace",
        _call_export,
        _observe_trace_text,
        shapes=SHAPES_FILTERED,
    ),
    Reader(
        "voice_chart",
        "voice",
        "trace",
        _call_chart(TRACE_VIEW, observe_type="voice"),
        _series_observation,
        rule_scopes=("field_off", "field_on"),
        control=False,
        shapes=SHAPES_FILTERED,
    ),
    Reader(
        "trace_chart",
        "chain",
        "trace",
        _call_chart(TRACE_VIEW),
        _series_observation,
        control=True,
        shapes=SHAPES_FILTERED,
    ),
    Reader(
        "trace_chart_tokens",
        "chain",
        "trace",
        _call_chart(TRACE_VIEW, "tokens"),
        _series_observation,
        control=False,
        shapes=SHAPES_FILTERED,
    ),
    Reader(
        "trace_list",
        "chain",
        "trace",
        _call_trace_list,
        _observe_trace_list,
        scopes=("single", "several"),
        shapes=SHAPES_LISTS,
    ),
    Reader(
        "trace_list_by_version",
        "chain",
        "trace",
        _call_trace_list_by_version,
        _observe_trace_list,
        world_options={"versioned": True},
    ),
    Reader(
        "trace_csv",
        "chain",
        "trace",
        _call_export,
        _observe_trace_text,
        shapes=SHAPES_FILTERED,
    ),
    Reader(
        "trace_neighbours",
        "chain",
        "trace",
        _call_trace_neighbours,
        _observe_neighbours,
        shapes=SHAPES_FILTERED,
    ),
    Reader(
        "agent_graph",
        "chain",
        "trace",
        _call_agent_graph,
        _observe_agent_graph,
        control=False,
        shapes=SHAPES_FILTERED,
    ),
    Reader(
        "span_list",
        "chain",
        "span",
        _call_span_list,
        _observe_span_list,
        scopes=("single", "several"),
        shapes=SHAPES_LISTS,
    ),
    Reader(
        "span_list_non_observe",
        "chain",
        "span",
        _call_span_list_by_version,
        _observe_span_list,
        world_options={"trace_type": "experiment", "versioned": True},
    ),
    Reader(
        "span_csv",
        "chain",
        "span",
        _call_span_export,
        _observe_span_text,
        shapes=SHAPES_FILTERED,
    ),
    Reader(
        "span_chart",
        "chain",
        "span",
        _call_chart(SPAN_VIEW),
        _series_observation,
        control=True,
        shapes=SHAPES_FILTERED,
    ),
    Reader(
        "span_neighbours",
        "chain",
        "span",
        _call_span_neighbours,
        _observe_neighbours,
        shapes=SHAPES_FILTERED,
    ),
    Reader(
        "project_chart",
        "chain",
        "span",
        _call_project_chart,
        _bundle_observation,
        control=False,
        shapes=SHAPES_FILTERED,
    ),
    Reader(
        "charts_all",
        "chain",
        "span",
        _call_charts(lambda w: {"id": "all", "type": "SYSTEM_METRICS"}),
        _bundle_observation,
        control=False,
        shapes=SHAPES_FILTERED,
    ),
    Reader(
        "charts_single",
        "chain",
        "span",
        _call_charts(lambda w: {"id": "latency", "type": "SYSTEM_METRIC"}),
        _series_observation,
        control=True,
        shapes=SHAPES_FILTERED,
    ),
    Reader(
        "charts_eval",
        "chain",
        "trace",
        _call_charts(_eval_config),
        _series_observation,
        extras=True,
        control=False,
    ),
    _chart_reader("eval_chart_trace", TRACE_VIEW, "trace", _eval_config),
    _chart_reader("eval_chart_span", SPAN_VIEW, "span", _eval_config),
    _chart_reader("annotation_chart_trace", TRACE_VIEW, "trace", _annotation_config),
    _chart_reader("annotation_chart_span", SPAN_VIEW, "span", _annotation_config),
    *(
        Reader(
            f"eval_task_{label}{suffix}",
            kind,
            grain,
            _call_eval_task(row_type, **options),
            _observe_voice_task_ids
            if row_type == "VOICE_CALLS"
            else _observe_trace_ids
            if grain == "trace"
            else _observe_span_ids,
            unordered=True,
            world_options=world_options,
        )
        for label, row_type, kind, grain in (
            ("spans", "SPANS", "chain", "span"),
            ("traces", "TRACES", "chain", "trace"),
            ("voice", "VOICE_CALLS", "voice", "trace"),
        )
        for suffix, options, world_options in (
            ("", {}, {}),
            ("_unlimited", {"unlimited": True}, {}),
            # The arrival window is a fixed day in the past: a span is inside
            # it only when its arrival time is its creation time.
            (
                "_continuous",
                {"continuous": True},
                {"arrival_from_created_at": True},
            ),
        )
    ),
    Reader(
        "queue_traces",
        "chain",
        "trace",
        _call_queue("traces"),
        _observe_trace_ids,
        unordered=True,
        shapes=SHAPES_FILTERED,
    ),
    Reader(
        "queue_spans",
        "chain",
        "span",
        _call_queue("spans"),
        _observe_span_ids,
        unordered=True,
        shapes=SHAPES_FILTERED,
    ),
    Reader(
        "queue_voice",
        "voice",
        "trace",
        _call_queue("voice"),
        _observe_trace_ids,
        unordered=True,
        rule_scopes=("field_off", "field_on"),
        shapes=SHAPES_FILTERED,
    ),
    Reader(
        "dashboard_latency",
        "chain",
        "span",
        _call_dashboard(DASHBOARD_LATENCY),
        _observe_dashboard,
        scopes=("single", "several"),
        control=False,
    ),
    Reader(
        "dashboard_score",
        "chain",
        "span",
        _call_dashboard(DASHBOARD_SCORE),
        _observe_dashboard,
        scopes=("single", "several"),
        control=False,
    ),
    # A filter and a breakdown on the attribute statement, and an attribute
    # filter on the latency statement, which replays the latest state of each
    # span.
    Reader(
        "dashboard_filtered_breakdown",
        "chain",
        "span",
        _call_dashboard(
            DASHBOARD_SCORE, filters=[TENANT_FILTER], breakdowns=[REGION_BREAKDOWN]
        ),
        _observe_dashboard,
        control=False,
    ),
    Reader(
        "dashboard_replay",
        "chain",
        "span",
        _call_dashboard(DASHBOARD_LATENCY, filters=[STATUS_FILTER]),
        _observe_dashboard,
        control=False,
    ),
    Reader(
        "dashboard_fast_path",
        "chain",
        "span",
        _call_fast_path,
        _observe_fast_path,
        scopes=("single", "several"),
        control=False,
    ),
    Reader(
        "monitors",
        "chain",
        "span",
        _call_monitor,
        _observe_monitor,
        scopes=("single", "several"),
        control=False,
    ),
    Reader(
        "reader_root_ids",
        "chain",
        "trace",
        _span_reader_call(lambda r, w: r.root_ids_by_project(w.project_id)),
        _observe_span_ids,
        unordered=True,
    ),
    Reader(
        "reader_ids",
        "chain",
        "span",
        _span_reader_call(lambda r, w: r.ids_by_project(w.project_id)),
        _observe_span_ids,
    ),
    Reader(
        "reader_recent_roots",
        "chain",
        "trace",
        _span_reader_call(
            lambda r, w: r.recent_root_trace_ids_by_project(w.project_id, limit=50)
        ),
        _observe_trace_ids,
    ),
    Reader(
        "reader_count",
        "chain",
        "span",
        _span_reader_call(
            lambda r, w: r.count_with_filters(
                project_id=w.project_id, start_time_range=_window(w)
            )
        ),
        _observe_count,
        control=False,
    ),
    Reader(
        "reader_root_count",
        "chain",
        "trace",
        _span_reader_call(
            lambda r, w: r.count_with_filters(
                project_id=w.project_id,
                start_time_range=_window(w),
                roots_only=True,
            )
        ),
        _observe_count,
        control=False,
    ),
    Reader(
        "reader_group_by_name",
        "chain",
        "span",
        _span_reader_call(
            lambda r, w: r.per_project_group_by_name(
                w.project_id, since=w.base, until=w.base + timedelta(hours=1)
            )
        ),
        _observe_group_by_name,
        unordered=True,
    ),
]
READERS: dict[str, Reader] = {reader.name: reader for reader in _READERS}


def make_worlds(env, reader: Reader, names=("a", "b"), *, base=None):
    base = base or PARITY_BASE
    return [
        seed_world(
            env,
            name,
            parity_specs(reader.kind),
            kind=reader.kind,
            # A minute apart, so two projects never tie on time and an
            # organisation-wide page has the same order on every run.
            base=base + timedelta(minutes=index),
            extras=reader.extras,
            provider_row=(name == "a"),
            **reader.world_options,
        )
        for index, name in enumerate(names)
    ]


# --- canonical form and comparison ---------------------------------------------

_VOLATILE_KEY = re.compile(
    r"(elapsed|duration|took|query_time|request_id|cursor)", re.IGNORECASE
)
# What a statement's parameters may drop: a cursor's values are part of the
# statement a second page sends, so they are compared.
_VOLATILE_PARAM_KEY = re.compile(
    r"(elapsed|duration|took|query_time|request_id)", re.IGNORECASE
)
_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
)


def _symbols(env, worlds) -> dict[str, str]:
    table = {
        str(env.organization.id): "<organization>",
        str(env.workspace.id): "<workspace>",
        str(env.user.id): "<user>",
        **env.extra_symbols,
    }
    for world in worlds:
        table[world.project_id] = f"<project:{world.name}>"
        for label, value in world.trace_ids.items():
            table[value] = f"<trace:{world.name}:{label}>"
        for label, value in world.span_ids.items():
            table[value] = f"<span:{world.name}:{label}>"
        if world.version is not None:
            table[str(world.version.id)] = f"<version:{world.name}>"
        if world.eval_config is not None:
            table[str(world.eval_config.id)] = f"<eval-config:{world.name}>"
        if world.annotation_label is not None:
            table[str(world.annotation_label.id)] = f"<label:{world.name}>"
    return dict(sorted(table.items(), key=lambda item: -len(item[0])))


def _canonical(
    value: Any,
    table: dict[str, str],
    *,
    unordered: bool = False,
    sort_ids: bool = False,
    volatile: re.Pattern = _VOLATILE_KEY,
) -> Any:
    """Replace ids by names and sort what the reader leaves unordered.

    ``sort_ids`` sorts any list of trace or span ids, and any list of rows that
    start with one (the continuous classifier binds ``(trace, span, start)``
    triples): a statement's parameters list ids in the order the store returned
    them, which follows random ids when several rows tie.
    """

    def again(item):
        return _canonical(
            item, table, unordered=unordered, sort_ids=sort_ids, volatile=volatile
        )

    if isinstance(value, str):
        for actual, token in table.items():
            value = value.replace(actual, token)
        return value
    if isinstance(value, dict):
        return {
            key: again(item)
            for key, item in value.items()
            if not volatile.search(str(key))
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [again(item) for item in value]
        symbolic = items and all(
            isinstance(i, str) and i.startswith("<") for i in items
        )
        projects = items and all(
            isinstance(i, str) and i.startswith("<project:") for i in items
        )
        ids = items and all(
            isinstance(i, str) and i.startswith(("<trace:", "<span:")) for i in items
        )
        rows = items and all(
            isinstance(i, (list, tuple))
            and i
            and isinstance(i[0], str)
            and i[0].startswith(("<trace:", "<span:"))
            for i in items
        )
        if projects or (sort_ids and (ids or rows)) or (unordered and symbolic):
            items.sort(key=_dump)
        return items
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return again(json.loads(json.dumps(value, default=_encode)))


def _dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=_encode)


def _parse_stamp(text: str) -> datetime:
    text = text.replace(" ", "T").replace("Z", "+00:00")
    stamp = datetime.fromisoformat(text)
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def _same_text(recorded: str, actual: str, drift: timedelta) -> bool:
    if _TIMESTAMP.split(recorded) != _TIMESTAMP.split(actual):
        return False
    old, new = _TIMESTAMP.findall(recorded), _TIMESTAMP.findall(actual)
    if len(old) != len(new):
        return False
    for first, second in zip(old, new, strict=True):
        a, b = _parse_stamp(first), _parse_stamp(second)
        if a != b and abs((b - a) - drift) > CLOCK_TOLERANCE:
            return False
    return True


def _first_difference(recorded: str, actual: str) -> str:
    for index, (a, b) in enumerate(zip(recorded, actual, strict=False)):
        if a != b:
            low = max(0, index - 80)
            return (
                f"at {index}: recorded ...{recorded[low : index + 80]!r} "
                f"now ...{actual[low : index + 80]!r}"
            )
    return f"lengths {len(recorded)} recorded, {len(actual)} now"


def _sort_key(statement: dict) -> tuple[str, str]:
    return (
        _TIMESTAMP.sub("<ts>", statement["sql"]),
        _TIMESTAMP.sub("<ts>", _dump(statement["params"])),
    )


def differences(recorded: dict, actual: dict) -> list[str]:
    drift = _parse_stamp(actual["clock"]) - _parse_stamp(recorded["clock"])
    problems = []
    old = sorted(recorded["statements"], key=_sort_key)
    new = sorted(actual["statements"], key=_sort_key)
    if len(old) != len(new):
        problems.append(f"{len(old)} statements recorded, {len(new)} sent now")
    for index, (a, b) in enumerate(zip(old, new, strict=False)):
        if not _same_text(a["sql"], b["sql"], drift):
            problems.append(
                f"statement {index} text: {_first_difference(a['sql'], b['sql'])}"
            )
        if not _same_text(_dump(a["params"]), _dump(b["params"]), drift):
            problems.append(
                f"statement {index} parameters: "
                f"{_first_difference(_dump(a['params']), _dump(b['params']))}"
            )
    if not _same_text(_dump(recorded["body"]), _dump(actual["body"]), drift):
        problems.append(
            "body: " + _first_difference(_dump(recorded["body"]), _dump(actual["body"]))
        )
    return problems


# The one reader whose reads may escape both capture points: it answers from
# hourly states through a client the capture does not wrap.
NO_STATEMENT_READERS = frozenset({"dashboard_fast_path"})


def run_case(env: Env, reader: Reader, scope: str, worlds) -> dict:
    """Call the reader once and return what it sent ClickHouse and answered."""
    from django.core.cache import cache

    cache.clear()
    clock = datetime.now(UTC)
    with recording(env) as recorder:
        payload = reader.call(env, worlds, scope)
    # A comparison of nothing passes whatever the reader does, so a case whose
    # reads were not seen is a failure, not a pass.
    assert recorder.statements or reader.name in NO_STATEMENT_READERS, (
        f"{reader.name}.{scope} sent no statement the capture can see"
    )
    table = _symbols(env, worlds)
    return {
        "case": f"{reader.name}.{scope}",
        "clock": clock.isoformat(),
        "statements": _canonical(
            recorder.statements, table, sort_ids=True, volatile=_VOLATILE_PARAM_KEY
        ),
        "body": _canonical(payload, table, unordered=reader.unordered),
    }


def check_against_file(actual: dict) -> None:
    path = FIXTURES / f"{actual['case']}.json"
    if RECORD:
        FIXTURES.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(actual, indent=2, sort_keys=True) + "\n")
        pytest.fail(
            f"recorded {path.name}; run again without {RECORD_VARIABLE} to compare"
        )
    assert path.exists(), (
        f"{path} is missing; record it on the commit before the change with "
        f"{RECORD_VARIABLE}=1"
    )
    problems = differences(json.loads(path.read_text()), actual)
    assert not problems, (
        f"{path.name} no longer matches what the reader sent and answered before "
        f"the change ({len(problems)} differences):\n" + "\n".join(problems)
    )


def _checked_out_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=Path(__file__).parent,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _modified_tracked_files() -> str:
    result = subprocess.run(
        [
            "git",
            "status",
            "--porcelain",
            "--untracked-files=no",
            "--",
            ":/",
            ":(exclude,top)futureagi/tracer/tests/integration/test_simulation_default_parity.py",
            ":(exclude,top)futureagi/tracer/tests/integration/fixtures",
        ],
        cwd=Path(__file__).parent,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip()


@pytest.fixture(scope="session", autouse=True)
def recording_is_on_the_recorded_commit():
    """Refuse to record anywhere but the commit before the change."""
    if not RECORD:
        return
    commit = _checked_out_commit()
    if not commit.startswith(RECORDED_COMMIT):
        pytest.fail(
            f"recording must run on {RECORDED_COMMIT}, but HEAD is "
            f"{commit or 'unreadable'}; recorded elsewhere, the comparison would "
            "be the build against itself",
            pytrace=False,
        )
    # The right commit with the change applied on top reads as that commit.
    # The module only exists after the change, so its absence proves the
    # pre-change tree for ea12cc3f4 alone; a later RECORDED_COMMIT has it.
    change_applied = RECORDED_COMMIT == "ea12cc3f4" and bool(
        importlib.util.find_spec("tracer.services.simulation_visibility")
    )
    if change_applied or _modified_tracked_files():
        pytest.fail(
            f"the checkout of {RECORDED_COMMIT} has the change applied or other "
            "edits to tracked files; record on a clean checkout, with only this "
            "file and the fixtures folder copied in",
            pytrace=False,
        )


@pytest.fixture(scope="session", autouse=True)
def clickhouse_must_be_reachable():
    """Fail, never skip, when the test ClickHouse is down.

    The shared client fixture skips the module in that case, and a skipped
    module reads as a pass.
    """
    try:
        from clickhouse_driver import Client

        ch = settings.CLICKHOUSE
        client = Client(
            host=ch.get("CH_HOST", "localhost"),
            port=int(ch.get("CH_PORT", "19000")),
            user=ch.get("CH_USERNAME", "default"),
            password=ch.get("CH_PASSWORD", ""),
        )
        try:
            client.execute("SELECT 1")
        finally:
            client.disconnect()
    except Exception as exc:
        pytest.fail(
            f"ClickHouse is not reachable, so these tests cannot run: {exc}",
            pytrace=False,
        )


@pytest.fixture
def fresh_base():
    age = datetime.now(UTC) - PARITY_BASE
    if age > MAX_BASE_AGE:
        pytest.fail(
            f"PARITY_BASE is {age.days} days old; ClickHouse retention may have "
            "dropped the rows. Set a newer day and record again."
        )
    return PARITY_BASE


CASES = [
    pytest.param(reader.name, scope, id=f"{reader.name}-{scope.replace('.', '-')}")
    for reader in READERS.values()
    for scope in (*reader.scopes, *(f"single.{shape}" for shape in reader.shapes))
]
RULE_CASES = [
    pytest.param(reader.name, scope, id=f"{reader.name}-{scope}")
    for reader in READERS.values()
    for scope in reader.rule_scopes
]


@pytest.mark.parametrize(("name", "scope"), CASES)
def test_sql_and_body_identical_without_marked_spans(env, fresh_base, name, scope):
    reader = READERS[name]
    worlds = make_worlds(env, reader, base=fresh_base)
    check_against_file(run_case(env, reader, scope, worlds))


@pytest.mark.parametrize(("name", "scope"), RULE_CASES)
def test_number_rule_field_off_and_on_as_before(env, fresh_base, name, scope):
    reader = READERS[name]
    worlds = make_worlds(env, reader, base=fresh_base)
    actual = run_case(env, reader, scope, worlds)

    if reader.observe is _observe_voice_list or name == "queue_voice":
        # Behaviour, independent of the recording: the field drops exactly the
        # calls dialled from a simulator number, and off keeps them.
        payload = reader.call(env, worlds, scope)
        _, labels = reader.observe(payload, worlds)
        simulator = {"0/sim_a", "0/sim_b"}
        if scope == "field_on":
            assert labels.isdisjoint(simulator)
            assert {"0/p_old", "0/p_mid", "0/p_new"} <= labels
        else:
            assert simulator <= labels
    check_against_file(actual)


# --- the branch a holding project takes ------------------------------------------

_TABLE = re.compile(r"\b(?:FROM|JOIN)\s+([A-Za-z_][\w.]*)", re.IGNORECASE)


def _tables(statements: list[dict]) -> frozenset[str]:
    names = set()
    for statement in statements:
        names.update(t.lower().split(".")[-1] for t in _TABLE.findall(statement["sql"]))
    return frozenset(names)


# The two rollups a holding project is allowed to skip are the hourly chart
# states (read by the unfiltered tokens chart) and the trace count rollup.
# Neither is compared here as "the same tables": the tokens chart is excluded
# below and has its own test, the count has its own test, and the fast path is
# refused outright for a holding project, so it has a test of its own too.
HOURLY_STATE_READERS = frozenset({"trace_chart_tokens", "dashboard_fast_path"})


@pytest.mark.skipif(
    RECORD, reason="needs the holding flag, which the recorded commit lacks"
)
@pytest.mark.parametrize(
    "name",
    [
        pytest.param(r.name, id=r.name)
        for r in READERS.values()
        if r.name not in HOURLY_STATE_READERS
    ],
)
def test_holding_project_takes_todays_branch_except_the_two_rollups(env_factory, name):
    from tracer.services.simulation_visibility import record_holding

    reader = READERS[name]
    base = relative_base()
    plain_env, holding_env = env_factory(), env_factory()
    plain = make_worlds(plain_env, reader, ("a",), base=base)
    holding = make_worlds(holding_env, reader, ("a",), base=base)
    record_holding(holding[0].project_id)

    with recording(plain_env) as plain_recorder:
        plain_payload = reader.call(plain_env, plain, "single")
    with recording(holding_env) as holding_recorder:
        holding_payload = reader.call(holding_env, holding, "single")

    # Same tables read and the same number of statements: the rule rides the
    # statements a reader already sends.
    assert _tables(holding_recorder.statements) == _tables(plain_recorder.statements)
    assert len(holding_recorder.statements) == len(plain_recorder.statements)
    # No marked trace exists, so the answers are the same.
    plain_value, _ = reader.observe(plain_payload, plain)
    holding_value, _ = reader.observe(holding_payload, holding)
    assert holding_value == plain_value


@pytest.mark.skipif(
    RECORD, reason="needs the holding flag, which the recorded commit lacks"
)
def test_unfiltered_chart_of_a_holding_project_reads_rows_not_hourly_states(
    env_factory,
):
    from tracer.services.simulation_visibility import record_holding

    reader = READERS["trace_chart_tokens"]
    base = relative_base()
    plain_env, holding_env = env_factory(), env_factory()
    plain = make_worlds(plain_env, reader, ("a",), base=base)
    holding = make_worlds(holding_env, reader, ("a",), base=base)
    record_holding(holding[0].project_id)

    # The chart carries only the date window, so a project that holds nothing
    # is answered from the hourly states.
    assert reader.call(plain_env, plain, "single")["query_provenance"] == (
        "materialized_rollup"
    )
    assert reader.call(holding_env, holding, "single")["query_provenance"] != (
        "materialized_rollup"
    )


@pytest.mark.skipif(
    RECORD, reason="needs the holding flag, which the recorded commit lacks"
)
def test_holding_count_leaves_the_trace_count_rollup(env_factory):
    from tracer.services.clickhouse.v2.query_builders.trace_list import (
        TraceListQueryBuilderV2,
    )
    from tracer.services.simulation_visibility import hide_simulated, record_holding

    reader = READERS["trace_list"]
    base = relative_base()
    plain_env, holding_env = env_factory(), env_factory()
    plain = make_worlds(plain_env, reader, ("a",), base=base)[0]
    holding = make_worlds(holding_env, reader, ("a",), base=base)[0]
    record_holding(holding.project_id)

    def count_statement(world) -> str:
        builder = TraceListQueryBuilderV2(
            project_id=world.project_id,
            filters=[world.window],
            page_number=0,
            page_size=30,
            eval_config_ids=[],
            annotation_label_ids=[],
            hide_simulated=hide_simulated(world.project_id),
        )
        builder.build()
        return builder.build_count_query()[0]

    # Only the date window is asked for, so a project that holds nothing is
    # counted from the rollup; the rollup carries no attribute, so a holding
    # project is counted from the rows.
    assert "trace_count_rollup" in count_statement(plain)
    assert "trace_count_rollup" not in count_statement(holding)


@pytest.mark.skipif(
    RECORD, reason="needs the holding flag, which the recorded commit lacks"
)
def test_dashboard_fast_path_refuses_a_holding_project_only(env_factory, monkeypatch):
    from tracer.services.simulation_visibility import record_holding
    from tracer.views import dashboard as dashboard_view

    reader = READERS["dashboard_fast_path"]
    base = relative_base()
    env = env_factory()
    worlds = make_worlds(env, reader, ("a", "b"), base=base)
    record_holding(worlds[0].project_id)
    # Nothing is stored, so the public read goes on to the fast path. The
    # dashboard view never takes that path for a trace metric today (it passes
    # none to the fast path), so the view's own decision helper and the public
    # read it hands the decision to are what this runs.
    monkeypatch.setattr(
        dashboard_view, "read_or_schedule_exact_snapshot", lambda *a, **k: {}
    )

    def public_read(world):
        config = dashboard_config([world], deepcopy(DASHBOARD_TOKENS))
        hide = dashboard_view._scope_holds_simulated(config["project_ids"])
        payload = dashboard_view._read_public_dashboard_query(
            config,
            cache_identity={
                "workspace_id": str(env.workspace.id),
                "query_config": config,
            },
            refresh=False,
            try_rollup=True,
            hide_simulated=hide,
        )
        return hide, payload

    plain_hides, plain = public_read(worlds[1])
    held_hides, held = public_read(worlds[0])

    assert not plain_hides
    assert held_hides
    assert plain.get("query_error_code") != "bounded_shape_unavailable"
    assert held["query_status"] == "degraded"
    assert held["query_error_code"] == "bounded_shape_unavailable"
