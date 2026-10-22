import { describe, it, expect } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import ConnectAgentButton from "../ConnectAgentButton";

let currentLocation = null;
const LocationProbe = () => {
  currentLocation = useLocation();
  return null;
};

const renderButton = () =>
  render(
    <MemoryRouter initialEntries={["/dashboard/agents/connected"]}>
      <ConnectAgentButton />
      <LocationProbe />
    </MemoryRouter>,
  );

describe("ConnectAgentButton", () => {
  it("opens the environments build page", () => {
    renderButton();
    fireEvent.click(screen.getByRole("button", { name: "Connect agent" }));
    expect(currentLocation.pathname).toBe("/dashboard/simulate/environments");
    expect(currentLocation.search).toBe("?tab=build");
  });

  it("is one button, with no menu of options", () => {
    renderButton();
    fireEvent.click(screen.getByRole("button", { name: "Connect agent" }));
    expect(screen.queryByRole("menu")).toBeNull();
    expect(screen.queryByRole("menuitem")).toBeNull();
  });
});
