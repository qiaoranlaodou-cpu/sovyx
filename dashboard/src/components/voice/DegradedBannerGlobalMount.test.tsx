/**
 * Vitest cohort for DegradedBannerGlobalMount in isolation.
 *
 * Mission C4 §T1.13 §9.1 row "Global mount" — 5 focused tests on
 * the global mount's render contract. Pairs with the sibling
 * ``DegradedBannerPerPageMount.test.tsx`` (focused per-page mount
 * tests) + ``DegradedBannerMounts.test.tsx`` (cross-mount dedup
 * invariants when BOTH render together).
 *
 * Mocks ``useEngineDegradedPoller`` per-test so each render
 * exercises a specific payload shape without touching the network.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { I18nextProvider } from "react-i18next";

import { DegradedBannerGlobalMount } from "./DegradedBannerGlobalMount";
import { DegradedBannerMountedProvider } from "@/contexts/degraded-banner-mounted";
import i18n from "@/lib/i18n";

const mockPollerData = vi.fn();

vi.mock("@/hooks/use-engine-degraded-poller", () => ({
  useEngineDegradedPoller: () => ({
    data: mockPollerData(),
    error: "ok",
    consecutive5xx: 0,
  }),
  ENGINE_DEGRADED_POLL_INTERVAL_MS: 5000,
}));

function renderGlobal() {
  return render(
    <MemoryRouter>
      <I18nextProvider i18n={i18n}>
        <DegradedBannerMountedProvider>
          <DegradedBannerGlobalMount />
        </DegradedBannerMountedProvider>
      </I18nextProvider>
    </MemoryRouter>,
  );
}

describe("DegradedBannerGlobalMount (isolated)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    cleanup();
  });

  it("renders nothing when poller data is null (pre-first-tick)", () => {
    mockPollerData.mockReturnValue(null);
    renderGlobal();
    expect(screen.queryByTestId("degraded-banner-global-mount")).toBeNull();
  });

  it("renders nothing when composite_axis_count is 0 (healthy)", () => {
    mockPollerData.mockReturnValue({
      axes: [],
      composite_severity: null,
      composite_axis_count: 0,
      ack: { acked: false },
    });
    renderGlobal();
    expect(screen.queryByTestId("degraded-banner-global-mount")).toBeNull();
  });

  it("renders the global mount wrapper when ≥1 axis is degraded", async () => {
    mockPollerData.mockReturnValue({
      axes: [
        {
          axis: "voice",
          reason: "failover_ladder_exhausted",
          severity: "error",
          title_token: "degraded.voice.ladderExhausted.title",
          body_token: "degraded.voice.ladderExhausted.body",
          action_chips: [],
          metadata: { candidates_tried: 2 },
          first_observed_monotonic: 1,
          last_observed_monotonic: 1,
          occurrence_count: 1,
        },
      ],
      composite_severity: "warn",
      composite_axis_count: 1,
      ack: { acked: false },
    });
    renderGlobal();
    await waitFor(() => {
      expect(screen.getByTestId("degraded-banner-global-mount")).toBeTruthy();
    });
    expect(screen.getByTestId("degraded-banner")).toBeTruthy();
  });

  it("renders nothing when payload.axes is missing entirely (defensive)", () => {
    // Mission C4 §16 synergy guardrail — malformed poller data
    // (e.g. from a shared useApiPoller mock returning a different
    // shape) must NOT crash the global mount.
    mockPollerData.mockReturnValue({
      composite_axis_count: 1,
      ack: { acked: false },
      // axes deliberately omitted
    });
    renderGlobal();
    expect(screen.queryByTestId("degraded-banner-global-mount")).toBeNull();
  });

  it("renders nothing when axes is an empty array even if count is non-zero", () => {
    // Server-side bug guardrail: composite_axis_count should equal
    // distinct axis count, but if the server stutters and reports a
    // mismatch, the mount must not render an empty banner.
    mockPollerData.mockReturnValue({
      axes: [],
      composite_severity: "warn",
      composite_axis_count: 1,
      ack: { acked: false },
    });
    renderGlobal();
    expect(screen.queryByTestId("degraded-banner-global-mount")).toBeNull();
  });
});
