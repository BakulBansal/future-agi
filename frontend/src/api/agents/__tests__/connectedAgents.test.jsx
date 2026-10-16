import { describe, it, expect } from "vitest";
import PropTypes from "prop-types";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  connectedAgentsQueryKey,
  mapConnectedAgent,
  useConnectedAgents,
} from "../connectedAgents";
import { CONNECTED_AGENTS_FIXTURE } from "../_fixtures/connectedAgents";

const makeWrapper = () => {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const Wrapper = ({ children }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  Wrapper.propTypes = { children: PropTypes.node };
  return { queryClient, Wrapper };
};

const load = async (params) => {
  const { Wrapper } = makeWrapper();
  const { result } = renderHook(() => useConnectedAgents(params), {
    wrapper: Wrapper,
  });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  return result.current.data;
};

describe("useConnectedAgents", () => {
  it("serves the first page and the full total", async () => {
    const data = await load({ page: 0, pageSize: 25, search: "" });
    expect(data.items).toHaveLength(25);
    expect(data.total).toBe(37);
  });

  it("serves the remainder on the second page", async () => {
    const data = await load({ page: 1, pageSize: 25, search: "" });
    expect(data.items).toHaveLength(12);
    expect(data.total).toBe(37);
  });

  it("filters by name or description, case-insensitively", async () => {
    const data = await load({ page: 0, pageSize: 25, search: "SUPPORT" });
    const matching = CONNECTED_AGENTS_FIXTURE.filter((a) =>
      `${a.name} ${a.description || ""}`.toLowerCase().includes("support"),
    );
    expect(matching.length).toBeGreaterThan(0);
    expect(data.total).toBe(matching.length);
    data.items.forEach((a) =>
      expect(`${a.name} ${a.description || ""}`.toLowerCase()).toContain(
        "support",
      ),
    );
  });

  it("does not hand out the fixture's own objects", async () => {
    const first = await load({ page: 0, pageSize: 25, search: "" });
    first.items[0].usedIn.push({ id: "x", name: "mutated" });
    const second = await load({ page: 0, pageSize: 25, search: "" });
    expect(second.items[0].usedIn).not.toContainEqual({
      id: "x",
      name: "mutated",
    });
  });

  it("keys the query by page, size and search", () => {
    expect(
      connectedAgentsQueryKey({ page: 2, pageSize: 50, search: "a" }),
    ).toEqual(["agents", "connected", { page: 2, pageSize: 50, search: "a" }]);
  });
});

describe("mapConnectedAgent", () => {
  it("camel-cases the API fields and keeps nulls", () => {
    expect(
      mapConnectedAgent({
        id: "a1",
        name: "Support",
        description: null,
        modality: "voice",
        direction: "inbound",
        source: "livekit",
        source_label: "LiveKit",
        latest_version: { id: "v-3", label: "v3" },
        version_count: 5,
        used_in: [{ id: "e1", name: "Support line" }],
        created_at: "2026-10-01T10:00:00Z",
      }),
    ).toEqual({
      id: "a1",
      name: "Support",
      description: null,
      modality: "voice",
      direction: "inbound",
      source: "livekit",
      sourceLabel: "LiveKit",
      latestVersionId: "v-3",
      latestLabel: "v3",
      versionCount: 5,
      usedIn: [{ id: "e1", name: "Support line" }],
      createdAt: "2026-10-01T10:00:00Z",
    });
  });

  it("tolerates a missing source label, version and environments", () => {
    const a = mapConnectedAgent({ id: "a2", name: "Bare" });
    expect(a.sourceLabel).toBeNull();
    expect(a.latestVersionId).toBeNull();
    expect(a.latestLabel).toBeNull();
    expect(a.versionCount).toBe(0);
    expect(a.usedIn).toEqual([]);
  });
});
