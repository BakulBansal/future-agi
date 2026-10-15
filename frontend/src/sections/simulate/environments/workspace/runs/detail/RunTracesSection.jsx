import { useEffect, useState } from "react";
import PropTypes from "prop-types";
import { alpha } from "@mui/material/styles";
import {
  Box,
  Button,
  ButtonBase,
  Pagination,
  Stack,
  Typography,
} from "@mui/material";
import { Link as RouterLink } from "react-router-dom";
import Iconify from "src/components/iconify";
import { paths } from "src/routes/paths";
import { fDateTime } from "src/utils/format-time";
import {
  RUN_TRACES_OBSERVABILITY,
  RUN_TRACES_PAGE_SIZE,
  useRunTraces,
} from "src/api/simulate-environments/runDetail";
import { useLLMTracingStoreShallow } from "src/sections/projects/LLMTracing/states";
import LLMTracingTraceDetailDrawer from "src/sections/projects/LLMTracing/LLMTracingTraceDetailDrawer";
import SectionCard from "../../../components/SectionCard";
import EmptyState from "../../../components/EmptyState";
import { RUN_TRACES_COPY } from "./runTraces.constants";

const MONO = "ui-monospace, Menlo, monospace";
const GRID = "minmax(0, 2fr) 90px 180px 100px";

export default function RunTracesSection({ executionId }) {
  const [page, setPage] = useState(1);
  const { observability, agentId, projectId, traces, count, totalPages } =
    useRunTraces(executionId, { page });
  if (observability === RUN_TRACES_OBSERVABILITY.OFF) {
    return <ObservabilityOff agentId={agentId} />;
  }
  if (!projectId) return null;
  return (
    <TracesCard
      projectId={projectId}
      traces={traces}
      count={count}
      page={page}
      totalPages={totalPages}
      onPageChange={setPage}
    />
  );
}
RunTracesSection.propTypes = {
  executionId: PropTypes.string,
};

function ObservabilityOff({ agentId }) {
  return (
    <SectionCard
      title={RUN_TRACES_COPY.title}
      subtitle={RUN_TRACES_COPY.subtitle}
    >
      <EmptyState
        icon="solar:graph-new-linear"
        title={RUN_TRACES_COPY.offTitle}
        body={RUN_TRACES_COPY.offBody}
        action={
          agentId ? (
            <Button
              component={RouterLink}
              to={`${paths.dashboard.simulate.agentDefinition}/${agentId}`}
              variant="contained"
              size="small"
              endIcon={<Iconify icon="solar:arrow-right-linear" width={15} />}
            >
              {RUN_TRACES_COPY.enable}
            </Button>
          ) : null
        }
      />
    </SectionCard>
  );
}
ObservabilityOff.propTypes = {
  agentId: PropTypes.string,
};

