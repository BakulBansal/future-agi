import { CONNECTED_AGENTS_PATH } from "../../agentsTabs";

export const DETAIL_TABS = [
  { id: "overview", label: "Overview" },
  { id: "definition", label: "Definition" },
  { id: "versions", label: "Versions" },
  { id: "environments", label: "Environments" },
];

export const COMING_SOON_TABS = ["Constraints", "Improvements", "Connections"];

// Code agents built in an RL environment show their environments; every other
// agent shows its versions (TH-8652, agent pages contract section 2).
export const visibleDetailTabs = (agentKind) =>
  agentKind === "source"
    ? ["overview", "definition", "environments"]
    : ["overview", "definition", "versions"];

export const agentDetailPath = (agentId, tab = "overview") =>
  `${CONNECTED_AGENTS_PATH}/${agentId}/${tab}`;

export const resolveVersionId = (raw, versions, activeVersionId) => {
  if (raw && versions.some((v) => v.id === raw)) return raw;
  return activeVersionId ?? versions[0]?.id ?? null;
};
