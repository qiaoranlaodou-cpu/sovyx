/**
 * Composite degraded-mode banner — single chrome that surfaces the
 * cross-axis EngineDegradedStore state to the operator.
 *
 * Mission C4 §T1.8 — primitive extracted from the C3
 * ``DeviceContentionBanner`` shape (same ``role="alert"`` +
 * ``--svx-color-warning`` palette + ``<Trans>`` i18n + clickable
 * chips) but generalised across N axes with severity escalation per
 * ADR-D6.
 *
 * Severity → palette:
 *
 * - ``warn``  → yellow (``--svx-color-warning``)
 * - ``error`` → red (``--svx-color-error``)
 * - ``critical`` → red + pulse animation
 *
 * Action chips are operator-actionable next-steps emitted by the
 * server-side store producers. The banner does NOT decide what
 * actions are appropriate — that responsibility lives at the
 * record site (see e.g. ``engine/bootstrap.py:735`` LLM axis chips).
 *
 * Anti-pattern compliance:
 *
 * - #18 — all consumer-side fetches go through ``api.*`` via the
 *   parent poller hook; this component is a pure render layer.
 * - #19 — no ``localStorage`` reads; ack persistence is server-side
 *   (Phase 3).
 * - i18n — every operator-visible string uses ``<Trans>`` or
 *   ``t(...)``; never hardcoded English.
 *
 * Mission anchor:
 * docs-internal/missions/MISSION-c4-degraded-mode-banner-2026-05-17.md
 * §T1.8.
 */
import { memo, useCallback } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router";
import { toast } from "sonner";
import {
  AlertTriangleIcon,
  AlertCircleIcon,
  AlertOctagonIcon,
} from "lucide-react";

import type { EngineDegradedPayload } from "@/hooks/use-engine-degraded-poller";
import { apiFetch } from "@/lib/api";

export type DegradedAxis = EngineDegradedPayload["axes"][number];
export type DegradedActionChip = DegradedAxis["action_chips"][number];

interface SeverityPalette {
  border: string;
  bg: string;
  iconClass: string;
  Icon: typeof AlertTriangleIcon;
}

const SEVERITY_PALETTE: Record<string, SeverityPalette> = {
  warn: {
    border: "border-[var(--svx-color-warning)]/40",
    bg: "bg-[var(--svx-color-warning)]/5",
    iconClass: "text-[var(--svx-color-warning)]",
    Icon: AlertTriangleIcon,
  },
  error: {
    border: "border-[var(--svx-color-error)]/40",
    bg: "bg-[var(--svx-color-error)]/5",
    iconClass: "text-[var(--svx-color-error)]",
    Icon: AlertCircleIcon,
  },
  critical: {
    border: "border-[var(--svx-color-error)]/60",
    bg: "bg-[var(--svx-color-error)]/10 animate-pulse",
    iconClass: "text-[var(--svx-color-error)]",
    Icon: AlertOctagonIcon,
  },
};

interface DegradedBannerProps {
  payload: EngineDegradedPayload;
  /**
   * Optional ack handler. When undefined the ack control is hidden
   * (Phase 1.B; Phase 3 wires the server-side ack endpoint and the
   * mounts inject the handler).
   */
  onAck?: (ttlSec: number) => void;
}

function _resolveSeverity(payload: EngineDegradedPayload): keyof typeof SEVERITY_PALETTE {
  const s = payload.composite_severity;
  if (s === "warn" || s === "error" || s === "critical") return s;
  return "warn";
}

// Safe palette resolution — TypeScript flags the index access as
// possibly undefined under strict null checks even though _resolveSeverity
// returns one of the static keys. Falling back to "warn" is the
// invariant we want.
function _resolvePalette(payload: EngineDegradedPayload): SeverityPalette {
  const severity = _resolveSeverity(payload);
  return SEVERITY_PALETTE[severity] ?? SEVERITY_PALETTE.warn!;
}

