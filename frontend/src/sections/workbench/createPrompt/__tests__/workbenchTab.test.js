import { describe, expect, it } from "vitest";

import { visibleWorkbenchTab } from "../workbenchTab";

describe("visibleWorkbenchTab", () => {
  it("opens Playground when an org restricted to Environments asks for Simulation", () => {
    expect(visibleWorkbenchTab("Simulation", true)).toBe("Playground");
  });

  it.each(["Playground", "Evaluation", "Metrics"])(
    "keeps %s for an org restricted to Environments",
    (tab) => {
      expect(visibleWorkbenchTab(tab, true)).toBe(tab);
    },
  );

  it("keeps Simulation for other orgs", () => {
    expect(visibleWorkbenchTab("Simulation", false)).toBe("Simulation");
  });
});
