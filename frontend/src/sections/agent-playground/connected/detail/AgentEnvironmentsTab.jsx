import PropTypes from "prop-types";
import React from "react";
import { useNavigate } from "react-router-dom";
import { Box, Typography } from "@mui/material";
import { useAgentEnvironments } from "src/api/agents/agentDetail";
import { paths } from "src/routes/paths";
import { Card, Row } from "./bits";
import { MONO, ago, rateTone } from "./format";
import useAgentRoute from "./useAgentRoute";

const COLUMNS = ["Environment", "Testing", "Last result"];

function LastResult({ env }) {
  const run = env.lastRun;
  if (!run) {
    return (
      <Box component="span" sx={{ color: "text.subtitle" }}>
        No runs yet
      </Box>
    );
  }
  const rest = [
    run.agentVersion ? `agent v${run.agentVersion.number}` : null,
    run.completedAt ? ago(run.completedAt) : `started ${ago(run.startedAt)}`,
  ].filter(Boolean);
  return (
    <>
      {run.passRate != null && (
        <Box
          component="span"
          sx={{ fontWeight: 700, color: rateTone(run.passRate) }}
        >
          {`${run.passRate}%`}
        </Box>
      )}
      <Box component="span" sx={{ color: "text.subtitle" }}>
        {`${run.passRate != null ? " · " : ""}${rest.join(" · ")}`}
      </Box>
    </>
  );
}

LastResult.propTypes = { env: PropTypes.object.isRequired };

/** Environments: every environment built for this code agent and how it did. */
export default function AgentEnvironmentsTab() {
  const { agentId, agent } = useAgentRoute();
  const navigate = useNavigate();
  const {
    data: envs,
    isLoading,
    isError,
    error,
  } = useAgentEnvironments(agentId, {
    // The agent_definition_id filter ships with environment_count; asking
    // before then would list every environment in the workspace.
    enabled: agent?.environmentCount != null,
  });

  let body;
  if (isError && !envs) {
    body = (
      <Typography
        sx={{ px: 2, py: 1.5, typography: "s2", color: "text.secondary" }}
      >
        {error?.message || "Couldn't load environments"}
      </Typography>
    );
  } else if (isLoading) {
    body = (
      <Typography
        sx={{ px: 2, py: 1.5, typography: "s2", color: "text.secondary" }}
      >
        Loading…
      </Typography>
    );
  } else if (!envs?.length) {
    body = (
      <Typography
        sx={{ px: 2, py: 1.5, typography: "s2", color: "text.secondary" }}
      >
        No environments test this agent yet
      </Typography>
    );
  } else {
    body = (
      <>
        <Row header cells={COLUMNS} />
        {envs.map((env) => (
          <Row
            key={env.id}
            onClick={() =>
              navigate(paths.dashboard.simulate.environments.detail(env.id))
            }
            cells={[
              <Box component="span" key="name" sx={{ fontWeight: 600 }}>
                {env.name}
              </Box>,
              <Box component="span" key="testing" sx={{ fontFamily: MONO }}>
                {env.lastRun?.agentVersion
                  ? `v${env.lastRun.agentVersion.number}`
                  : "—"}
              </Box>,
              <Box
                component="span"
                key="last"
                data-testid={`last-result-${env.id}`}
              >
                <LastResult env={env} />
              </Box>,
            ]}
          />
        ))}
      </>
    );
  }

  return <Card title="Environments testing this agent">{body}</Card>;
}
