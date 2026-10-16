import PropTypes from "prop-types";
import React, { useState } from "react";
import { Box, Stack, Switch, Typography } from "@mui/material";
import {
  useAgentVersion,
  useSetObservability,
} from "src/api/agents/agentDetail";

const NOT_YET = "Turning observability on or off here is coming soon";

/**
 * Observability on/off for the agent, from its observability_enabled. A
 * backend that doesn't send that yet can't change it either, so the switch
 * shows the active version's saved state, read-only. A refused change snaps
 * back and says why.
 */
export default function ObservabilityToggle({ agent }) {
  const supported = agent.observabilityEnabled != null;
  const active = useAgentVersion(
    agent.id,
    supported ? null : agent.activeVersionId,
  );
  const saved = supported
    ? agent.observabilityEnabled
    : !!active.data?.config?.observability_enabled;
  const [override, setOverride] = useState(null);
  const [error, setError] = useState(null);
  const mutation = useSetObservability(agent.id);

  const checked = override ?? saved;
  const change = (next) => {
    setOverride(next);
    setError(null);
    mutation.mutate(
      { enabled: next },
      {
        // The refetched agent is the truth once the change is accepted.
        onSuccess: () => setOverride(null),
        onError: (e) => {
          setOverride(null);
          setError(e?.message || "Couldn't change observability");
        },
      },
    );
  };

  let caption;
  if (error) caption = error;
  else if (!supported) caption = NOT_YET;
  else if (checked) caption = "Production calls land in Observe";
  else caption = "Not watched — you only see what simulations find";

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
            {caption}
          </Typography>
        </Box>
        <Switch
          size="small"
          checked={checked}
          disabled={!supported || mutation.isPending}
          onChange={(e) => change(e.target.checked)}
          inputProps={{ "aria-label": "Observability" }}
        />
      </Stack>
    </Box>
  );
}

ObservabilityToggle.propTypes = { agent: PropTypes.object.isRequired };
