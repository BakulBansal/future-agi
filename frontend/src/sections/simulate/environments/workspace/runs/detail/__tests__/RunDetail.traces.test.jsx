import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "src/utils/test-utils";

const mocks = vi.hoisted(() => ({
  runTraces: vi.fn(),
  store: {},
  drawerProps: vi.fn(),
}));

vi.mock("src/api/simulate-environments/runDetail", async (importOriginal) => ({
  ...(await importOriginal()),
  useRunTraces: (...args) => mocks.runTraces(...args),
}));

vi.mock("src/sections/projects/LLMTracing/states", () => ({
  useLLMTracingStoreShallow: (selector) => selector(mocks.store),
}));

vi.mock("src/sections/projects/LLMTracing/LLMTracingTraceDetailDrawer", () => ({
  default: (props) => {
    mocks.drawerProps(props);
    return <div data-testid="trace-drawer" />;
  },
}));

import RunTracesSection from "../RunTracesSection";

const TRACES = [
  {
    traceId: "trace-2",
    projectId: "project-1",
    name: "book_ride",
    startTime: "2026-10-15T10:02:00Z",
    status: "OK",
    latencyMs: 1830,
  },
  {
    traceId: "trace-1",
    projectId: "project-1",
    name: "verify_identity",
    startTime: "2026-10-15T10:01:00Z",
    status: "ERROR",
    latencyMs: 420,
  },
];

const page = (traces, { count = traces.length, totalPages = 1 } = {}) => ({
  observability: "on",
  agentId: "agent-1",
  projectId: "project-1",
  traces,
  count,
  totalPages,
});

describe("RunTracesSection", () => {
  beforeEach(() => {
    mocks.runTraces.mockReset();
    mocks.drawerProps.mockReset();
    mocks.store = {
      setTraceDetailDrawerOpen: vi.fn(),
      setVisibleTraces: vi.fn(),
      resetStates: vi.fn(),
    };
  });

  it("lists the run's traces and keeps the drawer's prev/next to those rows", () => {
    mocks.runTraces.mockReturnValue(page(TRACES));

    render(<RunTracesSection executionId="exec-1" />);

    expect(mocks.runTraces).toHaveBeenCalledWith("exec-1", { page: 1 });
    expect(screen.getByText("Traces")).toBeInTheDocument();
    expect(screen.getByText("2 traces")).toBeInTheDocument();
    expect(screen.getByText("book_ride")).toBeInTheDocument();
    expect(screen.getByText("verify_identity")).toBeInTheDocument();
    expect(screen.getByText("1,830 ms")).toBeInTheDocument();
    expect(mocks.store.setVisibleTraces).toHaveBeenLastCalledWith([
      { traceId: "trace-2", projectId: "project-1" },
      { traceId: "trace-1", projectId: "project-1" },
    ]);
  });

  it("opens the trace drawer read-only on the clicked row, pinned to the agent's project", () => {
    mocks.runTraces.mockReturnValue(page(TRACES));

    render(<RunTracesSection executionId="exec-1" />);
    fireEvent.click(screen.getByText("verify_identity"));

    expect(mocks.store.setTraceDetailDrawerOpen).toHaveBeenCalledWith({
      traceId: "trace-1",
      projectId: "project-1",
    });
    expect(screen.getByTestId("trace-drawer")).toBeInTheDocument();
    expect(mocks.drawerProps.mock.lastCall[0]).toMatchObject({
      readOnly: true,
    });
  });

  it("clears the trace drawer state when the section unmounts", () => {
    mocks.runTraces.mockReturnValue(page(TRACES));

    const { unmount } = render(<RunTracesSection executionId="exec-1" />);
    expect(mocks.store.resetStates).not.toHaveBeenCalled();
    unmount();

    expect(mocks.store.resetStates).toHaveBeenCalledTimes(1);
  });

  it("says when the run has no traces", () => {
    mocks.runTraces.mockReturnValue(page([]));

    render(<RunTracesSection executionId="exec-1" />);

    expect(screen.getByText("No traces for this run")).toBeInTheDocument();
    expect(
      screen.getByText(
        /Observability is on, but this agent's code didn't send/,
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: /Enable observability/ }),
    ).toBeNull();
    expect(screen.getByText("0 traces")).toBeInTheDocument();
  });

  it("renders nothing when the run has no traceable agent project", () => {
    mocks.runTraces.mockReturnValue({
      ...page([]),
      observability: "unavailable",
      agentId: null,
      projectId: null,
    });

    const { container } = render(<RunTracesSection executionId="exec-1" />);

    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByTestId("trace-drawer")).toBeNull();
    expect(mocks.store.setVisibleTraces).not.toHaveBeenCalled();
  });

  it("shows the whole count and pages through the run's traces", () => {
    mocks.runTraces.mockImplementation((_id, { page: current }) =>
      page(current === 1 ? TRACES : [TRACES[1]], { count: 23, totalPages: 2 }),
    );

    render(<RunTracesSection executionId="exec-1" />);

    expect(screen.getByText("23 traces")).toBeInTheDocument();
    expect(screen.getByText("Showing 1–2 of 23")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Go to page 2" }));

    expect(mocks.runTraces).toHaveBeenLastCalledWith("exec-1", { page: 2 });
    expect(screen.getByText("Showing 21–21 of 23")).toBeInTheDocument();
    expect(mocks.store.setVisibleTraces).toHaveBeenLastCalledWith([
      { traceId: "trace-1", projectId: "project-1" },
    ]);
  });

  it("hides the pager when everything fits on one page", () => {
    mocks.runTraces.mockReturnValue(page(TRACES));

    render(<RunTracesSection executionId="exec-1" />);

    expect(screen.queryByRole("navigation")).toBeNull();
  });

  it("asks to turn observability on, linking to the agent's settings", () => {
    mocks.runTraces.mockReturnValue({
      ...page([]),
      observability: "off",
      projectId: null,
    });

    render(<RunTracesSection executionId="exec-1" />);

    expect(
      screen.getByText("Observability is off for this agent"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /Enable observability/ }),
    ).toHaveAttribute("href", "/dashboard/simulate/agent-definitions/agent-1");
    expect(screen.queryByText("No traces for this run")).toBeNull();
    expect(screen.queryByTestId("trace-drawer")).toBeNull();
    expect(mocks.store.setVisibleTraces).not.toHaveBeenCalled();
  });
});
