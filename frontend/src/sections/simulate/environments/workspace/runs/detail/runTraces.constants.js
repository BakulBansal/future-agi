export const RUN_TRACES_COPY = {
  title: "Traces",
  subtitle: "What the agent's own code recorded during this run",
  count: (n) => `${n} ${n === 1 ? "trace" : "traces"}`,
  empty: "No traces for this run",
  emptyBody:
    "Observability is on, but this agent's code didn't send any traces while the run's calls were placed.",
  offTitle: "Observability is off for this agent",
  offBody:
    "Turn on observability to see the traces this agent's own code records during simulated runs.",
  enable: "Enable observability",
  tab: (count) => (count == null ? "Traces" : `Traces (${count})`),
  columns: {
    name: "Root span",
    status: "Status",
    started: "Started",
    latency: "Latency",
  },
  latency: (ms) => `${Math.round(ms).toLocaleString()} ms`,
  range: (start, end, count) =>
    `Showing ${start.toLocaleString()}–${end.toLocaleString()} of ${count.toLocaleString()}`,
};
