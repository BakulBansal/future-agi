import { useParams, useSearchParams } from "react-router-dom";
import { useAgentDetail } from "src/api/agents/agentDetail";
import { resolveVersionId } from "./agentDetailTabs";

// The agent in the URL and the version the page shows (`?v=`, else active).
export default function useAgentRoute() {
  const { agentId } = useParams();
  const [params] = useSearchParams();
  const query = useAgentDetail(agentId);
  const agent = query.data;
  const versionId = agent
    ? resolveVersionId(params.get("v"), agent.versions, agent.activeVersionId)
    : null;
  const version = agent?.versions.find((v) => v.id === versionId) ?? null;
  return { agentId, agent, query, versionId, version };
}
