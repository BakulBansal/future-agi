import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { HelmetProvider } from "react-helmet-async";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import axios from "src/utils/axios";
import {
  CODE_AGENT_DETAIL,
  HOSTED_AGENT_DETAIL,
} from "src/api/agents/_fixtures/agentDetail";
import AgentDetailLayout from "../AgentDetailLayout";
import AgentDetailIndexRedirect from "../AgentDetailIndexRedirect";
import { agentDetailPath } from "../agentDetailTabs";

vi.mock("src/utils/axios", () => ({
  default: { get: vi.fn(), put: vi.fn() },
  endpoints: {
    agentDefinitions: {
      detail: (id) => `/simulate/agent-definitions/${id}/`,
      versionDetail: (id, v) =>
        `/simulate/agent-definitions/${id}/versions/${v}/`,
      edit: (id) => `/simulate/agent-definitions/${id}/edit/`,
    },
  },
}));

let currentLocation;
const LocationProbe = () => {
  currentLocation = useLocation();
  return null;
};

const Tab = ({ name }) => <div data-testid={`tab-${name}`} />;

// Mirrors the connected/:agentId block in routes/sections/dashboard.jsx.
const renderAt = (url) => {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <HelmetProvider>
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[url]}>
          <Routes>
            <Route
              path="/dashboard/agents/connected/:agentId"
              element={<AgentDetailLayout />}
            >
              <Route index element={<AgentDetailIndexRedirect />} />
              <Route path="overview" element={<Tab name="overview" />} />
              <Route path="definition" element={<Tab name="definition" />} />
              <Route path="versions" element={<Tab name="versions" />} />
              <Route
                path="environments"
                element={<Tab name="environments" />}
              />
            </Route>
            <Route path="*" element={<div data-testid="elsewhere" />} />
          </Routes>
          <LocationProbe />
        </MemoryRouter>
      </QueryClientProvider>
    </HelmetProvider>,
  );
  return client;
};

const serve = (detail) =>
  axios.get.mockImplementation((url) =>
    url.includes(detail.id)
      ? Promise.resolve({ data: detail })
      : Promise.reject(new Error(`unexpected ${url}`)),
  );

const HOSTED = HOSTED_AGENT_DETAIL;
const CODE = CODE_AGENT_DETAIL;
const base = (a) => `/dashboard/agents/connected/${a.id}`;

beforeEach(() => {
  axios.get.mockReset();
});

describe("agentDetailTabs", () => {
  it("builds tab paths", () => {
    expect(agentDetailPath("a1")).toBe(
      "/dashboard/agents/connected/a1/overview",
    );
    expect(agentDetailPath("a1", "definition")).toBe(
      "/dashboard/agents/connected/a1/definition",
    );
  });
});

describe("AgentDetailLayout", () => {
  it("opens the bare agent URL on Overview", async () => {
    serve(HOSTED);
    renderAt(base(HOSTED));
    await screen.findByTestId("tab-overview");
    expect(currentLocation.pathname).toBe(`${base(HOSTED)}/overview`);
  });

  it("shows the hosted agent's header", async () => {
    serve(HOSTED);
    renderAt(`${base(HOSTED)}/overview`);
    expect(
      await screen.findByRole("heading", { name: "Northwind Pharmacy" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Vapi")).toBeInTheDocument();
    const subline = screen.getByTestId("agent-detail-subline");
    expect(subline).toHaveTextContent(HOSTED.assistant_id);
    expect(subline).toHaveTextContent("Created 13-10-2026");
    expect(subline).toHaveTextContent("No environments");
  });

  it("shows GitHub and the environment count for a code agent", async () => {
    serve(CODE);
    renderAt(`${base(CODE)}/overview`);
    const subline = await screen.findByTestId("agent-detail-subline");
    expect(subline).toHaveTextContent("GitHub");
    expect(subline).toHaveTextContent("1 environment");
  });

  it("leaves out pieces today's backend doesn't send", async () => {
    const today = { ...HOSTED, provider: null };
    [
      "agent_kind",
      "source",
      "environment_count",
      "observability_enabled",
    ].forEach((k) => delete today[k]);
    serve(today);
    renderAt(`${base(HOSTED)}/overview`);
    const subline = await screen.findByTestId("agent-detail-subline");
    expect(subline).not.toHaveTextContent(/environment/i);
  });

  it("goes back to Connected agents", async () => {
    serve(HOSTED);
    renderAt(`${base(HOSTED)}/overview`);
    fireEvent.click(await screen.findByRole("button", { name: "Back" }));
    expect(currentLocation.pathname).toBe("/dashboard/agents/connected");
  });

  it.each([
    ["hosted", HOSTED],
    ["code", CODE],
  ])("gives a %s agent both Versions and Environments", async (_, agent) => {
    serve(agent);
    renderAt(`${base(agent)}/overview`);
    await screen.findByRole("tab", { name: "Overview" });
    expect(screen.getByRole("tab", { name: /^Versions/ })).toBeInTheDocument();
    expect(
      screen.getByRole("tab", { name: /^Environments/ }),
    ).toBeInTheDocument();
  });

  it("shows Constraints, Improvements and Connections as disabled Coming soon tabs", async () => {
    serve(HOSTED);
    renderAt(`${base(HOSTED)}/overview`);
    await screen.findByRole("tab", { name: "Overview" });
    ["Constraints", "Improvements", "Connections"].forEach((name) => {
      const tab = screen.getByRole("tab", { name: new RegExp(name) });
      expect(tab).toBeDisabled();
      expect(within(tab).getByLabelText("Coming soon")).toBeInTheDocument();
    });
  });

  it("opens Environments on a hosted agent", async () => {
    serve(HOSTED);
    renderAt(`${base(HOSTED)}/environments`);
    await screen.findByTestId("tab-environments");
    expect(currentLocation.pathname).toBe(`${base(HOSTED)}/environments`);
  });

  it("counts versions from version_count, not the loaded list", async () => {
    serve({ ...HOSTED, version_count: 12 });
    renderAt(`${base(HOSTED)}/overview`);
    expect(
      await screen.findByRole("tab", { name: "Versions 12" }),
    ).toBeInTheDocument();
  });

  it("has no version selector", async () => {
    serve(HOSTED);
    renderAt(`${base(HOSTED)}/overview`);
    await screen.findByRole("tab", { name: "Overview" });
    expect(screen.queryByRole("combobox")).toBeNull();
  });

  it("switches tabs by path", async () => {
    serve(HOSTED);
    renderAt(`${base(HOSTED)}/overview`);
    fireEvent.click(await screen.findByRole("tab", { name: "Definition" }));
    await screen.findByTestId("tab-definition");
    expect(currentLocation.pathname).toBe(`${base(HOSTED)}/definition`);
  });

  it("keeps the page when a background refetch fails", async () => {
    serve(HOSTED);
    const client = renderAt(`${base(HOSTED)}/overview`);
    await screen.findByRole("heading", { name: "Northwind Pharmacy" });
    axios.get.mockRejectedValue({ message: "Server error" });
    await act(async () => {
      await client.refetchQueries();
    });
    await waitFor(() =>
      expect(client.getQueryState(["agents", "detail", HOSTED.id]).status).toBe(
        "error",
      ),
    );
    expect(
      screen.getByRole("heading", { name: "Northwind Pharmacy" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Server error")).toBeNull();
  });

  it("shows the server's message when the agent can't be loaded", async () => {
    axios.get.mockRejectedValue({ message: "Agent definition not found" });
    renderAt(`${base(HOSTED)}/overview`);
    expect(
      await screen.findByText("Agent definition not found"),
    ).toBeInTheDocument();
  });
});
