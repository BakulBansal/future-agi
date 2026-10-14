"""The fixture that answers "no project holds simulated traces" without a database.

A test with no database access sees every importer of the holding decision
answer "not holding". A test with database access, and a test marked
``real_holding_decision``, keep the real functions, so the suites that need the
real read still run it.
"""

import ast
import importlib
import os
import uuid
from pathlib import Path

import pytest
from django.test import TestCase

from model_hub.services import bulk_selection
from tracer.services import simulation_visibility as visibility
from tracer.services.clickhouse.v2 import span_reader
from tracer.views import trace as trace_view

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DECISIONS = ("hide_simulated", "holding_projects")
_SKIPPED_DIRS = {
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


class _ReadReached(Exception):
    """Raised by the stand-in for the read the real decision makes."""


def _read_is_reached(*_args, **_kwargs):
    raise _ReadReached


def _assert_the_real_decision_runs(monkeypatch):
    # Importers must hold the defining module's own functions, not stand-ins.
    assert trace_view.project_hides_simulated is visibility.hide_simulated
    assert trace_view.holding_projects is visibility.holding_projects
    # Calling one must run that module's code, which asks ``holding_projects``;
    # a stand-in would answer without ever making the read.
    monkeypatch.setattr(visibility, "holding_projects", _read_is_reached)
    with pytest.raises(_ReadReached):
        trace_view.project_hides_simulated(str(uuid.uuid4()))


@pytest.mark.unit
def test_a_test_without_database_access_sees_no_project_holding():
    # "p1" is not a UUID, so the real function cannot answer for it at all; a
    # pass here means the stand-in answered rather than the read.
    assert trace_view.project_hides_simulated("p1") is False
    assert span_reader.hide_simulated("p1") is False
    assert bulk_selection.project_hides_simulated("p1") is False
    assert trace_view.holding_projects(["p1", "p2"]) == frozenset()


@pytest.mark.django_db
def test_a_test_with_the_django_db_mark_runs_the_real_decision(monkeypatch):
    _assert_the_real_decision_runs(monkeypatch)


def test_a_test_using_the_db_fixture_runs_the_real_decision(db, monkeypatch):
    _assert_the_real_decision_runs(monkeypatch)


def test_a_test_using_the_transactional_db_fixture_runs_the_real_decision(
    transactional_db, monkeypatch
):
    _assert_the_real_decision_runs(monkeypatch)


@pytest.mark.unit
@pytest.mark.real_holding_decision
def test_a_test_marked_real_holding_decision_runs_the_real_decision(monkeypatch):
    _assert_the_real_decision_runs(monkeypatch)


def _imports_the_module_itself(node):
    if isinstance(node, ast.Import):
        return any(alias.name.endswith("simulation_visibility") for alias in node.names)
    # ``from . import simulation_visibility`` has no module name to compare.
    if isinstance(node, ast.ImportFrom) and (
        node.level or node.module == "tracer.services"
    ):
        return any(alias.name == "simulation_visibility" for alias in node.names)
    return False


def _importers_of_the_decision():
    """Each (module, name bound there, function named) outside the tests."""
    importers = []
    through_module = []
    for current, dirs, files in os.walk(_PROJECT_ROOT):
        dirs[:] = [name for name in dirs if name not in _SKIPPED_DIRS]
        for file_name in files:
            if not file_name.endswith(".py") or file_name == "conftest.py":
                continue
            if file_name.startswith("test_"):
                continue
            path = Path(current) / file_name
            source = path.read_text(encoding="utf-8", errors="ignore")
            if "simulation_visibility" not in source:
                continue
            parts = list(path.relative_to(_PROJECT_ROOT).with_suffix("").parts)
            if parts[-1] == "__init__":
                parts.pop()
            module_name = ".".join(parts)
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, ast.ImportFrom) and (node.module or "").endswith(
                    "simulation_visibility"
                ):
                    importers.extend(
                        (module_name, alias.asname or alias.name, alias.name)
                        for alias in node.names
                        if alias.name in _DECISIONS
                    )
                elif _imports_the_module_itself(node):
                    through_module.append(module_name)
    return importers, through_module


def _importers_still_real(importers):
    still_real = []
    for module_name, bound_name, function_name in importers:
        # An import inside a function or under TYPE_CHECKING binds no module
        # attribute, so the fixture could not patch it either.
        try:
            function = getattr(importlib.import_module(module_name), bound_name)
        except (AttributeError, ImportError):
            still_real.append(
                f"{module_name}.{bound_name} "
                "(not a module attribute: import it at module level)"
            )
            continue
        project_id = str(uuid.uuid4())
        if function_name == "hide_simulated":
            argument, expected = project_id, False
        else:
            argument, expected = [project_id], frozenset()
        # Without the stand-in the real function cannot run here, and a refusal
        # counts as not answering.
        try:
            answered_not_holding = function(argument) == expected
        except Exception:
            answered_not_holding = False
        if not answered_not_holding:
            still_real.append(f"{module_name}.{bound_name}")
    return still_real


@pytest.mark.unit
def test_every_importer_of_the_holding_decision_answers_not_holding():
    importers, through_module = _importers_of_the_decision()
    assert importers, "the scan found no importer, so it is looking in the wrong place"
    # A module that reaches the functions through the module object would not be
    # patched, whatever the fixture lists.
    assert not through_module, (
        f"these modules import simulation_visibility itself: {through_module}"
    )

    still_real = _importers_still_real(importers)

    assert not still_real, (
        "these importers still look up the real holding decision in a test "
        f"without a database; list them in tracer/tests/conftest.py: {still_real}"
    )


@pytest.mark.unit
def test_an_importer_that_binds_no_module_attribute_is_named_with_its_module():
    missing = _importers_still_real(
        [("tracer.views.trace", "no_such_binding", "hide_simulated")]
    )

    assert missing == [
        "tracer.views.trace.no_such_binding "
        "(not a module attribute: import it at module level)"
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    "statement",
    [
        "from . import simulation_visibility",
        "from .. import simulation_visibility as visibility",
        "from tracer.services import simulation_visibility",
        "import tracer.services.simulation_visibility",
    ],
)
def test_the_scan_sees_every_way_of_importing_the_module_itself(statement):
    assert _imports_the_module_itself(ast.parse(statement).body[0])


@pytest.mark.unit
def test_the_scan_ignores_an_import_of_an_unrelated_name():
    assert not _imports_the_module_itself(ast.parse("from . import other").body[0])


class TestDjangoTestCaseKeepsTheRealDecision(TestCase):
    # A TestCase gets its database from the class, with no mark and no fixture
    # name, so the fixture must still see it as a test with database access.
    def test_a_test_case_runs_the_real_decision(self):
        assert trace_view.project_hides_simulated is visibility.hide_simulated
        assert trace_view.holding_projects is visibility.holding_projects
