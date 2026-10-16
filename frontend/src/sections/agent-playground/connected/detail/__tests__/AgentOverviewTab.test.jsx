import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { toRun } from "src/api/agents/agentDetail";
import {
  ENVIRONMENT_RUN,
  SIMULATE_RUN,
} from "src/api/agents/_fixtures/agentDetail";
import AgentOverviewTab from "../AgentOverviewTab";

const runsState = { current: {} };

vi.mock("src/api/agents/agentDetail", async () => {
  const actual = await vi.importActual("src/api/agents/agentDetail");
  return { ...actual, useAgentRuns: () => runsState.current };
});

let currentLocation;
const LocationProbe = () => {
  currentLocation = useLocation();
  return null;
};

const renderTab = () =>
  render(
    <MemoryRouter initialEntries={["/dashboard/agents/connected/a1/overview"]}>
      <Routes>
        <Route
          path="/dashboard/agents/connected/:agentId/overview"
          element={<AgentOverviewTab />}
        />
        <Route path="*" element={null} />
      </Routes>
      <LocationProbe />
    </MemoryRouter>,
  );

const loaded = (raw) => ({
  isLoading: false,
  isError: false,
  data: raw.map(toRun),
});

beforeEach(() => {
  runsState.current = loaded([ENVIRONMENT_RUN, SIMULATE_RUN]);
});

describe("AgentOverviewTab", () => {
  it("shows only Recent runs with the ticket's columns", () => {
    renderTab();
    expect(screen.getByText("Recent runs")).toBeInTheDocument();
    const headers = screen
      .getAllByRole("columnheader")
      .map((h) => h.textContent.trim());
    expect(headers).toEqual([
      "Run",
      "Environment",
      "Agent version",
      "Pass rate",
      "When",
    ]);
  });

  it("shows an environment run's number, environment, version and pass rate", () => {
    renderTab();
    expect(screen.getByText("Run 2")).toBeInTheDocument();
    expect(screen.getByText("ride-voice-agent")).toBeInTheDocument();
    expect(screen.getByText("v3")).toBeInTheDocument();
    expect(
      screen.getByTestId("pass-rate-68dedb99-42b6-46e7-8e94-b511041fa2cb"),
    ).toHaveTextContent("30% 3/10");
  });

  it("marks a running Simulate run and gives it no pass rate", () => {
    renderTab();
    expect(screen.getByText("Old refill check")).toBeInTheDocument();
    expect(screen.getByText("Simulate")).toBeInTheDocument();
    expect(screen.getByText("Running")).toBeInTheDocument();
    expect(
      screen.getByTestId("pass-rate-c766900d-00e6-41c7-b922-c4e8eed1e7ec"),
    ).toHaveTextContent("—");
    expect(screen.getByText(/^started /)).toBeInTheDocument();
  });

  it("treats an evaluating run as still running, with no final rate", () => {
    runsState.current = loaded([{ ...ENVIRONMENT_RUN, status: "evaluating" }]);
    renderTab();
    expect(screen.getByText("Running")).toBeInTheDocument();
    expect(
      screen.getByTestId("pass-rate-68dedb99-42b6-46e7-8e94-b511041fa2cb"),
    ).toHaveTextContent("—");
  });

  it("tags failed and cancelled runs", () => {
    runsState.current = loaded([
      { ...ENVIRONMENT_RUN, status: "failed" },
      {
        ...SIMULATE_RUN,
        status: "cancelled",
        completed_at: "2026-10-16T11:00:00Z",
      },
    ]);
    renderTab();
    expect(screen.getByText("Failed")).toBeInTheDocument();
    expect(screen.getByText("Cancelled")).toBeInTheDocument();
  });

  it("opens an environment run on its environment's run page", () => {
    renderTab();
    fireEvent.click(screen.getByText("Run 2"));
    expect(currentLocation.pathname).toBe(
      "/dashboard/simulate/environments/e5391b8a-156e-46dd-b5a4-4761971f2ccd/runs/b741b14e-91c0-403d-81e5-5dd7e40d09c7/68dedb99-42b6-46e7-8e94-b511041fa2cb",
    );
  });

  it("opens a Simulate run on the Simulate run page", () => {
    renderTab();
    fireEvent.click(screen.getByText("Run 1"));
    expect(currentLocation.pathname).toBe(
      "/dashboard/simulate/test/c8cc6400-c677-4efd-9ad0-d177c912dad4/c766900d-00e6-41c7-b922-c4e8eed1e7ec",
    );
  });

  it("says when nothing has run yet", () => {
    runsState.current = loaded([]);
    renderTab();
    expect(
      screen.getByText("No simulations have run against this agent yet."),
    ).toBeInTheDocument();
  });

  it("says runs aren't available yet while the endpoint is missing", () => {
    runsState.current = {
      isLoading: false,
      isError: true,
      error: {
        code: "runs_unavailable",
        message: "Recent runs aren't available yet",
      },
    };
    renderTab();
    expect(
      screen.getByText("Recent runs aren't available yet"),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("No simulations have run against this agent yet."),
    ).toBeNull();
  });

  it("shows other load errors as errors, not as no runs", () => {
    runsState.current = { isLoading: false, isError: true, error: {} };
    renderTab();
    expect(screen.getByText("Couldn't load recent runs")).toBeInTheDocument();
  });
});
