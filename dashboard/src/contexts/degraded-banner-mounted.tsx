/**
 * Mount-dedup context for the composite degraded banner.
 *
 * Mission C4 §T1.10/§T1.11 — the banner is mounted at two layers:
 *
 * - {@link DegradedBannerGlobalMount} at the app-layout shell
 *   (visible regardless of route).
 * - {@link DegradedBannerPerPageMount} at /voice + /voice/health
 *   pages (richer per-page context).
 *
 * Without dedup the operator would see TWO copies of the same banner
 * stacked at the top of the page when navigating to the voice route.
 * This context lets the per-page mount declare itself active; the
 * global mount yields when ``perPageMounted=true``.
 *
 * Pattern source: matches the C3 dashboard convention of context-based
 * mount coordination (sibling of the existing toast Toaster pattern
 * at app-layout.tsx). No external dependency added.
 *
 * Mission anchor:
 * docs-internal/missions/MISSION-c4-degraded-mode-banner-2026-05-17.md
 * §T1.10 + §T1.11.
 */
import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";

interface DegradedBannerMountedContextValue {
  /** True iff a per-page mount is currently rendered. */
  perPageMounted: boolean;
  /** Per-page mounts call this on mount (true) + unmount (false). */
  setPerPageMounted: (mounted: boolean) => void;
}

const DegradedBannerMountedContext =
  createContext<DegradedBannerMountedContextValue>({
    perPageMounted: false,
    setPerPageMounted: () => {
      // Default no-op when provider absent — safe fallback for tests
      // that render a per-page mount in isolation. The provider's
      // setter overrides this at the app shell.
    },
  });

interface DegradedBannerMountedProviderProps {
  children: ReactNode;
}

export function DegradedBannerMountedProvider({
  children,
}: DegradedBannerMountedProviderProps) {
  const [perPageMounted, setPerPageMounted] = useState(false);

  const setMounted = useCallback((mounted: boolean) => {
    setPerPageMounted(mounted);
  }, []);

  const value = useMemo(
    () => ({
      perPageMounted,
      setPerPageMounted: setMounted,
    }),
    [perPageMounted, setMounted],
  );

  return (
    <DegradedBannerMountedContext.Provider value={value}>
      {children}
    </DegradedBannerMountedContext.Provider>
  );
}

export function useDegradedBannerMounted(): DegradedBannerMountedContextValue {
  return useContext(DegradedBannerMountedContext);
}
