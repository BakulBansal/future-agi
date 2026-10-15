from uuid import UUID

from pydantic import BaseModel as PydanticBaseModel
from pydantic import Field

from ai_tools.base import BaseTool, ToolContext, ToolResult
from ai_tools.formatting import markdown_table, section
from ai_tools.registry import register_tool


class GetProjectEvalAttributesInput(PydanticBaseModel):
    project_id: UUID = Field(
        description="The UUID of the project to fetch eval attributes for"
    )


@register_tool
class GetProjectEvalAttributesTool(BaseTool):
    name = "get_project_eval_attributes"
    description = (
        "Returns the list of all available span/eval attribute keys for a project. "
        "These are the valid values that can be used in the 'mapping' field when "
        "creating a custom eval config. Use this to discover what attribute keys "
        "exist in the project's spans before configuring eval mappings."
    )
    category = "tracing"
    input_model = GetProjectEvalAttributesInput

    def execute(
        self, params: GetProjectEvalAttributesInput, context: ToolContext
    ) -> ToolResult:

        from tracer.models.project import Project
        from tracer.utils.sql_queries import SQL_query_handler

        # Validate project
        try:
            project = Project.objects.get(
                id=params.project_id, organization=context.organization
            )
        except Project.DoesNotExist:
            return ToolResult.not_found("Project", str(params.project_id))

        # Fetch all distinct attribute keys for the project
        attributes = SQL_query_handler.get_span_attributes_for_project(
            str(params.project_id)
        )

        if not attributes:
            return ToolResult(
                content=section(
                    f"Eval Attributes: {project.name}",
                    "_No span attributes found for this project. "
                    "Ensure the project has traces with span attributes._",
                ),
                data={"attributes": [], "project_id": str(params.project_id)},
            )

        sorted_attributes = sorted(attributes)

        rows = [[attr] for attr in sorted_attributes]
        table = markdown_table(["Attribute Key"], rows)

        content = section(
            f"Eval Attributes: {project.name} ({len(sorted_attributes)})",
            f"These are the available attribute keys that can be used in eval config mappings.\n\n{table}",
        )

        return ToolResult(
            content=content,
            data={
                "attributes": sorted_attributes,
                "project_id": str(params.project_id),
            },
        )
