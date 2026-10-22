import React from "react";
import { useNavigate } from "react-router-dom";
import { Button } from "@mui/material";
import SvgColor from "src/components/svg-color";
import { CONNECT_AGENT_PATH } from "../agentsTabs";

// Agents are connected from Environments > Build.
export default function ConnectAgentButton() {
  const navigate = useNavigate();
  return (
    <Button
      variant="contained"
      color="primary"
      size="small"
      onClick={() => navigate(CONNECT_AGENT_PATH)}
      startIcon={
        <SvgColor src="/assets/icons/ic_add.svg" sx={{ height: 20, width: 20 }} />
      }
    >
      Connect agent
    </Button>
  );
}
