import { useEffect, useMemo, useState } from "react";
import { useAgentDefinitionVersions } from "src/api/agent-definition/agent-definition-version";
import { toVersion, useAgentVersion } from "src/api/agents/agentDetail";

/**
 * The agent's versions a page at a time, newest first. A selected version
 * that isn't on the first page (a deep link to an old one) is pinned to the
 * top from its own detail, and stays pinned while more pages load; its copy
 * further down is left out so it shows once. Whether to pin is decided once
 * per selection, when the first page arrives, so rows never jump.
 */
export default function usePinnedVersions(agentId, versionId) {
  const list = useAgentDefinitionVersions({ selectedAgentId: agentId });
  const selected = useAgentVersion(agentId, versionId);
  const loaded = useMemo(
    () =>
      (list.data?.pages ?? []).flatMap((p) => p.results ?? []).map(toVersion),
    [list.data],
  );

  const [pin, setPin] = useState({ forId: null, pinned: false });
  const decided = pin.forId === versionId;
  useEffect(() => {
    if (!list.data || !versionId || decided) return;
    setPin({
      forId: versionId,
      pinned: !loaded.some((v) => v.id === versionId),
    });
  }, [list.data, versionId, decided, loaded]);

  const pinned = decided && pin.pinned && selected.data ? selected.data : null;
  const inList = loaded.find((v) => v.id === versionId);
  return {
    versions: pinned
      ? [pinned, ...loaded.filter((v) => v.id !== versionId)]
      : loaded,
    selected: inList ?? selected.data ?? null,
    loaded: !!list.data,
    isLoading: list.isLoading,
    hasNextPage: list.hasNextPage,
    isFetchingNextPage: list.isFetchingNextPage,
    fetchNextPage: list.fetchNextPage,
  };
}
