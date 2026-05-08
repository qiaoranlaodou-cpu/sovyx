/**
 * Training slice tests — Mission MISSION-v0.30.0-single-mind-ga §T1.3 (D5).
 *
 * Validates: initial state, fetchTrainingJobs (success + error),
 * fetchTrainingJobDetail, startTraining (202 + 409 + 503),
 * cancelTrainingJob (success + error), error helper extraction.
 *
 * WebSocket subscribeToTrainingJob is NOT tested at this level —
 * jsdom's WebSocket mock has limitations; that flow gets covered by
 * the integration test (Mission §T1.6).
 */
import { describe, it, expect, beforeEach, vi } from "vitest";

import type {
  TrainingJobStatus,
  TrainingJobSummary,
} from "@/types/api";

import { useDashboardStore } from "../dashboard";

const HEALTHY_JOB: TrainingJobSummary = {
  job_id: "aria",
  wake_word: "Aria",
  mind_id: "aria",
  language: "en",
  status: "synthesizing" as TrainingJobStatus,
  progress: 0.5,
  samples_generated: 100,
  target_samples: 200,
  started_at: "2026-05-03T00:00:00Z",
  updated_at: "2026-05-03T00:01:00Z",
  completed_at: "",
  output_path: "",
  error_summary: "",
  cancelled_signalled: false,
};

const COMPLETE_JOB: TrainingJobSummary = {
  ...HEALTHY_JOB,
  status: "complete" as TrainingJobStatus,
  progress: 1.0,
  samples_generated: 200,
  completed_at: "2026-05-03T00:30:00Z",
  output_path: "/data/wake_word_models/pretrained/aria.onnx",
};

function _resetTrainingState() {
  useDashboardStore.setState({
    trainingJobs: [],
    currentTrainingJob: null,
    trainingLoading: false,
    trainingError: null,
    trainingWs: null,
  });
}

beforeEach(() => {
  _resetTrainingState();
  vi.restoreAllMocks();
});

// ── Initial state ─────────────────────────────────────────────────────

describe("training slice — initial state", () => {
  it("starts with empty jobs and no error", () => {
    const state = useDashboardStore.getState();
    expect(state.trainingJobs).toEqual([]);
    expect(state.currentTrainingJob).toBeNull();
    expect(state.trainingLoading).toBe(false);
    expect(state.trainingError).toBeNull();
    expect(state.trainingWs).toBeNull();
  });
});

// ── clearTrainingError ────────────────────────────────────────────────

describe("training slice — clearTrainingError", () => {
  it("clears the error field", () => {
    useDashboardStore.setState({ trainingError: "boom" });
    useDashboardStore.getState().clearTrainingError();
    expect(useDashboardStore.getState().trainingError).toBeNull();
  });
});

// ── fetchTrainingJobs ─────────────────────────────────────────────────

describe("training slice — fetchTrainingJobs", () => {
  it("populates jobs on success", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ jobs: [HEALTHY_JOB], total_count: 1 }),
    } as Response);

    await useDashboardStore.getState().fetchTrainingJobs();

    const state = useDashboardStore.getState();
    expect(state.trainingJobs).toHaveLength(1);
    expect(state.trainingJobs[0].job_id).toBe("aria");
    expect(state.trainingLoading).toBe(false);
    expect(state.trainingError).toBeNull();
  });

  it("sets error on network failure", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("Network down"));

    await useDashboardStore.getState().fetchTrainingJobs();

    const state = useDashboardStore.getState();
    expect(state.trainingLoading).toBe(false);
    expect(state.trainingError).toContain("Network");
  });
});

// ── fetchTrainingJobDetail ────────────────────────────────────────────

describe("training slice — fetchTrainingJobDetail", () => {
  it("populates currentTrainingJob on success", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: true,
      json: () =>
        Promise.resolve({
          summary: HEALTHY_JOB,
          history: [{ status: "pending", progress: 0 }],
          history_truncated: false,
        }),
    } as Response);

    await useDashboardStore.getState().fetchTrainingJobDetail("aria");

    const state = useDashboardStore.getState();
    expect(state.currentTrainingJob).not.toBeNull();
    expect(state.currentTrainingJob?.summary.job_id).toBe("aria");
  });
});

// ── startTraining ─────────────────────────────────────────────────────

