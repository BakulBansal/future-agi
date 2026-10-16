import PropTypes from "prop-types";
import React from "react";
import { Box, Skeleton, Stack, Typography } from "@mui/material";
import { useAgentVersion } from "src/api/agents/agentDetail";
import { sourceLabel } from "../connectedAgentLabels";
import { Card } from "./bits";
import { MONO } from "./format";
import { hostedRows, storedAgentRows } from "./definitionRows";
import ComingSoonPreview from "./ComingSoonPreview";
import ObservabilityToggle from "./ObservabilityToggle";
import FileStructurePreview from "./previews/FileStructurePreview";
import useAgentRoute from "./useAgentRoute";

function DefinitionRow({ row }) {
  return (
    <Stack
      direction="row"
      alignItems="flex-start"
      spacing={2}
      sx={{
        px: 2,
        py: 1,
        borderTop: "1px solid",
        borderColor: "divider",
        "&:first-of-type": { borderTop: "none" },
      }}
    >
      <Typography
        sx={{
          typography: "s2",
          color: "text.subtitle",
          width: 150,
          flexShrink: 0,
          lineHeight: "24px",
        }}
      >
        {row.label}
      </Typography>
      <Box sx={{ flex: 1, minWidth: 0 }}>
        {row.items ? (
          <Stack direction="row" flexWrap="wrap" useFlexGap gap={0.75}>
            {row.items.map((item) => (
              <Box
                key={item}
                sx={{
                  px: 1,
                  height: 24,
                  display: "inline-flex",
                  alignItems: "center",
                  borderRadius: 0.75,
                  border: "1px solid",
                  borderColor: "divider",
                  bgcolor: "background.neutral",
                }}
              >
                <Typography
                  sx={{
                    typography: "s3",
                    fontWeight: 500,
                    fontFamily: row.mono ? MONO : undefined,
                    fontSize: row.mono ? 11.5 : undefined,
                  }}
                >
                  {item}
                </Typography>
              </Box>
            ))}
          </Stack>
        ) : (
          <Typography
            noWrap={!row.multiline}
            title={row.multiline ? undefined : String(row.value)}
            sx={{
              typography: "s2",
              lineHeight: "24px",
              fontFamily: row.mono ? MONO : undefined,
              ...(row.multiline && {
                whiteSpace: "pre-wrap",
                display: "-webkit-box",
                WebkitLineClamp: 6,
                WebkitBoxOrient: "vertical",
                overflow: "hidden",
              }),
            }}
          >
            {row.value}
          </Typography>
        )}
      </Box>
    </Stack>
  );
}

DefinitionRow.propTypes = { row: PropTypes.object.isRequired };

const Note = ({ children, tone }) => (
  <Typography
    sx={{
      px: 2,
      py: 1,
      borderTop: "1px solid",
      borderColor: "divider",
      // Right under the card title, which already draws that line.
      "&:first-child": { borderTop: "none" },
      typography: "s3",
      color: tone === "warning" ? "#D97706" : "text.subtitle",
    }}
  >
    {children}
  </Typography>
);

Note.propTypes = { children: PropTypes.node, tone: PropTypes.string };

/** Definition: what we read for the selected version, and observability. */
export default function AgentDefinitionTab() {
  const { agentId, agent, versionId } = useAgentRoute();
  const {
    data: version,
    isLoading,
    isError,
  } = useAgentVersion(agentId, versionId);

  if (!agent) return null;

  const isCode = (version?.agentKind ?? agent.agentKind) === "source";
  const details = version?.agentDetails;
  const readFrom = sourceLabel(agent.source ?? version?.config?.provider);

  let body;
  if (!versionId) {
    body = <Note>No definition stored for this version</Note>;
  } else if (isLoading) {
    body = (
      <Box sx={{ px: 2, py: 1.5 }}>
        <Skeleton variant="text" />
        <Skeleton variant="text" />
        <Skeleton variant="text" width="60%" />
      </Box>
    );
  } else if (isError && !version) {
    body = <Note>Couldn&apos;t load this version</Note>;
  } else if (details) {
    body = (
      <>
        {storedAgentRows(details).map((row) => (
          <DefinitionRow key={row.label} row={row} />
        ))}
        {details.setupStale && (
          <Note tone="warning">
            This setup may be out of date. The agent changed after it was
            stored.
          </Note>
        )}
      </>
    );
  } else if (isCode) {
    body = <Note>No definition stored for this version</Note>;
  } else {
    const rows = hostedRows(version ?? {});
    body = rows.length ? (
      rows.map((row) => <DefinitionRow key={row.label} row={row} />)
    ) : (
      <Note>No definition stored for this version</Note>
    );
  }

  return (
    <Stack spacing={2}>
      <Card
        title={
          <>
            <span>What we read</span>{" "}
            {version && (
              <Box
                component="span"
                sx={{
                  fontFamily: MONO,
                  fontWeight: 600,
                  color: "text.subtitle",
                }}
              >
                {`· ${version.label}`}
              </Box>
            )}
          </>
        }
        action={
          readFrom !== "—" ? (
            <Typography
              noWrap
              sx={{ typography: "s3", color: "text.subtitle" }}
            >
              {`Read from ${readFrom}`}
            </Typography>
          ) : null
        }
      >
        {body}
      </Card>
      <ObservabilityToggle agent={agent} />
      <ComingSoonPreview
        title="File structure"
        description="Browse the files we read from your agent, version by version."
      >
        <FileStructurePreview versionLabel={version?.label} />
      </ComingSoonPreview>
    </Stack>
  );
}
