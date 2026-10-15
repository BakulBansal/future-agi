"""The hidden simulation project stays out of the project managers."""

import pytest
from django.db import IntegrityError, transaction
from rest_framework import status

from model_hub.models.ai_model import AIModel
from simulate.models.agent_definition import AgentDefinition
from tfc.utils.error_codes import get_error_message
from tracer.models.project import Project
from tracer.models.project_version import ProjectVersion
from tracer.models.trace import Trace
from tracer.services.simulation_project import (
    RESERVED_PROJECT_NAME_MESSAGE,
    ReservedProjectNameError,
)
from tracer.tests.test_simulation_project import NOT_RESERVED_NAMES, RESERVED_NAME
from tracer.utils.otel import get_or_create_project

pytestmark = pytest.mark.django_db


def _agent(organization, workspace, name):
    return AgentDefinition.objects.create(
        agent_name=name,
        description=f"{name} description",
        agent_type="text",
        inbound=True,
        organization=organization,
        workspace=workspace,
    )


def _project(manager, organization, workspace, name, deleted=False, **extra):
    return manager.create(
        name=name,
        organization=organization,
        workspace=workspace,
        model_type=AIModel.ModelTypes.GENERATIVE_LLM,
        trace_type="observe",
        deleted=deleted,
        **extra,
    )


def _four_projects(organization, workspace):
    visible_live = _project(Project.objects, organization, workspace, "visible live")
    visible_deleted = _project(
        Project.objects, organization, workspace, "visible deleted", deleted=True
    )
    hidden_live = _project(
        Project.with_simulation,
        organization,
        workspace,
        "hidden live",
        simulation_agent=_agent(organization, workspace, "live agent"),
    )
    hidden_deleted = _project(
        Project.with_simulation,
        organization,
        workspace,
        "hidden deleted",
        deleted=True,
        simulation_agent=_agent(organization, workspace, "deleted agent"),
    )
    return visible_live, visible_deleted, hidden_live, hidden_deleted


@pytest.mark.parametrize("manager", ["objects", "all_objects", "no_workspace_objects"])
def test_the_three_managers_exclude_it_and_with_simulation_includes_it(
    manager, organization, workspace
):
    visible_live, visible_deleted, hidden_live, hidden_deleted = _four_projects(
        organization, workspace
    )
    expected = {
        "objects": {visible_live.id},
        "no_workspace_objects": {visible_live.id},
        "all_objects": {visible_live.id, visible_deleted.id},
    }[manager]

    returned = set(
        getattr(Project, manager)
        .filter(organization=organization)
        .values_list("id", flat=True)
    )

    assert returned == expected
    assert not returned & {hidden_live.id, hidden_deleted.id}
    assert set(
        Project.with_simulation.filter(organization=organization).values_list(
            "id", flat=True
        )
    ) == {visible_live.id, visible_deleted.id, hidden_live.id, hidden_deleted.id}
    assert '"simulation_agent_id" IS NULL' in str(getattr(Project, manager).all().query)
    assert '"simulation_agent_id" IS NULL' not in str(
        Project.with_simulation.all().query
    )
    assert "JOIN" not in str(Project.no_workspace_objects.all().query)
    assert Project._meta.default_manager_name == "objects"
    assert Project._default_manager.name == "objects"
    assert [m.name for m in Project._meta.managers] == [
        "objects",
        "all_objects",
        "no_workspace_objects",
        "with_simulation",
    ]


def test_forward_foreign_key_still_reaches_it(organization, workspace):
    agent = _agent(organization, workspace, "forward agent")
    hidden = _project(
        Project.with_simulation,
        organization,
        workspace,
        "forward hidden",
        simulation_agent=agent,
    )
    version = ProjectVersion.objects.create(
        project=hidden, name="Run", version="v1", metadata={}
    )
    trace = Trace.objects.create(
        project=hidden,
        project_version=version,
        name="Trace",
        metadata={},
        input={"prompt": "hi"},
        output={"response": "hello"},
    )

    assert ProjectVersion.no_workspace_objects.get(pk=version.pk).project == hidden
    assert Trace.no_workspace_objects.get(pk=trace.pk).project == hidden
    assert AgentDefinition._base_manager.get(pk=agent.pk).simulation_project == hidden


def test_save_full_clean_does_not_see_a_hidden_name_but_the_database_does(
    organization, workspace
):
    hidden = _project(
        Project.with_simulation,
        organization,
        workspace,
        "taken by a hidden project",
        simulation_agent=_agent(organization, workspace, "name agent"),
    )
    twin = Project(
        name=hidden.name,
        trace_type=hidden.trace_type,
        organization=hidden.organization,
        workspace=hidden.workspace,
        model_type=AIModel.ModelTypes.GENERATIVE_LLM,
    )

    twin.full_clean()
    with pytest.raises(IntegrityError), transaction.atomic():
        twin.save()


