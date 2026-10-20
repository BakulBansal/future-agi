import PropTypes from "prop-types";
import { Box, Button, Stack, Typography } from "@mui/material";
import Iconify from "src/components/iconify";
import SideDrawer from "../../components/SideDrawer";
import { ScenarioDetailBody } from "./ScenarioDetail";
import { SCENARIOS_COPY } from "./scenarios.constants";
import { SCENARIO_SHAPE } from "./scenarios.shapes";

// One scenario in full, opened from its table row: its number, name and use
// case over the same sections the list view expands to, what it tests first.
// Edit hands off to the scenario editor.
export default function ScenarioDrawer({ row, onClose, onEdit, locked = false }) {
  return (
    <SideDrawer open={!!row} onClose={onClose} width={560}>
      {row && (
        <Stack sx={{ height: "100%", minHeight: 0 }}>
          <Box sx={{ px: 3, pt: 2.5, pb: 2, pr: 6, borderBottom: "1px solid", borderColor: "divider" }}>
            {/* A scenario the build added outside the suite has no number. */}
            {row.number != null && (
              <Typography
                sx={{
                  typography: "s3", color: "text.subtitle", fontVariantNumeric: "tabular-nums", mb: 0.5,
                }}
              >
                {SCENARIOS_COPY.drawerNumber(row.number)}
              </Typography>
            )}
            <Typography
              sx={{
                typography: "m3", fontWeight: "fontWeightBold",
                fontFamily: "ui-monospace, Menlo, monospace", wordBreak: "break-word",
              }}
            >
              {row.name || row.title}
            </Typography>
            {row.useCase && (
              <Typography sx={{ typography: "s2", color: "text.secondary", mt: 0.5 }}>
                {row.useCase}
              </Typography>
            )}
            <Stack direction="row" alignItems="center" justifyContent="flex-end" sx={{ mt: 1.5 }}>
              {!locked && onEdit && (
                <Button
                  size="small"
                  variant="outlined"
                  color="inherit"
                  startIcon={<Iconify icon="solar:pen-new-square-linear" width={15} />}
                  onClick={() => onEdit(row)}
                >
                  {SCENARIOS_COPY.editLabel}
                </Button>
              )}
            </Stack>
          </Box>
          <Box sx={{ flex: 1, minHeight: 0, overflow: "auto" }}>
            <ScenarioDetailBody row={row} testsFirst sx={{ px: 3, py: 2.5 }} />
          </Box>
        </Stack>
      )}
    </SideDrawer>
  );
}

ScenarioDrawer.propTypes = {
  row: SCENARIO_SHAPE,
  onClose: PropTypes.func.isRequired,
  onEdit: PropTypes.func,
  locked: PropTypes.bool,
};
