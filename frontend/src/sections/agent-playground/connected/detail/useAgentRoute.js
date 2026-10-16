import { useParams } from "react-router-dom";
import { useAgentDetail } from "src/api/agents/agentDetail";

// The agent in the URL and the version the page shows (its active version).
export default function useAgentRoute() {
  const { agentId } = useParams();
  const query = useAgentDetail(agentId);
  const agent = query.data;
  const versionId = agent
    ? agent.activeVersionId ?? agent.versions[0]?.id ?? null
    : null;
  const version = agent?.versions.find((v) => v.id === versionId) ?? null;
  return { agentId, agent, query, versionId, version };
}
