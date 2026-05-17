/**
 * Vitest cohort for the global + per-page mount dedup logic.
 *
 * Mission C4 §T1.13 (§9.1 row "Global mount" + "Per-page mount").
 *
 * Asserts:
 *
 * - Global mount renders alone when no per-page mount is active.
 * - Per-page mount renders and signals via the
 *   ``DegradedBannerMountedContext``; the global mount yields.
 * - Both mounts read the SAME ``/api/engine/degraded`` payload via
 *   ``useEngineDegradedPoller`` (mocked here to avoid network).
 */
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { I18nextProvider } from "react-i18next";

import { DegradedBannerGlobalMount } from "./DegradedBannerGlobalMount";
import { DegradedBannerPerPageMount } from "./DegradedBannerPerPageMount";
import { DegradedBannerMountedProvider } from "@/contexts/degraded-banner-mounted";
import i18n from "@/lib/i18n";

vi.mock("@/hooks/use-engine-degraded-poller", () => ({
  useEngineDegradedPoller: () => ({
    data: {
      axes: [
        {
          axis: "voice",
          reason: "failover_ladder_exhausted",
          severity: "error",
          title_token: "degraded.voice.ladderExhausted.title",
          body_token: "degraded.voice.ladderExhausted.body",
          action_chips: [],
          metadata: {},
          first_observed_monotonic: 1,
          last_observed_monotonic: 1,
          occurrence_count: 1,
        },
      ],
      composite_severity: "warn",
      composite_axis_count: 1,
      ack: { acked: false },
    },
    error: "ok",
    consecutive5xx: 0,
  }),
  ENGINE_DEGRADED_POLL_INTERVAL_MS: 5000,
}));

function renderShell(includePerPage: boolean) {
  return render(
    <MemoryRouter>
      <I18nextProvider i18n={i18n}>
        <DegradedBannerMountedProvider>
          <DegradedBannerGlobalMount />
          {includePerPage ? <DegradedBannerPerPageMount /> : null}
        </DegradedBannerMountedProvider>
      </I18nextProvider>
    </MemoryRouter>,
  );
}

describe("DegradedBanner mount dedup", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    cleanup();
  });

  it("global mount renders when no per-page mount is active", async () => {
    renderShell(false);
    await waitFor(() => {
      expect(screen.getByTestId("degraded-banner-global-mount")).toBeTruthy();
    });
    expect(screen.queryByTestId("degraded-banner-per-page-mount")).toBeNull();
  });

  it("per-page mount renders + global mount yields when both present", async () => {
    renderShell(true);
    await waitFor(() => {
      expect(screen.getByTestId("degraded-banner-per-page-mount")).toBeTruthy();
    });
    // Global mount yields (no global-mount data-testid present).
    expect(screen.queryByTestId("degraded-banner-global-mount")).toBeNull();
  });

  it("both mounts use the same composite payload (single banner DOM node)", async () => {
    renderShell(true);
    await waitFor(() => {
      const banners = screen.queryAllByTestId("degraded-banner");
      expect(banners.length).toBe(1);
    });
  });
});
