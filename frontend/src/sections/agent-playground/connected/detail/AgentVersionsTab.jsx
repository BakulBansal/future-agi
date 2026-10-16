import PropTypes from "prop-types";
import React from "react";
import { format } from "date-fns";
import { Box, Stack, Typography } from "@mui/material";
import { alpha } from "@mui/material/styles";
import ComingSoonPreview from "./ComingSoonPreview";
import CompareVersionsPreview from "./previews/CompareVersionsPreview";
import { Card } from "./bits";
import { MONO } from "./format";
import useAgentRoute from "./useAgentRoute";

function Chip({ label, tone }) {
  return (
    <Box
      sx={{
        px: 0.75,
        height: 18,
        display: "inline-flex",
        alignItems: "center",
        borderRadius: 0.75,
        bgcolor: (t) =>
          tone === "primary"
            ? alpha(
                t.palette.primary.main,
                t.palette.mode === "dark" ? 0.16 : 0.1,
              )
            : alpha(t.palette.text.primary, 0.07),
        color: tone === "primary" ? "primary.main" : "text.secondary",
      }}
    >
      <Typography sx={{ typography: "s3", fontWeight: 700 }}>
        {label}
      </Typography>
    </Box>
  );
}

Chip.propTypes = { label: PropTypes.string, tone: PropTypes.string };

/** Versions: a read-only timeline, and a preview of Compare. */
export default function AgentVersionsTab() {
  const { agent } = useAgentRoute();
  if (!agent) return null;
  const { versions } = agent;

  return (
    <Box
      sx={{
        display: "grid",
        gap: 2,
        gridTemplateColumns: { xs: "1fr", lg: "minmax(340px, 420px) 1fr" },
        alignItems: "start",
      }}
    >
      <Card
        title={`${versions.length} version${versions.length === 1 ? "" : "s"}`}
      >
        <Box sx={{ px: 2, py: 1.75 }}>
          {versions.length === 0 && (
            <Typography sx={{ typography: "s2", color: "text.secondary" }}>
              No versions yet
            </Typography>
          )}
          {versions.map((v, i) => {
            const isNewest = i === 0;
            return (
              <Stack
                key={v.id}
                data-testid="version-entry"
                direction="row"
                spacing={1.5}
                sx={{ pb: i === versions.length - 1 ? 0 : 2.25 }}
              >
                <Stack alignItems="center" sx={{ flexShrink: 0, pt: 0.25 }}>
                  <Box
                    sx={{
                      width: 10,
                      height: 10,
                      borderRadius: "50%",
                      bgcolor: isNewest ? "primary.main" : "transparent",
                      border: "2px solid",
                      borderColor: isNewest ? "primary.main" : "text.disabled",
                    }}
                  />
                  {i < versions.length - 1 && (
                    <Box
                      sx={{ width: 1.5, flex: 1, mt: 0.5, bgcolor: "divider" }}
                    />
                  )}
                </Stack>
                <Box flex={1} minWidth={0}>
                  <Stack
                    direction="row"
                    alignItems="center"
                    spacing={0.75}
                    flexWrap="wrap"
                    rowGap={0.5}
                  >
                    <Typography
                      data-testid="version-label"
                      sx={{
                        typography: "s2",
                        fontWeight: 700,
                        fontFamily: MONO,
                      }}
                    >
                      {v.label}
                    </Typography>
                    {v.isLatest && <Chip label="Latest" tone="primary" />}
                    {v.isActive && <Chip label="Active" />}
                  </Stack>
                  {v.commitMessage && (
                    <Typography
                      sx={{
                        typography: "s3",
                        color: "text.secondary",
                        mt: 0.375,
                      }}
                    >
                      {v.commitMessage}
                    </Typography>
                  )}
                  {v.createdAt && (
                    <Typography
                      sx={{
                        typography: "s3",
                        color: "text.subtitle",
                        mt: 0.25,
                      }}
                    >
                      {format(new Date(v.createdAt), "dd-MM-yyyy, h:mm a")}
                    </Typography>
                  )}
                </Box>
              </Stack>
            );
          })}
        </Box>
      </Card>
      <ComingSoonPreview
        title="Compare"
        description="Compare any two versions to see exactly what changed."
      >
        <CompareVersionsPreview />
      </ComingSoonPreview>
    </Box>
  );
}
