import { paths } from "src/routes/paths";

export const PLAYGROUND_AGENTS_PATH = `${paths.dashboard.agents}?tab=playground`;

export const CONNECT_AGENT_PATH = `${paths.dashboard.simulate.environments.root}?tab=build`;

const TABS = ["connected", "playground"];

export const resolveAgentsTab = (raw) =>
  TABS.includes(raw) ? raw : "connected";
