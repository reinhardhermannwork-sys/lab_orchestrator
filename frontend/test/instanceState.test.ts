import { describe, expect, it } from "vitest";

import { describeState, formatRemaining, pollInterval } from "../src/instanceState.ts";

const ALL_STATES = [
  "REQUESTED",
  "PROVISIONING",
  "STARTING",
  "WAITING_READY",
  "READY",
  "CONNECTED",
  "DISCONNECTED_GRACE",
  "DESTROYING",
  "FAILED",
  "CLEANUP",
  "DESTROYED",
];

describe("describeState", () => {
  it("knows every orchestrator state", () => {
    for (const state of ALL_STATES) {
      expect(describeState(state).label).not.toBe(state);
    }
  });

  it("orders the startup steps", () => {
    expect(["REQUESTED", "PROVISIONING", "STARTING", "WAITING_READY"].map((s) => describeState(s).step)).toEqual(
      [1, 2, 3, 4],
    );
  });
});

describe("pollInterval", () => {
  it("polls fast while changing, slowly when ready, and stops when ended", () => {
    expect(pollInterval("PROVISIONING")).toBe(2_000);
    expect(pollInterval("DESTROYING")).toBe(2_000);
    expect(pollInterval("READY")).toBe(15_000);
    expect(pollInterval("DESTROYED")).toBeNull();
  });
});

describe("formatRemaining", () => {
  const now = new Date("2026-10-01T10:00:00Z");
  it.each([
    ["2026-10-01T13:52:30Z", "3h 52m"],
    ["2026-10-01T10:12:00Z", "12m"],
    ["2026-10-01T10:00:30Z", "less than a minute"],
    ["2026-10-01T09:59:00Z", "expired"],
    ["not a date", "unknown"],
  ])("%s -> %s", (expiresAt, expected) => {
    expect(formatRemaining(expiresAt, now)).toBe(expected);
  });
});