export const DegradedBanner = memo(function DegradedBanner({
  payload,
  onAck,
}: DegradedBannerProps) {
  const { t } = useTranslation("voice");
  const navigate = useNavigate();

  const severity = _resolveSeverity(payload);
  const palette = _resolvePalette(payload);
  const Icon = palette.Icon;
  const axisCount = payload.composite_axis_count;

  const handleChipClick = useCallback(
    (chip: DegradedActionChip) => {
      if (chip.action === "navigate") {
        void navigate(chip.target);
        return;
      }
      if (chip.action === "external_link") {
        // Open in a new tab; rel guards against tabnapping.
        window.open(chip.target, "_blank", "noopener,noreferrer");
        return;
      }
      if (chip.action === "command_hint") {
        // Mission H4 §4.8 ADR-D8 + v0.49.26 — copy CLI command to
        // clipboard so the operator can paste it into their terminal.
        // Fails gracefully when navigator.clipboard is unavailable
        // (insecure context, older browsers).
        const clipboard = navigator.clipboard;
        if (clipboard && typeof clipboard.writeText === "function") {
          clipboard
            .writeText(chip.target)
            .then(() => {
              toast.success(
                t("degraded.engine_resources.toast.copySuccess", {
                  command: chip.target,
                }),
              );
            })
            .catch(() => {
              toast.error(t("degraded.engine_resources.toast.copyFailed"));
            });
        } else {
          toast.error(t("degraded.engine_resources.toast.copyFailed"));
        }
        return;
      }
      if (chip.action === "api_post" || chip.action === "dispatch") {
        // Mission H4 §4.8 ADR-D8 + v0.49.26 — POST to target endpoint
        // and surface the ack outcome via toast. ``dispatch`` is the
        // pre-H4 alias kept for back-compat (see ActionChipSchema
        // docstring at types/schemas.ts).
        apiFetch(chip.target, { method: "POST" })
          .then((resp) => {
            if (resp.ok) {
              toast.success(t("degraded.engine_resources.toast.ackSuccess"));
            } else {
              toast.error(
                t("degraded.engine_resources.toast.ackFailed", {
                  status: resp.status,
                }),
              );
            }
          })
          .catch(() => {
            toast.error(
              t("degraded.engine_resources.toast.ackFailed", {
                status: "network",
              }),
            );
          });
        return;
      }
      // Unknown action — log a debug breadcrumb so test fixtures and
      // local dev can detect the gap without exercising the network.
      // eslint-disable-next-line no-console
      console.debug("degraded_chip_unknown_action", chip.action, chip.target);
    },
    [navigate, t],
  );

  // Hide chrome entirely when no axis is degraded — the consumer mounts
  // ALSO short-circuit, but this defense-in-depth guarantees the banner
  // never renders empty.
  if (axisCount === 0 || payload.axes.length === 0) return null;

  return (
    <div
      role="alert"
      aria-live="polite"
      data-testid="degraded-banner"
      data-severity={severity}
      className={[
        "rounded-[var(--svx-radius-lg)] border p-4 space-y-3",
        palette.border,
        palette.bg,
      ].join(" ")}
    >
      <div className="flex items-start gap-3">
        <Icon className={`size-5 shrink-0 mt-0.5 ${palette.iconClass}`} aria-hidden="true" />
        <div className="flex-1 space-y-3">
          <div className="text-sm font-medium text-[var(--svx-color-text-primary)]">
            {axisCount === 1 && payload.axes[0]
              ? t(payload.axes[0].title_token, payload.axes[0].metadata)
              : t("degraded.composite.title", { count: axisCount })}
          </div>
          {payload.axes.map((axis) => (
            <div key={axis.reason} className="space-y-2">
              {axisCount > 1 ? (
                <div className="text-[12px] font-medium text-[var(--svx-color-text-primary)]/80">
                  {t(axis.title_token, axis.metadata)}
                </div>
              ) : null}
              <p className="text-[12px] text-[var(--svx-color-text-secondary)]">
                {t(axis.body_token, axis.metadata)}
              </p>
              {axis.action_chips.length > 0 ? (
                <div className="flex flex-wrap gap-2">
                  {axis.action_chips.map((chip, idx) => (
                    <button
                      key={`${axis.reason}-${idx}`}
                      type="button"
                      onClick={() => handleChipClick(chip)}
                      data-testid={`degraded-chip-${axis.reason}-${idx}`}
                      className={[
                        "inline-flex items-center gap-1.5",
                        "rounded-[var(--svx-radius-md)] border px-2.5 py-1.5",
                        "text-[11px] font-medium transition-colors",
                        chip.style === "primary"
                          ? "border-[var(--svx-color-primary)] bg-[var(--svx-color-primary)]/10 text-[var(--svx-color-primary)] hover:bg-[var(--svx-color-primary)]/20"
                          : "border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] text-[var(--svx-color-text-primary)] hover:border-[var(--svx-color-text-tertiary)]",
                      ].join(" ")}
                    >
                      {t(chip.label_token)}
                    </button>
                  ))}
                </div>
              ) : null}
            </div>
          ))}
        </div>
        {onAck && !payload.ack.acked ? (
          <button
            type="button"
            onClick={() => onAck(3600)}
            data-testid="degraded-banner-ack"
            className="shrink-0 text-[11px] text-[var(--svx-color-text-tertiary)] underline hover:text-[var(--svx-color-text-primary)]"
          >
            {t("degraded.composite.ack")}
          </button>
        ) : null}
      </div>
    </div>
  );
});
