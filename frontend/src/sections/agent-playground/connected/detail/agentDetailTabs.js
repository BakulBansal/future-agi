import { CONNECTED_AGENTS_PATH } from "../../agentsTabs";

export const DETAIL_TABS = [
  { id: "overview", label: "Overview" },
  { id: "definition", label: "Definition" },
  { id: "versions", label: "Versions" },
  { id: "environments", label: "Environments" },
];

export const COMING_SOON_TABS = ["Constraints", "Improvements", "Connections"];

export const agentDetailPath = (agentId, tab = "overview") =>
  `${CONNECTED_AGENTS_PATH}/${agentId}/${tab}`;

// The version the page shows: `?v=` when it names one of the agent's
// versions, else the active one.
export const resolveVersionId = (raw, versions, activeVersionId) => {
  if (raw && versions.some((v) => v.id === raw)) return raw;
  return activeVersionId ?? versions[0]?.id ?? null;
};