function TracesCard({
  projectId,
  traces,
  count,
  page,
  totalPages,
  onPageChange,
}) {
  const { setTraceDetailDrawerOpen, setVisibleTraces, resetStates } =
    useLLMTracingStoreShallow((state) => ({
      setTraceDetailDrawerOpen: state.setTraceDetailDrawerOpen,
      setVisibleTraces: state.setVisibleTraces,
      resetStates: state.resetStates,
    }));

  useEffect(() => {
    setVisibleTraces(
      traces.map((trace) => ({ traceId: trace.traceId, projectId })),
    );
  }, [traces, projectId, setVisibleTraces]);

  useEffect(() => () => resetStates(), [resetStates]);

  return (
    <SectionCard
      title={RUN_TRACES_COPY.title}
      subtitle={RUN_TRACES_COPY.subtitle}
      action={
        <Typography
          sx={{
            typography: "s3",
            color: "text.subtitle",
            fontVariantNumeric: "tabular-nums",
          }}
        >
          {RUN_TRACES_COPY.count(count)}
        </Typography>
      }
    >
      {traces.length === 0 ? (
        <EmptyState
          icon="solar:graph-new-linear"
          title={RUN_TRACES_COPY.empty}
          body={RUN_TRACES_COPY.emptyBody}
        />
      ) : (
        <Box>
          <Box
            sx={{
              display: "grid",
              gridTemplateColumns: GRID,
              columnGap: 2,
              px: 2.5,
              py: 1,
              borderBottom: "1px solid",
              borderColor: "divider",
            }}
          >
            {Object.values(RUN_TRACES_COPY.columns).map((label) => (
              <Typography
                key={label}
                sx={{
                  typography: "s3",
                  fontWeight: "fontWeightBold",
                  textTransform: "uppercase",
                  letterSpacing: 0.6,
                  color: "text.subtitle",
                }}
              >
                {label}
              </Typography>
            ))}
          </Box>
          <Stack
            divider={
              <Box sx={{ borderBottom: "1px solid", borderColor: "divider" }} />
            }
          >
            {traces.map((trace) => (
              <TraceRow
                key={trace.traceId}
                trace={trace}
                onOpen={() =>
                  setTraceDetailDrawerOpen({
                    traceId: trace.traceId,
                    projectId,
                  })
                }
              />
            ))}
          </Stack>
          {totalPages > 1 && (
            <Stack
              direction="row"
              alignItems="center"
              justifyContent="space-between"
              sx={{
                px: 2.5,
                py: 1.25,
                borderTop: "1px solid",
                borderColor: "divider",
              }}
            >
              <Typography
                sx={{
                  typography: "s3",
                  color: "text.subtitle",
                  fontVariantNumeric: "tabular-nums",
                }}
              >
                {RUN_TRACES_COPY.range(
                  (page - 1) * RUN_TRACES_PAGE_SIZE + 1,
                  (page - 1) * RUN_TRACES_PAGE_SIZE + traces.length,
                  count,
                )}
              </Typography>
              <Pagination
                size="small"
                shape="rounded"
                count={totalPages}
                page={page}
                onChange={(_, value) => onPageChange(value)}
                siblingCount={1}
                boundaryCount={1}
              />
            </Stack>
          )}
        </Box>
      )}
      <LLMTracingTraceDetailDrawer readOnly />
    </SectionCard>
  );
}
TracesCard.propTypes = {
  projectId: PropTypes.string.isRequired,
  count: PropTypes.number.isRequired,
  page: PropTypes.number.isRequired,
  totalPages: PropTypes.number.isRequired,
  onPageChange: PropTypes.func.isRequired,
  traces: PropTypes.arrayOf(
    PropTypes.shape({
      traceId: PropTypes.string.isRequired,
      name: PropTypes.string,
      startTime: PropTypes.string,
      status: PropTypes.string,
      latencyMs: PropTypes.number,
    }),
  ).isRequired,
};

function TraceRow({ trace, onOpen }) {
  return (
    <ButtonBase
      onClick={onOpen}
      sx={{
        width: "100%",
        display: "grid",
        gridTemplateColumns: GRID,
        columnGap: 2,
        alignItems: "center",
        textAlign: "left",
        minHeight: 40,
        px: 2.5,
        "&:hover": {
          bgcolor: (t) =>
            alpha(
              t.palette.text.primary,
              t.palette.mode === "dark" ? 0.03 : 0.02,
            ),
        },
      }}
    >
      <Typography
        noWrap
        title={trace.name}
        sx={{
          typography: "s2",
          fontWeight: "fontWeightSemiBold",
          fontFamily: MONO,
        }}
      >
        {trace.name}
      </Typography>
      <Typography
        noWrap
        sx={{
          typography: "s3",
          color: "text.subtitle",
          textTransform: "uppercase",
        }}
      >
        {trace.status}
      </Typography>
      <Typography noWrap sx={{ typography: "s3", color: "text.subtitle" }}>
        {trace.startTime ? fDateTime(trace.startTime) : ""}
      </Typography>
      <Typography
        noWrap
        sx={{
          typography: "s3",
          color: "text.subtitle",
          fontVariantNumeric: "tabular-nums",
        }}
      >
        {trace.latencyMs == null
          ? ""
          : RUN_TRACES_COPY.latency(trace.latencyMs)}
      </Typography>
    </ButtonBase>
  );
}
TraceRow.propTypes = {
  trace: PropTypes.shape({
    traceId: PropTypes.string.isRequired,
    name: PropTypes.string,
    startTime: PropTypes.string,
    status: PropTypes.string,
    latencyMs: PropTypes.number,
  }).isRequired,
  onOpen: PropTypes.func.isRequired,
};
