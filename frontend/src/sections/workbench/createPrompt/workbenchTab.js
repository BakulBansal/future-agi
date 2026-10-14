// An org restricted to Environments has no Simulation tab, so a
// ?tab=Simulation link opens Playground instead.
export function visibleWorkbenchTab(tab, restrictSimulateToEnvironments) {
  return restrictSimulateToEnvironments && tab === "Simulation"
    ? "Playground"
    : tab;
}