VIEW_PATHS = ("view_create", "view_rename", "view_patch")
VIEW_NOT_RESERVED_NAMES = [name for name in NOT_RESERVED_NAMES if name == name.strip()]
HARMLESS_NAME = "A harmless name"
RESERVED_PATH_CASES = (
    [(path, RESERVED_NAME, True) for path in (*VIEW_PATHS, "get_or_create_project")]
    + [(path, f" {RESERVED_NAME} ", True) for path in VIEW_PATHS]
    + [(path, name, False) for path in VIEW_PATHS for name in VIEW_NOT_RESERVED_NAMES]
    + [("get_or_create_project", name, False) for name in NOT_RESERVED_NAMES]
)


@pytest.mark.parametrize(
    "path, name, refused",
    RESERVED_PATH_CASES,
    ids=[
        f"{path}-{'refused' if refused else 'accepted'}-{index}"
        for index, (path, _name, refused) in enumerate(RESERVED_PATH_CASES)
    ],
)
def test_reserved_name_is_refused_by_create_rename_tools_and_get_or_create(
    path, name, refused, auth_client, user, organization, workspace
):
    target = _project(Project.objects, organization, workspace, "Target project")

    if path == "get_or_create_project":
        if refused:
            with pytest.raises(ReservedProjectNameError) as raised:
                get_or_create_project(
                    name, str(organization.id), "observe", str(user.id), workspace.id
                )
            assert str(raised.value) == RESERVED_PROJECT_NAME_MESSAGE
        else:
            project = get_or_create_project(
                name, str(organization.id), "observe", str(user.id), workspace.id
            )
            assert project.name == name
            assert Project.no_workspace_objects.filter(pk=project.pk).exists()
    elif path == "view_create":
        response = auth_client.post(
            "/tracer/project/",
            {"name": name, "model_type": "GenerativeLLM", "trace_type": "observe"},
            format="json",
        )
        if refused:
            assert response.status_code == status.HTTP_400_BAD_REQUEST
            assert response.json()["message"] == RESERVED_PROJECT_NAME_MESSAGE
        else:
            assert response.status_code == status.HTTP_200_OK
            assert Project.objects.get(name=name).trace_type == "observe"
    elif path == "view_rename":
        response = auth_client.post(
            "/tracer/project/update_project_name/",
            {"project_id": str(target.id), "name": name},
            format="json",
        )
        if refused:
            assert response.status_code == status.HTTP_400_BAD_REQUEST
            assert response.json()["message"] == RESERVED_PROJECT_NAME_MESSAGE
        else:
            assert response.status_code == status.HTTP_200_OK
            assert Project.objects.get(pk=target.pk).name == name
    else:
        response = auth_client.patch(
            f"/tracer/project/{target.id}/", {"name": name}, format="json"
        )
        if refused:
            body = response.json()
            assert response.status_code == status.HTTP_400_BAD_REQUEST
            assert body["details"]["name"] == [RESERVED_PROJECT_NAME_MESSAGE]
            assert body["message"] == "name: " + RESERVED_PROJECT_NAME_MESSAGE
        else:
            assert response.status_code == status.HTTP_200_OK
            assert Project.objects.get(pk=target.pk).name == name

    if refused:
        assert not Project.with_simulation.filter(
            name__in={name, name.strip()}
        ).exists()
        if path in ("view_rename", "view_patch"):
            assert Project.objects.get(pk=target.pk).name == "Target project"


@pytest.mark.parametrize("path", ["view_patch", "view_rename"])
def test_a_hidden_project_cannot_be_renamed(path, auth_client, organization, workspace):
    hidden = _project(
        Project.with_simulation,
        organization,
        workspace,
        "Hidden project",
        simulation_agent=_agent(organization, workspace, "Hidden agent"),
    )

    if path == "view_patch":
        response = auth_client.patch(
            f"/tracer/project/{hidden.id}/", {"name": HARMLESS_NAME}, format="json"
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND
    else:
        response = auth_client.post(
            "/tracer/project/update_project_name/",
            {"project_id": str(hidden.id), "name": HARMLESS_NAME},
            format="json",
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.json()["message"] == get_error_message("PROJECT_NOT_FOUND")
    assert Project.with_simulation.get(pk=hidden.pk).name == "Hidden project"
