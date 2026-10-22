import React from "react";
import { Helmet } from "react-helmet-async";
import AgentView from "./AgentView";

export default function PlaygroundAgentsPage() {
  return (
    <>
      <Helmet>
        <title>Agents</title>
      </Helmet>
      <AgentView />
    </>
  );
}
