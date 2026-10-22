import { describe, it, expect, vi } from "vitest";
import {
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { HelmetProvider } from "react-helmet-async";
import {
  MemoryRouter,
  Route,
  Routes,
  useLocation,
} from "react-router-dom";
import AgentsLanding from "../AgentsLanding";
import AgentsIndexRedirect from "../AgentsIndexRedirect";
import ConnectedAgentsTab from "../ConnectedAgentsTab";
import PlaygroundAgentsPage from "../PlaygroundAgentsPage";
import {
  CONNECTED_AGENTS_PATH,
  PLAYGROUND_AGENTS_PATH,
  agentsTabPath,
  resolveAgentsTab,
} from "../agentsTabs";

vi.mock("../AgentView", () => ({
  default: () => <div data-testid="playground-list" />,
}));

vi.mock("../connected/ConnectedAgentsView", () => ({
  default: ({ onConnect }) => (
    <div data-testid="connected-list">
      <button type="button" onClick={onConnect}>
        Connect an agent
      </button>
    </div>
  ),
}));

vi.mock("src/components/tooltip/CustomTooltip", () => ({
  default: ({ title, show, children }) => (
    <div
      data-testid="custom-tooltip"
      data-title={title}
      data-show={String(show)}
    >
      {children}
    </div>
  ),
}));

vi.mock("src/utils/axios", () => ({
  default: {
    get: vi.fn(() =>
      Promise.resolve({
        data: { result: { graphs: [], metadata: { total_count: 0 } } },
      }),
    ),
  },
  endpoints: { agentPlayground: { listGraphs: "/graphs/" } },
}));

let currentLocation = null;
const LocationProbe = () => {
  currentLocation = useLocation();
  return null;
};

// Mirrors the agents block in routes/sections/dashboard.jsx: the Playground
// list is its own page, outside the Agents landing.
const agentsRoutes = (
  <Routes>
    <Route path="/dashboard/agents" element={<AgentsLanding />}>
      <Route index element={<AgentsIndexRedirect />} />
      <Route path="connected" element={<ConnectedAgentsTab />} />
    </Route>
    <Route path="/dashboard/agents/playground" element={<PlaygroundAgentsPage />} />
    <Route path="*" element={<div data-testid="elsewhere" />} />
  </Routes>
);

const renderAt = (url, ui = agentsRoutes) =>
  render(
    <HelmetProvider>
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter initialEntries={[url]}>
          {ui}
          <LocationProbe />
        </MemoryRouter>
      </QueryClientProvider>
    </HelmetProvider>,
  );

describe("resolveAgentsTab", () => {
  it("opens Connected agents unless the Playground is asked for", () => {
    expect(resolveAgentsTab(undefined)).toBe("connected");
    expect(resolveAgentsTab(null)).toBe("connected");
    expect(resolveAgentsTab("")).toBe("connected");
    expect(resolveAgentsTab("garbage")).toBe("connected");
    expect(resolveAgentsTab("connected")).toBe("connected");
    expect(resolveAgentsTab("playground")).toBe("playground");
  });
});

describe("agents tab paths", () => {
  it("gives each tab its own path", () => {
    expect(CONNECTED_AGENTS_PATH).toBe("/dashboard/agents/connected");
    expect(PLAYGROUND_AGENTS_PATH).toBe("/dashboard/agents/playground");
    expect(agentsTabPath("connected")).toBe(CONNECTED_AGENTS_PATH);
    expect(agentsTabPath("playground")).toBe(PLAYGROUND_AGENTS_PATH);
  });
});

describe("AgentsIndexRedirect", () => {
  it("sends the bare Agents page to Connected agents", async () => {
    renderAt("/dashboard/agents");
    await screen.findByTestId("connected-list");
    expect(currentLocation.pathname).toBe("/dashboard/agents/connected");
    expect(currentLocation.search).toBe("");
  });

  it("sends a legacy ?tab=playground link to the Playground path", async () => {
    renderAt("/dashboard/agents?tab=playground");
    await screen.findByTestId("playground-list");
    expect(currentLocation.pathname).toBe("/dashboard/agents/playground");
    expect(currentLocation.search).toBe("");
  });

  it("sends an unknown legacy tab to Connected agents", async () => {
    renderAt("/dashboard/agents?tab=garbage");
    await screen.findByTestId("connected-list");
    expect(currentLocation.pathname).toBe("/dashboard/agents/connected");
  });

  it("keeps other query params when it redirects", async () => {
    renderAt("/dashboard/agents?tab=connected&moved=agent-definitions");
    await screen.findByTestId("connected-list");
    expect(currentLocation.pathname).toBe("/dashboard/agents/connected");
    expect(currentLocation.search).toBe("?moved=agent-definitions");
  });
});

describe("AgentsLanding", () => {
  it("shows Connected agents with no tabs", () => {
    renderAt("/dashboard/agents/connected");
    expect(screen.getByTestId("connected-list")).toBeInTheDocument();
    expect(screen.queryAllByRole("tab")).toHaveLength(0);
    expect(screen.queryByText("Agent Playground")).toBeNull();
  });

  it("opens the environments build page from Connect agent", () => {
    renderAt("/dashboard/agents/connected");
    fireEvent.click(screen.getByRole("button", { name: "Connect agent" }));
    expect(currentLocation.pathname).toBe("/dashboard/simulate/environments");
    expect(currentLocation.search).toBe("?tab=build");
  });

  it("sends the empty state's Connect an agent to the environments build page", () => {
    renderAt("/dashboard/agents/connected");
    fireEvent.click(screen.getByRole("button", { name: "Connect an agent" }));
    expect(currentLocation.pathname).toBe("/dashboard/simulate/environments");
    expect(currentLocation.search).toBe("?tab=build");
  });

  it("explains the page in a CustomTooltip on the header info icon", () => {
    renderAt("/dashboard/agents/connected");
    const tip = screen
      .getAllByTestId("custom-tooltip")
      .find((t) => t.dataset.title?.startsWith("Connect agents"));
    expect(tip).toBeDefined();
    expect(tip).toHaveAttribute("data-show", "true");
    expect(tip.dataset.title).toBe(
      "Connect agents that already run somewhere else to test them in simulations.",
    );
  });
});

describe("Playground list page", () => {
  it("sets the browser tab title", async () => {
    document.title = "";
    renderAt("/dashboard/agents/playground");
    await waitFor(() => expect(document.title).toBe("Agents"));
  });

  it("opens by URL as its own page, outside the Agents landing", () => {
    renderAt("/dashboard/agents/playground");
    expect(screen.getByTestId("playground-list")).toBeInTheDocument();
    expect(screen.queryByText("Agents")).toBeNull();
    expect(screen.queryByRole("button", { name: "Connect agent" })).toBeNull();
  });
});
