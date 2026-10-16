import React, { useMemo } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Stack, Typography } from "@mui/material";
import { useAgentRuns } from "src/api/agents/agentDetail";
import FitDataTable from "./FitDataTable";
import { QuietTag } from "./bits";
import { MONO, ago } from "./format";
import { PassRateCell, endTagOf, isRunning, runPath } from "./runCells";

const EMPTY = "No simulations have run against this agent yet.";
const RECENT_RUNS = 15;

const errorText = (error) => error?.message || "Couldn't load recent runs";

/** Overview: the agent's newest runs, across environments and Simulate. */
export default function AgentOverviewTab() {
  const { agentId } = useParams();
  const navigate = useNavigate();
  const {
    data: runs,
    isLoading,
    isError,
    error,
  } = useAgentRuns(agentId, { limit: RECENT_RUNS });

  const columns = useMemo(
    () => [
      {
        id: "run",
        header: "Run",
        size: 160,
        cell: ({ row: { original: r } }) => (
          <Stack
            direction="row"
            alignItems="center"
            spacing={0.75}
            sx={{ minWidth: 0 }}
          >
            <Typography noWrap sx={{ typography: "s2", fontWeight: 600 }}>
              {`Run ${r.runNumber}`}
            </Typography>
            {isRunning(r) && <QuietTag>Running</QuietTag>}
            {endTagOf(r) && <QuietTag>{endTagOf(r)}</QuietTag>}
          </Stack>
        ),
      },
      {
        id: "environment",
        header: "Environment",
        meta: { flex: 1.2 },
        minSize: 180,
        cell: ({ row: { original: r } }) => {
          const name = r.environment?.name ?? r.runTestName;
          return (
            <Stack
              direction="row"
              alignItems="center"
              spacing={0.75}
              sx={{ minWidth: 0 }}
            >
              <Typography noWrap title={name} sx={{ typography: "s2" }}>
                {name || "—"}
              </Typography>
              {r.kind === "simulate" && <QuietTag>Simulate</QuietTag>}
            </Stack>
          );
        },
      },
      {
        id: "version",
        header: "Agent version",
        size: 140,
        cell: ({ row: { original: r } }) => (
          <Typography
            noWrap
            sx={{ typography: "s3", fontWeight: 600, fontFamily: MONO }}
          >
            {r.agentVersion ? `v${r.agentVersion.number}` : "—"}
          </Typography>
        ),
      },
      {
        id: "passRate",
        header: "Pass rate",
        size: 200,
        cell: ({ row: { original: r } }) => <PassRateCell run={r} />,
      },
      {
        id: "when",
        header: "When",
        size: 130,
        cell: ({ row: { original: r } }) => (
          <Typography noWrap sx={{ typography: "s3", color: "text.subtitle" }}>
            {r.completedAt ? ago(r.completedAt) : `started ${ago(r.startedAt)}`}
          </Typography>
        ),
      },
    ],
    [],
  );

  let body;
  if (isError && !runs) {
    body = (
      <Typography sx={{ py: 1.5, typography: "s2", color: "text.secondary" }}>
        {errorText(error)}
      </Typography>
    );
  } else if (!isLoading && !runs?.length) {
    body = (
      <Typography sx={{ py: 1.5, typography: "s2", color: "text.secondary" }}>
        {EMPTY}
      </Typography>
    );
  } else {
    body = (
      <FitDataTable
        columns={columns}
        data={runs ?? []}
        isLoading={isLoading}
        getRowId={(r) => r.testExecutionId}
        onRowClick={(r) => navigate(runPath(r))}
      />
    );
  }

  return (
    <Stack spacing={0.5}>
      <Typography sx={{ typography: "s1", fontWeight: 700 }}>
        Recent runs
      </Typography>
      {body}
    </Stack>
  );
}