describe("training slice — startTraining", () => {
  it("returns job_id on 202 Accepted", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    // POST returns 202.
    fetchSpy.mockResolvedValueOnce({
      ok: true,
      json: () =>
        Promise.resolve({
          job_id: "aria",
          stream_url: "/api/voice/training/jobs/aria/stream",
        }),
    } as Response);
    // Refetch returns the new job.
    fetchSpy.mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ jobs: [HEALTHY_JOB], total_count: 1 }),
    } as Response);

    const result = await useDashboardStore.getState().startTraining({
      wake_word: "Aria",
      mind_id: "aria",
      negatives_dir: "/data/negatives",
    });

    expect(result).toBe("aria");
    expect(useDashboardStore.getState().trainingError).toBeNull();
    expect(useDashboardStore.getState().trainingJobs).toHaveLength(1);
  });

  it("returns null + populates error on 409 Conflict", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: false,
      status: 409,
      text: () =>
        Promise.resolve(
          JSON.stringify({
            detail: "A training job for 'Aria' is already in flight.",
          }),
        ),
    } as Response);

    const result = await useDashboardStore.getState().startTraining({
      wake_word: "Aria",
      mind_id: "aria",
      negatives_dir: "/data/negatives",
    });

    expect(result).toBeNull();
    expect(useDashboardStore.getState().trainingError).toContain("already in flight");
  });

  it("returns null + populates error on 503 backend unavailable", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: false,
      status: 503,
      text: () =>
        Promise.resolve(
          JSON.stringify({
            detail:
              "Trainer backend unavailable: install [training] extras + register_default_backend",
          }),
        ),
    } as Response);

    const result = await useDashboardStore.getState().startTraining({
      wake_word: "Aria",
      mind_id: "aria",
      negatives_dir: "/data/negatives",
    });

    expect(result).toBeNull();
    expect(useDashboardStore.getState().trainingError).toContain(
      "register_default_backend",
    );
  });
});

// ── cancelTrainingJob ─────────────────────────────────────────────────

describe("training slice — cancelTrainingJob", () => {
  it("returns true on successful cancel", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    fetchSpy.mockResolvedValueOnce({
      ok: true,
      json: () =>
        Promise.resolve({
          job_id: "aria",
          cancel_signal_written: true,
          already_terminal: false,
        }),
    } as Response);
    fetchSpy.mockResolvedValueOnce({
      ok: true,
      json: () =>
        Promise.resolve({
          jobs: [{ ...HEALTHY_JOB, cancelled_signalled: true }],
          total_count: 1,
        }),
    } as Response);

    const result = await useDashboardStore.getState().cancelTrainingJob("aria");

    expect(result).toBe(true);
    expect(useDashboardStore.getState().trainingJobs[0].cancelled_signalled).toBe(true);
  });

  it("returns false + populates error on 404", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: false,
      status: 404,
      text: () => Promise.resolve(JSON.stringify({ detail: "job not found: ghost" })),
    } as Response);

    const result = await useDashboardStore.getState().cancelTrainingJob("ghost");

    expect(result).toBe(false);
    expect(useDashboardStore.getState().trainingError).toContain("not found");
  });
});

// ── unsubscribeFromTrainingJob (no WS active) ─────────────────────────

describe("training slice — unsubscribeFromTrainingJob", () => {
  it("is a no-op when no WS is active", () => {
    expect(() => useDashboardStore.getState().unsubscribeFromTrainingJob()).not.toThrow();
    expect(useDashboardStore.getState().trainingWs).toBeNull();
  });
});

// ── subscribeToTrainingJob — WS auth token regression ─────────────────
//
// Phase 3.A Layer A — anti-pattern #35 cluster P0.A5. Pre-fix this slice
// read ``sessionStorage.sovyx_auth_token`` while every other site
// (lib/api.ts, hooks, calibration slice) reads ``sovyx_token``. The
// token-key drift broke wake-word training WS auth silently because
// no test exercised the WS path. Pin the regression: the URL must
// carry the canonical key's value.

describe("training slice — subscribeToTrainingJob WS auth token", () => {
  it("constructs ws URL using the canonical sovyx_token key", () => {
    sessionStorage.clear();
    sessionStorage.setItem("sovyx_token", "canonical-token-abc");
    // Drift sentinel: if production code regresses to the wrong key,
    // we want the test to fail with a clear signal rather than masking.
    sessionStorage.setItem("sovyx_auth_token", "DRIFT-SENTINEL-MUST-NOT-APPEAR");

    let observedUrl = "";
    const OriginalWebSocket = globalThis.WebSocket;
    class MockWebSocket {
      url: string;
      onopen: ((this: WebSocket) => unknown) | null = null;
      onmessage: ((this: WebSocket, ev: MessageEvent) => unknown) | null = null;
      onclose: ((this: WebSocket, ev: CloseEvent) => unknown) | null = null;
      onerror: ((this: WebSocket, ev: Event) => unknown) | null = null;
      readyState = 0;
      constructor(url: string) {
        this.url = url;
        observedUrl = url;
      }
      close(): void {
        this.readyState = 3;
      }
      send(): void {
        // no-op — tests don't exercise outbound frames
      }
    }
    // ts-eslint understandably balks at the assignment — this is the
    // standard jsdom WebSocket-mock pattern.
    (globalThis as { WebSocket: typeof WebSocket }).WebSocket =
      MockWebSocket as unknown as typeof WebSocket;

    try {
      useDashboardStore.getState().subscribeToTrainingJob("aria-job-1");
      expect(observedUrl).toContain("canonical-token-abc");
      expect(observedUrl).not.toContain("DRIFT-SENTINEL-MUST-NOT-APPEAR");
    } finally {
      // Tear down the WS + restore the global so subsequent tests are clean.
      useDashboardStore.getState().unsubscribeFromTrainingJob();
      (globalThis as { WebSocket: typeof WebSocket }).WebSocket =
        OriginalWebSocket;
      sessionStorage.clear();
    }
  });
});
