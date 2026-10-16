import PropTypes from "prop-types";
import React from "react";
import { Box, MenuItem, Select } from "@mui/material";
import { MONO, ago } from "./format";

// Picks the version the tabs show. Only changes this page.
export default function AgentVersionSelect({ versions, value, onChange }) {
  const labelOf = (id) => versions.find((v) => v.id === id);
  return (
    <Select
      size="small"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      renderValue={(id) => {
        const v = labelOf(id);
        if (!v) return "";
        return v.isActive ? `${v.label} · active` : `Viewing ${v.label}`;
      }}
      sx={{ height: 32, typography: "s2", fontWeight: 600, minWidth: 120 }}
      // Long histories scroll inside the menu instead of running off the page.
      MenuProps={{ PaperProps: { sx: { maxHeight: 320 } } }}
    >
      {versions.map((v) => (
        <MenuItem key={v.id} value={v.id} sx={{ typography: "s2" }}>
          <Box
            component="span"
            sx={{ fontFamily: MONO, fontWeight: 600, mr: 1 }}
          >
            {v.label}
          </Box>
          <Box component="span" sx={{ color: "text.subtitle" }}>
            {[v.isActive ? "active" : null, ago(v.createdAt)]
              .filter(Boolean)
              .join(" · ")}
          </Box>
        </MenuItem>
      ))}
    </Select>
  );
}

AgentVersionSelect.propTypes = {
  versions: PropTypes.array.isRequired,
  value: PropTypes.string.isRequired,
  onChange: PropTypes.func.isRequired,
};
