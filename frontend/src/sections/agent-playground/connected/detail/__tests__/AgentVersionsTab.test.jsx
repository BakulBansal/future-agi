import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import axios from "src/utils/axios";
import { HOSTED_AGENT_DETAIL } from "src/api/agents/_fixtures/agentDetail";
import AgentVersionsTab from "../AgentVersionsTab";

vi.mock("src/utils/axios", () => ({
  default: { get: vi.fn() },
  endpoints: {
    agentDefinitions: {
      detail: (id) => `/simulate/agent-definitions/${id}/`,
      versions: (id) => `/simulate/agent-definitions/${id}/versions/`,
    },
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

// The paginated versions list, newest first, as GET .../versions/ sends it.
const [V2, V1] = [...HOSTED_AGENT_DETAIL.versions].sort(
  (a, b) => b.version_number - a.version_number,
);
const servePages = (pages) =>
  axios.get.mockImplementation((url, config) => {
    if (url.endsWith("/versions/")) {
      const page = config.params.page;
      return Promise.resolve({
        data: {
          count: pages.flat().length,
          next: page < pages.length ? "next" : null,
          previous: null,
          total_pages: pages.length,
          current_page: page,
          results: pages[page - 1],
        },
      });
    }
    return Promise.resolve({ data: HOSTED_AGENT_DETAIL });
  });

beforeEach(() => {
  axios.get.mockReset();
  servePages([[V2, V1]]);
});

describe("AgentVersionsTab", () => {
  it("reads versions from the paginated versions list", async () => {
    renderTab();
    await screen.findAllByTestId("version-entry");
    expect(axios.get).toHaveBeenCalledWith(
      `/simulate/agent-definitions/${HOSTED_AGENT_DETAIL.id}/versions/`,
      { params: { page: 1, limit: 10 } },
    );
  });

  it("shows skeleton rows while the first page loads", async () => {
    axios.get.mockImplementation((url) =>
      url.endsWith("/versions/")
        ? new Promise(() => {})
        : Promise.resolve({ data: HOSTED_AGENT_DETAIL }),
    );
    renderTab();
    expect(
      (await screen.findAllByTestId("version-skeleton")).length,
    ).toBeGreaterThan(0);
    expect(screen.queryByTestId("version-entry")).toBeNull();
    expect(screen.queryByText("No versions yet")).toBeNull();
  });

  it("adds skeleton rows below the list while the next page loads", async () => {
    let releasePage2;
    axios.get.mockImplementation((url, config) => {
      if (!url.endsWith("/versions/"))
        return Promise.resolve({ data: HOSTED_AGENT_DETAIL });
      if (config.params.page === 1) {
        return Promise.resolve({
          data: { count: 2, next: "next", current_page: 1, results: [V2] },
        });
      }
      return new Promise((resolve) => {
        releasePage2 = () =>
          resolve({
            data: { count: 2, next: null, current_page: 2, results: [V1] },
          });
      });
    });
    renderTab();
    await screen.findAllByTestId("version-entry");
    expect(screen.queryByTestId("version-skeleton")).toBeNull();

    const list = screen.getByTestId("versions-scroll");
    Object.defineProperty(list, "scrollHeight", {
      value: 800,
      configurable: true,
    });
    Object.defineProperty(list, "clientHeight", {
      value: 300,
      configurable: true,
    });
    Object.defineProperty(list, "scrollTop", {
      value: 500,
      configurable: true,
    });
    fireEvent.scroll(list);

    expect(
      (await screen.findAllByTestId("version-skeleton")).length,
    ).toBeGreaterThan(0);
    releasePage2();
    expect(await screen.findByText("First version")).toBeInTheDocument();
    expect(screen.queryByTestId("version-skeleton")).toBeNull();
  });

  it("loads older versions when the list is scrolled to the end", async () => {
    servePages([[V2], [V1]]);
    renderTab();
    await screen.findAllByTestId("version-entry");
    expect(screen.getAllByTestId("version-entry")).toHaveLength(1);
    expect(screen.queryByRole("button")).toBeNull();

    const list = screen.getByTestId("versions-scroll");
    Object.defineProperty(list, "scrollHeight", {
      value: 800,
      configurable: true,
    });
    Object.defineProperty(list, "clientHeight", {
      value: 300,
      configurable: true,
    });
    Object.defineProperty(list, "scrollTop", {
      value: 500,
      configurable: true,
    });
    fireEvent.scroll(list);

    expect(await screen.findByText("First version")).toBeInTheDocument();
    expect(screen.getAllByTestId("version-entry")).toHaveLength(2);
  });

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

  it("is read-only when every version is shown", async () => {
    renderTab();
    await screen.findByText("2 versions");
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("previews Compare under a Coming soon note", async () => {
    renderTab();
    const region = await screen.findByRole("region", { name: "Compare" });
    expect(
      within(region).getByText(/Compare any two versions/),
    ).toBeInTheDocument();
    expect(within(region).getByLabelText("Coming soon")).toBeInTheDocument();
    expect(within(region).getByTestId("coming-soon-preview")).toHaveTextContent(
      "prompts/system.md",
    );
  });
});
