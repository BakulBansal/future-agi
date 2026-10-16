import { describe, it, expect } from "vitest";
import {
  humanize,
  modalityLabel,
  sourceLabel,
  usedInLabel,
  versionsParts,
} from "../connectedAgentLabels";

describe("humanize", () => {
  it("title-cases snake and kebab values", () => {
    expect(humanize("video_call")).toBe("Video Call");
    expect(humanize("vonage-sip")).toBe("Vonage Sip");
    expect(humanize("voice")).toBe("Voice");
  });
});

describe("modalityLabel", () => {
  it("adds the direction when there is one", () => {
    expect(modalityLabel({ modality: "voice", direction: "inbound" })).toBe(
      "Voice · Inbound",
    );
    expect(modalityLabel({ modality: "voice", direction: "outbound" })).toBe(
      "Voice · Outbound",
    );
  });

  it("shows the modality alone without a direction", () => {
    expect(modalityLabel({ modality: "chat", direction: null })).toBe("Chat");
    expect(modalityLabel({ modality: "voice" })).toBe("Voice");
  });

  it("shows a modality it has never seen", () => {
    expect(modalityLabel({ modality: "video_call" })).toBe("Video Call");
  });

  it("falls back to a dash with no modality", () => {
    expect(modalityLabel({ modality: null })).toBe("—");
  });
});

describe("sourceLabel", () => {
  it("prefers the label the API sends", () => {
    expect(sourceLabel({ source: "livekit", sourceLabel: "LiveKit" })).toBe(
      "LiveKit",
    );
  });

  it("humanises a raw source it has never seen", () => {
    expect(sourceLabel({ source: "vonage_sip" })).toBe("Vonage Sip");
  });

  it("falls back to a dash with no source", () => {
    expect(sourceLabel({ source: null })).toBe("—");
  });
});

describe("usedInLabel", () => {
  it("says so when no environment uses it", () => {
    expect(usedInLabel([])).toEqual({
      text: "No environments yet",
      empty: true,
    });
    expect(usedInLabel(undefined)).toEqual({
      text: "No environments yet",
      empty: true,
    });
  });

  it("names a single environment", () => {
    expect(usedInLabel([{ id: "e1", name: "Support line" }])).toEqual({
      text: "Support line",
      empty: false,
    });
  });

  it("counts several environments", () => {
    expect(
      usedInLabel([
        { id: "e1", name: "A" },
        { id: "e2", name: "B" },
        { id: "e3", name: "C" },
      ]),
    ).toEqual({ text: "3 environments", empty: false });
  });
});

describe("versionsParts", () => {
  it("splits the latest version from the total", () => {
    expect(versionsParts({ latestLabel: "v3", versionCount: 5 })).toEqual({
      version: "v3",
      rest: " · 5 total",
    });
  });

  it("shows only the total when there is no latest label", () => {
    expect(versionsParts({ latestLabel: null, versionCount: 0 })).toEqual({
      version: "",
      rest: "0 total",
    });
  });
});
