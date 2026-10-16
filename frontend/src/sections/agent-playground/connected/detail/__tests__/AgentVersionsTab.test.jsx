import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import axios from "src/utils/axios";
import { HOSTED_AGENT_DETAIL } from "src/api/agents/_fixtures/agentDetail";
import AgentVersionsTab from "../AgentVersionsTab";

vi.mock("src/utils/axios", () => ({
  default: { get: vi.fn() },
  endpoints: {
    agentDefinitions: { detail: (id) => `/simulate/agent-definitions/${id}/` },
  },
}));

const renderTab = () =>
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <MemoryRouter
        initialEntries={[
          `/dashboard/agents/connected/${HOSTED_AGENT_DETAIL.id}/versions`,
        ]}
      >
        <Routes>
          <Route
            path="/dashboard/agents/connected/:agentId/versions"
            element={<AgentVersionsTab />}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );

beforeEach(() => {
  axios.get.mockReset();
  axios.get.mockResolvedValue({ data: HOSTED_AGENT_DETAIL });
});

describe("AgentVersionsTab", () => {
  it("lists every version, newest first", async () => {
    renderTab();
    expect(await screen.findByText("2 versions")).toBeInTheDocument();
    const entries = screen.getAllByTestId("version-entry");
    expect(
      entries.map((e) => within(e).getByTestId("version-label").textContent),
    ).toEqual(["v2", "v1"]);
  });

  it("marks the active and latest version", async () => {
    renderTab();
    const [newest, oldest] = await screen.findAllByTestId("version-entry");
    expect(within(newest).getByText("Latest")).toBeInTheDocument();
    expect(within(newest).getByText("Active")).toBeInTheDocument();
    expect(within(oldest).queryByText("Latest")).toBeNull();
    expect(within(oldest).queryByText("Active")).toBeNull();
  });

  it("shows each version's commit message and date", async () => {
    renderTab();
    const [newest] = await screen.findAllByTestId("version-entry");
    expect(within(newest).getByText("Shorter greeting")).toBeInTheDocument();
    expect(within(newest).getByText(/15-10-2026/)).toBeInTheDocument();
  });

  it("is read-only", async () => {
    renderTab();
    await screen.findByText("2 versions");
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("marks Compare as coming soon", async () => {
    renderTab();
    expect(await screen.findByText("Compare")).toBeInTheDocument();
    expect(screen.getByLabelText("Coming soon")).toBeInTheDocument();
  });
});
