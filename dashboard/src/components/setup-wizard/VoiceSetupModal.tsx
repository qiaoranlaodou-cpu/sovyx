/**
 * VoiceSetupModal -- specialized setup wizard for the voice pipeline.
 *
 * Flow:
 *   1. Hardware detection (CPU, RAM, GPU, audio devices)
 *   2. Show recommended models for detected tier
 *   3. User clicks "Enable Voice"
 *   4. Backend checks deps, creates pipeline, registers in ServiceRegistry
 *   5. If deps missing: show install instructions with copy button
 *   6. If success: close modal, voice is active
 */

import { useCallback, useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import {
  MicIcon,
  MicOffIcon,
  LoaderIcon,
  CopyIcon,
  CheckIcon,
  XCircleIcon,
  PackageIcon,
  Volume2Icon,
} from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { useResolvedMindId } from "@/hooks/use-resolved-mind-id";
import { verifyVoiceRunning } from "@/hooks/use-voice-running-verification";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogTrigger,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import { HardwareDetection, type SelectedDevices } from "./HardwareDetection";

interface MissingDep {
  module: string;
  package: string;
}

interface EnableResponse {
  ok: boolean;
  status?: string;
  error?: string;
  detail?: string;
  device?: number | string | null;
  host_api?: string | null;
  observed_peak_rms_db?: number;
  missing_deps?: MissingDep[];
  missing_models?: Array<{ name: string; install_command: string }>;
  install_command?: string;
  tts_engine?: string;
}

interface CaptureSilenceInfo {
  detail: string;
  device: number | string | null;
  hostApi: string | null;
  observedPeakRmsDb: number;
}

interface VoiceSetupModalProps {
  trigger?: React.ReactNode;
  onEnabled?: () => void;
}

export function VoiceSetupModal({ trigger, onEnabled }: VoiceSetupModalProps) {
  const { t } = useTranslation("voice");
  // v0.32.2 Phase 3.A Layer A — anti-pattern #35 cluster P0.A4. Pre-fix
  // ``handleEnable`` POSTed the bare ``devices`` array as the body
  // (zero ``mind_id``); now the body wraps the devices alongside the
  // resolved mind_id so multi-mind operators target the right mind.
  const { mindId } = useResolvedMindId();
  const [open, setOpen] = useState(false);
  const [enabling, setEnabling] = useState(false);
  const [detected, setDetected] = useState(false);
  const [depsIssue, setDepsIssue] = useState<{
    missing: MissingDep[];
    command: string;
  } | null>(null);
  const [audioError, setAudioError] = useState(false);
  const [silenceInfo, setSilenceInfo] = useState<CaptureSilenceInfo | null>(null);
  const [enableError, setEnableError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [devices, setDevices] = useState<SelectedDevices>({
    input_device: null,
    output_device: null,
  });

  const handleDetected = useCallback(() => {
    setDetected(true);
  }, []);

  const handleEnable = useCallback(async () => {
    setEnabling(true);
    setDepsIssue(null);
    setAudioError(false);
    setSilenceInfo(null);
    setEnableError(null);

    try {
      // v0.32.2 Phase 3.A Layer A — body wraps devices + explicit mind_id.
      // The backend's ``/api/voice/enable`` accepts `input_device` /
      // `output_device` at the top level alongside `mind_id`; spreading
      // the existing `devices` object keeps backward compatibility.
      const result = await api.post<EnableResponse>("/api/voice/enable", {
        ...devices,
        mind_id: mindId,
      });
      if (result.ok) {
        // v0.31.6 T3.1 — backend ``ok: true`` only proves the enable
        // request did not error; it does NOT prove the pipeline is
        // actually running (mind.yaml write under contextlib.suppress
        // can drop the persisted state silently). Poll /api/voice/status
        // before declaring success + closing the modal so an operator
        // doesn't see "Voice enabled!" toast immediately followed by a
        // dead voice surface on the next page.
        const verdict = await verifyVoiceRunning();
        if (verdict.status !== "running") {
          setEnableError(t(`setup.verify.${verdict.status}`));
          setEnabling(false);
          return;
        }
        toast.success(
          result.tts_engine
            ? t("setupModal.toastEnabledWithEngine", { engine: result.tts_engine })
            : t("setupModal.toastEnabled"),
        );
        setOpen(false);
        onEnabled?.();
      }
    } catch (err) {
      if (err instanceof ApiError) {
        try {
          const body = JSON.parse(err.message) as EnableResponse;
          if (body.error === "missing_deps" && body.missing_deps) {
            setDepsIssue({
              missing: body.missing_deps,
              command: body.install_command ?? "pip install sovyx[voice]",
            });
          } else if (body.error === "capture_silence") {
            setSilenceInfo({
              detail: body.detail ?? t("setupModal.fallbackError.silenceFallback"),
              device: body.device ?? null,
              hostApi: body.host_api ?? null,
              observedPeakRmsDb:
                typeof body.observed_peak_rms_db === "number"
                  ? body.observed_peak_rms_db
                  : Number.NEGATIVE_INFINITY,
            });
          } else if (
            typeof body.error === "string" &&
            body.error.toLowerCase().includes("audio")
          ) {
            setAudioError(true);
          } else {
            setEnableError(body.error ?? t("setupModal.fallbackError.enableFailed"));
          }
        } catch {
          setEnableError(err.message || t("setupModal.fallbackError.genericFailure"));
        }
      } else {
        setEnableError(t("setupModal.fallbackError.genericFailure"));
      }
    } finally {
      setEnabling(false);
    }
  }, [onEnabled, devices, mindId, t]);

  const handleCopy = useCallback(
    (command: string) => {
      void navigator.clipboard.writeText(command);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    },
    [],
  );

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger
        render={
          (trigger as React.ReactElement) ?? (
            <Button variant="outline" size="sm">
              <MicIcon className="mr-1.5 size-3.5" />
              {t("setupModal.trigger")}
            </Button>
          )
        }
      />
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{t("setupModal.title")}</DialogTitle>
          <DialogDescription>{t("setupModal.description")}</DialogDescription>
        </DialogHeader>

        <div className="py-2 space-y-4">
          <HardwareDetection onDetected={handleDetected} onDeviceChange={setDevices} />

          {/* Dependency issue panel */}
          {depsIssue && (
            <div className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-warning)]/40 bg-[var(--svx-color-warning)]/5 p-4 space-y-3">
              <div className="flex items-center gap-2 text-xs font-medium text-[var(--svx-color-text-primary)]">
                <PackageIcon className="size-4 text-[var(--svx-color-warning)]" />
                {t("setupModal.depsPanel.title")}
              </div>

              <div className="space-y-1.5">
                {depsIssue.missing.map((dep) => (
                  <div
                    key={dep.module}
                    className="flex items-center gap-2 text-xs"
                  >
                    <XCircleIcon className="size-3 text-[var(--svx-color-error)] shrink-0" />
                    <span className="font-mono text-[var(--svx-color-text-secondary)]">
                      {dep.package}
                    </span>
                    <span className="text-[var(--svx-color-text-tertiary)]">
                      {t("setupModal.depsPanel.notInstalled")}
                    </span>
                  </div>
                ))}
              </div>

              <div className="space-y-2">
                <p className="text-[11px] text-[var(--svx-color-text-secondary)]">
                  {t("setupModal.depsPanel.oneTimeInstall")}
                </p>
                <div className="flex items-center gap-2 rounded-[var(--svx-radius-md)] border border-[var(--svx-color-border-default)] bg-[var(--svx-color-bg-surface)] px-3 py-2">
                  <code className="flex-1 text-xs font-mono text-[var(--svx-color-text-primary)]">
                    {depsIssue.command}
                  </code>
                  <button
                    type="button"
                    onClick={() => handleCopy(depsIssue.command)}
                    className="shrink-0 rounded-[var(--svx-radius-sm)] p-1 text-[var(--svx-color-text-tertiary)] hover:text-[var(--svx-color-text-primary)] transition-colors"
                    aria-label={t("setupModal.depsPanel.copyCommandAria")}
                  >
                    {copied ? (
                      <CheckIcon className="size-3.5 text-[var(--svx-color-success)]" />
                    ) : (
                      <CopyIcon className="size-3.5" />
                    )}
                  </button>
                </div>
                <p className="text-[11px] text-[var(--svx-color-text-tertiary)]">
                  {t("setupModal.depsPanel.afterInstallHint")}
                </p>
              </div>
            </div>
          )}

          {/* Audio hardware error panel */}
          {audioError && (
            <div className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-error)]/40 bg-[var(--svx-color-error)]/5 p-4 space-y-3">
              <div className="flex items-center gap-2 text-xs font-medium text-[var(--svx-color-text-primary)]">
                <Volume2Icon className="size-4 text-[var(--svx-color-error)]" />
                {t("setupModal.audioErrorPanel.title")}
              </div>
              <p className="text-xs text-[var(--svx-color-text-secondary)] leading-relaxed">
                {t("setupModal.audioErrorPanel.body")}
              </p>
            </div>
          )}

          {/* Capture silence panel — backend tried every host-API variant
              and every one delivered zeros. Surfaces host_api + observed
              RMS so the user has actionable diagnostic data. */}
          {silenceInfo && (
            <div className="rounded-[var(--svx-radius-lg)] border border-[var(--svx-color-error)]/40 bg-[var(--svx-color-error)]/5 p-4 space-y-3">
              <div className="flex items-center gap-2 text-xs font-medium text-[var(--svx-color-text-primary)]">
                <MicOffIcon className="size-4 text-[var(--svx-color-error)]" />
                {t("setupModal.silencePanel.title")}
              </div>
              <p className="text-xs text-[var(--svx-color-text-secondary)] leading-relaxed">
                {t("setupModal.silencePanel.body")}
              </p>
              <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[11px] font-mono text-[var(--svx-color-text-tertiary)]">
                {silenceInfo.hostApi && (
                  <>
                    <dt>{t("setupModal.silencePanel.hostApiLabel")}</dt>
                    <dd className="text-[var(--svx-color-text-secondary)]">
                      {silenceInfo.hostApi}
                    </dd>
                  </>
                )}
                {silenceInfo.device !== null && (
                  <>
                    <dt>{t("setupModal.silencePanel.deviceLabel")}</dt>
                    <dd className="text-[var(--svx-color-text-secondary)]">
                      {String(silenceInfo.device)}
                    </dd>
                  </>
                )}
                {Number.isFinite(silenceInfo.observedPeakRmsDb) && (
                  <>
                    <dt>{t("setupModal.silencePanel.peakRmsLabel")}</dt>
                    <dd className="text-[var(--svx-color-text-secondary)]">
                      {silenceInfo.observedPeakRmsDb.toFixed(1)} dBFS
                    </dd>
                  </>
                )}
              </dl>
              <p className="text-[11px] text-[var(--svx-color-text-tertiary)]">
                {t("setupModal.silencePanel.fixHint")}
              </p>
            </div>
          )}

          {/* Generic error */}
          {enableError && !depsIssue && !audioError && !silenceInfo && (
            <div className="flex items-center gap-2 rounded-[var(--svx-radius-md)] bg-[var(--svx-color-error)]/10 px-3 py-2.5 text-xs text-[var(--svx-color-error)]">
              <XCircleIcon className="size-3.5 shrink-0" />
              <span>{enableError}</span>
            </div>
          )}
        </div>

        <DialogFooter showCloseButton>
          {detected && (
            <Button
              onClick={handleEnable}
              disabled={enabling}
              className="min-w-[140px]"
            >
              {enabling ? (
                <LoaderIcon className="mr-2 size-3.5 animate-spin" />
              ) : (
                <MicIcon className="mr-2 size-3.5" />
              )}
              {enabling ? t("setupModal.enablingAction") : t("setupModal.enableAction")}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
