import { useMemo } from "react";
import { useAgentDefinitionVersions } from "src/api/agent-definition/agent-definition-version";
import { toVersion, useAgentVersion } from "src/api/agents/agentDetail";

/**
 * The agent's versions a page at a time, newest first, with the selected
 * version pinned to the top until its own page loads. The pin comes from
 * that version's detail, so a deep link to an old version shows it at once.
 */
export default function usePinnedVersions(agentId, versionId) {
  const list = useAgentDefinitionVersions({ selectedAgentId: agentId });
  const selected = useAgentVersion(agentId, versionId);
  const loaded = useMemo(
    () =>
      (list.data?.pages ?? []).flatMap((p) => p.results ?? []).map(toVersion),
    [list.data],
  );
  const inList = loaded.find((v) => v.id === versionId);
  const pinned = !inList && selected.data ? selected.data : null;
  return {
    versions: pinned ? [pinned, ...loaded] : loaded,
    selected: inList ?? pinned,
    loaded: !!list.data,
    isLoading: list.isLoading,
    hasNextPage: list.hasNextPage,
    isFetchingNextPage: list.isFetchingNextPage,
    fetchNextPage: list.fetchNextPage,
  };
}
