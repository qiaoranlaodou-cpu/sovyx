/* Mission H4 §T3.4 — ResourceHealthSection widget.
 *
 * Operator-facing surface for the per-cohort instrumentation introduced
 * by Phase 1.A (SSoT) + Phase 1.B (snapshotter wire). Polls
 * /api/engine/resources every 30 s with exponential backoff on 5xx +
 * renders collapsible rows per cohort section (process / asyncio /
 * to_thread / lock_dict / onnx / gc / tracemalloc / exception_cohort).
 *
 * Mounts inside ``voice-health.tsx`` alongside the QuarantineSection
 * + FailoverHistorySection so operators have one place to see the
 * engine's runtime resource state.
 *
 * Mirrors the C3 FailoverHistorySection pattern — uses ``useApiPoller``
 * for backoff + ``isDegraded`` indicator on poller failure.
 */

import { ChevronDownIcon, ChevronRightIcon, Loader2Icon } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { useApiPoller } from "@/hooks/use-api-poller";
import { EngineResourcesResponseSchema } from "@/types/schemas";

const POLL_INTERVAL_MS = 30_000;

// Section order = canonical FieldSpec.section ordering used by the
// Python SSoT _HEALTH_SNAPSHOT_FIELDS mapping.
const SECTION_ORDER = [
  "process",
  "asyncio",
  "to_thread",
  "lock_dict",
  "onnx",
  "gc",
  "tracemalloc",
  "exception_cohort",
] as const;

// Map cohort sections → the field keys they own (mirrors
// _HEALTH_SNAPSHOT_FIELDS[k].section grouping post-MISSION-A.1 closure).
//
// MISSION-A.2.P1 F-004: post-A.1 the dashboard renders the 35 NEW
// CANONICAL keys. The 9 LENIENT shims (system.rss_bytes,
// exception_cohort.{retained_bytes_estimate,distinct_group_id_count},
// to_thread.{active_workers,pool_size,queue_depth,max_workers},
// asyncio.{current_running_task_name,running_count,pending_count}) are
// emitted by the backend snapshotter for external Grafana / log
// forwarders but DO NOT appear in the operator dashboard — operators
// see the disambiguated post-A.1 names directly. Sunset v0.55.0 retires
// all 9 shims (ADR-D14 + ADR-D15 + ADR-D16).
const SECTION_FIELDS: Record<(typeof SECTION_ORDER)[number], readonly string[]> = {
  process: [
    "process.rss_bytes",
    "process.vms_bytes",
    "process.cpu_percent",
    "process.num_threads",
    "process.num_handles_or_fds",
    "process.open_files_count",
    "process.connections_count",
    "process.memory_percent",
    "process.cpu_times_user_s",
    "process.cpu_times_system_s",
  ],
  asyncio: [
    "asyncio.task_count",
    // MISSION-A.1.P3.b F-014 (ADR-D16): math-vs-name renames.
    "asyncio.not_done_count",
    "asyncio.awaiting_count",
    // MISSION-A.1.P3 F-005 (ADR-D15): replaces observation-paradox field.
    "asyncio.all_task_names",
    // LIVE executor introspection (twin of the to_thread.*_at_last_dispatch
    // STALE block — operators compare both for freshness contrast).
    "asyncio.default_executor_state",
  ],
  to_thread: [
    // MISSION-A.1.P3.b F-007 (ADR-D16): twin-name freshness rename.
    "to_thread.pool_size_at_last_dispatch",
    "to_thread.queue_depth_at_last_dispatch",
    "to_thread.max_workers_at_last_dispatch",
    "to_thread.dispatch_count_total",
    "to_thread.dispatch_count_per_label",
  ],
  lock_dict: [
    "lock_dict.total_cardinality",
    "lock_dict.per_owner",
    "lock_dict.instance_count",
  ],
  onnx: ["onnx.session_count", "onnx.session_labels"],
  gc: ["gc.collections_by_gen", "gc.objects_count"],
  tracemalloc: [
    "tracemalloc.is_tracing",
    "tracemalloc.current_kb",
    "tracemalloc.peak_kb",
  ],
  exception_cohort: [
    // MISSION-A.1.P2 F-002+F-003 (ADR-D14): cumulative-vs-window split.
    "exception_cohort.window_retained_bytes",
    "exception_cohort.window_distinct_group_id_count",
    "exception_cohort.cumulative_retained_bytes_since_start",
    "exception_cohort.cumulative_distinct_group_id_count",
    "exception_cohort.last_observation_monotonic",
  ],
};

