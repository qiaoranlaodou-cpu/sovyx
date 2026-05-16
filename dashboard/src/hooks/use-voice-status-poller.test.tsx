/**
 * Tests for use-voice-status-poller (Mission C2 §T2.3).
 *
 * Two test surfaces:
 *
 * 1. ``intervalForFailureCount`` — pure decision helper, no React,
 *    no timers. Unit-tests the backoff tier boundaries against the
 *    decision table in the hook docstring.
 * 2. Hook behavior — minimal end-to-end with the api.get mock,
 *    validated via ``waitFor`` against the state-machine surface
 *    (status / error / consecutive5xx). No precise-timing
 *    assertions: those are owned by the pure helper above. This
 *    isolation sidesteps the React 19 + fake-timer act() warning
 *    that surfaces when both are mixed.
 */
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, api } from "@/lib/api";
import {
  BASELINE_INTERVAL_MS,
  DEGRADED_AFTER_5XX,
  DEGRADED_INTERVAL_MS,
  FIRST_BACKOFF_AFTER_5XX,
  FIRST_BACKOFF_INTERVAL_MS,
  SUSTAINED_BACKOFF_AFTER_5XX,
  SUSTAINED_BACKOFF_INTERVAL_MS,
  intervalForFailureCount,
  useVoiceStatusPoller,
} from "./use-voice-status-poller";

const _OK_STATUS = {
  pipeline: { running: true, state: "idle", latency_ms: 12 },
  capture: {
    running: true,
    input_device: 7,
    host_api: "ALSA",
    sample_rate: 16_000,
    frames_delivered: 1024,
    last_rms_db: -42,
  },
  stt: { engine: "MoonshineSTT", model: "moonshine-tiny", state: "ready" },
  tts: { engine: "PiperTTS", model: "en_US-lessac-medium", initialized: true },
  wake_word: { enabled: false, phrase: null },
  vad: { enabled: true },
  wyoming: { connected: false, endpoint: null },
  hardware: { tier: "PI5", ram_mb: 4096 },
  preflight_warnings: [],
};

describe("intervalForFailureCount (C2 §T2.3 pure helper)", () => {
  it("returns baseline for 0 and 1 consecutive 5xx", () => {
    expect(intervalForFailureCount(0)).toBe(BASELINE_INTERVAL_MS);
    expect(intervalForFailureCount(1)).toBe(BASELINE_INTERVAL_MS);
  });

  it("escalates to first-tier backoff at the documented threshold", () => {
    expect(intervalForFailureCount(FIRST_BACKOFF_AFTER_5XX)).toBe(
      FIRST_BACKOFF_INTERVAL_MS,
    );
    expect(intervalForFailureCount(FIRST_BACKOFF_AFTER_5XX + 1)).toBe(
      FIRST_BACKOFF_INTERVAL_MS,
    );
  });

  it("escalates to sustained-tier backoff at the documented threshold", () => {
    expect(intervalForFailureCount(SUSTAINED_BACKOFF_AFTER_5XX)).toBe(
      SUSTAINED_BACKOFF_INTERVAL_MS,
    );
    expect(intervalForFailureCount(SUSTAINED_BACKOFF_AFTER_5XX + 5)).toBe(
      SUSTAINED_BACKOFF_INTERVAL_MS,
    );
  });

  it("escalates to degraded-tier interval at the documented threshold", () => {
    expect(intervalForFailureCount(DEGRADED_AFTER_5XX)).toBe(
      DEGRADED_INTERVAL_MS,
    );
    expect(intervalForFailureCount(DEGRADED_AFTER_5XX + 100)).toBe(
      DEGRADED_INTERVAL_MS,
    );
  });

  it("is monotonically non-decreasing across the 5xx count axis", () => {
    let prev = intervalForFailureCount(0);
    for (let i = 1; i <= 50; i++) {
      const next = intervalForFailureCount(i);
      expect(next).toBeGreaterThanOrEqual(prev);
      prev = next;
    }
  });
});

describe("useVoiceStatusPoller hook (C2 §T2.3)", () => {
  let getSpy: ReturnType<typeof vi.spyOn>;
  let warnSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    getSpy = vi.spyOn(api, "get");
    warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
  });

  afterEach(() => {
    getSpy.mockRestore();
    warnSpy.mockRestore();
  });

  it("returns null status when disabled and never polls", () => {
    getSpy.mockResolvedValue(_OK_STATUS);
    const { result } = renderHook(() =>
      useVoiceStatusPoller({ enabled: false }),
    );
    expect(result.current.status).toBeNull();
    expect(result.current.error).toBe("ok");
    expect(getSpy).not.toHaveBeenCalled();
  });

  it("populates status after a successful first poll", async () => {
    getSpy.mockResolvedValue(_OK_STATUS);
    const { result } = renderHook(() =>
      useVoiceStatusPoller({ enabled: true }),
    );
    await waitFor(() => {
      expect(result.current.status).not.toBeNull();
    });
    expect(result.current.error).toBe("ok");
    expect(result.current.consecutive5xx).toBe(0);
  });

  it("increments consecutive5xx on a 5xx response", async () => {
    getSpy.mockRejectedValue(new ApiError(503, "downstream down"));
    const { result } = renderHook(() =>
      useVoiceStatusPoller({ enabled: true }),
    );
    await waitFor(() => {
      expect(result.current.consecutive5xx).toBeGreaterThanOrEqual(1);
    });
    // Still ok-state at 1 (below degraded threshold).
    expect(result.current.error).toBe("ok");
  });

  it("does NOT count 4xx as backoff failures", async () => {
    getSpy.mockRejectedValueOnce(new ApiError(401, "unauthorized"));
    getSpy.mockResolvedValueOnce(_OK_STATUS);
    const { result } = renderHook(() =>
      useVoiceStatusPoller({ enabled: true }),
    );
    // After the 401 + the subsequent 2xx, consecutive5xx must still be 0.
    await waitFor(() => {
      expect(result.current.status).not.toBeNull();
    });
    expect(result.current.consecutive5xx).toBe(0);
    expect(result.current.error).toBe("ok");
  });

  it("resets consecutive5xx on a 2xx after prior 5xx", async () => {
    getSpy.mockRejectedValueOnce(new ApiError(503, "down"));
    getSpy.mockResolvedValue(_OK_STATUS);
    const { result } = renderHook(() =>
      useVoiceStatusPoller({ enabled: true }),
    );
    await waitFor(
      () => {
        expect(result.current.consecutive5xx).toBe(0);
        expect(result.current.status).not.toBeNull();
      },
      { timeout: 3000 },
    );
    expect(result.current.error).toBe("ok");
  });
});
