import PropTypes from "prop-types";
import { Box, Stack, Typography } from "@mui/material";

// Small pieces shared by the agent detail tabs, from the designer prototype.

export const MONO = "ui-monospace, Menlo, monospace";

export const ago = (iso) => {
  if (!iso) return "—";
  const s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  const d = Math.round(s / 86400);
  return d === 1 ? "yesterday" : `${d}d ago`;
};

export const rateTone = (rate) =>
  rate >= 80 ? "#16A34A" : rate >= 50 ? "text.primary" : "#DC2626";

export function Pill({ children }) {
  return (
    <Box
      sx={{
        px: 0.75,
        height: 20,
        display: "inline-flex",
        alignItems: "center",
        borderRadius: 0.75,
        border: "1px solid",
        borderColor: "divider",
        flexShrink: 0,
      }}
    >
      <Typography
        sx={{ typography: "s3", fontWeight: 600, color: "text.secondary" }}
      >
        {children}
      </Typography>
    </Box>
  );
}
Pill.propTypes = { children: PropTypes.node };

export function Card({ title, action, children }) {
  return (
    <Box
      sx={{
        border: "1px solid",
        borderColor: "divider",
        borderRadius: 1.5,
        bgcolor: "background.paper",
        overflow: "hidden",
        minWidth: 0,
      }}
    >
      <Stack
        direction="row"
        alignItems="center"
        spacing={1}
        sx={{
          px: 2,
          py: 1.25,
          borderBottom: "1px solid",
          borderColor: "divider",
        }}
      >
        <Typography
          component="div"
          sx={{ typography: "s2", fontWeight: 700, flex: 1 }}
        >
          {title}
        </Typography>
        {action}
      </Stack>
      {children}
    </Box>
  );
}
Card.propTypes = {
  title: PropTypes.node,
  action: PropTypes.node,
  children: PropTypes.node,
};

export function Row({ cells, header = false, onClick }) {
  const clickable = onClick
    ? {
        role: "button",
        tabIndex: 0,
        onClick,
        onKeyDown: (e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            onClick();
          }
        },
      }
    : {};
  return (
    <Box
      {...clickable}
      sx={{
        display: "grid",
        gridTemplateColumns: `repeat(${cells.length}, minmax(0, 1fr))`,
        gap: 1.5,
        px: 2,
        py: header ? 0.75 : 1,
        alignItems: "center",
        borderTop: "1px solid",
        borderColor: "divider",
        cursor: onClick ? "pointer" : "default",
        "&:hover": onClick ? { bgcolor: "action.hover" } : {},
        "&:focus-visible": {
          outline: "2px solid",
          outlineColor: "primary.main",
        },
      }}
    >
      {cells.map((c, i) => (
        <Typography
          // eslint-disable-next-line react/no-array-index-key
          key={i}
          component="div"
          noWrap
          sx={{
            typography: "s2",
            ...(header && { fontSize: 13 }),
            fontWeight: header ? 500 : 400,
            color: header ? "text.secondary" : "text.primary",
          }}
        >
          {c}
        </Typography>
      ))}
    </Box>
  );
}
Row.propTypes = {
  cells: PropTypes.array.isRequired,
  header: PropTypes.bool,
  onClick: PropTypes.func,
};
