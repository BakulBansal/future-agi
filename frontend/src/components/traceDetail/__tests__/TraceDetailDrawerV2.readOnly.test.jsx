import React from "react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "src/utils/test-utils";

const mocks = vi.hoisted(() => ({
  getSavedViews: vi.fn(() => ({ data: undefined })),
  getTraceDetail: vi.fn(() => ({ data: undefined, isLoading: true })),
  toolbarProps: vi.fn(),
}));

vi.mock("src/api/project/saved-views", async (importOriginal) => ({
  ...(await importOriginal()),
  useGetSavedViews: (...args) => mocks.getSavedViews(...args),
}));

vi.mock("src/api/project/trace-detail", () => ({
  useGetTraceDetail: (...args) => mocks.getTraceDetail(...args),
}));

vi.mock("../DrawerToolbar", () => ({
  default: (props) => {
    mocks.toolbarProps(props);
    return null;
  },
}));

import TraceDetailDrawerV2 from "../TraceDetailDrawerV2";

const renderDrawer = (props) =>
  render(
    <QueryClientProvider client={new QueryClient()}>
      <TraceDetailDrawerV2
        traceId="trace-1"
        open
        onClose={vi.fn()}
        projectId="project-1"
        {...props}
      />
    </QueryClientProvider>,
  );

describe("TraceDetailDrawerV2 read-only", () => {
  beforeEach(() => {
    mocks.getSavedViews.mockClear();
    mocks.getTraceDetail.mockClear();
    mocks.toolbarProps.mockClear();
  });

  it("reads the project's saved views and offers to edit them when it can act", () => {
    renderDrawer();

    expect(mocks.getSavedViews).toHaveBeenLastCalledWith("project-1");
    expect(mocks.toolbarProps.mock.lastCall[0].readOnly).toBe(false);
    expect(mocks.getTraceDetail).toHaveBeenLastCalledWith("trace-1", {
      projectId: "project-1",
    });
  });

  it("asks for no saved views and locks the toolbar", () => {
    renderDrawer({ readOnly: true });

    expect(mocks.getSavedViews).toHaveBeenLastCalledWith(null);
    expect(mocks.toolbarProps.mock.lastCall[0].readOnly).toBe(true);
    expect(mocks.getTraceDetail).toHaveBeenLastCalledWith("trace-1", {
      projectId: "project-1",
    });
  });
});
