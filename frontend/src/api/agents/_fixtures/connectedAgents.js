// Stand-in for the connected agents list API until it lands. Raw rows are
// snake_case, as the backend would send them; the hook maps them.

const HAND_WRITTEN = [
  {
    id: "ca-01",
    name: "Customer support line",
    description: "Inbound phone support for an online storefront",
    modality: "voice",
    direction: "inbound",
    source: "vapi",
    source_label: "Vapi",
    latest_version: { id: "ca-01-v3", label: "v3" },
    version_count: 5,
    used_in: [
      { id: "env-01", name: "Customer support line" },
      { id: "env-02", name: "Refund edge cases" },
    ],
    created_at: "2026-09-14T09:12:00Z",
  },
  {
    id: "ca-02",
    name: "Guest booking line",
    description: "Books rides for a guest on the account holder's behalf",
    modality: "voice",
    direction: "outbound",
    source: "retell",
    source_label: "Retell",
    latest_version: { id: "ca-02-v2", label: "v2" },
    version_count: 2,
    used_in: [{ id: "env-03", name: "Guest booking line" }],
    created_at: "2026-10-11T14:40:00Z",
  },
  {
    id: "ca-03",
    name: "Clinic receptionist",
    description: null,
    modality: "voice",
    direction: "inbound",
    source: "livekit",
    source_label: "LiveKit",
    latest_version: { id: "ca-03-v1", label: "v1" },
    version_count: 1,
    used_in: [],
    created_at: "2026-10-15T08:05:00Z",
  },
  {
    id: "ca-04",
    name: "Research assistant",
    description: "Answers questions with cited web sources",
    modality: "chat",
    direction: null,
    source: "github",
    source_label: "GitHub",
    latest_version: { id: "ca-04-v4", label: "v4" },
    version_count: 4,
    used_in: [
      { id: "env-04", name: "Deep research" },
      { id: "env-05", name: "Citation audit" },
      { id: "env-06", name: "Long-form answers" },
    ],
    created_at: "2026-08-30T11:20:00Z",
  },
  {
    id: "ca-05",
    name: "Billing chat",
    description: "Explains invoices and support plans",
    modality: "chat",
    direction: null,
    source: "upload",
    latest_version: { id: "ca-05-v1", label: "v1" },
    version_count: 1,
    used_in: [],
    created_at: "2026-10-02T16:45:00Z",
  },
  {
    id: "ca-06",
    name: "Collections reminder",
    description: null,
    modality: "voice",
    direction: "outbound",
    source: "vonage_sip",
    latest_version: { id: "ca-06-v2", label: "v2" },
    version_count: 3,
    used_in: [{ id: "env-07", name: "Late payments" }],
    created_at: "2026-09-21T07:30:00Z",
  },
  {
    id: "ca-07",
    name: "Video intake",
    description: "Collects claim details over a video call",
    modality: "video_call",
    direction: "inbound",
    source: "bland",
    latest_version: { id: "ca-07-v1", label: "v1" },
    version_count: 1,
    used_in: [],
    created_at: "2026-10-12T12:00:00Z",
  },
];

const VOICE_SOURCES = [
  { source: "vapi", source_label: "Vapi" },
  { source: "retell", source_label: "Retell" },
  { source: "livekit", source_label: "LiveKit" },
];
const CHAT_SOURCES = [
  { source: "github", source_label: "GitHub" },
  { source: "upload" },
];

// Thirty more plain rows so the list spans two pages at the default size.
const GENERATED = Array.from({ length: 30 }, (_, i) => {
  const n = i + 8;
  const voice = i % 3 !== 0;
  const sources = voice ? VOICE_SOURCES : CHAT_SOURCES;
  const versions = (i % 4) + 1;
  const envs = i % 5;
  return {
    id: `ca-${String(n).padStart(2, "0")}`,
    name: `${voice ? "Voice" : "Chat"} agent ${n}`,
    description: i % 6 === 5 ? null : `Handles support requests for team ${n}`,
    modality: voice ? "voice" : "chat",
    direction: voice ? (i % 2 ? "outbound" : "inbound") : null,
    ...sources[i % sources.length],
    latest_version: { id: `ca-${n}-v${versions}`, label: `v${versions}` },
    version_count: versions,
    used_in: Array.from({ length: envs }, (__, k) => ({
      id: `env-${n}-${k}`,
      name: `Environment ${n}.${k + 1}`,
    })),
    created_at: new Date(Date.UTC(2026, 7, 1 + i, 10, 0)).toISOString(),
  };
});

export const CONNECTED_AGENTS_FIXTURE = [...HAND_WRITTEN, ...GENERATED];
