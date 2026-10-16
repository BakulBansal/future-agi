import PropTypes from "prop-types";
import React, { useState } from "react";
import {
  Box,
  CircularProgress,
  Stack,
  Switch,
  Typography,
} from "@mui/material";
import { alpha } from "@mui/material/styles";
import Iconify from "src/components/iconify";
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
  if (!supported) caption = NOT_YET;
  else if (checked)
    caption = "Tracking this agent's calls and logs in Observe.";
  else caption = "Turn on to track calls, monitor logs and debug this agent.";

  return (
    <Box
      sx={{
        border: "1px solid",
        borderColor: "divider",
        borderRadius: 1.5,
        bgcolor: "background.paper",
        overflow: "hidden",
      }}
    >
      <Stack
        direction="row"
        alignItems="center"
        spacing={1}
        sx={{ px: 2, minHeight: 44 }}
      >
        <Iconify
          icon="solar:eye-scan-linear"
          width={16}
          aria-hidden="true"
          sx={{
            flexShrink: 0,
            color: checked ? "success.main" : "text.subtitle",
            transition: (t) => t.transitions.create("color"),
          }}
        />
        <Typography sx={{ typography: "s2", fontWeight: 600, flexShrink: 0 }}>
          Observability
        </Typography>
        <Typography
          noWrap
          title={caption}
          sx={{
            typography: "s3",
            color: "text.subtitle",
            flex: 1,
            minWidth: 0,
          }}
        >
          {caption}
        </Typography>
        {mutation.isPending && (
          <CircularProgress size={12} aria-label="Saving" />
        )}
        <Switch
          size="small"
          checked={checked}
          disabled={!supported || mutation.isPending}
          onChange={(e) => change(e.target.checked)}
          inputProps={{ "aria-label": "Observability" }}
        />
      </Stack>
      {error && (
        <Stack
          role="alert"
          direction="row"
          alignItems="flex-start"
          spacing={1}
          sx={{
            px: 2,
            py: 1,
            borderTop: "1px solid",
            borderColor: (t) =>
              alpha("#DC2626", t.palette.mode === "dark" ? 0.35 : 0.25),
            bgcolor: (t) =>
              alpha("#DC2626", t.palette.mode === "dark" ? 0.1 : 0.05),
          }}
        >
          <Iconify
            icon="solar:danger-circle-linear"
            width={16}
            sx={{ color: "#DC2626", mt: 0.125, flexShrink: 0 }}
          />
          <Typography sx={{ typography: "s3", color: "text.primary" }}>
            {error}
          </Typography>
        </Stack>
      )}
    </Box>
  );
}

ObservabilityToggle.propTypes = { agent: PropTypes.object.isRequired };
