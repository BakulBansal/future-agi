import React, { Suspense } from "react";
import { Helmet } from "react-helmet-async";
import { Outlet } from "react-router-dom";
import { Box, CircularProgress, Stack, Typography } from "@mui/material";
import Iconify from "src/components/iconify";
import CustomTooltip from "src/components/tooltip/CustomTooltip";
import ConnectAgentButton from "./components/ConnectAgentButton";

/**
 * Agents: the ones that live elsewhere and were connected to run simulations
 * against. The Playground list is its own page, reached by URL or from the
 * builder and experiments, not from here.
 */
export default function AgentsLanding() {
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
        <Stack
          direction="row"
          alignItems="center"
          justifyContent="space-between"
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
              title="Connect agents that already run somewhere else to test them in simulations."
            >
              <Box sx={{ display: "flex", color: "text.subtitle" }}>
                <Iconify icon="solar:info-circle-linear" width={16} />
              </Box>
            </CustomTooltip>
          </Stack>
          <ConnectAgentButton />
        </Stack>

        <Box
          sx={{
            flex: 1,
            minHeight: 0,
            display: "flex",
            flexDirection: "column",
          }}
        >
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
      </Box>
    </>
  );
}
