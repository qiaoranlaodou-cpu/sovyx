/**
 * _ProfileReview -- terminal-state DONE render for the calibration
 * pipeline.
 *
 * Replaces the inline "done" branch of the prior monolithic
 * TerminalView. Surfaces the operator-actionable summary:
 *
 * * The detected hypothesis (triage_winner_hid) when one was crowned;
 * * The persisted profile path so operators can locate + audit the
 *   serialized state on disk;
 * * A localized explanation that points at the CLI commands the
 *   operator can run for deeper inspection (`--show`) or to undo
 *   the apply (`--rollback`); rollback is NOT a UI button — the
 *   `calibration.review.decision_explanation` i18n string carries
 *   the CLI breadcrumb. rc.7 (Agent 2 NEW.4) closed the prior gap
 *   where the i18n promised an in-UI rollback affordance that
 *   didn't exist.
 * * The continue button advancing the onboarding flow.
 *
 * Subcomponent of VoiceCalibrationStep per spec §6.3 (T3.4 split).
 * History: introduced in v0.30.25; rc.7 docstring synced with the
 * actual rendered surface (no rollback button — CLI command only).
 */

import { useCallback, useState } from "react";
import { useTranslation } from "react-i18next";
import { AlertCircleIcon, CheckCircle2Icon, LoaderIcon } from "lucide-react";

import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";

interface ProfileReviewProps {
  triageWinnerHid: string | null;
  profilePath: string | null;
  onCompleted: () => void;
}

interface VoiceStatusResponse {
  pipeline: { running: boolean };
}

// v0.31.4 GAP 7 closure: receipt-check retry loop tuning.
const _STATUS_CHECK_RETRIES = 3;
const _STATUS_CHECK_DELAY_MS = 1000;

async function _verifyVoiceRunning(): Promise<boolean> {
  // Polls /api/voice/status up to 3 times with 1s delays. Returns
  // true the first time pipeline.running===true; false if all
  // retries exhaust without seeing a running pipeline. Network
  // errors are treated as transient (continue retry); only a
  // confirmed not-running state at the final attempt returns false.
  for (let attempt = 0; attempt < _STATUS_CHECK_RETRIES; attempt += 1) {
    if (attempt > 0) {
      await new Promise((resolve) =>
        setTimeout(resolve, _STATUS_CHECK_DELAY_MS),
      );
    }
    try {
      const status = await api.get<VoiceStatusResponse>("/api/voice/status");
      if (status.pipeline.running === true) {
        return true;
      }
    } catch {
      // Network blip / 4xx — retry up to the cap.
    }
  }
  return false;
}

export function ProfileReview({
  triageWinnerHid,
  profilePath,
  onCompleted,
}: ProfileReviewProps) {
  const { t } = useTranslation("voice");
  const [verifying, setVerifying] = useState(false);
  const [verificationError, setVerificationError] = useState<string | null>(null);

  // v0.31.4 GAP 7 closure: pre-v0.31.4 ``onCompleted`` advanced the
  // onboarding immediately on Confirm click — without polling
  // /api/voice/status to verify the pipeline actually registered.
  // If backend persistence failed silently (e.g. mind.yaml write
  // wrapped in contextlib.suppress), operator saw "Confirm" → next
  // onboarding step + voice off. Now: poll voice status; if running
  // confirmed → advance; otherwise show error banner with explicit
  // recovery path (retry / manual enable on Voice page).
  const handleConfirm = useCallback(async () => {
    setVerifying(true);
    setVerificationError(null);
    try {
      const running = await _verifyVoiceRunning();
      if (running) {
        onCompleted();
      } else {
        setVerificationError(
          t("calibration.review.verification_failed", {
            defaultValue:
              "Calibration finished but the voice pipeline isn't running yet. Open Settings → Voice → Recalibrate to retry, or enable manually from the Voice page.",
          }),
        );
      }
    } finally {
      setVerifying(false);
    }
  }, [onCompleted, t]);

  // rc.10 (Agent 2 fix #5): map raw HypothesisId enum value (H1, H10,
  // etc.) to a friendly localized description so non-technical
  // operators see "Microphone volume was too low" instead of "H10".
  // Falls back to the raw hid if no label is registered (defensive
  // for future H-IDs not yet localized).
  const friendlyHypothesisLabel = triageWinnerHid
    ? t(`calibration.hypothesis.${triageWinnerHid}`, { defaultValue: triageWinnerHid })
    : null;
  return (
    <div className="space-y-4" data-testid="voice-calibration-profile-review">
      <div className="flex items-start gap-2 rounded-md border border-green-200 bg-green-50 p-3 text-sm text-green-900">
        <CheckCircle2Icon className="size-5 flex-shrink-0 mt-0.5" />
        <div className="space-y-1">
          <p className="font-medium">{t("calibration.terminal.done.title")}</p>
          {triageWinnerHid !== null && (
            <p className="text-xs">
              {t("calibration.terminal.done.winner", { hid: friendlyHypothesisLabel })}
            </p>
          )}
          {profilePath !== null && (
            <p className="text-xs font-mono break-all">{profilePath}</p>
          )}
        </div>
      </div>
      <div className="rounded-md border bg-background/50 p-3 text-xs text-muted-foreground">
        <p className="font-medium text-foreground">
          {t("calibration.review.title")}
        </p>
        <p className="mt-1">{t("calibration.review.decision_explanation")}</p>
      </div>
      {verificationError !== null && (
        <div
          className="flex items-start gap-2 rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-900"
          data-testid="voice-calibration-verification-failed"
          role="alert"
        >
          <AlertCircleIcon className="size-5 flex-shrink-0 mt-0.5" />
          <p>{verificationError}</p>
        </div>
      )}
      <Button onClick={() => void handleConfirm()} size="lg" disabled={verifying}>
        {verifying ? (
          <LoaderIcon className="mr-2 size-4 animate-spin" />
        ) : null}
        {verifying
          ? t("calibration.review.verifying", { defaultValue: "Verifying..." })
          : t("calibration.review.confirm")}
      </Button>
    </div>
  );
}
