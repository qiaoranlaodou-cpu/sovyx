/**
 * TtsEngineCard — operator picks the preferred TTS engine.
 *
 * Issue #39. The choice persists to ``mind.yaml`` via
 * ``POST /api/voice/enable`` (the same endpoint the wizard uses), and
 * the factory honors ``MindConfig.voice_tts_engine`` on next pipeline
 * boot. ``"auto"`` keeps the legacy detect-tts-engine flow.
 */

import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Loader2Icon, Volume2Icon } from "lucide-react";
import { toast } from "sonner";

import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

interface TtsEnginesResponse {
  available: string[];
  default: string;
}

export function TtsEngineCard() {
  const { t } = useTranslation("settings");
  const [data, setData] = useState<TtsEnginesResponse | null>(null);
  const [selected, setSelected] = useState<string>("auto");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const res = await api.get<TtsEnginesResponse>(
          "/api/voice/wizard/tts-engines",
          { signal: controller.signal },
        );
        setData(res);
      } catch (e) {
        if (e instanceof DOMException && e.name === "AbortError") return;
        // Endpoint failure leaves the card dormant (no controls rendered)
        // — voice may not be installed at all on this host.
        setData(null);
      } finally {
        setLoading(false);
      }
    })();
    return () => controller.abort();
  }, []);

  const handleSave = async () => {
    setSaving(true);
    try {
      const res = await api.post<{ ok: boolean; error?: string }>(
        "/api/voice/enable",
        { tts_engine: selected },
      );
      if (res.ok) {
        toast.success(t("tts.saved", "TTS engine preference saved"));
      } else {
        toast.error(res.error ?? t("tts.saveFailed", "Failed to save"));
      }
    } catch {
      toast.error(t("tts.saveFailed", "Failed to save"));
    } finally {
      setSaving(false);
    }
  };

  // No card surface until we know what's available.
  if (loading) return null;
  // Only render when at least one cloud engine is importable beyond the
  // always-present "auto" — otherwise the operator has no real choice.
  if (!data || data.available.length <= 1) return null;

  return (
    <Card data-testid="tts-engine-card">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Volume2Icon className="size-4" />
          {t("tts.title", "TTS Engine")}
        </CardTitle>
        <CardDescription>
          {t(
            "tts.description",
            "Pick which voice engine generates speech. \"Auto\" prefers Piper when both are available.",
          )}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <select
          value={selected}
          onChange={(e) => setSelected(e.target.value)}
          className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm text-foreground outline-none transition-colors focus:border-ring focus:ring-2 focus:ring-ring/50"
          data-testid="tts-engine-select"
        >
          {data.available.map((engine) => (
            <option key={engine} value={engine}>
              {t(`tts.engine.${engine}`, engine)}
            </option>
          ))}
        </select>
        <div className="flex justify-end">
          <Button size="sm" onClick={() => void handleSave()} disabled={saving}>
            {saving && <Loader2Icon className="mr-1.5 size-3.5 animate-spin" />}
            {t("tts.save", "Save preference")}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
