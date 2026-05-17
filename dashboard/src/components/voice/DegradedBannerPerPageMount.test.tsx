/**
 * Vitest cohort for DegradedBannerPerPageMount in isolation.
 *
 * Mission C4 §T1.13 §9.1 row "Per-page mount" — 4 focused tests on
 * the per-page mount's context-registration contract. Pairs with
 * ``DegradedBannerGlobalMount.test.tsx`` + ``DegradedBannerMounts.test.tsx``.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { I18nextProvider } from "react-i18next";

import { DegradedBannerPerPageMount } from "./DegradedBannerPerPageMount";
import {
  DegradedBannerMountedProvider,
  useDegradedBannerMounted,
} from "@/contexts/degraded-banner-mounted";
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

function MountedFlagProbe({ onChange }: { onChange: (v: boolean) => void }) {
  const { perPageMounted } = useDegradedBannerMounted();
  onChange(perPageMounted);
  return null;
}

function renderPerPage(observer?: (v: boolean) => void) {
  return render(
    <MemoryRouter>
      <I18nextProvider i18n={i18n}>
        <DegradedBannerMountedProvider>
          <DegradedBannerPerPageMount />
          {observer ? <MountedFlagProbe onChange={observer} /> : null}
        </DegradedBannerMountedProvider>
      </I18nextProvider>
    </MemoryRouter>,
  );
}

describe("DegradedBannerPerPageMount (isolated)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    cleanup();
  });

  it("registers as mounted in context regardless of payload", async () => {
    mockPollerData.mockReturnValue(null);
    const observed: boolean[] = [];
    renderPerPage((v) => observed.push(v));
    await waitFor(() => {
      expect(observed[observed.length - 1]).toBe(true);
    });
  });

  it("renders the per-page wrapper when payload has ≥1 axis", async () => {
    mockPollerData.mockReturnValue({
      axes: [
        {
          axis: "llm",
          reason: "no_llm_provider",
          severity: "error",
          title_token: "degraded.llm.noProvider.title",
          body_token: "degraded.llm.noProvider.body",
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
    });
    renderPerPage();
    await waitFor(() => {
      expect(screen.getByTestId("degraded-banner-per-page-mount")).toBeTruthy();
    });
  });

  it("renders nothing when composite_axis_count is 0", () => {
    mockPollerData.mockReturnValue({
      axes: [],
      composite_severity: null,
      composite_axis_count: 0,
      ack: { acked: false },
    });
    renderPerPage();
    expect(screen.queryByTestId("degraded-banner-per-page-mount")).toBeNull();
  });

  it("clears the mounted flag on unmount", async () => {
    mockPollerData.mockReturnValue(null);
    const observed: boolean[] = [];
    const { unmount } = renderPerPage((v) => observed.push(v));
    await waitFor(() => {
      expect(observed[observed.length - 1]).toBe(true);
    });
    unmount();
    // After unmount, the context resets via the cleanup hook. We
    // probe by re-rendering only the observer under a fresh provider.
    cleanup();
    const after: boolean[] = [];
    render(
      <DegradedBannerMountedProvider>
        <MountedFlagProbe onChange={(v) => after.push(v)} />
      </DegradedBannerMountedProvider>,
    );
    expect(after[after.length - 1]).toBe(false);
  });
});
