import { describe, it, expect, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { HelmetProvider } from "react-helmet-async";
import { MemoryRouter, useLocation } from "react-router-dom";
import AgentsLanding from "../AgentsLanding";
import { PLAYGROUND_AGENTS_PATH, resolveAgentsTab } from "../agentsTabs";
import AgentListView from "../AgentListView/AgentListView";

vi.mock("../AgentView", () => ({
  default: ({ embedded }) => (
    <div data-testid="playground-list" data-embedded={String(embedded)} />
  ),
}));

vi.mock("../connected/ConnectedAgentsView", () => ({
  default: () => <div data-testid="connected-list" />,
}));

vi.mock("../components/CreateAgentsButton", () => ({
  default: () => <button type="button">Create Agents</button>,
}));

vi.mock("../hooks/useCanEditAgent", () => ({
  default: () => ({ canCreate: true, canUpdate: true, canDelete: true }),
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

let currentSearch = "";
const LocationProbe = () => {
  currentSearch = useLocation().search;
  return null;
};

const renderAt = (url, ui = <AgentsLanding />) =>
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

describe("PLAYGROUND_AGENTS_PATH", () => {
  it("opens the Agents page on the Agent Playground tab", () => {
    const url = new URL(PLAYGROUND_AGENTS_PATH, "http://localhost");
    expect(url.pathname).toBe("/dashboard/agents");
    expect(resolveAgentsTab(url.searchParams.get("tab"))).toBe("playground");
  });
});

describe("AgentsLanding", () => {
  it("lands on Connected agents with no tab in the URL", () => {
    renderAt("/dashboard/agents");
    expect(
      screen.getByRole("tab", { name: "Connected agents" }),
    ).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("connected-list")).toBeInTheDocument();
    expect(screen.queryByTestId("playground-list")).toBeNull();
  });

  it("falls back to Connected agents for an unknown tab", () => {
    renderAt("/dashboard/agents?tab=garbage");
    expect(screen.getByTestId("connected-list")).toBeInTheDocument();
  });

  it("switches to the Playground and records it in the URL", () => {
    renderAt("/dashboard/agents?tab=connected");
    fireEvent.click(screen.getByRole("tab", { name: "Agent Playground" }));
    expect(currentSearch).toBe("?tab=playground");
    expect(screen.getByTestId("playground-list")).toHaveAttribute(
      "data-embedded",
      "true",
    );
    expect(screen.queryByTestId("connected-list")).toBeNull();
  });

  it("shows the Agents header and the Create Agents button", () => {
    renderAt("/dashboard/agents");
    expect(screen.getByText("Agents")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Create Agents" }),
    ).toBeInTheDocument();
  });
});

describe("AgentListView embedded", () => {
  it("drops its own Agent Playground header and docs link", async () => {
    renderAt("/dashboard/agents?tab=playground", <AgentListView embedded />);
    await waitFor(() =>
      expect(screen.getByPlaceholderText(/search/i)).toBeInTheDocument(),
    );
    expect(screen.queryByText("Agent Playground")).toBeNull();
    expect(screen.queryByText("View Docs")).toBeNull();
  });
});
