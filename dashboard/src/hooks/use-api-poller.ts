/**
 * use-api-poller — generic GET-endpoint poller with exponential 5xx backoff.
 *
 * Mission C3 §T2.10 — extracted from the C2
 * ``use-voice-status-poller.ts`` pattern so the failover-history
 * widget (and any future polling consumer) gets the same circuit-
 * breaker discipline without duplicating state-machine code.
 *
 * Behaviour mirror:
 *
 *   0–1 5xx in a row →   baseline interval (transient blips don't penalise)
 *   2–3              →   3× baseline (early backoff)
 *   4–10             →  10× baseline (sustained backoff)
 *   ≥ 11             →  20× baseline (degraded — banner shown)
 *
 * Returns to baseline on the first 2xx. Each consumer instance owns
 * its own backoff state — mounting the same hook twice does NOT
 * share counters.
 *
 * Why this isn't a refactor of ``use-voice-status-poller.ts``:
 * C2 mission stays OPEN through F1/F4 telemetry calibration window;
 * touching the C2 hook's shipped code risks invalidating that
 * calibration. A future mission can DRY the two consumers once C2
 * STRICT-flips.
 *
 * Mission anchor:
 * docs-internal/missions/MISSION-c3-failover-ladder-iteration-2026-05-16.md
 * §T2.10.
 */
import { useEffect, useRef, useState } from "react";
import type { z, ZodTypeAny } from "zod";

import { ApiError, api, isAbortError } from "@/lib/api";

export const FIRST_BACKOFF_AFTER_5XX = 2;
export const SUSTAINED_BACKOFF_AFTER_5XX = 4;
export const DEGRADED_AFTER_5XX = 11;
export const FIRST_BACKOFF_MULTIPLIER = 3;
export const SUSTAINED_BACKOFF_MULTIPLIER = 10;
export const DEGRADED_MULTIPLIER = 20;

export type ApiPollerErrorState = "ok" | "degraded";

export interface UseApiPollerOptions<S extends ZodTypeAny> {
  /** The endpoint path. Static across the hook's lifetime. */
  endpoint: string;
  /** Zod schema for runtime validation. */
  schema: S;
  /** Baseline poll interval in ms. */
  baselineIntervalMs: number;
  /** Master enable — when false, no poll runs and prior state is preserved. */
  enabled: boolean;
  /** Optional warn-tag for the one-shot ``console.warn`` on degraded transition. */
  warnTag?: string;
}

export interface ApiPollerResult<T> {
  /** Latest fetched payload — null until first successful poll. */
  data: T | null;
  /** ``"degraded"`` after 11 consecutive 5xx — surface as UI banner. */
  error: ApiPollerErrorState;
  /** Count of consecutive 5xx responses — exposed for diagnostics. */
  consecutive5xx: number;
}

/**
 * Tier the next-tick delay based on consecutive 5xx count and the
 * provided baseline.
 */
export function intervalForFailureCount(
  consecutive5xx: number,
  baselineMs: number,
): number {
  if (consecutive5xx >= DEGRADED_AFTER_5XX) return baselineMs * DEGRADED_MULTIPLIER;
  if (consecutive5xx >= SUSTAINED_BACKOFF_AFTER_5XX) {
    return baselineMs * SUSTAINED_BACKOFF_MULTIPLIER;
  }
  if (consecutive5xx >= FIRST_BACKOFF_AFTER_5XX) {
    return baselineMs * FIRST_BACKOFF_MULTIPLIER;
  }
  return baselineMs;
}

/**
 * Generic polling hook with exponential 5xx backoff + degraded-state
 * surface. ``S`` is the zod schema type; ``T`` is inferred from it.
 */
export function useApiPoller<S extends ZodTypeAny, T = z.infer<S>>(
  options: UseApiPollerOptions<S>,
): ApiPollerResult<T> {
  const { endpoint, schema, baselineIntervalMs, enabled, warnTag } = options;
  const [data, setData] = useState<T | null>(null);
  const [errorState, setErrorState] = useState<ApiPollerErrorState>("ok");
  const [consecutive5xx, setConsecutive5xx] = useState(0);

  const consecutive5xxRef = useRef(0);
  const errorStateRef = useRef<ApiPollerErrorState>("ok");
  const enteredDegradedRef = useRef(false);

  useEffect(() => {
    if (!enabled) {
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
        const next = await api.get<T>(endpoint, {
          signal: controller.signal,
          schema,
        });
        if (cancelled) return;
        if (consecutive5xxRef.current !== 0 || errorStateRef.current !== "ok") {
          consecutive5xxRef.current = 0;
          errorStateRef.current = "ok";
          enteredDegradedRef.current = false;
          setConsecutive5xx(0);
          setErrorState("ok");
        }
        setData(next);
        scheduleNext(baselineIntervalMs);
      } catch (err) {
        if (isAbortError(err)) return;
        let next5xx = consecutive5xxRef.current;
        if (err instanceof ApiError && err.status >= 500) {
          next5xx += 1;
        } else if (err instanceof ApiError) {
          // 4xx — auth / not-found. Don't bump 5xx count.
        } else {
          // Network / parse error — bump backoff.
          next5xx += 1;
        }
        consecutive5xxRef.current = next5xx;
        setConsecutive5xx(next5xx);
        if (next5xx >= DEGRADED_AFTER_5XX && !enteredDegradedRef.current) {
          enteredDegradedRef.current = true;
          errorStateRef.current = "degraded";
          setErrorState("degraded");
          if (warnTag) {
            const lastStatus = err instanceof ApiError ? err.status : "network";
            const lastError = err instanceof Error ? err.message : String(err);
            // eslint-disable-next-line no-console
            console.warn(warnTag, {
              consecutive_5xx: next5xx,
              last_status: lastStatus,
              last_error: lastError,
            });
          }
        }
        scheduleNext(intervalForFailureCount(next5xx, baselineIntervalMs));
      }
    };

    void tick();

    return () => {
      cancelled = true;
      controller.abort();
      if (timeoutId !== undefined) clearTimeout(timeoutId);
    };
  }, [endpoint, schema, baselineIntervalMs, enabled, warnTag]);

  return { data, error: errorState, consecutive5xx };
}
