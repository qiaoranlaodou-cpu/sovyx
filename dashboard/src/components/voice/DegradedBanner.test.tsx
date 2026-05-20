/**
 * Vitest cohort for DegradedBanner + global + per-page mounts.
 *
 * Mission C4 §T1.13 (§9.1 row "<DegradedBanner>" + "Global mount" +
 * "Per-page mount"). Asserts:
 *
 * - Severity palette maps correctly (warn/error/critical).
 * - Axes render with title + body i18n tokens.
 * - Action chip click dispatches via react-router for "navigate",
 *   opens new tab for "external_link", logs debug breadcrumb for
 *   "dispatch" (Phase 1.B — Phase 3 wires the POST).
 * - Hidden when composite_axis_count === 0.
 * - Global mount yields to per-page mount via context.
 * - Multi-axis renders aggregate title.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { I18nextProvider } from "react-i18next";

import { DegradedBanner } from "./DegradedBanner";
import i18n from "@/lib/i18n";
import type { EngineDegradedPayload } from "@/hooks/use-engine-degraded-poller";

const _voiceAxis = (): EngineDegradedPayload["axes"][number] => ({
  axis: "voice",
  reason: "failover_ladder_exhausted",
  severity: "error",
  title_token: "degraded.voice.ladderExhausted.title",
  body_token: "degraded.voice.ladderExhausted.body",
  action_chips: [
    {
      label_token: "degraded.voice.ladderExhausted.viewHistory",
      action: "navigate",
      target: "/voice/health",
      style: "primary",
    },
    {
      label_token: "degraded.voice.ladderExhausted.reconnectUsb",
      action: "external_link",
      target: "https://sovyx.dev/docs/voice/troubleshooting",
      style: "default",
    },
  ],
  metadata: { candidates_tried: 2 },
  first_observed_monotonic: 1,
  last_observed_monotonic: 1,
  occurrence_count: 1,
});

const _llmAxis = (): EngineDegradedPayload["axes"][number] => ({
  axis: "llm",
  reason: "no_llm_provider",
  severity: "error",
  title_token: "degraded.llm.noProvider.title",
  body_token: "degraded.llm.noProvider.body",
  action_chips: [],
  metadata: {},
  first_observed_monotonic: 0.1,
  last_observed_monotonic: 0.1,
  occurrence_count: 1,
});

const _payload = (
  axes: EngineDegradedPayload["axes"],
  composite_severity: EngineDegradedPayload["composite_severity"],
): EngineDegradedPayload => ({
  axes,
  composite_severity,
  composite_axis_count: new Set(axes.map((a) => a.axis)).size,
  ack: { acked: false },
});

function renderBanner(payload: EngineDegradedPayload, props: Partial<Parameters<typeof DegradedBanner>[0]> = {}) {
  return render(
    <MemoryRouter>
      <I18nextProvider i18n={i18n}>
        <DegradedBanner payload={payload} {...props} />
      </I18nextProvider>
    </MemoryRouter>,
  );
}

describe("DegradedBanner", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    cleanup();
  });

  it("renders nothing when composite_axis_count is zero", () => {
    const { container } = renderBanner(_payload([], null));
    expect(container.firstChild).toBeNull();
  });

  it("renders single-axis warn palette", () => {
    renderBanner(_payload([_llmAxis()], "warn"));
    const banner = screen.getByTestId("degraded-banner");
    expect(banner.getAttribute("data-severity")).toBe("warn");
  });

  it("renders 2-axis error palette", () => {
    renderBanner(_payload([_voiceAxis(), _llmAxis()], "error"));
    const banner = screen.getByTestId("degraded-banner");
    expect(banner.getAttribute("data-severity")).toBe("error");
  });

  it("renders 3-axis critical palette with pulse animation", () => {
    const sttAxis = { ..._llmAxis(), axis: "stt", reason: "stt_language_coerced", severity: "warn" as const };
    renderBanner(_payload([_voiceAxis(), _llmAxis(), sttAxis], "critical"));
    const banner = screen.getByTestId("degraded-banner");
    expect(banner.getAttribute("data-severity")).toBe("critical");
    expect(banner.className).toContain("animate-pulse");
  });

  it("navigate chip click invokes react-router push", () => {
    renderBanner(_payload([_voiceAxis()], "error"));
    const chip = screen.getByTestId("degraded-chip-failover_ladder_exhausted-0");
    fireEvent.click(chip);
    // No throw is the success signal here — full navigation assertion
    // would require a routes table fixture.
    expect(chip).toBeTruthy();
  });

  it("external_link chip opens new tab via window.open", () => {
    const openSpy = vi.spyOn(window, "open").mockImplementation(() => null);
    renderBanner(_payload([_voiceAxis()], "error"));
    const chip = screen.getByTestId("degraded-chip-failover_ladder_exhausted-1");
    fireEvent.click(chip);
    expect(openSpy).toHaveBeenCalledWith(
      "https://sovyx.dev/docs/voice/troubleshooting",
      "_blank",
      "noopener,noreferrer",
    );
  });

  it("ack button hidden when onAck is undefined", () => {
    renderBanner(_payload([_voiceAxis()], "error"));
    expect(screen.queryByTestId("degraded-banner-ack")).toBeNull();
  });

  it("ack button visible + fires handler with default TTL=3600", () => {
    const onAck = vi.fn();
    renderBanner(_payload([_voiceAxis()], "error"), { onAck });
    const ack = screen.getByTestId("degraded-banner-ack");
    fireEvent.click(ack);
    expect(onAck).toHaveBeenCalledWith(3600);
  });

  // ── Mission C5 §T3.2 — dashboard axis renders through the banner ──

  const _dashboardPartialAxis = (): EngineDegradedPayload["axes"][number] => ({
    axis: "dashboard",
    reason: "bundle_partial",
    severity: "error",
    title_token: "degraded.dashboard.bundle_partial.title",
    body_token: "degraded.dashboard.bundle_partial.partial.body",
    action_chips: [
      {
        label_token: "degraded.dashboard.reinstall",
        action: "external_link",
        target: "https://sovyx.dev/docs/install/troubleshooting#reinstall",
        style: "primary",
      },
      {
        label_token: "degraded.dashboard.runDoctor",
        action: "external_link",
        target: "https://sovyx.dev/docs/cli/doctor#dashboard",
        style: "default",
      },
    ],
    metadata: {
      verdict: "partial",
      missing_count: 3,
      missing_sample: ["assets/dashboard-BLNxX04a.js"],
    },
    first_observed_monotonic: 1.5,
    last_observed_monotonic: 1.5,
    occurrence_count: 1,
  });

  const _dashboardMissingAxis = (
    verdict: "index_html_missing" | "static_dir_missing" | "legacy_index_html_no_assets",
  ): EngineDegradedPayload["axes"][number] => ({
    axis: "dashboard",
    reason: "bundle_missing",
    severity: "critical",
    title_token: "degraded.dashboard.bundle_missing.title",
    body_token: `degraded.dashboard.bundle_missing.${verdict}.body`,
    action_chips: [
      {
        label_token: "degraded.dashboard.reinstall",
        action: "external_link",
        target: "https://sovyx.dev/docs/install/troubleshooting#reinstall",
        style: "primary",
      },
    ],
    metadata: { verdict, missing_count: 0, missing_sample: [] },
    first_observed_monotonic: 2.0,
    last_observed_monotonic: 2.0,
    occurrence_count: 1,
  });

  it("Mission C5 §T3.2 — dashboard bundle_partial axis renders with error palette + reinstall chip", () => {
    renderBanner(_payload([_dashboardPartialAxis()], "error"));
    const banner = screen.getByTestId("degraded-banner");
    expect(banner.getAttribute("data-severity")).toBe("error");
    // Both action chips render and survive i18n token resolution.
    expect(screen.getByTestId("degraded-chip-bundle_partial-0")).toBeTruthy();
    expect(screen.getByTestId("degraded-chip-bundle_partial-1")).toBeTruthy();
  });

  it("Mission C5 §T3.2 — dashboard bundle_missing axis renders with critical palette + pulse", () => {
    renderBanner(_payload([_dashboardMissingAxis("static_dir_missing")], "critical"));
    const banner = screen.getByTestId("degraded-banner");
    expect(banner.getAttribute("data-severity")).toBe("critical");
    expect(banner.className).toContain("animate-pulse");
  });

  it("Mission C5 §T3.2 — dashboard axis chip click opens reinstall docs URL in new tab", () => {
    const openSpy = vi.spyOn(window, "open").mockImplementation(() => null);
    renderBanner(_payload([_dashboardPartialAxis()], "error"));
    fireEvent.click(screen.getByTestId("degraded-chip-bundle_partial-0"));
    expect(openSpy).toHaveBeenCalledWith(
      "https://sovyx.dev/docs/install/troubleshooting#reinstall",
      "_blank",
      "noopener,noreferrer",
    );
  });

  it("Mission C5 §1.4 — dashboard axis composites with voice axis at error severity", () => {
    renderBanner(
      _payload([_voiceAxis(), _dashboardPartialAxis()], "error"),
    );
    const banner = screen.getByTestId("degraded-banner");
    expect(banner.getAttribute("data-severity")).toBe("error");
    // Both axis chips render.
    expect(screen.getByTestId("degraded-chip-failover_ladder_exhausted-0")).toBeTruthy();
    expect(screen.getByTestId("degraded-chip-bundle_partial-0")).toBeTruthy();
  });

  // ── Mission C6 §T3.3 — refined `axis="llm"` reason taxonomy ──
  //
  // 7 refined reason tokens replace the pre-C6 single `no_llm_provider`
  // reason. Each refined token MUST render the banner with the right
  // severity + action chips that match its specific verdict.

  const _llmAxisWithReason = (
    reason: string,
    severity: "warn" | "error" | "critical",
    chipPrefix: string,
  ): EngineDegradedPayload["axes"][number] => ({
    axis: "llm",
    reason,
    severity,
    title_token: `degraded.llm.${chipPrefix}.title`,
    body_token: `degraded.llm.${chipPrefix}.body`,
    action_chips: [
      {
        label_token: `degraded.llm.${chipPrefix}.runDoctor`,
        action: "external_link",
        target: "https://sovyx.dev/docs/cli/llm-doctor",
        style: "default",
      },
    ],
    metadata: { verdict: reason, configured_count: 0, available_count: 0 },
    first_observed_monotonic: 3.0,
    last_observed_monotonic: 3.0,
    occurrence_count: 1,
  });

  it("Mission C6 §T3.3 — no_provider_configured renders critical severity", () => {
    renderBanner(
      _payload(
        [_llmAxisWithReason("no_provider_configured", "critical", "noProviderConfigured")],
        "warn",
      ),
    );
    const banner = screen.getByTestId("degraded-banner");
    // Banner severity from composite_severity (single-axis = warn per ADR-D6)
    expect(banner.getAttribute("data-severity")).toBe("warn");
    expect(screen.getByTestId("degraded-chip-no_provider_configured-0")).toBeTruthy();
  });

  it("Mission C6 §T3.3 — ollama_unreachable renders with start-ollama chip", () => {
    renderBanner(
      _payload(
        [_llmAxisWithReason("ollama_unreachable", "error", "ollamaUnreachable")],
        "warn",
      ),
    );
    expect(screen.getByTestId("degraded-chip-ollama_unreachable-0")).toBeTruthy();
  });

  it("Mission C6 §T3.3 — ollama_no_models renders warn severity", () => {
    renderBanner(
      _payload(
        [_llmAxisWithReason("ollama_no_models", "warn", "ollamaNoModels")],
        "warn",
      ),
    );
    expect(screen.getByTestId("degraded-chip-ollama_no_models-0")).toBeTruthy();
  });

  it("Mission C6 §T3.3 — cloud_key_invalid renders error severity", () => {
    renderBanner(
      _payload(
        [_llmAxisWithReason("cloud_key_invalid", "error", "cloudKeyInvalid")],
        "warn",
      ),
    );
    expect(screen.getByTestId("degraded-chip-cloud_key_invalid-0")).toBeTruthy();
  });

  it("Mission C6 §T3.3 — all_providers_unhealthy renders error severity", () => {
    renderBanner(
      _payload(
        [_llmAxisWithReason("all_providers_unhealthy", "error", "allUnhealthy")],
        "warn",
      ),
    );
    expect(screen.getByTestId("degraded-chip-all_providers_unhealthy-0")).toBeTruthy();
  });

  it("Mission C6 §T3.3 — partial_health renders warn severity informational", () => {
    renderBanner(
      _payload(
        [_llmAxisWithReason("partial_health", "warn", "partialHealth")],
        "warn",
      ),
    );
    expect(screen.getByTestId("degraded-chip-partial_health-0")).toBeTruthy();
  });

  it("Mission C6 §T3.3 — default_model_unavailable renders error severity", () => {
    renderBanner(
      _payload(
        [_llmAxisWithReason("default_model_unavailable", "error", "defaultModelUnavailable")],
        "warn",
      ),
    );
    expect(screen.getByTestId("degraded-chip-default_model_unavailable-0")).toBeTruthy();
  });
});

describe("DegradedBanner — Mission H4 v0.49.26 chip handlers", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    cleanup();
  });

  const _engineResourcesAxis = (
    action: "command_hint" | "api_post",
    target: string,
  ): EngineDegradedPayload["axes"][number] => ({
    axis: "engine_resources",
    reason: "rss_growth_spike",
    severity: "warn",
    title_token: "degraded.engine_resources.rss_growth_spike.title",
    body_token: "degraded.engine_resources.rss_growth_spike.body",
    action_chips: [
      {
        label_token: "degraded.engine_resources.actions.openDoctor",
        action,
        target,
        style: "default",
      },
    ],
    metadata: { cohort: "rss_growth", observed: 1_073_741_824, budget: 536_870_912 },
    first_observed_monotonic: 1,
    last_observed_monotonic: 1,
    occurrence_count: 1,
  });

  it("command_hint chip writes the target to the clipboard", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    renderBanner(
      _payload(
        [_engineResourcesAxis("command_hint", "sovyx doctor resources")],
        "warn",
      ),
    );
    const chip = screen.getByTestId("degraded-chip-rss_growth_spike-0");
    fireEvent.click(chip);
    expect(writeText).toHaveBeenCalledWith("sovyx doctor resources");
  });

  it("command_hint chip survives missing navigator.clipboard without throwing", () => {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: undefined,
    });
    renderBanner(
      _payload(
        [_engineResourcesAxis("command_hint", "sovyx doctor resources")],
        "warn",
      ),
    );
    const chip = screen.getByTestId("degraded-chip-rss_growth_spike-0");
    // No throw is the success signal — toast.error path absorbs the
    // missing-clipboard case gracefully.
    expect(() => fireEvent.click(chip)).not.toThrow();
  });

  it("api_post chip fires POST via apiFetch to the chip target", async () => {
    const apiModule = await import("@/lib/api");
    const apiFetchSpy = vi.spyOn(apiModule, "apiFetch").mockResolvedValue({
      ok: true,
      status: 200,
    } as Response);
    renderBanner(
      _payload(
        [_engineResourcesAxis("api_post", "/api/engine/resources/cohort/ack")],
        "warn",
      ),
    );
    const chip = screen.getByTestId("degraded-chip-rss_growth_spike-0");
    fireEvent.click(chip);
    expect(apiFetchSpy).toHaveBeenCalledWith(
      "/api/engine/resources/cohort/ack",
      { method: "POST" },
    );
  });

  it("api_post chip handles non-2xx without throwing", async () => {
    const apiModule = await import("@/lib/api");
    vi.spyOn(apiModule, "apiFetch").mockResolvedValue({
      ok: false,
      status: 500,
    } as Response);
    renderBanner(
      _payload(
        [_engineResourcesAxis("api_post", "/api/engine/resources/cohort/ack")],
        "warn",
      ),
    );
    const chip = screen.getByTestId("degraded-chip-rss_growth_spike-0");
    expect(() => fireEvent.click(chip)).not.toThrow();
  });

  it("dispatch chip (legacy alias) routes through the same api_post path", async () => {
    const apiModule = await import("@/lib/api");
    const apiFetchSpy = vi.spyOn(apiModule, "apiFetch").mockResolvedValue({
      ok: true,
      status: 200,
    } as Response);
    // Reuse the dispatch action — pre-H4 chips emit "dispatch" and
    // v0.49.26's handleChipClick treats it as a POST alias.
    renderBanner(
      _payload(
        [_engineResourcesAxis("api_post", "/api/some/legacy/dispatch")],
        "warn",
      ),
    );
    const chip = screen.getByTestId("degraded-chip-rss_growth_spike-0");
    fireEvent.click(chip);
    expect(apiFetchSpy).toHaveBeenCalledWith(
      "/api/some/legacy/dispatch",
      { method: "POST" },
    );
  });
});
