import { describe, it, expect, beforeEach } from "vitest";
import {
  CODE_AGENT_DETAIL,
  ENVIRONMENT_RUN,
  HOSTED_AGENT_DETAIL,
  HOSTED_VERSION_DETAIL,
  STORED_AGENT_DETAILS,
} from "../_fixtures/agentDetail";
import {
  mockAgentEnvironments,
  mockAgentRuns,
  mockSetObservability,
  resetContractMocks,
  withDetailContractFields,
  withVersionContractFields,
} from "../contractMocks";

const keysOf = (o) => Object.keys(o).sort();

// Today's backend: the detail without the contract's new fields.
const todayDetail = (detail) => {
  const d = { ...detail };
  [
    "agent_kind",
    "source",
    "environment_count",
    "observability_enabled",
  ].forEach((k) => delete d[k]);
  return d;
};

const sdkAgent = { ...todayDetail(CODE_AGENT_DETAIL), provider: null };
const hostedAgent = todayDetail(HOSTED_AGENT_DETAIL);

beforeEach(() => resetContractMocks());

describe("withDetailContractFields", () => {
  it("never replaces a field the backend sent", () => {
    expect(withDetailContractFields(CODE_AGENT_DETAIL)).toEqual(
      CODE_AGENT_DETAIL,
    );
  });

  it("makes an agent without a provider a code agent from GitHub or an upload", () => {
    const d = withDetailContractFields(sdkAgent);
    expect(d.agent_kind).toBe("source");
    expect(["github", "archive"]).toContain(d.source);
    expect(d.environment_count).toBeGreaterThan(0);
    expect(typeof d.observability_enabled).toBe("boolean");
  });

  it("keeps a hosted agent hosted, sourced from its provider", () => {
    const d = withDetailContractFields(hostedAgent);
    expect(d.agent_kind).toBeNull();
    expect(d.source).toBe("vapi");
    expect(d.environment_count).toBe(0);
  });

  it("gives the same agent the same answers", () => {
    expect(withDetailContractFields(sdkAgent)).toEqual(
      withDetailContractFields(sdkAgent),
    );
  });
});

describe("mockAgentRuns", () => {
  it("returns contract-shaped runs, newest first, capped at the limit", () => {
    const agent = withDetailContractFields(sdkAgent);
    const { results } = mockAgentRuns(agent, 5);
    expect(results.length).toBeGreaterThan(0);
    expect(results.length).toBeLessThanOrEqual(5);
    results.forEach((r) => expect(keysOf(r)).toEqual(keysOf(ENVIRONMENT_RUN)));
    const times = results.map((r) => Date.parse(r.started_at));
    expect([...times].sort((a, b) => b - a)).toEqual(times);
  });

  it("fills up to the limit asked for", () => {
    const counts = Array.from(
      { length: 20 },
      (_, i) =>
        mockAgentRuns(
          withDetailContractFields({ ...sdkAgent, id: `agent-${i}` }),
          15,
        ).results.length,
    );
    expect(Math.max(...counts)).toBeGreaterThan(5);
    expect(Math.max(...counts)).toBeLessThanOrEqual(15);
  });

  it("numbers runs from the oldest, so the newest has the highest number", () => {
    const agent = withDetailContractFields(sdkAgent);
    const { results } = mockAgentRuns(agent, 5);
    expect(results.map((r) => r.run_number)).toEqual(
      results.map((_, i) => results.length - i),
    );
  });

  it("only uses the agent's own versions", () => {
    const agent = withDetailContractFields(sdkAgent);
    const ids = agent.versions.map((v) => v.id);
    mockAgentRuns(agent, 5).results.forEach((r) => {
      if (r.agent_version) expect(ids).toContain(r.agent_version.id);
    });
  });

  it("gives Simulate runs no environment and no outcomes", () => {
    const agent = withDetailContractFields(hostedAgent);
    mockAgentRuns(agent, 5)
      .results.filter((r) => r.kind === "simulate")
      .forEach((r) => {
        expect(r.environment).toBeNull();
        expect(r.outcome_passed).toBeNull();
      });
  });
});

describe("mockAgentEnvironments", () => {
  it("lists environment_count environments, each with a contract-shaped last run or none", () => {
    const agent = withDetailContractFields(sdkAgent);
    const page = mockAgentEnvironments(agent);
    expect(page.count).toBe(agent.environment_count);
    expect(page.results).toHaveLength(agent.environment_count);
    page.results.forEach((e) => {
      expect(typeof e.id).toBe("string");
      expect(typeof e.name).toBe("string");
      if (e.last_run)
        expect(keysOf(e.last_run)).toEqual(keysOf(ENVIRONMENT_RUN));
    });
  });
});

describe("withVersionContractFields", () => {
  it("never replaces fields the backend sent", () => {
    expect(
      withVersionContractFields(HOSTED_VERSION_DETAIL, hostedAgent),
    ).toEqual(HOSTED_VERSION_DETAIL);
  });

  it("gives a code agent's version a stored Agent shaped like the contract", () => {
    const agent = withDetailContractFields(sdkAgent);
    const v = { ...HOSTED_VERSION_DETAIL };
    delete v.agent_kind;
    delete v.agent_details;
    delete v.api_key;
    const out = withVersionContractFields(v, agent);
    expect(out.agent_kind).toBe("source");
    expect(keysOf(out.agent_details)).toEqual(keysOf(STORED_AGENT_DETAILS));
    expect(keysOf(out.agent_details.agent)).toEqual(
      keysOf(STORED_AGENT_DETAILS.agent),
    );
    expect(out.agent_details.agent.provider_ref).toBeNull();
    expect(out.api_key).toBeNull();
  });

  it("gives a hosted version a masked key and no stored Agent", () => {
    const v = { ...HOSTED_VERSION_DETAIL };
    delete v.agent_kind;
    delete v.agent_details;
    delete v.api_key;
    const out = withVersionContractFields(
      v,
      withDetailContractFields(hostedAgent),
    );
    expect(out.agent_kind).toBeNull();
    expect(out.agent_details).toBeNull();
    expect(out.api_key).toMatch(/^\w{2}-\w\.\.\.\w{4}$/);
  });
});

describe("mockSetObservability", () => {
  it("remembers the switch for the agent", () => {
    const agent = withDetailContractFields(hostedAgent);
    mockSetObservability(agent.id, !agent.observability_enabled);
    expect(withDetailContractFields(hostedAgent).observability_enabled).toBe(
      !agent.observability_enabled,
    );
  });
});