// MISSION-A.2.P1 F-004: per-field operator-trust disclosure tooltips.
// Each entry maps a canonical SSoT field key → an i18n tooltip key that
// resolves to a short explanation of the field's semantic hazard
// (staleness, cross-platform overload, observation paradox, etc.). The
// tooltip surfaces as a native ``title`` attribute on the field label
// so operators see the disclosure on hover without UI churn.
//
// Fields not listed here have no semantic-trust hazard documented in
// the Mission A audit; rendering proceeds without a tooltip.
const FIELD_TOOLTIPS: Record<string, string> = {
  "process.cpu_percent": "resources.fieldTooltips.process_cpu_percent",
  "process.num_handles_or_fds": "resources.fieldTooltips.process_num_handles_or_fds",
  "process.memory_percent": "resources.fieldTooltips.process_memory_percent",
  "process.cpu_times_user_s": "resources.fieldTooltips.process_cpu_times_s",
  "process.cpu_times_system_s": "resources.fieldTooltips.process_cpu_times_s",
  "process.connections_count": "resources.fieldTooltips.process_connections_count",
  "process.open_files_count": "resources.fieldTooltips.process_open_files_count",
  "asyncio.not_done_count": "resources.fieldTooltips.asyncio_not_done_count",
  "asyncio.awaiting_count": "resources.fieldTooltips.asyncio_awaiting_count",
  "asyncio.all_task_names": "resources.fieldTooltips.asyncio_all_task_names",
  "asyncio.default_executor_state": "resources.fieldTooltips.asyncio_default_executor_state",
  "to_thread.pool_size_at_last_dispatch":
    "resources.fieldTooltips.to_thread_at_last_dispatch",
  "to_thread.queue_depth_at_last_dispatch":
    "resources.fieldTooltips.to_thread_at_last_dispatch",
  "to_thread.max_workers_at_last_dispatch":
    "resources.fieldTooltips.to_thread_at_last_dispatch",
  "gc.objects_count": "resources.fieldTooltips.gc_objects_count",
  "tracemalloc.is_tracing": "resources.fieldTooltips.tracemalloc_is_tracing",
  "exception_cohort.window_retained_bytes":
    "resources.fieldTooltips.exception_cohort_window",
  "exception_cohort.window_distinct_group_id_count":
    "resources.fieldTooltips.exception_cohort_window",
  "exception_cohort.cumulative_retained_bytes_since_start":
    "resources.fieldTooltips.exception_cohort_cumulative",
  "exception_cohort.cumulative_distinct_group_id_count":
    "resources.fieldTooltips.exception_cohort_cumulative",
  "exception_cohort.last_observation_monotonic":
    "resources.fieldTooltips.exception_cohort_last_observation_monotonic",
};

function formatValue(value: unknown): string {
  if (value === null || value === undefined) {
    return "—";
  }
  if (typeof value === "boolean") {
    return value ? "true" : "false";
  }
  if (typeof value === "number") {
    return value.toLocaleString();
  }
  if (Array.isArray(value)) {
    if (value.length === 0) {
      return "[]";
    }
    return `[${value.slice(0, 5).map(String).join(", ")}${value.length > 5 ? ", …" : ""}]`;
  }
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>);
    if (entries.length === 0) {
      return "{}";
    }
    return `{${entries
      .slice(0, 3)
      .map(([k, v]) => `${k}: ${String(v)}`)
      .join(", ")}${entries.length > 3 ? ", …" : ""}}`;
  }
  return String(value);
}

interface SectionRowProps {
  section: (typeof SECTION_ORDER)[number];
  cohorts: Record<string, unknown>;
}

