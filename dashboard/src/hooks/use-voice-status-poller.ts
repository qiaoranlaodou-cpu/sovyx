/**
 * use-voice-status-poller — Mission C2 §T2.3 frontend circuit breaker.
 *
 * Wraps the 2 Hz polling loop the voice page (``pages/voice.tsx``)
 * used pre-mission to read ``GET /api/voice/status``. The
 * v0.43.1 forensic audit (§C2 + §H8) observed the pre-mission
 * loop hammered the backend 960× over 480 s with NO backoff while
 * the boundary 500'd every call — contributing to H4 (RSS growth
 * from ExceptionGroup retention) and H6 (~10 s frame-drop cadence
 * correlated with the 500 cadence).
 *
 * This hook adds an exponential 5xx backoff with a degraded-state
 * surface so a recurrence of the bug class cannot amplify into the
 * same observability storm.
 *
 * Mission anchor:
 * docs-internal/missions/MISSION-c2-voice-status-response-contract-2026-05-16.md §T2.3
 */
import { useEffect, useRef, useState } from "react";
import type { z } from "zod";

import { ApiError, api, isAbortError } from "@/lib/api";
import { VoiceStatusResponseSchema } from "@/types/schemas";

/**
 * Inferred from the zod schema rather than imported from
 * ``types/api.ts`` — the page-local ``VoiceStatus`` interface in
 * ``pages/voice.tsx`` predates this hook and is intentionally not
 * promoted (the schema is the canonical runtime contract).
 */
export type VoiceStatusResponse = z.infer<typeof VoiceStatusResponseSchema>;

export const BASELINE_INTERVAL_MS = 500;
export const FIRST_BACKOFF_INTERVAL_MS = 1500;
export const SUSTAINED_BACKOFF_INTERVAL_MS = 5000;
export const DEGRADED_INTERVAL_MS = 10_000;
export const FIRST_BACKOFF_AFTER_5XX = 2;
export const SUSTAINED_BACKOFF_AFTER_5XX = 4;
export const DEGRADED_AFTER_5XX = 11;

export type PollerErrorState = "ok" | "degraded";

export interface UseVoiceStatusPollerOptions {
  /** Master enable — when false, no poll runs and prior state is preserved. */
  enabled: boolean;
}

export interface VoiceStatusPollerResult {
  /** Latest status — null until first successful poll. */
  status: VoiceStatusResponse | null;
  /** ``"degraded"`` after 11 consecutive 5xx — surface as a UI banner. */
  error: PollerErrorState;
  /** Count of consecutive 5xx responses — exposed for diagnostics. */
  consecutive5xx: number;
}

/**
 * Tier the next-tick delay based on consecutive 5xx count.
 *
 * Decision table:
 *
 *   0–1 5xx in a row →   500 ms (baseline; transient blips don't penalise)
 *   2–3              → 1 500 ms (early backoff)
 *   4–10             → 5 000 ms (sustained backoff)
 *   ≥ 11             → 10 000 ms (degraded — banner shown)
 *
 * Returns to baseline on the first 2xx.
 */
export function intervalForFailureCount(consecutive5xx: number): number {
  if (consecutive5xx >= DEGRADED_AFTER_5XX) return DEGRADED_INTERVAL_MS;
  if (consecutive5xx >= SUSTAINED_BACKOFF_AFTER_5XX) return SUSTAINED_BACKOFF_INTERVAL_MS;
  if (consecutive5xx >= FIRST_BACKOFF_AFTER_5XX) return FIRST_BACKOFF_INTERVAL_MS;
  return BASELINE_INTERVAL_MS;
}

/**
 * Hook that polls ``GET /api/voice/status`` with exponential 5xx backoff.
 *
 * Each consumer instance owns its own backoff state — mounting the
 * voice page twice does NOT share a single backoff counter (intentional;
 * matches React's component-scoped state model).
 *
 * Emits ``console.warn("voice.status.poller.degraded", …)`` exactly
 * once when the hook transitions into ``error: "degraded"``. Operators
 * with devtools open see the trail; production builds without devtools
 * see only the in-page banner.
 */
export function useVoiceStatusPoller(
  options: UseVoiceStatusPollerOptions,
): VoiceStatusPollerResult {
  const { enabled } = options;
  const [status, setStatus] = useState<VoiceStatusResponse | null>(null);
  const [errorState, setErrorState] = useState<PollerErrorState>("ok");
  const [consecutive5xx, setConsecutive5xx] = useState(0);

  // Refs hold the latest values for the polling loop without triggering
  // re-renders. The loop reads these on each tick to decide the next
  // delay.
  const consecutive5xxRef = useRef(0);
  const errorStateRef = useRef<PollerErrorState>("ok");
  const enteredDegradedRef = useRef(false);

  useEffect(() => {
    if (!enabled) {
      // Reset state when disabled so a re-enable starts from baseline.
      consecutive5xxRef.current = 0;
      errorStateRef.current = "ok";
      enteredDegradedRef.current = false;
      setConsecutive5xx(0);
      setErrorState("ok");
      return;
    }
    const controller = new AbortController();
    let cancelled = false;
    let timeoutId: ReturnType<typeof setTimeout> | undefined;

    const scheduleNext = (delay: number) => {
      if (cancelled) return;
      timeoutId = setTimeout(() => void tick(), delay);
    };

    const tick = async () => {
      if (cancelled) return;
      try {
        const next = await api.get<VoiceStatusResponse>("/api/voice/status", {
          signal: controller.signal,
          schema: VoiceStatusResponseSchema,
        });
        if (cancelled) return;
        // Success — reset backoff state.
        if (consecutive5xxRef.current !== 0 || errorStateRef.current !== "ok") {
          consecutive5xxRef.current = 0;
          errorStateRef.current = "ok";
          enteredDegradedRef.current = false;
          setConsecutive5xx(0);
          setErrorState("ok");
        }
        setStatus(next);
        scheduleNext(BASELINE_INTERVAL_MS);
      } catch (err) {
        if (isAbortError(err)) return;
        let next5xx = consecutive5xxRef.current;
        if (err instanceof ApiError && err.status >= 500) {
          next5xx += 1;
        } else if (err instanceof ApiError) {
          // 4xx — auth / not-found / rate-limit. Don't bump 5xx count;
          // surface as transient. The voice page's outer fetchData
          // path owns the persistent-error UX.
        } else {
          // Network / parse error — treat like a 5xx for backoff
          // purposes (server-side problem the user can't act on).
          next5xx += 1;
        }
        consecutive5xxRef.current = next5xx;
        setConsecutive5xx(next5xx);
        if (next5xx >= DEGRADED_AFTER_5XX && !enteredDegradedRef.current) {
          enteredDegradedRef.current = true;
          errorStateRef.current = "degraded";
          setErrorState("degraded");
          const lastStatus = err instanceof ApiError ? err.status : "network";
          const lastError = err instanceof Error ? err.message : String(err);
          // One warn per degraded transition — NOT per failed poll.
          // eslint-disable-next-line no-console
          console.warn("voice.status.poller.degraded", {
            consecutive_5xx: next5xx,
            last_status: lastStatus,
            last_error: lastError,
          });
        }
        scheduleNext(intervalForFailureCount(next5xx));
      }
    };

    // First tick fires immediately on enable; subsequent ticks
    // schedule themselves via ``scheduleNext``.
    void tick();

    return () => {
      cancelled = true;
      controller.abort();
      if (timeoutId !== undefined) clearTimeout(timeoutId);
    };
  }, [enabled]);

  return { status, error: errorState, consecutive5xx };
}
