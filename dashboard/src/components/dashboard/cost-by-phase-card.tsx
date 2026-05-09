/**
 * CostByPhaseCard — surfaces today's LLM cost broken down by cognitive
 * phase (think / reflect / dream / act / safety / pii_guard /
 * financial_gate / contradiction / conv_import / unknown).
 *
 * Issue #43. Renders nothing when total_cost is zero so the overview
 * page doesn't grow an empty card on a freshly-started daemon.
 */

import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { ActivityIcon } from "lucide-react";

import { api } from "@/lib/api";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { CostBreakdownResponseSchema } from "@/types/schemas";
import type { CostBreakdownResponse } from "@/types/api";

const _PHASE_ORDER = [
  "think",
  "reflect",
  "dream",
  "act",
  "safety",
  "pii_guard",
  "financial_gate",
  "contradiction",
  "conv_import",
  "unknown",
] as const;

const _PHASE_COLOR: Record<string, string> = {
  think: "bg-sky-500",
  reflect: "bg-violet-500",
  dream: "bg-fuchsia-500",
  act: "bg-emerald-500",
  safety: "bg-amber-500",
  pii_guard: "bg-rose-500",
  financial_gate: "bg-orange-500",
  contradiction: "bg-red-500",
  conv_import: "bg-cyan-500",
  unknown: "bg-zinc-500",
};

function formatUsd(value: number): string {
  if (value < 0.01) return `$${value.toFixed(4)}`;
  return `$${value.toFixed(2)}`;
}

export function CostByPhaseCard() {
  const { t } = useTranslation("overview");
  const [data, setData] = useState<CostBreakdownResponse | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    const tick = async () => {
      try {
        const res = await api.get<CostBreakdownResponse>(
          "/api/stats/breakdown",
          {
            signal: controller.signal,
            schema: CostBreakdownResponseSchema,
          },
        );
        setData(res);
      } catch (e) {
        if (e instanceof DOMException && e.name === "AbortError") return;
        // Best-effort surface — failures don't break the overview.
        setData(null);
      }
    };
    void tick();
    const id = setInterval(() => void tick(), 30_000);
    return () => {
      controller.abort();
      clearInterval(id);
    };
  }, []);

  if (!data || data.total_cost <= 0) return null;

  const entries: Array<{ phase: string; cost: number }> = _PHASE_ORDER
    .map((phase) => ({
      phase: phase as string,
      cost: data.by_phase[phase] ?? 0,
    }))
    .filter((e) => e.cost > 0);

  // Phases reported by the backend that aren't in our known list —
  // surface them as "other" buckets rather than dropping silently.
  const knownSet = new Set<string>(_PHASE_ORDER);
  const extraTotal = Object.entries(data.by_phase)
    .filter(([k]) => !knownSet.has(k))
    .reduce((sum, [, v]) => sum + v, 0);
  if (extraTotal > 0) {
    entries.push({ phase: "other", cost: extraTotal });
  }

  if (entries.length === 0) return null;

  return (
    <Card data-testid="cost-by-phase-card">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <ActivityIcon className="size-4" />
          {t("phaseBreakdown.title", "Cost by cognitive phase")}
        </CardTitle>
        <CardDescription>
          {t(
            "phaseBreakdown.description",
            "Today's spend attributed to each cognitive phase.",
          )}
        </CardDescription>
      </CardHeader>
      <CardContent>
        <ul className="space-y-2">
          {entries.map(({ phase, cost }) => {
            const pct = (cost / data.total_cost) * 100;
            return (
              <li
                key={phase}
                className="flex flex-col gap-1"
                data-testid={`phase-row-${phase}`}
              >
                <div className="flex items-center justify-between text-sm">
                  <span className="font-medium text-foreground">
                    {t(`phaseBreakdown.phase.${phase}`, phase)}
                  </span>
                  <span className="text-muted-foreground">
                    {formatUsd(cost)}{" "}
                    <span className="text-xs">({pct.toFixed(1)}%)</span>
                  </span>
                </div>
                <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
                  <div
                    className={`h-full ${_PHASE_COLOR[phase] ?? "bg-zinc-500"}`}
                    style={{ width: `${Math.max(pct, 1)}%` }}
                  />
                </div>
              </li>
            );
          })}
        </ul>
      </CardContent>
    </Card>
  );
}
