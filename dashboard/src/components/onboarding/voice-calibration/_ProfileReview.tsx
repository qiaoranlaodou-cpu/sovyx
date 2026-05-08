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
 *
 * v0.31.6 T3.1 + T3.5 — the inline ``_verifyVoiceRunning`` was
 * extracted to ``hooks/use-voice-running-verification.ts`` so the
 * receipt-check pattern can be parity-ported across every "voice
 * just enabled" surface (VoiceSetupModal + VoiceStep::enableWithDevices),
 * AND swallow-all-errors was replaced with a classified verdict so
 * 401/403/404 surface differentiated banners (auth_failed,
 * endpoint_missing) instead of looping the wrong "transient blip"
 * banner forever.
 */

import { useCallback, useState } from "react";
import { useTranslation } from "react-i18next";
import { AlertCircleIcon, CheckCircle2Icon, LoaderIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { verifyVoiceRunning } from "@/hooks/use-voice-running-verification";
import { assertNever } from "@/lib/utils";

interface ProfileReviewProps {
  triageWinnerHid: string | null;
  profilePath: string | null;
  onCompleted: () => void;
}

export function ProfileReview({
  triageWinnerHid,
  profilePath,
  onCompleted,
}: ProfileReviewProps) {
  const { t } = useTranslation("voice");
  const [verifying, setVerifying] = useState(false);
  const [verificationError, setVerificationError] = useState<string | null>(null);

  // v0.31.4 GAP 7 closure (re-extracted in v0.31.6 T3.1): pre-v0.31.4
  // ``onCompleted`` advanced the onboarding immediately on Confirm
  // click — without polling /api/voice/status to verify the pipeline
  // actually registered. If backend persistence failed silently (e.g.
  // mind.yaml write wrapped in contextlib.suppress), operator saw
  // "Confirm" → next onboarding step + voice off. Now: poll voice
  // status; if running confirmed → advance; otherwise show the
  // verdict-specific banner with explicit recovery path (auth retry,
  // upgrade Sovyx, retry from settings, …).
  const handleConfirm = useCallback(async () => {
    setVerifying(true);
    setVerificationError(null);
    try {
      const verdict = await verifyVoiceRunning();
      if (verdict.status === "running") {
        onCompleted();
        return;
      }
      // Map each non-running verdict to a differentiated i18n key so
      // the operator gets the actionable next step (sign in again,
      // upgrade, retry from settings) instead of the catch-all banner.
      //
      // v0.31.7 T3.8 (LOW.8) — switch terminates with ``assertNever``
      // so any future variant added to the verdict union (see
      // ``hooks/use-voice-running-verification.ts``) without an
      // explicit case here will fail tsc at compile time. The runtime
      // throw is the safety net for schema drift; the practical guard
      // is the compile-time check.
      switch (verdict.status) {
        case "auth_failure":
          setVerificationError(
            t("calibration.review.auth_failed", {
              defaultValue:
                "Your session expired during calibration. Sign in again to confirm voice setup.",
            }),
          );
          break;
        case "endpoint_missing":
          setVerificationError(
            t("calibration.review.endpoint_missing", {
              defaultValue:
                "Voice routes aren't available on this Sovyx daemon. Upgrade Sovyx or contact your operator.",
            }),
          );
          break;
        case "transient_failure":
          setVerificationError(
            t("calibration.review.transient_failure", {
              defaultValue:
                "Couldn't reach the voice service to verify setup ({{lastError}}). Check your network and retry.",
              lastError: verdict.lastError,
            }),
          );
          break;
        case "not_running":
          setVerificationError(
            t("calibration.review.verification_failed", {
              defaultValue:
                "Calibration finished but the voice pipeline isn't running yet. Open Settings → Voice → Recalibrate to retry, or enable manually from the Voice page.",
            }),
          );
          break;
        default:
          // The early return at line ~70 narrows the type by removing
          // ``status === "running"`` from the union; assertNever enforces
          // exhaustive coverage at compile time over the remaining
          // 4 variants. Adding a 5th non-running variant to
          // ``VoiceRunningVerdict`` (in ``use-voice-running-verification.ts``)
          // without an explicit case here will fail tsc.
          assertNever(verdict);
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
