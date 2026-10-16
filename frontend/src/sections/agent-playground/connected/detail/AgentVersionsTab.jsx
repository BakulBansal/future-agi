import PropTypes from "prop-types";
import React, { useMemo } from "react";
import { format } from "date-fns";
import { Box, Skeleton, Stack, Typography } from "@mui/material";
import { useScrollEnd } from "src/hooks/use-scroll-end";
import { useAgentDefinitionVersions } from "src/api/agent-definition/agent-definition-version";
import { toVersion } from "src/api/agents/agentDetail";
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

// A timeline row while it loads: the dot, then label, message and date.
function VersionSkeleton() {
  return (
    <Stack
      data-testid="version-skeleton"
      direction="row"
      spacing={1.5}
      sx={{ pb: 2.25 }}
    >
      <Skeleton
        variant="circular"
        width={10}
        height={10}
        sx={{ mt: 0.5, flexShrink: 0 }}
      />
      <Box flex={1} minWidth={0}>
        <Skeleton variant="text" width="35%" height={20} />
        <Skeleton variant="text" width="70%" height={16} />
        <Skeleton variant="text" width="45%" height={16} />
      </Box>
    </Stack>
  );
}

const skeletons = (n) =>
  // eslint-disable-next-line react/no-array-index-key
  Array.from({ length: n }, (_, i) => (
    <VersionSkeleton key={`skeleton-${i}`} />
  ));

/**
 * Versions: a read-only timeline from the paginated versions list (the one
 * the Agent Definition page uses), newest first, and a preview of Compare.
 */
export default function AgentVersionsTab() {
  const { agentId, agent } = useAgentRoute();
  const { data, fetchNextPage, hasNextPage, isFetchingNextPage, isLoading } =
    useAgentDefinitionVersions({ selectedAgentId: agentId });
  const versions = useMemo(
    () => (data?.pages ?? []).flatMap((p) => p.results ?? []).map(toVersion),
    [data],
  );
  // Like the Agent Definition page: the next page loads at the list's end.
  // The listener attaches once the list exists, so it waits for the agent.
  const scrollRef = useScrollEnd(() => {
    if (hasNextPage && !isFetchingNextPage) fetchNextPage();
  }, [
    hasNextPage,
    isFetchingNextPage,
    fetchNextPage,
    !!agent,
    versions.length,
  ]);
  if (!agent) return null;
  const total = agent.versionCount;

  return (
    <Box
      sx={{
        display: "grid",
        gap: 2,
        gridTemplateColumns: { xs: "1fr", lg: "minmax(340px, 420px) 1fr" },
        alignItems: "start",
      }}
    >
      <Card title={`${total} version${total === 1 ? "" : "s"}`}>
        <Box
          ref={scrollRef}
          data-testid="versions-scroll"
          sx={{
            px: 2,
            py: 1.75,
            maxHeight: "calc(100vh - 300px)",
            overflowY: "auto",
          }}
        >
          {isLoading && skeletons(3)}
          {data && versions.length === 0 && (
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
          {isFetchingNextPage && skeletons(2)}
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
