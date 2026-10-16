import React, { Suspense } from "react";
import { Helmet } from "react-helmet-async";
import {
  Navigate,
  Outlet,
  useLocation,
  useMatch,
  useNavigate,
  useSearchParams,
} from "react-router-dom";
import {
  Box,
  CircularProgress,
  Skeleton,
  Stack,
  Tab,
  Typography,
} from "@mui/material";
import { CustomTabs } from "src/components/tabs/tabs";
import ComingSoonChip from "src/sections/simulate/environments/components/ComingSoonChip";
import { CONNECTED_AGENTS_PATH } from "../../agentsTabs";
import AgentDetailHeader from "./AgentDetailHeader";
import AgentVersionSelect from "./AgentVersionSelect";
import {
  COMING_SOON_TABS,
  DETAIL_TABS,
  agentDetailPath,
  visibleDetailTabs,
} from "./agentDetailTabs";
import useAgentRoute from "./useAgentRoute";

const errorText = (error) =>
  error?.message || error?.detail || "Couldn't load this agent";

/**
 * A connected agent: header, version selector and tabs. Each tab is its own
 * route rendered into the Outlet; `?v=` carries the version across tabs.
 */
export default function AgentDetailLayout() {
  const navigate = useNavigate();
  const { search } = useLocation();
  const [params, setParams] = useSearchParams();
  const { agentId, agent, query, versionId } = useAgentRoute();
  const match = useMatch(`${CONNECTED_AGENTS_PATH}/:agentId/:tab/*`);
  const tab = match?.params.tab;

  const back = () => navigate(CONNECTED_AGENTS_PATH);

  if (query.isError) {
    return (
      <Box sx={{ p: 2 }}>
        <Typography sx={{ typography: "s1", color: "text.secondary" }}>
          {errorText(query.error)}
        </Typography>
      </Box>
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

  const visible = visibleDetailTabs(agent.agentKind);
  if (tab && !visible.includes(tab)) {
    return <Navigate to={`${agentDetailPath(agentId)}${search}`} replace />;
  }

  const showVersions =
    visible.includes("versions") && agent.versions.length > 0;
  const pickVersion = (id) => {
    const next = new URLSearchParams(params);
    if (id === agent.activeVersionId) next.delete("v");
    else next.set("v", id);
    setParams(next, { replace: true });
  };

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
          onOpenEnvironments={
            visible.includes("environments")
              ? () =>
                  navigate(
                    `${agentDetailPath(agentId, "environments")}${search}`,
                  )
              : undefined
          }
          action={
            showVersions && versionId ? (
              <AgentVersionSelect
                versions={agent.versions}
                value={versionId}
                onChange={pickVersion}
              />
            ) : null
          }
        />
        <Box sx={{ borderBottom: "1px solid", borderColor: "divider", mb: 2 }}>
          <CustomTabs
            value={visible.includes(tab) ? tab : false}
            onChange={(_, v) =>
              navigate(`${agentDetailPath(agentId, v)}${search}`)
            }
          >
            {DETAIL_TABS.filter((t) => visible.includes(t.id)).map((t) => (
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
                    <ComingSoonChip compact />
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
