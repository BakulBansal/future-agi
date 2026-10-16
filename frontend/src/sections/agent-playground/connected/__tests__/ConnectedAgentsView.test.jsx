import { describe, it, expect, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import ConnectedAgentsView from "../ConnectedAgentsView";

const renderView = (props = {}) => {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/dashboard/agents?tab=connected"]}>
        <ConnectedAgentsView onConnect={vi.fn()} {...props} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
};

const nextPageButton = () => {
  const buttons = screen.getAllByRole("button");
  return buttons[buttons.length - 1];
};

describe("ConnectedAgentsView", () => {
  it("shows the ticket's columns in order and none of the hidden ones", async () => {
    renderView();
    await screen.findByText("Clinic receptionist");
    const headers = screen
      .getAllByRole("columnheader")
      .map((h) => h.textContent.trim());
    expect(headers).toEqual([
      "Agent name",
      "Modality",
      "Source",
      "Versions",
      "Used in",
      "Created at",
    ]);
    ["Owner", "Production", "Last updated", "Connected"].forEach((h) =>
      expect(screen.queryByRole("columnheader", { name: h })).toBeNull(),
    );
  });

  it("renders each cell from what the API sends", async () => {
    renderView();
    await screen.findByText("Clinic receptionist");
    expect(screen.getAllByText("Voice · Inbound").length).toBeGreaterThan(0);
    expect(screen.getByText("Video Call · Inbound")).toBeInTheDocument();
    expect(screen.getByText("Vonage Sip")).toBeInTheDocument();
    expect(screen.getAllByText("No environments yet").length).toBeGreaterThan(
      0,
    );
    expect(screen.getAllByText("3 environments").length).toBeGreaterThan(0);
    expect(
      screen.getByText("Inbound phone support for an online storefront"),
    ).toBeInTheDocument();
  });

  it("pages through the list 25 at a time", async () => {
    renderView();
    await screen.findByText("1–25 of 37");
    expect(screen.getByText("Clinic receptionist")).toBeInTheDocument();

    fireEvent.click(nextPageButton());

    await screen.findByText("26–37 of 37");
    expect(await screen.findByText("Voice agent 37")).toBeInTheDocument();
    expect(screen.queryByText("Clinic receptionist")).toBeNull();
  });

  it("goes back to the first page when the search changes", async () => {
    renderView();
    await screen.findByText("1–25 of 37");
    fireEvent.click(nextPageButton());
    await screen.findByText("26–37 of 37");

    fireEvent.change(screen.getByPlaceholderText("Search"), {
      target: { value: "receptionist" },
    });

    await waitFor(
      () => expect(screen.getByText("1–1 of 1")).toBeInTheDocument(),
      {
        timeout: 2000,
      },
    );
    expect(screen.getByText("Clinic receptionist")).toBeInTheDocument();
  });

  it("says nothing matches a search instead of showing the empty state", async () => {
    renderView();
    await screen.findByText("1–25 of 37");

    fireEvent.change(screen.getByPlaceholderText("Search"), {
      target: { value: "zzzz" },
    });

    await waitFor(
      () =>
        expect(
          screen.getByText("No agents match your search"),
        ).toBeInTheDocument(),
      { timeout: 2000 },
    );
    expect(screen.queryByText("No connected agents yet")).toBeNull();
  });
});
