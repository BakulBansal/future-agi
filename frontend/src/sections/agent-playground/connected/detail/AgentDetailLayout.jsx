import React, { Suspense } from "react";
import { Helmet } from "react-helmet-async";
import { Outlet, useLocation, useMatch, useNavigate } from "react-router-dom";
import {
  Box,
  CircularProgress,
  IconButton,
  Skeleton,
  Stack,
  Tab,
  Typography,
} from "@mui/material";
import Iconify from "src/components/iconify";
import { CustomTabs } from "src/components/tabs/tabs";
import ComingSoonChip from "src/sections/simulate/environments/components/ComingSoonChip";
import { CONNECTED_AGENTS_PATH } from "../../agentsTabs";
import AgentDetailHeader from "./AgentDetailHeader";
import {
  COMING_SOON_TABS,
  DETAIL_TABS,
  agentDetailPath,
} from "./agentDetailTabs";
import useAgentRoute from "./useAgentRoute";

const errorText = (error) =>
  error?.message || error?.detail || "Couldn't load this agent";

/**
 * A connected agent: header and tabs. Each tab is its own route rendered into
 * the Outlet. Every agent gets Versions and Environments — there's no field
 * yet that tells an environment-built agent from an older one — and each tab
 * shows what its API returns.
 */
export default function AgentDetailLayout() {
  const navigate = useNavigate();
  const { search } = useLocation();
  const { agentId, agent, query } = useAgentRoute();
  const match = useMatch(`${CONNECTED_AGENTS_PATH}/:agentId/:tab/*`);
  const tab = match?.params.tab;

  const back = () => navigate(CONNECTED_AGENTS_PATH);

  // A failed background refetch keeps the loaded page; only a first load
  // that fails shows the error.
  if (query.isError && !agent) {
    return (
      <Stack direction="row" alignItems="center" spacing={1.5} sx={{ p: 2 }}>
        <IconButton size="small" onClick={back} aria-label="Back">
          <Iconify icon="eva:arrow-ios-back-fill" width={18} />
        </IconButton>
        <Typography sx={{ typography: "s1", color: "text.secondary" }}>
          {errorText(query.error)}
        </Typography>
      </Stack>
    );
  }

  if (!agent) {
    return (
      <Stack spacing={1.5} sx={{ p: 2 }}>
        <Skeleton variant="text" width={280} height={32} />
        <Skeleton variant="text" width={420} />
        <Skeleton variant="rounded" height={36} />
      </Stack>
    );
  }

  const tabLabel = (t) => {
    if (t.id === "versions") return `Versions ${agent.versions.length}`;
    if (t.id === "environments" && agent.environmentCount != null) {
      return `Environments ${agent.environmentCount}`;
    }
    return t.label;
  };

  return (
    <>
      <Helmet>
        <title>{`${agent.name} · Agents`}</title>
      </Helmet>
      <Box sx={{ p: 2, height: "100%", overflow: "auto" }}>
        <AgentDetailHeader
          agent={agent}
          onBack={back}
          onOpenEnvironments={() =>
            navigate(`${agentDetailPath(agentId, "environments")}${search}`)
          }
        />
        <Box sx={{ borderBottom: "1px solid", borderColor: "divider", mb: 2 }}>
          <CustomTabs
            value={DETAIL_TABS.some((t) => t.id === tab) ? tab : false}
            onChange={(_, v) =>
              navigate(`${agentDetailPath(agentId, v)}${search}`)
            }
          >
            {DETAIL_TABS.map((t) => (
              <Tab key={t.id} value={t.id} label={tabLabel(t)} disableRipple />
            ))}
            {COMING_SOON_TABS.map((label) => (
              <Tab
                key={label}
                value={label}
                disabled
                disableRipple
                label={
                  <Stack direction="row" alignItems="center" spacing={0.75}>
                    <span>{label}</span>
                    <ComingSoonChip />
                  </Stack>
                }
              />
            ))}
          </CustomTabs>
        </Box>
        <Suspense
          fallback={
            <Box sx={{ display: "flex", justifyContent: "center", p: 4 }}>
              <CircularProgress size={24} />
            </Box>
          }
        >
          <Outlet />
        </Suspense>
      </Box>
    </>
  );
}
