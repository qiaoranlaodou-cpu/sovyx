/**
 * use-voice-running-verification — shared "is voice actually running?"
 * verification helper for every "voice was just enabled" surface.
 *
 * v0.31.6 T3.1 + T3.5 — extracted from the inline ``_verifyVoiceRunning``
 * that v0.31.4 GAP 7 added to ``_ProfileReview.tsx``. Three sibling
 * surfaces (``VoiceSetupModal``, ``VoiceStep::enableWithDevices`` and
 * the original ``_ProfileReview``) each had a "voice setup completed"
 * branch that trusted ``result.ok===true`` from ``/api/voice/enable``
 * (or the calibration completion flow) without ever polling
 * ``/api/voice/status`` to confirm the pipeline actually wired up.
 * If the backend silently failed to register the pipeline (e.g.
 * ``mind.yaml`` write wrapped in ``contextlib.suppress``), the
 * operator clicked Confirm + got dropped on the next page with voice
 * disabled and zero feedback. v0.31.6 T3.1 ports the receipt-check
 * pattern to all three surfaces via this shared helper.
 *
 * v0.31.6 T3.5 closes the M5 finding — the original inline helper
 * swallowed every error as "transient blip", which meant 401/403/404
 * responses (auth expired, route gone after backend skew) were
 * indistinguishable from a one-tick network glitch and the operator
 * saw the wrong banner forever. The classified ``VerificationResult``
 * union below lets call sites surface differentiated i18n messages
 * (auth-expired CTA vs. transient retry vs. endpoint-missing operator
 * action) instead of a single catch-all "verification failed".
 *
 * Why a function and not a hook:
 *   The verification fires from a button-click callback (Confirm,
 *   Enable Voice). State for "verifying" + "verification error" lives
 *   on the calling component because the message text and recovery CTA
 *   depend on the surface (modal vs. onboarding vs. wizard). Returning
 *   a discriminated-union value lets each surface decide what to show.
 */

import { api, ApiError } from "@/lib/api";
import { VoiceStatusResponseSchema } from "@/types/schemas";

export type VerificationResult =
  /** ``pipeline.running===true`` was observed on at least one poll. */
  | { status: "running" }
  /** All polls completed successfully but every one reported running=false. */
  | { status: "not_running" }
  /** 401/403 — operator's session token is gone or rejected. */
  | { status: "auth_failure" }
  /** 404 — backend route is missing (frontend / backend version skew). */
  | { status: "endpoint_missing" }
  /** Network blip / 5xx storm — every attempt hit a non-classified failure. */
  | { status: "transient_failure"; lastError: string };

interface VerifyVoiceRunningOptions {
  /** Number of poll attempts. Defaults to 3 (matches v0.31.4 GAP 7). */
  attempts?: number;
  /** Inter-attempt delay in milliseconds. Defaults to 1000 ms. */
  delayMs?: number;
}

interface VoiceStatusResponse {
  pipeline: { running: boolean };
}

const DEFAULT_ATTEMPTS = 3;
const DEFAULT_DELAY_MS = 1000;

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Poll ``/api/voice/status`` up to ``attempts`` times and classify the
 * outcome.
 *
 * Decision table:
 *
 *   * any poll returns ``pipeline.running===true``        → ``running``
 *   * ``ApiError(401|403)`` on any attempt                → ``auth_failure`` (no retry)
 *   * ``ApiError(404)`` on any attempt                    → ``endpoint_missing`` (no retry)
 *   * every poll returned successfully + running=false    → ``not_running``
 *   * every poll threw a non-classified error             → ``transient_failure``
 *
 * The early-exit on auth/endpoint failures matches the verifier-verdict
 * ordering rule (CLAUDE.md anti-pattern #37): cheapest + most-actionable
 * verdict first, retry only the genuinely transient classes.
 */
export async function verifyVoiceRunning(
  opts?: VerifyVoiceRunningOptions,
): Promise<VerificationResult> {
  const attempts = opts?.attempts ?? DEFAULT_ATTEMPTS;
  const delayMs = opts?.delayMs ?? DEFAULT_DELAY_MS;

  let lastError = "";
  let observedSuccessfulPoll = false;

  for (let attempt = 0; attempt < attempts; attempt += 1) {
    if (attempt > 0) {
      await sleep(delayMs);
    }
    try {
      const status = await api.get<VoiceStatusResponse>("/api/voice/status", {
        schema: VoiceStatusResponseSchema,
      });
      observedSuccessfulPoll = true;
      if (status.pipeline.running === true) {
        return { status: "running" };
      }
      // Successful response but pipeline isn't up yet — keep retrying.
    } catch (err) {
      if (err instanceof ApiError) {
        // Auth failures + missing routes are deterministic operator
        // actions; retrying won't change the outcome and only delays
        // the actionable banner. Bail immediately.
        if (err.status === 401 || err.status === 403) {
          return { status: "auth_failure" };
        }
        if (err.status === 404) {
          return { status: "endpoint_missing" };
        }
      }
      lastError = err instanceof Error ? err.message : String(err);
      // Non-classified — treat as transient and continue the retry loop.
    }
  }

  // Loop exhausted without a running observation. If we ever saw a
  // successful poll the pipeline is just slow / not registered (the
  // canonical v0.31.4 GAP 7 case); if every attempt threw, surface the
  // last error so the operator can triage from the banner.
  if (observedSuccessfulPoll) {
    return { status: "not_running" };
  }
  return { status: "transient_failure", lastError };
}
