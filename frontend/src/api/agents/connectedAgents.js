import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { CONNECTED_AGENTS_FIXTURE } from "./_fixtures/connectedAgents";

export const connectedAgentsQueryKey = ({ page, pageSize, search }) => [
  "agents",
  "connected",
  { page, pageSize, search },
];

export const mapConnectedAgent = (raw) => ({
  id: raw.id,
  name: raw.name,
  description: raw.description ?? null,
  modality: raw.modality ?? null,
  direction: raw.direction ?? null,
  source: raw.source ?? null,
  sourceLabel: raw.source_label ?? null,
  latestVersionId: raw.latest_version?.id ?? null,
  latestLabel: raw.latest_version?.label ?? null,
  versionCount: raw.version_count ?? 0,
  usedIn: raw.used_in ?? [],
  createdAt: raw.created_at ?? null,
});

// Pages and searches the fixture the way the server will, so swapping the
// queryFn to axios is the only change once the list API lands.
const fetchFixturePage = ({ page, pageSize, search }) => {
  const q = (search || "").trim().toLowerCase();
  const matching = structuredClone(CONNECTED_AGENTS_FIXTURE).filter(
    (a) => !q || `${a.name} ${a.description || ""}`.toLowerCase().includes(q),
  );
  const start = page * pageSize;
  return {
    results: matching.slice(start, start + pageSize),
    count: matching.length,
  };
};

// TODO: swap to axios(endpoints.agents.connected) once the list API lands
export function useConnectedAgents({ page, pageSize, search }) {
  return useQuery({
    queryKey: connectedAgentsQueryKey({ page, pageSize, search }),
    queryFn: async () => fetchFixturePage({ page, pageSize, search }),
    select: (d) => ({
      items: d.results.map(mapConnectedAgent),
      total: d.count,
    }),
    placeholderData: keepPreviousData,
  });
}
