import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import axios, { endpoints } from "src/utils/axios";
import { listHarnessEnvironments } from "src/api/simulate-environments/harnessEnvironments";
import {
  AGENT_PAGES_CONTRACT_MOCKS,
  mockAgentEnvironments,
  mockAgentRuns,
  mockSetObservability,
  withDetailContractFields,
  withVersionContractFields,
} from "./contractMocks";

// Agent detail page data (agent pages contract, sections 2-6). Fields marked
// new in the contract may be missing on an older backend; they map to null
// so the page can hide what it doesn't know rather than show a guess.

const unwrap = (data) => data?.result ?? data;

// The runs endpoint (contract section 3) isn't in the generated contract
// surface yet, and contracts:check rejects any reference to a path outside it.
// While contract mocks are on the runs come from contractMocks.js.
// TODO: once the backend ships it and contracts are regenerated, fetch
// apiPath("/simulate/agent-definitions/{agent_id}/runs/") with { limit }.
export class RunsUnavailableError extends Error {
  constructor() {
    super("Recent runs aren't available yet");
    this.code = "runs_unavailable";
  }
}

export const agentDetailQueryKey = (agentId) => ["agents", "detail", agentId];

export const passRateOf = ({ passed, failed, skipped }) => {
  if (passed == null) return null;
  const measured = passed + (failed ?? 0) + (skipped ?? 0);
  return measured ? Math.round((passed / measured) * 100) : null;
};

const toVersionRef = (v) => (v ? { id: v.id, number: v.version_number } : null);

export const toRun = (raw) => ({
  kind: raw.kind,
  testExecutionId: raw.test_execution_id,
  runTestId: raw.run_test_id,
  runTestName: raw.run_test_name ?? "",
  runNumber: raw.run_number,
  status: raw.status,
  environment: raw.environment ?? null,
  agentVersion: toVersionRef(raw.agent_version),
  passed: raw.outcome_passed ?? null,
  failed: raw.outcome_failed ?? null,
  skipped: raw.outcome_skipped ?? null,
  passRate: passRateOf({
    passed: raw.outcome_passed ?? null,
    failed: raw.outcome_failed,
    skipped: raw.outcome_skipped,
  }),
  startedAt: raw.started_at ?? null,
  completedAt: raw.completed_at ?? null,
});

const toVersion = (v) => ({
  id: v.id,
  number: v.version_number,
  label: v.version_name_display || `v${v.version_number}`,
  status: v.status ?? null,
  isActive: !!v.is_active,
  isLatest: !!v.is_latest,
  commitMessage: v.commit_message ?? null,
  createdAt: v.created_at ?? null,
});

const newField = (raw, key) => (key in raw ? raw[key] : null);

export const mapAgentDetail = (raw) => ({
  id: raw.id,
  name: raw.agent_name,
  description: raw.description ?? null,
  agentType: raw.agent_type ?? null,
  assistantId: raw.assistant_id || null,
  provider: raw.provider || null,
  createdAt: raw.created_at ?? null,
  versions: (raw.versions ?? [])
    .map(toVersion)
    .sort((a, b) => b.number - a.number),
  activeVersionId: raw.active_version?.id ?? null,
  versionCount: raw.version_count ?? (raw.versions ?? []).length,
  agentKind: newField(raw, "agent_kind"),
  source: newField(raw, "source"),
  environmentCount: newField(raw, "environment_count"),
  observabilityEnabled: newField(raw, "observability_enabled"),
});

const toAgentDetails = (d) =>
  d
    ? {
        agent: d.agent,
        sdkVersion: d.sdk_version,
        storedAt: d.stored_at,
        setupStale: !!d.setup_stale,
      }
    : null;

// `apiKey` stays undefined when the backend doesn't send the field at all, and
// null when it sends null (no key saved).
export const mapVersionDetail = (raw) => ({
  id: raw.id,
  label: raw.version_name_display || `v${raw.version_number}`,
  agentKind: raw.agent_kind ?? null,
  agentDetails: toAgentDetails(raw.agent_details),
  apiKey: raw.api_key,
  description: raw.description ?? null,
  config: raw.configuration_snapshot ?? {},
});

const mocking = () => AGENT_PAGES_CONTRACT_MOCKS.enabled;

// Raw agent detail, with the contract's new fields filled in while mocking.
const fetchAgentDetail = async (agentId) => {
  const raw = unwrap(
    (await axios.get(endpoints.agentDefinitions.detail(agentId))).data,
  );
  return mocking() ? withDetailContractFields(raw) : raw;
};

const agentDetailQuery = (agentId) => ({
  queryKey: agentDetailQueryKey(agentId),
  queryFn: () => fetchAgentDetail(agentId),
});

export function useAgentDetail(agentId) {
  return useQuery({
    ...agentDetailQuery(agentId),
    select: mapAgentDetail,
    enabled: !!agentId,
  });
}

export function useAgentRuns(agentId, { limit = 5 } = {}) {
  const queryClient = useQueryClient();
  return useQuery({
    queryKey: ["agents", "runs", agentId, limit],
    queryFn: async () => {
      if (!mocking()) throw new RunsUnavailableError();
      const detail = await queryClient.fetchQuery(agentDetailQuery(agentId));
      return mockAgentRuns(detail, limit);
    },
    select: (d) => (d?.results ?? []).map(toRun),
    enabled: !!agentId,
    retry: false,
  });
}

export function useAgentVersion(agentId, versionId) {
  const queryClient = useQueryClient();
  return useQuery({
    queryKey: ["agents", "version", agentId, versionId],
    queryFn: async () => {
      const raw = unwrap(
        (
          await axios.get(
            endpoints.agentDefinitions.versionDetail(agentId, versionId),
          )
        ).data,
      );
      if (!mocking()) return raw;
      const detail = await queryClient.fetchQuery(agentDetailQuery(agentId));
      return withVersionContractFields(raw, detail);
    },
    select: mapVersionDetail,
    enabled: !!agentId && !!versionId,
  });
}

export function useAgentEnvironments(agentId, { enabled = true } = {}) {
  const queryClient = useQueryClient();
  return useQuery({
    queryKey: ["agents", "environments", agentId],
    queryFn: async () => {
      if (!mocking()) {
        return listHarnessEnvironments({
          agentDefinitionId: agentId,
          page: 1,
          limit: 100,
        });
      }
      const detail = await queryClient.fetchQuery(agentDetailQuery(agentId));
      return mockAgentEnvironments(detail);
    },
    select: (d) =>
      (d?.results ?? []).map((e) => ({
        id: e.id,
        name: e.name,
        lastRun: e.last_run ? toRun(e.last_run) : null,
      })),
    enabled: !!agentId && enabled,
  });
}

export function useSetObservability(agentId) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ enabled }) =>
      mocking()
        ? mockSetObservability(agentId, enabled)
        : axios.put(endpoints.agentDefinitions.edit(agentId), {
            observability_enabled: enabled,
          }),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: agentDetailQueryKey(agentId) }),
  });
}
