import { describe, it, expect, vi, beforeEach } from "vitest";
import {
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
import {
  agentDetailPath,
  resolveVersionId,
  visibleDetailTabs,
} from "../agentDetailTabs";

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
const renderAt = (url) =>
  render(
    <HelmetProvider>
      <QueryClientProvider
        client={
          new QueryClient({ defaultOptions: { queries: { retry: false } } })
        }
      >
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
  it("shows Versions for agents not built in an environment", () => {
    expect(visibleDetailTabs(null)).toEqual([
      "overview",
      "definition",
      "versions",
    ]);
    expect(visibleDetailTabs("hosted")).toEqual([
      "overview",
      "definition",
      "versions",
    ]);
  });

  it("shows Environments instead for code agents built in one", () => {
    expect(visibleDetailTabs("source")).toEqual([
      "overview",
      "definition",
      "environments",
    ]);
  });

  it("builds tab paths", () => {
    expect(agentDetailPath("a1")).toBe(
      "/dashboard/agents/connected/a1/overview",
    );
    expect(agentDetailPath("a1", "definition")).toBe(
      "/dashboard/agents/connected/a1/definition",
    );
  });

  it("falls back to the active version for an unknown ?v=", () => {
    const versions = [{ id: "v2" }, { id: "v1" }];
    expect(resolveVersionId("v1", versions, "v2")).toBe("v1");
    expect(resolveVersionId("nope", versions, "v2")).toBe("v2");
    expect(resolveVersionId(null, versions, null)).toBe("v2");
    expect(resolveVersionId(null, [], null)).toBeNull();
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

  it("gives a hosted agent Versions and no Environments", async () => {
    serve(HOSTED);
    renderAt(`${base(HOSTED)}/overview`);
    await screen.findByRole("tab", { name: "Overview" });
    expect(screen.getByRole("tab", { name: /^Versions/ })).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: /^Environments/ })).toBeNull();
    expect(screen.getByRole("combobox")).toBeInTheDocument();
  });

  it("gives a code agent Environments and no Versions or version selector", async () => {
    serve(CODE);
    renderAt(`${base(CODE)}/overview`);
    await screen.findByRole("tab", { name: "Overview" });
    expect(
      screen.getByRole("tab", { name: /^Environments/ }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: /^Versions/ })).toBeNull();
    expect(screen.queryByRole("combobox")).toBeNull();
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

  it("sends a hidden tab's URL to Overview", async () => {
    serve(HOSTED);
    renderAt(`${base(HOSTED)}/environments`);
    await screen.findByTestId("tab-overview");
    expect(currentLocation.pathname).toBe(`${base(HOSTED)}/overview`);
  });

  it("switches tabs by path and keeps the chosen version", async () => {
    serve(HOSTED);
    renderAt(`${base(HOSTED)}/overview?v=v1-id`);
    fireEvent.click(await screen.findByRole("tab", { name: "Definition" }));
    await screen.findByTestId("tab-definition");
    expect(currentLocation.pathname).toBe(`${base(HOSTED)}/definition`);
    expect(currentLocation.search).toBe("?v=v1-id");
  });

  it("writes the picked version to ?v= and clears it for the active one", async () => {
    serve(HOSTED);
    renderAt(`${base(HOSTED)}/overview`);
    const select = await screen.findByRole("combobox");
    expect(select).toHaveTextContent("v2 · active");

    fireEvent.mouseDown(select);
    fireEvent.click(await screen.findByRole("option", { name: /^v1/ }));
    await waitFor(() => expect(currentLocation.search).toBe("?v=v1-id"));

    fireEvent.mouseDown(screen.getByRole("combobox"));
    fireEvent.click(await screen.findByRole("option", { name: /^v2/ }));
    await waitFor(() => expect(currentLocation.search).toBe(""));
  });

  it("shows the server's message when the agent can't be loaded", async () => {
    axios.get.mockRejectedValue({ message: "Agent definition not found" });
    renderAt(`${base(HOSTED)}/overview`);
    expect(
      await screen.findByText("Agent definition not found"),
    ).toBeInTheDocument();
  });
});
