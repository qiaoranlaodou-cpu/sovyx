/**
 * Tests for use-api-poller (Mission C3 §T2.10 generic poller).
 *
 * Two test surfaces, matching the C2 use-voice-status-poller pattern:
 *
 * 1. ``intervalForFailureCount`` — pure decision helper. Unit-tests
 *    the backoff multiplier tiers against the documented decision
 *    table.
 * 2. Hook behavior — end-to-end with the api.get mock, validated via
 *    ``waitFor``. No precise-timing assertions (those belong to the
 *    pure helper).
 */
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { z } from "zod";

import { ApiError, api } from "@/lib/api";
import {
  DEGRADED_AFTER_5XX,
  DEGRADED_MULTIPLIER,
  FIRST_BACKOFF_AFTER_5XX,
  FIRST_BACKOFF_MULTIPLIER,
  SUSTAINED_BACKOFF_AFTER_5XX,
  SUSTAINED_BACKOFF_MULTIPLIER,
  intervalForFailureCount,
  useApiPoller,
} from "./use-api-poller";

const _TestSchema = z.object({
  ok: z.boolean(),
  value: z.number().optional(),
});

describe("intervalForFailureCount (Mission C3 §T2.10 pure helper)", () => {
  const baseline = 5_000;

  it("returns baseline for 0 and 1 consecutive 5xx", () => {
    expect(intervalForFailureCount(0, baseline)).toBe(baseline);
    expect(intervalForFailureCount(1, baseline)).toBe(baseline);
  });

  it("escalates to first-tier backoff at the documented threshold", () => {
    expect(intervalForFailureCount(FIRST_BACKOFF_AFTER_5XX, baseline)).toBe(
      baseline * FIRST_BACKOFF_MULTIPLIER,
    );
    expect(intervalForFailureCount(FIRST_BACKOFF_AFTER_5XX + 1, baseline)).toBe(
      baseline * FIRST_BACKOFF_MULTIPLIER,
    );
  });

  it("escalates to sustained-tier backoff at the documented threshold", () => {
    expect(intervalForFailureCount(SUSTAINED_BACKOFF_AFTER_5XX, baseline)).toBe(
      baseline * SUSTAINED_BACKOFF_MULTIPLIER,
    );
    expect(intervalForFailureCount(SUSTAINED_BACKOFF_AFTER_5XX + 5, baseline)).toBe(
      baseline * SUSTAINED_BACKOFF_MULTIPLIER,
    );
  });

  it("escalates to degraded-tier interval at the documented threshold", () => {
    expect(intervalForFailureCount(DEGRADED_AFTER_5XX, baseline)).toBe(
      baseline * DEGRADED_MULTIPLIER,
    );
    expect(intervalForFailureCount(DEGRADED_AFTER_5XX + 100, baseline)).toBe(
      baseline * DEGRADED_MULTIPLIER,
    );
  });

  it("scales proportionally with the baseline", () => {
    // The same failure count yields proportionally larger delays for
    // larger baselines.
    expect(intervalForFailureCount(0, 1_000)).toBe(1_000);
    expect(intervalForFailureCount(0, 10_000)).toBe(10_000);
    expect(intervalForFailureCount(FIRST_BACKOFF_AFTER_5XX, 1_000)).toBe(
      1_000 * FIRST_BACKOFF_MULTIPLIER,
    );
  });
});

describe("useApiPoller hook behavior (Mission C3 §T2.10)", () => {
  let apiGetSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    apiGetSpy = vi.spyOn(api, "get");
  });

  afterEach(() => {
    apiGetSpy.mockRestore();
  });

  it("returns data: null + error: ok on first mount before any poll lands", () => {
    apiGetSpy.mockImplementation(() => new Promise(() => {})); // never resolves
    const { result } = renderHook(() =>
      useApiPoller({
        endpoint: "/api/test",
        schema: _TestSchema,
        baselineIntervalMs: 5_000,
        enabled: true,
      }),
    );
    expect(result.current.data).toBeNull();
    expect(result.current.error).toBe("ok");
    expect(result.current.consecutive5xx).toBe(0);
  });

  it("populates data after a successful first poll", async () => {
    apiGetSpy.mockResolvedValueOnce({ ok: true, value: 42 });
    const { result } = renderHook(() =>
      useApiPoller({
        endpoint: "/api/test",
        schema: _TestSchema,
        baselineIntervalMs: 5_000,
        enabled: true,
      }),
    );
    await waitFor(() => {
      expect(result.current.data).toEqual({ ok: true, value: 42 });
    });
    expect(result.current.error).toBe("ok");
    expect(result.current.consecutive5xx).toBe(0);
  });

  it("disabled hook does NOT call the api", () => {
    apiGetSpy.mockResolvedValue({ ok: true });
    renderHook(() =>
      useApiPoller({
        endpoint: "/api/test",
        schema: _TestSchema,
        baselineIntervalMs: 5_000,
        enabled: false,
      }),
    );
    expect(apiGetSpy).not.toHaveBeenCalled();
  });

  it("transitions to degraded after 11 consecutive 5xx", async () => {
    apiGetSpy.mockImplementation(() =>
      Promise.reject(new ApiError(503, "service unavailable")),
    );
    const consoleWarnSpy = vi
      .spyOn(console, "warn")
      .mockImplementation(() => {});
    const { result } = renderHook(() =>
      useApiPoller({
        endpoint: "/api/test",
        schema: _TestSchema,
        baselineIntervalMs: 1, // tight so the backoff window completes quickly
        enabled: true,
        warnTag: "test.poller.degraded",
      }),
    );
    await waitFor(
      () => {
        expect(result.current.error).toBe("degraded");
      },
      { timeout: 5_000 },
    );
    expect(result.current.consecutive5xx).toBeGreaterThanOrEqual(
      DEGRADED_AFTER_5XX,
    );
    // One console.warn per degraded transition.
    expect(consoleWarnSpy).toHaveBeenCalledWith(
      "test.poller.degraded",
      expect.objectContaining({
        consecutive_5xx: expect.any(Number),
      }),
    );
    consoleWarnSpy.mockRestore();
  });

  it("recovery: resets consecutive5xx + clears degraded on first 2xx", async () => {
    let calls = 0;
    apiGetSpy.mockImplementation(() => {
      calls += 1;
      // First 11 calls 5xx (transitions to degraded), then 2xx.
      if (calls <= 11) {
        return Promise.reject(new ApiError(503, "down"));
      }
      return Promise.resolve({ ok: true });
    });
    const { result } = renderHook(() =>
      useApiPoller({
        endpoint: "/api/test",
        schema: _TestSchema,
        baselineIntervalMs: 1,
        enabled: true,
      }),
    );
    // Eventually we should recover (degraded → ok after the 2xx).
    await waitFor(
      () => {
        expect(result.current.error).toBe("ok");
        expect(result.current.consecutive5xx).toBe(0);
        expect(result.current.data).toEqual({ ok: true });
      },
      { timeout: 10_000 },
    );
  });

  it("does not bump 5xx count on 4xx errors", async () => {
    apiGetSpy.mockImplementation(() =>
      Promise.reject(new ApiError(401, "unauthorized")),
    );
    const { result } = renderHook(() =>
      useApiPoller({
        endpoint: "/api/test",
        schema: _TestSchema,
        baselineIntervalMs: 1,
        enabled: true,
      }),
    );
    // Wait a moment for several poll attempts.
    await new Promise((r) => setTimeout(r, 100));
    // 4xx should not bump the 5xx counter.
    expect(result.current.consecutive5xx).toBe(0);
    expect(result.current.error).toBe("ok");
  });
});
