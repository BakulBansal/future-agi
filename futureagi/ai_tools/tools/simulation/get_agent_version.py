from uuid import UUID

from pydantic import BaseModel as PydanticBaseModel
from pydantic import Field

from ai_tools.base import BaseTool, ToolContext, ToolResult
from ai_tools.formatting import (
    format_datetime,
    format_number,
    format_status,
    key_value_block,
    section,
    truncate,
)
from ai_tools.registry import register_tool


class GetAgentVersionInput(PydanticBaseModel):
    agent_id: UUID = Field(description="The UUID of the agent definition")
    version_id: UUID = Field(description="The UUID of the version to retrieve")


@register_tool
class GetAgentVersionTool(BaseTool):
    name = "get_agent_version"
    description = (
        "Returns detailed information about a specific agent version, "
        "including score, pass rate, test count, status, and configuration snapshot."
    )
    category = "simulation"
    input_model = GetAgentVersionInput

    def execute(self, params: GetAgentVersionInput, context: ToolContext) -> ToolResult:

        from simulate.models.agent_definition import AgentDefinition
        from simulate.models.agent_version import AgentVersion
        from tracer.services.observability_switch import snapshot_as_shown

        try:
            agent = AgentDefinition.objects.get(id=params.agent_id)
        except AgentDefinition.DoesNotExist:
            return ToolResult.not_found("Agent", str(params.agent_id))

        try:
            version = AgentVersion.objects.get(
                id=params.version_id, agent_definition=agent
            )
        except AgentVersion.DoesNotExist:
            return ToolResult.not_found("Agent Version", str(params.version_id))

        score = format_number(version.score) if version.score is not None else "—"
        pass_rate = f"{version.pass_rate}%" if version.pass_rate is not None else "—"

        info = key_value_block(
            [
                ("Version ID", f"`{version.id}`"),
                ("Agent", agent.agent_name),
                ("Version", version.version_name),
                ("Version Number", str(version.version_number)),
                ("Status", format_status(version.status)),
                ("Score", score),
                ("Test Count", str(version.test_count)),
                ("Pass Rate", pass_rate),
                (
                    "Description",
                    truncate(version.description, 300) if version.description else "—",
                ),
                (
                    "Commit Message",
                    (
                        truncate(version.commit_message, 200)
                        if version.commit_message
                        else "—"
                    ),
                ),
                (
                    "Release Notes",
                    (
                        truncate(version.release_notes, 300)
                        if version.release_notes
                        else "—"
                    ),
                ),
                ("Is Active", "Yes" if version.is_active else "No"),
                ("Is Latest", "Yes" if version.is_latest else "No"),
                ("Created", format_datetime(version.created_at)),
            ]
        )

        content = section(f"Agent Version: {version.version_name}", info)

        # Configuration snapshot
        if version.configuration_snapshot:
            content += "\n\n### Configuration Snapshot\n\n"
            snapshot = snapshot_as_shown(version.configuration_snapshot, agent)
            snapshot_pairs = []
            for key, value in list(snapshot.items())[:15]:
                snapshot_pairs.append((key, truncate(str(value), 100)))
            content += key_value_block(snapshot_pairs)

        data = {
            "id": str(version.id),
            "agent_id": str(agent.id),
            "version_number": version.version_number,
            "version_name": version.version_name,
            "status": version.status,
            "score": float(version.score) if version.score is not None else None,
            "test_count": version.test_count,
            "pass_rate": (
                float(version.pass_rate) if version.pass_rate is not None else None
            ),
        }

        return ToolResult(content=content, data=data)
