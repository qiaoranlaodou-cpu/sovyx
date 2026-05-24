/**
 * Router test — exercises the lazy-loaded route + ErrorBoundary +
 * Suspense fallback wiring without spinning up the full AppLayout
 * (which would pull every page into the test bundle).
 */

import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { Suspense, lazy } from "react";
import { ErrorBoundary } from "@/components/error-boundary";

// Reuse the exact PageWrapper semantics from router.tsx locally so we
// can exercise them without pulling in createBrowserRouter (which
// requires a real history).
function PageWrapper({ children }: { children: React.ReactNode }) {
  return (
    <ErrorBoundary>
      <Suspense fallback={<div data-testid="route-fallback">Loading…</div>}>
        {children}
      </Suspense>
    </ErrorBoundary>
  );
}

describe("router page wrapper", () => {
  it("shows the Suspense fallback while the lazy chunk resolves", async () => {
    let resolveLazy: (value: { default: React.ComponentType }) => void = () => {};
    const Lazy = lazy(
      () =>
        new Promise<{ default: React.ComponentType }>((resolve) => {
          resolveLazy = resolve;
        }),
    );

    render(
      <PageWrapper>
        <Lazy />
      </PageWrapper>,
    );
    expect(screen.getByTestId("route-fallback")).toBeInTheDocument();

    resolveLazy({ default: () => <div>resolved page</div> });
    expect(await screen.findByText("resolved page")).toBeInTheDocument();
  });

  it("catches render errors from a lazy page via the ErrorBoundary", async () => {
    const Explode = lazy(() =>
      Promise.resolve({
        default: function Explode(): React.ReactElement {
          throw new Error("boom");
        },
      }),
    );

    // Suppress the expected React error log so the test output stays clean.
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    render(
      <PageWrapper>
        <Explode />
      </PageWrapper>,
    );
    // ErrorBoundary i18n'd fallback shows the "Try again" button
    expect(await screen.findByRole("button", { name: /try again/i })).toBeInTheDocument();
    spy.mockRestore();
  });
});

// vi import for mockImplementation used above
import { vi } from "vitest";

import { matchRoutes } from "react-router";
import { router } from "./router";

/**
 * LIVE-1 Bug B — server-emitted degraded-banner navigate chips must target
 * a REGISTERED client route. Before the fix, `/settings/providers`,
 * `/settings/voice` and `/voice/logs` fell through to the `*` NotFound route
 * (SPA-404), so the operator could not act on the banner.
 *
 * `matchRoutes` returns the matched route branch; we assert the matched leaf
 * is not the catch-all `path: "*"` (NotFound). The path part is what matters —
 * `#hash` fragments are client-side and never reach the router.
 */
describe("degraded-banner chip navigate targets resolve to a real route", () => {
  const resolves = (path: string): boolean => {
    const matches = matchRoutes(router.routes, path);
    if (!matches || matches.length === 0) return false;
    const leaf = matches[matches.length - 1];
    return leaf.route.path !== "*";
  };

  // Mirrors the server-side producers (Python make_action_chip "navigate"
  // targets). Keep in sync with engine/_llm_dispatch.py, voice/factory/
  // _validate.py and voice/health/capture_integrity.py.
  it.each([
    "/settings/providers", // engine/_llm_dispatch.py (6 LLM no-provider chips)
    "/settings/voice", // voice/factory/_validate.py (STT language coerce)
    "/logs", // voice/health/capture_integrity.py (quarantine "view logs")
    "/voice/health", // quarantine "run doctor" + others (already registered)
    "/engine/resources", // resource-cohort governor (already registered)
  ])("resolves %s to a non-NotFound route", (path) => {
    expect(resolves(path)).toBe(true);
  });

  it("still routes a genuinely unknown path to NotFound (negative control)", () => {
    expect(resolves("/settings/does-not-exist")).toBe(false);
  });
});
