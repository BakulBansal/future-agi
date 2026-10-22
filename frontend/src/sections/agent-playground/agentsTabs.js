import { paths } from "src/routes/paths";

export const AGENTS_TABS = ["connected", "playground"];

export const agentsTabPath = (tab) => `${paths.dashboard.agents}/${tab}`;

export const CONNECTED_AGENTS_PATH = agentsTabPath("connected");

export const PLAYGROUND_AGENTS_PATH = agentsTabPath("playground");

export const CONNECT_AGENT_PATH = `${paths.dashboard.simulate.environments.root}?tab=build`;

export const resolveAgentsTab = (raw) =>
  AGENTS_TABS.includes(raw) ? raw : "connected";
