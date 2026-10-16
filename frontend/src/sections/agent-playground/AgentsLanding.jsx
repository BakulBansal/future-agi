import React from "react";
import { Helmet } from "react-helmet-async";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Box, Stack, Tab, Typography } from "@mui/material";
import Iconify from "src/components/iconify";
import { SegmentedTabs } from "src/components/tabs/tabs";
import CustomTooltip from "src/components/tooltip/CustomTooltip";
import AgentView from "./AgentView";
import ConnectedAgentsView from "./connected/ConnectedAgentsView";
import CreateAgentsButton from "./components/CreateAgentsButton";
import useCanEditAgent from "./hooks/useCanEditAgent";
import { CONNECT_AGENT_PATH, resolveAgentsTab } from "./agentsTabs";

/**
 * Agents — the ones built here in the Playground, and the ones that live
 * elsewhere and were connected to run simulations against.
 */
export default function AgentsLanding() {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const tab = resolveAgentsTab(params.get("tab"));
  const { canCreate } = useCanEditAgent();

  return (
    <>
      <Helmet>
        <title>Agents</title>
      </Helmet>
      <Box
        sx={{
          height: "100%",
          display: "flex",
          flexDirection: "column",
          p: 2,
          gap: 1.5,
          minHeight: 0,
          overflow: "hidden",
        }}
      >
        <Stack direction="row" alignItems="center" spacing={0.75}>
          <Typography
            typography="m2"
            fontWeight="fontWeightMedium"
            color="text.primary"
          >
            Agents
          </Typography>
          <CustomTooltip
            show
            arrow
            size="small"
            title="Build agents in the Playground, or connect agents that already run somewhere else to test them in simulations."
          >
            <Box sx={{ display: "flex", color: "text.subtitle" }}>
              <Iconify icon="solar:info-circle-linear" width={16} />
            </Box>
          </CustomTooltip>
        </Stack>

        <Stack
          direction="row"
          alignItems="center"
          justifyContent="space-between"
        >
          <SegmentedTabs
            value={tab}
            onChange={(_, v) => setParams({ tab: v }, { replace: true })}
          >
            <Tab value="connected" label="Connected agents" disableRipple />
            <Tab value="playground" label="Agent Playground" disableRipple />
          </SegmentedTabs>
          <CreateAgentsButton canCreatePlayground={canCreate} />
        </Stack>

        <Box
          sx={{
            flex: 1,
            minHeight: 0,
            display: "flex",
            flexDirection: "column",
          }}
        >
          {tab === "playground" ? (
            <AgentView embedded />
          ) : (
            <ConnectedAgentsView
              onConnect={() => navigate(CONNECT_AGENT_PATH)}
            />
          )}
        </Box>
      </Box>
    </>
  );
}
