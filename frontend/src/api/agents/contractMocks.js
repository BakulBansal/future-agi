// TODO: delete this file once the agent pages backend ships and contracts are
// regenerated (agent pages contract, sections 2-6). Until then the agent
// pages run on contract-shaped stand-ins so the frontend isn't blocked:
// - responses: a field the backend leaves out is filled in; a field it sends
//   is never touched, so real values take over field by field;
// - calls that can't work yet (recent runs, the environments filter, saving
//   observability) are answered here while AGENT_PAGES_CONTRACT_MOCKS.enabled.

// On in the app; off under vitest so tests exercise the real contract path
// unless a test turns it on.
export const AGENT_PAGES_CONTRACT_MOCKS = {
  enabled: import.meta.env.MODE !== "test",
};

const ENVIRONMENT_NAMES = [
  "ride-voice-agent",
  "Customer support line",
  "Refund edge cases",
  "Late payments",
  "Booking changes",
];
const RUN_STATUSES = [
  "completed",
  "completed",
  "running",
  "completed",
  "failed",
];
const HOUR = 3600 * 1000;

const hashOf = (value) =>
  [...String(value)].reduce((h, c) => (h * 31 + c.charCodeAt(0)) >>> 0, 7);

const observabilityById = new Map();

export const resetContractMocks = () => observabilityById.clear();

const fill = (raw, key, value) => (key in raw ? raw[key] : value);

const isCodeAgent = (raw) => !raw.provider;

/** Agent detail (section 2): agent_kind, source, environment_count, observability_enabled. */
export const withDetailContractFields = (raw) => {
  const seed = hashOf(raw.id);
  const code = isCodeAgent(raw);
  return {
    ...raw,
    agent_kind: fill(raw, "agent_kind", code ? "source" : null),
    source: fill(
      raw,
      "source",
      code ? (seed % 2 ? "archive" : "github") : raw.provider,
    ),
    environment_count: fill(
      raw,
      "environment_count",
      code ? (seed % 3) + 1 : 0,
    ),
    observability_enabled: fill(
      raw,
      "observability_enabled",
      observabilityById.get(raw.id) ?? false,
    ),
  };
};

const versionRef = (agent, i) => {
  const versions = agent.versions ?? [];
  if (!versions.length) return null;
  const v = versions[i % versions.length];
  return { id: v.id, version_number: v.version_number };
};

const environmentsOf = (agent) =>
  Array.from({ length: agent.environment_count ?? 0 }, (_, i) => ({
    id: `${agent.id}-env-${i + 1}`,
    name: ENVIRONMENT_NAMES[(hashOf(agent.id) + i) % ENVIRONMENT_NAMES.length],
  }));

const run = (agent, i, env, total = 1) => {
  const seed = hashOf(`${agent.id}:${i}`);
  const status = RUN_STATUSES[seed % RUN_STATUSES.length];
  const started = Date.now() - (i * 26 + (seed % 5) + 2) * HOUR;
  const simulate = !env;
  const measured = 10 + (seed % 40);
  const passed = Math.round(measured * (0.35 + (seed % 60) / 100));
  const done = status !== "running";
  return {
    kind: simulate ? "simulate" : "environment",
    test_execution_id: `${agent.id}-run-${i + 1}`,
    run_test_id: `${agent.id}-suite-${simulate ? "sim" : env.id}`,
    run_test_name: simulate ? `${agent.agent_name} check` : agent.agent_name,
    run_number: total - i,
    status,
    environment: env,
    agent_version: versionRef(agent, i),
    outcome_passed: simulate ? null : passed,
    outcome_failed: simulate ? null : measured - passed,
    outcome_skipped: simulate ? null : 0,
    started_at: new Date(started).toISOString(),
    completed_at: done ? new Date(started + HOUR / 2).toISOString() : null,
  };
};

/** Recent runs (section 3), newest first. Hosted agents get old Simulate runs too. */
export const mockAgentRuns = (agent, limit = 5) => {
  const envs = environmentsOf(agent);
  const count = 1 + (hashOf(agent.id) % limit);
  const results = Array.from({ length: Math.min(count, limit) }, (_, i) =>
    run(
      agent,
      i,
      envs.length ? envs[i % envs.length] : null,
      Math.min(count, limit),
    ),
  );
  return { results };
};

/** Environments of an agent (section 6), each with its newest run. */
export const mockAgentEnvironments = (agent) => {
  const runs = mockAgentRuns(agent, 5).results;
  const results = environmentsOf(agent).map((env, i) => ({
    id: env.id,
    name: env.name,
    last_run:
      runs.find((r) => r.environment?.id === env.id) ??
      (i ? null : run(agent, 0, env)),
  }));
  return {
    count: results.length,
    next: null,
    previous: null,
    total_pages: 1,
    current_page: 1,
    results,
  };
};

const storedAgentOf = (agent) => ({
  agent: {
    schema_version: "1.0",
    type: agent.agent_type === "text" ? "chat" : agent.agent_type || "voice",
    kind: "source",
    name: (agent.agent_name || "agent")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "_"),
    provider_ref: null,
    behaviour: [
      {
        id: "main",
        shape: "external",
        instructions: agent.description || null,
        tools: ["lookup_booking", "create_booking"],
        transitions: [],
      },
    ],
    tools: [
      {
        name: "lookup_booking",
        description: "Find a booking by reference.",
        kind: "builtin",
        parameters: null,
        endpoint: null,
      },
      {
        name: "create_booking",
        description: "Create a new booking.",
        kind: "http",
        parameters: null,
        endpoint: null,
      },
    ],
    model: {
      provider: "openai",
      name: "gpt-4.1",
      temperature: 0.3,
      max_tokens: 1024,
    },
    voice: null,
    telephony: [],
    supports: [],
    direction_answer: null,
    gaps: ["flow_not_described"],
    instrumentation: "none",
    setup_hash: `sh1:${hashOf(agent.id).toString(16)}`,
  },
  sdk_version: "0.2.3",
  stored_at: agent.created_at,
  setup_stale: false,
});

/** Version detail (section 4): agent_kind, agent_details and the masked api_key. */
export const withVersionContractFields = (raw, agent) => {
  const code = agent.agent_kind === "source";
  const masked = `sk-${"abcdef"[hashOf(agent.id) % 6]}...${(
    hashOf(raw.id) % 65536
  )
    .toString(16)
    .padStart(4, "0")}`;
  return {
    ...raw,
    agent_kind: fill(raw, "agent_kind", agent.agent_kind ?? null),
    agent_details: fill(
      raw,
      "agent_details",
      code ? storedAgentOf(agent) : null,
    ),
    api_key: fill(raw, "api_key", code ? null : masked),
  };
};

/** Observability toggle (section 5): remembered for the session. */
export const mockSetObservability = (agentId, enabled) => {
  observabilityById.set(agentId, enabled);
  return { message: "Agent definition updated successfully" };
};
