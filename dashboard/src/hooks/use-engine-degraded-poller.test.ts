/**
 * Vitest cohort for useEngineDegradedPoller.
 *
 * Mission C4 §T1.13 §9.1 row "useEngineDegradedPoller" — 6 focused
 * tests on the thin wrapper around useApiPoller. Mocks useApiPoller
 * so the underlying network primitives are not exercised here (those
 * live in use-api-poller.test.ts).
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { renderHook } from "@testing-library/react";

import {
  ENGINE_DEGRADED_POLL_INTERVAL_MS,
  useEngineDegradedPoller,
} from "./use-engine-degraded-poller";

const mockUseApiPoller = vi.fn();

vi.mock("@/hooks/use-api-poller", () => ({
  useApiPoller: (options: unknown) => {
    mockUseApiPoller(options);
    return { data: null, error: "ok", consecutive5xx: 0 };
  },
}));

describe("useEngineDegradedPoller", () => {
  beforeEach(() => {
    mockUseApiPoller.mockClear();
  });

  it("exports the 5-second baseline interval constant", () => {
    expect(ENGINE_DEGRADED_POLL_INTERVAL_MS).toBe(5000);
  });

  it("calls useApiPoller with the /api/engine/degraded endpoint", () => {
    renderHook(() => useEngineDegradedPoller());
    expect(mockUseApiPoller).toHaveBeenCalledWith(
      expect.objectContaining({ endpoint: "/api/engine/degraded" }),
    );
  });

  it("passes the 5-second baseline interval to the underlying poller", () => {
    renderHook(() => useEngineDegradedPoller());
    expect(mockUseApiPoller).toHaveBeenCalledWith(
      expect.objectContaining({
        baselineIntervalMs: ENGINE_DEGRADED_POLL_INTERVAL_MS,
      }),
    );
  });

  it("defaults enabled=true when no option is provided", () => {
    renderHook(() => useEngineDegradedPoller());
    expect(mockUseApiPoller).toHaveBeenCalledWith(
      expect.objectContaining({ enabled: true }),
    );
  });

  it("forwards explicit enabled=false to the underlying poller", () => {
    renderHook(() => useEngineDegradedPoller({ enabled: false }));
    expect(mockUseApiPoller).toHaveBeenCalledWith(
      expect.objectContaining({ enabled: false }),
    );
  });

  it("passes a warnTag for the one-shot degraded-state console.warn", () => {
    renderHook(() => useEngineDegradedPoller());
    expect(mockUseApiPoller).toHaveBeenCalledWith(
      expect.objectContaining({ warnTag: "engine_degraded_poller_degraded" }),
    );
  });
});
