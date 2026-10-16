import PropTypes from "prop-types";
import React from "react";
import { format } from "date-fns";
import { Box, IconButton, Stack, Typography } from "@mui/material";
import Iconify from "src/components/iconify";
import { sourceLabel } from "../connectedAgentLabels";
import { Pill } from "./bits";
import { MONO } from "./format";

const environmentsText = (count) => {
  if (count == null) return null;
  if (count === 0) return "No environments";
  return `${count} environment${count === 1 ? "" : "s"}`;
};

/**
 * Back, name and source pill; then where the agent lives · created · how many
 * environments test it. Pieces the backend doesn't send yet are left out.
 */
export default function AgentDetailHeader({
  agent,
  onBack,
  onOpenEnvironments,
  action,
}) {
  const isCode = agent.agentKind === "source";
  const source = agent.source ?? agent.provider;
  const where = isCode
    ? source
      ? sourceLabel(source)
      : null
    : agent.assistantId;
  const envs = environmentsText(agent.environmentCount);
  const pieces = [
    where && (
      <Box
        component="span"
        key="where"
        sx={isCode ? undefined : { fontFamily: MONO }}
      >
        {where}
      </Box>
    ),
    agent.createdAt &&
      `Created ${format(new Date(agent.createdAt), "dd-MM-yyyy")}`,
    envs &&
      (onOpenEnvironments && agent.environmentCount ? (
        <Box
          component="span"
          key="envs"
          role="link"
          tabIndex={0}
          onClick={onOpenEnvironments}
          onKeyDown={(e) => e.key === "Enter" && onOpenEnvironments()}
          sx={{
            cursor: "pointer",
            "&:hover": { color: "text.primary", textDecoration: "underline" },
          }}
        >
          {envs}
        </Box>
      ) : (
        envs
      )),
  ].filter(Boolean);

  return (
    <Stack direction="row" alignItems="center" spacing={1.5} sx={{ mb: 1.5 }}>
      <IconButton size="small" onClick={onBack} aria-label="Back">
        <Iconify icon="eva:arrow-ios-back-fill" width={18} />
      </IconButton>
      <Box flex={1} minWidth={0}>
        <Stack direction="row" alignItems="center" spacing={1}>
          <Typography
            component="h1"
            noWrap
            sx={{ typography: "m3", fontWeight: 700 }}
          >
            {agent.name}
          </Typography>
          {source && <Pill>{sourceLabel(source)}</Pill>}
        </Stack>
        <Typography
          data-testid="agent-detail-subline"
          noWrap
          sx={{ typography: "s3", color: "text.subtitle", mt: 0.25 }}
        >
          {pieces.map((p, i) => (
            // eslint-disable-next-line react/no-array-index-key
            <React.Fragment key={i}>
              {i > 0 && " · "}
              {p}
            </React.Fragment>
          ))}
        </Typography>
      </Box>
      {action}
    </Stack>
  );
}

AgentDetailHeader.propTypes = {
  agent: PropTypes.object.isRequired,
  onBack: PropTypes.func.isRequired,
  onOpenEnvironments: PropTypes.func,
  action: PropTypes.node,
};
