/**
 * Tests for ``verifyVoiceRunning`` — shared receipt-check helper used by
 * every "voice was just enabled" surface (VoiceSetupModal, VoiceStep,
 * _ProfileReview).
 *
 * v0.31.6 T3.1 + T3.5: each branch of the ``VerificationResult`` union
 * is locked in by an explicit test so a regression that re-introduces
 * swallow-all-errors (the pre-T3.5 bug) lands red instead of green.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";

import { verifyVoiceRunning } from "./use-voice-running-verification";

const mockGet = vi.fn();

vi.mock("@/lib/api", () => ({
  api: {
    get: (...args: unknown[]) => mockGet(...args),
  },
  // Re-export a minimal ApiError so the hook's ``err instanceof ApiError``
  // narrowing finds the same constructor the test creates. Keeping the
  // shape identical to the production class is enough for instanceof.
  ApiError: class ApiError extends Error {
    public readonly body: Record<string, unknown> | null = null;
    constructor(
      public status: number,
      message: string,
    ) {
      super(message);
      this.name = "ApiError";
    }
  },
}));

// Pull the test ApiError back out of the mocked module so test bodies
// throw the SAME constructor the hook checks against. Importing the
// real one would not match because vi.mock rewrites the module exports.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
let ApiError: any;
beforeEach(async () => {
  mockGet.mockReset();
  const apiMod = await import("@/lib/api");
  ApiError = apiMod.ApiError;
});

// Tight delays keep the test suite fast — production defaults are
// 3 attempts × 1000 ms = 3 s; tests use 0 ms so the retry loop fires
// instantly without sleeps holding up the suite.
const TEST_OPTS = { attempts: 3, delayMs: 0 };

describe("verifyVoiceRunning", () => {
  it("returns {status: 'running'} when the first poll reports running=true", async () => {
    mockGet.mockResolvedValueOnce({ pipeline: { running: true } });

    const result = await verifyVoiceRunning(TEST_OPTS);

    expect(result).toEqual({ status: "running" });
    expect(mockGet).toHaveBeenCalledTimes(1);
    expect(mockGet).toHaveBeenCalledWith(
      "/api/voice/status",
      expect.objectContaining({ schema: expect.anything() }),
    );
  });

  it("returns {status: 'running'} when a later poll flips to running=true", async () => {
    // First two polls report running=false; the third confirms running.
    mockGet
      .mockResolvedValueOnce({ pipeline: { running: false } })
      .mockResolvedValueOnce({ pipeline: { running: false } })
      .mockResolvedValueOnce({ pipeline: { running: true } });

    const result = await verifyVoiceRunning(TEST_OPTS);

    expect(result).toEqual({ status: "running" });
    expect(mockGet).toHaveBeenCalledTimes(3);
  });

  it("returns {status: 'not_running'} when every poll reports running=false", async () => {
    mockGet.mockResolvedValue({ pipeline: { running: false } });

    const result = await verifyVoiceRunning(TEST_OPTS);

    expect(result).toEqual({ status: "not_running" });
    expect(mockGet).toHaveBeenCalledTimes(3);
  });

  it("returns {status: 'auth_failure'} immediately on 401 (no retry)", async () => {
    mockGet.mockRejectedValueOnce(new ApiError(401, "Unauthorized"));

    const result = await verifyVoiceRunning(TEST_OPTS);

    expect(result).toEqual({ status: "auth_failure" });
    // Auth failures are deterministic — the helper bails on the first
    // attempt instead of burning the retry budget.
    expect(mockGet).toHaveBeenCalledTimes(1);
  });

  it("returns {status: 'auth_failure'} immediately on 403 (no retry)", async () => {
    mockGet.mockRejectedValueOnce(new ApiError(403, "Forbidden"));

    const result = await verifyVoiceRunning(TEST_OPTS);

    expect(result).toEqual({ status: "auth_failure" });
    expect(mockGet).toHaveBeenCalledTimes(1);
  });

  it("returns {status: 'endpoint_missing'} immediately on 404 (no retry)", async () => {
    mockGet.mockRejectedValueOnce(new ApiError(404, "Not Found"));

    const result = await verifyVoiceRunning(TEST_OPTS);

    expect(result).toEqual({ status: "endpoint_missing" });
    expect(mockGet).toHaveBeenCalledTimes(1);
  });

  it("returns {status: 'transient_failure', lastError} when every poll throws", async () => {
    // Three different non-classified failures — 5xx + network + ApiError 502.
    mockGet
      .mockRejectedValueOnce(new Error("ECONNRESET"))
      .mockRejectedValueOnce(new ApiError(502, "Bad Gateway"))
      .mockRejectedValueOnce(new Error("network down"));

    const result = await verifyVoiceRunning(TEST_OPTS);

    expect(result.status).toBe("transient_failure");
    if (result.status === "transient_failure") {
      // The helper retains the LAST error message so the operator-facing
      // banner can surface it via {{lastError}} interpolation.
      expect(result.lastError).toBe("network down");
    }
    expect(mockGet).toHaveBeenCalledTimes(3);
  });

  it("retries past a transient blip + returns running when pipeline comes up", async () => {
    // Real-world: one network blip then the pipeline is healthy.
    mockGet
      .mockRejectedValueOnce(new Error("ECONNRESET"))
      .mockResolvedValueOnce({ pipeline: { running: true } });

    const result = await verifyVoiceRunning(TEST_OPTS);

    expect(result).toEqual({ status: "running" });
    expect(mockGet).toHaveBeenCalledTimes(2);
  });

  it("classifies a 401 mid-loop without retrying further", async () => {
    // First poll: pipeline not running. Second poll: token expired.
    // The helper must abandon the loop on 401 and surface auth_failure.
    mockGet
      .mockResolvedValueOnce({ pipeline: { running: false } })
      .mockRejectedValueOnce(new ApiError(401, "Unauthorized"));

    const result = await verifyVoiceRunning(TEST_OPTS);

    expect(result).toEqual({ status: "auth_failure" });
    expect(mockGet).toHaveBeenCalledTimes(2);
  });
});
