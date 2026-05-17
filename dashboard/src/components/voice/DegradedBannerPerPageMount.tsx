/**
 * Per-page degraded-banner mount — registered on /voice + /voice/health.
 *
 * Mission C4 §T1.11. Declares the per-page mount as active via the
 * ``DegradedBannerMountedContext`` so the global mount yields.
 * Provides richer per-axis context (closer to the page's domain) and
 * lives at the natural top-of-page position rather than the dashboard
 * shell's header.
 */
import { useEffect } from "react";
import { DegradedBanner } from "./DegradedBanner";
import { useEngineDegradedPoller } from "@/hooks/use-engine-degraded-poller";
import { useDegradedBannerMounted } from "@/contexts/degraded-banner-mounted";

export function DegradedBannerPerPageMount() {
  const { setPerPageMounted } = useDegradedBannerMounted();
  const { data } = useEngineDegradedPoller();

  useEffect(() => {
    setPerPageMounted(true);
    return () => setPerPageMounted(false);
  }, [setPerPageMounted]);

  if (!data || !data.axes || data.axes.length === 0) return null;
  if ((data.composite_axis_count ?? 0) === 0) return null;

  return (
    <div
      data-testid="degraded-banner-per-page-mount"
      className="mb-4"
    >
      <DegradedBanner payload={data} />
    </div>
  );
}