function SectionRow({ section, cohorts }: SectionRowProps) {
  const { t } = useTranslation("voice");
  const [open, setOpen] = useState(false);
  const fields = SECTION_FIELDS[section];
  const presentFields = fields.filter((f) => f in cohorts);

  return (
    <div
      className="rounded border border-[var(--svx-color-border)] bg-[var(--svx-color-surface)]"
      data-testid={`resource-section-${section}`}
    >
      <button
        type="button"
        onClick={() => setOpen((prev) => !prev)}
        className="flex w-full items-center justify-between px-3 py-2 text-left transition-colors hover:bg-[var(--svx-color-surface-hover)]"
        aria-expanded={open}
      >
        <span className="flex items-center gap-2 font-mono text-xs">
          {open ? (
            <ChevronDownIcon className="size-3" />
          ) : (
            <ChevronRightIcon className="size-3" />
          )}
          {t(`resources.sections.${section}.title`)}
        </span>
        <span className="font-mono text-[10px] text-[var(--svx-color-text-tertiary)]">
          {presentFields.length} {t("resources.fieldsLabel")}
        </span>
      </button>
      {open && (
        <div className="border-t border-[var(--svx-color-border)] px-3 py-2">
          <p className="mb-2 text-[11px] text-[var(--svx-color-text-tertiary)]">
            {t(`resources.sections.${section}.description`)}
          </p>
          <dl className="space-y-1">
            {presentFields.map((field) => {
              const tooltipKey = FIELD_TOOLTIPS[field];
              const tooltip = tooltipKey ? t(tooltipKey) : undefined;
              return (
                <div
                  key={field}
                  className="flex items-baseline justify-between gap-3 text-xs"
                  data-testid={`resource-field-${field}`}
                >
                  <dt
                    className="font-mono text-[var(--svx-color-text-secondary)]"
                    title={tooltip}
                    data-testid={
                      tooltip ? `resource-field-tooltip-${field}` : undefined
                    }
                  >
                    {field}
                  </dt>
                  <dd className="font-mono text-[var(--svx-color-text-primary)] break-all text-right">
                    {formatValue(cohorts[field])}
                  </dd>
                </div>
              );
            })}
          </dl>
        </div>
      )}
    </div>
  );
}

export function ResourceHealthSection() {
  const { t } = useTranslation("voice");

  const { data: snapshot, error: pollerError } = useApiPoller<
    typeof EngineResourcesResponseSchema,
    import("zod").z.infer<typeof EngineResourcesResponseSchema>
  >({
    endpoint: "/api/engine/resources",
    schema: EngineResourcesResponseSchema,
    baselineIntervalMs: POLL_INTERVAL_MS,
    enabled: true,
    warnTag: "engine.resources.poller.degraded",
  });

  const cohorts = snapshot?.cohorts ?? {};
  const observedAt = snapshot?.observed_at_unix;
  const isDegraded = pollerError === "degraded";

  const observedAtLabel = useMemo(() => {
    if (typeof observedAt !== "number") return "—";
    return new Date(observedAt * 1000).toLocaleTimeString();
  }, [observedAt]);

  return (
    <section
      aria-labelledby="resource-health-heading"
      className="space-y-3"
      data-testid="resource-health-section"
    >
      <div className="flex items-baseline justify-between">
        <h2
          id="resource-health-heading"
          className="text-sm font-semibold uppercase tracking-wider text-[var(--svx-color-text-secondary)]"
        >
          {t("resources.title")}
        </h2>
        <span className="font-mono text-[11px] text-[var(--svx-color-text-tertiary)]">
          {observedAtLabel}
        </span>
      </div>
      <p className="text-xs text-[var(--svx-color-text-tertiary)]">
        {t("resources.subtitle")}
      </p>
      {!snapshot && !isDegraded && (
        <div
          className="flex items-center gap-2 text-xs text-[var(--svx-color-text-tertiary)]"
          data-testid="resource-health-loading"
        >
          <Loader2Icon className="size-3.5 animate-spin" />
          {t("resources.loading")}
        </div>
      )}
      {isDegraded && (
        <div
          className="rounded border border-[var(--svx-color-warning-border)] bg-[var(--svx-color-warning-bg)] px-3 py-2 text-xs text-[var(--svx-color-warning-text)]"
          data-testid="resource-health-degraded"
        >
          {t("resources.degraded")}
        </div>
      )}
      {snapshot && (
        <div className="space-y-2" data-testid="resource-health-sections">
          {SECTION_ORDER.map((section) => (
            <SectionRow key={section} section={section} cohorts={cohorts} />
          ))}
        </div>
      )}
    </section>
  );
}
