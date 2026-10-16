import PropTypes from "prop-types";
import React, { useEffect, useState } from "react";
import { Box, Stack, Switch, Typography } from "@mui/material";
import {
  useAgentVersion,
  useSetObservability,
} from "src/api/agents/agentDetail";

/**
 * Observability on/off for the agent. Starts from the agent's
 * observability_enabled, or its active version's switch on a backend that
 * doesn't send that yet. A refused change snaps back and says why.
 */
export default function ObservabilityToggle({ agent }) {
  const active = useAgentVersion(agent.id, agent.activeVersionId);
  const saved =
    agent.observabilityEnabled ??
    active.data?.config?.observability_enabled ??
    false;
  const [override, setOverride] = useState(null);
  const [error, setError] = useState(null);
  const mutation = useSetObservability(agent.id);

  useEffect(() => {
    setOverride(null);
  }, [agent.observabilityEnabled]);

  const checked = override ?? saved;
  const change = (next) => {
    setOverride(next);
    setError(null);
    mutation.mutate(
      { enabled: next },
      {
        onError: (e) => {
          setOverride(null);
          setError(e?.message || "Couldn't change observability");
        },
      },
    );
  };

  return (
    <Box
      sx={{
        border: "1px solid",
        borderColor: "divider",
        borderRadius: 1.5,
        bgcolor: "background.paper",
        px: 2,
        py: 1.5,
      }}
    >
      <Stack direction="row" alignItems="center" spacing={1.5}>
        <Box flex={1} minWidth={0}>
          <Typography sx={{ typography: "s2", fontWeight: 600 }}>
            Observability
          </Typography>
          <Typography
            sx={{
              typography: "s3",
              color: error ? "#DC2626" : "text.subtitle",
            }}
          >
            {error ||
              (checked
                ? "Production calls land in Observe"
                : "Not watched — you only see what simulations find")}
          </Typography>
        </Box>
        <Switch
          size="small"
          checked={checked}
          disabled={mutation.isPending}
          onChange={(e) => change(e.target.checked)}
          inputProps={{ "aria-label": "Observability" }}
        />
      </Stack>
    </Box>
  );
}

ObservabilityToggle.propTypes = { agent: PropTypes.object.isRequired };
