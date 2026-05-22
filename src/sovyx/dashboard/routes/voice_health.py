"""L7 Voice Health REST surface — ADR §4.7.

Endpoints (all protected by ``verify_token``, all under ``/api/voice/health``):

* ``GET  /``            — snapshot of ComboStore + CaptureOverrides.
* ``POST /reprobe``     — run a single probe (warm or cold) on an endpoint.
* ``POST /forget``      — invalidate a ComboStore entry.
* ``POST /pin``         — write an entry to CaptureOverrides.

The store is a small on-disk JSON (< 5 KB) so handlers instantiate the
reader on demand instead of keeping a process-long handle. That matches
how the CLI accesses the same files and makes the backend immune to
stale in-memory state when an operator edits the JSON manually.

WebSocket streaming (``WS /stream``) lands in a follow-up once the L4
watchdog exposes a subscribable diagnosis-update source.
"""

from __future__ import annotations

import asyncio
import math
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Final, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
)
from starlette.status import (
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
    HTTP_500_INTERNAL_SERVER_ERROR,
    HTTP_503_SERVICE_UNAVAILABLE,
)

from sovyx.dashboard.routes._deps import verify_token
from sovyx.observability.logging import get_logger
from sovyx.voice.health import (
    CaptureOverrides,
    Combo,
    ComboEntry,
    ComboStore,
    Diagnosis,
    EndpointQuarantine,
    OverrideEntry,
    ProbeMode,
    ProbeResult,
    QuarantineEntry,
    RemediationHint,
    get_default_quarantine,
    probe,
)
from sovyx.voice.health._factory_integration import (
    resolve_capture_overrides_path,
    resolve_combo_store_path,
)
from sovyx.voice.health._failover_history import get_default_failover_history
from sovyx.voice.health._quarantine_reasons import QuarantineReason, is_lifecycle_tag

if TYPE_CHECKING:
    from sovyx.engine.config import EngineConfig
    from sovyx.engine.registry import ServiceRegistry
    from sovyx.voice.vad import SileroVAD

logger = get_logger(__name__)

router = APIRouter(
    prefix="/api/voice/health",
    dependencies=[Depends(verify_token)],
    tags=["voice-health"],
)


# ── JSON-safe float clamps ──────────────────────────────────────────────
# Probe paths legitimately emit ``float('-inf')`` (muted mics, stream-open
# failures, hard-timeouts) and could in principle emit ``nan`` from a
# degenerate VAD run. Starlette's ``JSONResponse.render()`` calls
# ``json.dumps(..., allow_nan=False)`` which raises ``ValueError`` on any
# non-finite float — every reprobe of a muted mic would otherwise return
# HTTP 500 instead of the MUTED diagnosis the panel exists to surface.

_RMS_DB_MIN = -90.0
_RMS_DB_MAX = 0.0


def _safe_rms_db(value: float) -> float:
    """Clamp ``rms_db`` to a JSON-serialisable finite range."""
    if not math.isfinite(value):
        return _RMS_DB_MIN
    return max(_RMS_DB_MIN, min(_RMS_DB_MAX, value))


def _safe_prob(value: float | None) -> float | None:
    """Clamp a 0-1 probability; return ``None`` for non-finite values."""
    if value is None:
        return None
    if not math.isfinite(value):
        return None
    return max(0.0, min(1.0, value))


# ── Mission C.1 § — QuarantineReason transport binding (LENIENT) ───────
# Env-knob rollback (Layer 1) for Mission C.1 staged-adoption foundation.
# C.1-a narrows ``QuarantineEntryModel.{reason,derived_reason,resolved_reason}``
# to ``QuarantineReason | str`` so pydantic smart-mode coerces known
# enum values to ``QuarantineReason`` instances while accepting H3
# lifecycle tags (``"probe_pinned"`` / ``"probe_store"`` / …) and
# legacy values via the ``str`` Union arm. The field validator below
# emits a structured WARN whenever a value lands on the ``str`` arm
# WITHOUT being a recognised lifecycle tag — that is the "neither
# QuarantineReason nor lifecycle tag" drift surface anti-pattern #46
# was authored against.
#
# Default behaviour is LENIENT (knob unset / "0" / "false"): the
# validator passes unknown values through with WARN. The operator can
# opt the boundary in to STRICT (raise :class:`ValueError` → pydantic
# ``ValidationError`` → HTTP 500 at the API surface) by setting
# ``SOVYX_TRANSPORT__QUARANTINE_REASON_STRICT=true`` before daemon
# start. Phase 3 STRICT v0.53.0 (H3 cycle close) flips the default to
# True; this knob then becomes the rollback escape.
_QUARANTINE_REASON_STRICT_ENV: Final = "SOVYX_TRANSPORT__QUARANTINE_REASON_STRICT"


def _quarantine_reason_strict() -> bool:
    """Return True iff the operator opted in to STRICT boundary rejection.

    Reads :data:`_QUARANTINE_REASON_STRICT_ENV` at call time so tests can
    monkeypatch ``os.environ`` per-case. Accepts ``"1"``/``"true"``/
    ``"yes"`` (case-insensitive) as True; anything else (including the
    default unset state) returns False — the LENIENT v0.49.40+ default.
    """
    return os.environ.get(_QUARANTINE_REASON_STRICT_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _coerce_quarantine_reason(value: object) -> object:
    """Mission C.1 §C.1-a — BeforeValidator: coerce enum-value str to enum.

    Pydantic v2 smart-mode Union picks ``str`` over :class:`QuarantineReason`
    for plain string inputs because both arms validate the same value
    and ``str`` is the wider base type. Run this BeforeValidator FIRST
    to convert any string that matches a :class:`QuarantineReason` value
    into the enum instance — pydantic then validates the result against
    the Union and the :class:`QuarantineReason` arm wins. Non-matching
    strings (lifecycle tags, drift values) fall through unchanged.
    """
    if isinstance(value, QuarantineReason):
        return value
    if isinstance(value, str):
        try:
            return QuarantineReason(value)
        except ValueError:
            return value
    return value  # let pydantic raise for non-str non-enum types


def _validate_quarantine_reason_field(value: QuarantineReason | str) -> QuarantineReason | str:
    """Mission C.1 §C.1-a field-validator for ``reason`` family fields.

    Accepts three classes of value:

    1. :class:`QuarantineReason` instance (already coerced via pydantic
       smart-mode Union narrowing) — passes through unchanged.
    2. ``str`` matching a recognised H3 lifecycle tag
       (``"probe_pinned"`` / ``"probe_store"`` / ``"probe_cascade"`` /
       ``"factory_integration"`` / ``"kernel_invalidated_recheck"`` /
       ``"probe"`` / ``"watchdog_recheck"`` per
       :func:`sovyx.voice.health._quarantine_reasons.is_lifecycle_tag`)
       — passes through unchanged.
    3. ``str`` empty string (default for ``derived_reason`` /
       ``resolved_reason``) — passes through unchanged.

    Anything else is "drift" — neither a :class:`QuarantineReason` member
    nor a known lifecycle tag. LENIENT default emits a structured WARN
    + passes the value through. STRICT (via
    :func:`_quarantine_reason_strict`) raises :class:`ValueError`,
    surfacing the drift as a pydantic ``ValidationError`` at the
    boundary so the dashboard sees an HTTP 500 instead of a silent
    classification lie.
    """
    if isinstance(value, QuarantineReason):
        return value
    # pydantic smart-mode Union(QuarantineReason | str) lands enum-string
    # values on the QuarantineReason branch automatically; reaching this
    # point with a ``str`` means the value did NOT match an enum member.
    if value == "":
        return value
    if is_lifecycle_tag(value):
        return value
    if _quarantine_reason_strict():
        msg = (
            f"quarantine reason {value!r} is neither a QuarantineReason member "
            f"nor a known H3 lifecycle tag — see CLAUDE.md anti-pattern #46 + "
            f"Mission C.1 §C.1-a (env knob {_QUARANTINE_REASON_STRICT_ENV})."
        )
        raise ValueError(msg)
    logger.warning(
        "voice_quarantine_reason_unrecognized",
        reason=value,
        strict=False,
        anti_pattern="46",
        mission="C.1",
        # Operator action: investigate producer site that emitted this
        # value; once classified, either add it to QuarantineReason (if
        # terminal) or to _LIFECYCLE_TAGS (if lifecycle annotation).
    )
    return value


# ── Pydantic models ─────────────────────────────────────────────────────


class ComboModel(BaseModel):
    """Wire shape of :class:`~sovyx.voice.health.contract.Combo`."""

    host_api: str
    sample_rate: int
    channels: int
    sample_format: str
    exclusive: bool
    auto_convert: bool
    frames_per_buffer: int

    def to_domain(self, *, platform_key: str = "") -> Combo:
        return Combo(
            host_api=self.host_api,
            sample_rate=self.sample_rate,
            channels=self.channels,
            sample_format=self.sample_format,
            exclusive=self.exclusive,
            auto_convert=self.auto_convert,
            frames_per_buffer=self.frames_per_buffer,
            platform_key=platform_key,
        )

    @classmethod
    def from_domain(cls, combo: Combo) -> ComboModel:
        return cls(
            host_api=combo.host_api,
            sample_rate=combo.sample_rate,
            channels=combo.channels,
            sample_format=combo.sample_format,
            exclusive=combo.exclusive,
            auto_convert=combo.auto_convert,
            frames_per_buffer=combo.frames_per_buffer,
        )


class RemediationHintModel(BaseModel):
    code: str
    severity: Literal["info", "warn", "error"]
    cli_action: str | None = None

    @classmethod
    def from_domain(cls, hint: RemediationHint) -> RemediationHintModel:
        # `RemediationHint.__post_init__` enforces severity ∈ {info, warn, error}
        # at construction time, so narrowing at the boundary is sound.
        severity = cast('Literal["info", "warn", "error"]', hint.severity)
        return cls(code=hint.code, severity=severity, cli_action=hint.cli_action)


class ProbeHistoryModel(BaseModel):
    ts: str
    mode: str
    diagnosis: str
    vad_max_prob: float | None
    rms_db: float
    duration_ms: int


class ProbeResultModel(BaseModel):
    diagnosis: str
    mode: str
    combo: ComboModel
    vad_max_prob: float | None
    vad_mean_prob: float | None
    rms_db: float
    callbacks_fired: int
    duration_ms: int
    error: str | None = None
    remediation: RemediationHintModel | None = None

    @classmethod
    def from_domain(cls, result: ProbeResult) -> ProbeResultModel:
        return cls(
            diagnosis=result.diagnosis.value,
            mode=result.mode.value,
            combo=ComboModel.from_domain(result.combo),
            vad_max_prob=_safe_prob(result.vad_max_prob),
            vad_mean_prob=_safe_prob(result.vad_mean_prob),
            rms_db=_safe_rms_db(result.rms_db),
            callbacks_fired=result.callbacks_fired,
            duration_ms=result.duration_ms,
            error=result.error,
            remediation=(
                RemediationHintModel.from_domain(result.remediation)
                if result.remediation is not None
                else None
            ),
        )


class ComboEntryModel(BaseModel):
    endpoint_guid: str
    device_friendly_name: str
    device_interface_name: str
    device_class: str
    endpoint_fxproperties_sha: str
    winning_combo: ComboModel
    validated_at: str
    validation_mode: str
    vad_max_prob_at_validation: float | None
    vad_mean_prob_at_validation: float | None
    rms_db_at_validation: float
    probe_duration_ms: int
    detected_apos_at_validation: list[str]
    cascade_attempts_before_success: int
    boots_validated: int
    last_boot_validated: str
    last_boot_diagnosis: str
    probe_history: list[ProbeHistoryModel]
    pinned: bool
    needs_revalidation: bool

    @classmethod
    def from_domain(cls, entry: ComboEntry) -> ComboEntryModel:
        return cls(
            endpoint_guid=entry.endpoint_guid,
            device_friendly_name=entry.device_friendly_name,
            device_interface_name=entry.device_interface_name,
            device_class=entry.device_class,
            endpoint_fxproperties_sha=entry.endpoint_fxproperties_sha,
            winning_combo=ComboModel.from_domain(entry.winning_combo),
            validated_at=entry.validated_at,
            validation_mode=entry.validation_mode.value,
            vad_max_prob_at_validation=_safe_prob(entry.vad_max_prob_at_validation),
            vad_mean_prob_at_validation=_safe_prob(entry.vad_mean_prob_at_validation),
            rms_db_at_validation=_safe_rms_db(entry.rms_db_at_validation),
            probe_duration_ms=entry.probe_duration_ms,
            detected_apos_at_validation=list(entry.detected_apos_at_validation),
            cascade_attempts_before_success=entry.cascade_attempts_before_success,
            boots_validated=entry.boots_validated,
            last_boot_validated=entry.last_boot_validated,
            last_boot_diagnosis=entry.last_boot_diagnosis.value,
            probe_history=[
                ProbeHistoryModel(
                    ts=h.ts,
                    mode=h.mode.value,
                    diagnosis=h.diagnosis.value,
                    vad_max_prob=_safe_prob(h.vad_max_prob),
                    rms_db=_safe_rms_db(h.rms_db),
                    duration_ms=h.duration_ms,
                )
                for h in entry.probe_history
            ],
            pinned=entry.pinned,
            needs_revalidation=entry.needs_revalidation,
        )


class OverrideEntryModel(BaseModel):
    endpoint_guid: str
    device_friendly_name: str
    pinned_combo: ComboModel
    pinned_at: str
    pinned_by: str
    reason: str

    @classmethod
    def from_domain(cls, entry: OverrideEntry) -> OverrideEntryModel:
        return cls(
            endpoint_guid=entry.endpoint_guid,
            device_friendly_name=entry.device_friendly_name,
            pinned_combo=ComboModel.from_domain(entry.pinned_combo),
            pinned_at=entry.pinned_at,
            pinned_by=entry.pinned_by,
            reason=entry.reason,
        )


class QuarantineEntryModel(BaseModel):
    """Wire shape of :class:`~sovyx.voice.health._quarantine.QuarantineEntry`.

    Surfaces §4.4.7 kernel-invalidated quarantine to the dashboard so
    operators can see which capture endpoints sovyx has stopped probing
    and why.

    Mission C1 §T2.2 — exposes :attr:`reason` (legacy lifecycle tag —
    ``"apo_degraded"`` / ``"watchdog_recheck"`` / …) and
    :attr:`derived_reason` (Mission C1 LENIENT alias).
    Mission H3 §T2.8 + ADR-D2 — adds :attr:`resolved_reason` as the
    canonical SSoT-resolved field and :attr:`effective_reason` computed
    property that frontends + monitoring tooling read for the
    ``resolved or derived or reason`` field-chain fallback. Phase 3
    STRICT v0.53.0 promotes ``resolved_reason`` → ``reason`` and drops
    ``derived_reason``.

    Mission C.1 §C.1-a — narrows ``reason`` / ``derived_reason`` /
    ``resolved_reason`` from plain ``str`` to ``QuarantineReason | str``
    Union. Pydantic smart-mode coerces enum-value strings
    (``"apo_degraded"`` / ``"vad_frontend_dead"`` / …) to
    :class:`QuarantineReason` instances at the boundary so typed
    consumers (OpenAPI codegen, pydantic-typed Python clients) read
    enum members. The ``str`` Union arm preserves the H3 LENIENT-window
    backward-compat for lifecycle-tag values (``"probe_pinned"`` /
    ``"probe_store"`` / …). The paired
    :func:`_validate_quarantine_reason_field` validator surfaces drift
    (neither enum nor lifecycle tag) as a structured WARN under LENIENT
    default and as a :class:`ValidationError` when the operator opts in
    via ``SOVYX_TRANSPORT__QUARANTINE_REASON_STRICT=true``. Phase 3
    STRICT v0.53.0 H3 cycle close (Gate 14 STRICT flip) drops the
    ``| str`` Union arm leaving the field strictly typed
    :class:`QuarantineReason`.

    ``model_config`` ships ``extra="allow"`` per CLAUDE.md anti-pattern
    #40 — every QuarantineEntry field that ships downstream is forward-
    additive; future H3-sibling fields (e.g. composite-store metadata)
    land here without breaking older clients.
    """

    model_config = ConfigDict(extra="allow")

    endpoint_guid: str
    device_friendly_name: str
    device_interface_name: str
    host_api: str
    added_at_monotonic: float
    expires_at_monotonic: float
    seconds_until_expiry: float
    reason: Annotated[QuarantineReason | str, BeforeValidator(_coerce_quarantine_reason)]
    derived_reason: Annotated[
        QuarantineReason | str, BeforeValidator(_coerce_quarantine_reason)
    ] = ""
    resolved_reason: Annotated[
        QuarantineReason | str, BeforeValidator(_coerce_quarantine_reason)
    ] = ""

    _validate_reason = field_validator(
        "reason",
        "derived_reason",
        "resolved_reason",
        mode="after",
    )(_validate_quarantine_reason_field)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def effective_reason(self) -> str:
        """Mission H3 ADR-D2 — H3-canonical field-chain fallback.

        Returns the first non-empty value in priority order:
        :attr:`resolved_reason` (H3 SSoT-resolved) →
        :attr:`derived_reason` (Mission C1 LENIENT alias) →
        :attr:`reason` (legacy default). Phase 3 STRICT v0.53.0
        simplifies the body to ``return self.reason``.

        Returns the underlying string value (via :class:`QuarantineReason`
        being a :class:`StrEnum`, ``str(member)`` collapses to the enum
        value); downstream JSON serialisation is unchanged.
        """
        resolved = str(self.resolved_reason) if self.resolved_reason else ""
        derived = str(self.derived_reason) if self.derived_reason else ""
        reason = str(self.reason) if self.reason else ""
        return resolved or derived or reason

    @classmethod
    def from_domain(
        cls,
        entry: QuarantineEntry,
        *,
        now_monotonic: float,
        quarantine_s: float,
    ) -> QuarantineEntryModel:
        # ``seconds_until_expiry`` is clamped to ``[0, quarantine_s]``.
        # The lower clamp prevents negative leftover-time once an entry
        # is past expiry. The UPPER clamp honors the documented contract
        # against IEEE 754 precision residuals: ``(added + quarantine_s)
        # - now_monotonic`` returns ``quarantine_s + tiny_epsilon`` on
        # Windows when ``now == added`` (same ~15.6 ms monotonic tick;
        # CLAUDE.md anti-pattern #22). The literal ``quarantine_s``
        # float is exact, so ``min(quarantine_s, ...)`` guarantees the
        # snapshot's invariant ``seconds_until_expiry ∈ [0, quarantine_s]``.
        raw = entry.expires_at_monotonic - now_monotonic
        return cls(
            endpoint_guid=entry.endpoint_guid,
            device_friendly_name=entry.device_friendly_name,
            device_interface_name=entry.device_interface_name,
            host_api=entry.host_api,
            added_at_monotonic=entry.added_at_monotonic,
            expires_at_monotonic=entry.expires_at_monotonic,
            seconds_until_expiry=min(quarantine_s, max(0.0, raw)),
            reason=entry.reason,
            derived_reason=entry.derived_reason,
            resolved_reason=entry.resolved_reason,
        )


class QuarantineSnapshotResponse(BaseModel):
    """Snapshot of every endpoint currently in §4.4.7 quarantine."""

    entries: list[QuarantineEntryModel]
    count: int


# ── Mission C3 §T2.9 — failover-history models ──────────────────────────


class FailoverCandidateModel(BaseModel):
    """Per-candidate detail within a ladder run.

    Mirrors ``FailoverCandidateRecord`` from
    ``voice/health/_failover_history.py``. ``extra="allow"`` matches
    the ``VoiceStatusResponse`` forward-additive policy so future
    fields land without a schema migration.
    """

    model_config = {"extra": "allow"}

    index: int
    target_endpoint: str
    target_friendly_name: str = ""
    verdict: str  # "succeeded" | "failed" | "skipped"
    error_class: str = ""
    error_detail: str = ""
    elapsed_ms: int | None = None
    skipped_reason: str | None = None


class FailoverHistoryEntryModel(BaseModel):
    """One ladder invocation captured for the dashboard widget.

    Mirrors ``FailoverLadderRunRecord`` from
    ``voice/health/_failover_history.py``.
    """

    model_config = {"extra": "allow"}

    ladder_id: str
    started_monotonic: float
    completed_monotonic: float | None = None
    verdict: str  # "in_progress" | "succeeded" | "exhausted"
    candidates_tried: int = 0
    succeeded_index: int | None = None
    candidates: list[FailoverCandidateModel] = Field(default_factory=list)
    from_endpoint: str = ""
    elapsed_ms: int | None = None
    derived_reason: str = ""
    mind_id: str = ""


class FailoverHistoryResponse(BaseModel):
    """Failover-history snapshot. Mission C3 §T2.9.

    Returned by ``GET /api/voice/health/failover-history``. ``entries``
    are sorted newest-first (most recent ladder run first). The ring
    is bounded by ``VoiceTuningConfig.failover_history_ring_capacity``
    (default 32); operators read the full ring; the dashboard widget
    renders the most recent 16.
    """

    model_config = {"extra": "allow"}

    entries: list[FailoverHistoryEntryModel] = Field(default_factory=list)
    ring_capacity: int


class HealthSnapshotResponse(BaseModel):
    combo_store: list[ComboEntryModel]
    overrides: list[OverrideEntryModel]
    quarantine_count: int
    data_dir: str
    voice_enabled: bool


class ReprobeRequest(BaseModel):
    endpoint_guid: str = Field(min_length=1)
    # PortAudio indices rotate across reboots / hot-plugs; the ComboStore
    # only persists the stable endpoint GUID. Callers that *know* the
    # current numeric index may pass it, otherwise the handler resolves
    # it server-side from the ComboEntry's friendly name.
    device_index: int | None = Field(default=None, ge=0)
    mode: Literal["cold", "warm"] = "warm"
    combo: ComboModel | None = None
    duration_ms: int | None = Field(default=None, ge=100, le=10_000)


class ReprobeResponse(BaseModel):
    endpoint_guid: str
    result: ProbeResultModel


class ForgetRequest(BaseModel):
    endpoint_guid: str = Field(min_length=1)
    reason: str = Field(default="dashboard-forget", min_length=1)


class ForgetResponse(BaseModel):
    endpoint_guid: str
    invalidated: bool


class PinRequest(BaseModel):
    endpoint_guid: str = Field(min_length=1)
    device_friendly_name: str
    combo: ComboModel
    source: Literal["user", "wizard", "cli"] = "user"
    reason: str = ""


class PinResponse(BaseModel):
    endpoint_guid: str
    pinned: bool


# ── Helpers ─────────────────────────────────────────────────────────────


def _resolve_data_dir(request: Request) -> Path:
    """Return the Sovyx data directory from engine config or its default."""
    engine_config: EngineConfig | None = getattr(request.app.state, "engine_config", None)
    if engine_config is not None:
        return engine_config.database.data_dir
    return Path.home() / ".sovyx"


async def _load_combo_store(data_dir: Path) -> ComboStore:
    """Instantiate and load the ComboStore on a worker thread (blocking I/O)."""
    store = ComboStore(resolve_combo_store_path(data_dir))
    await asyncio.to_thread(store.load)
    return store


async def _load_capture_overrides(data_dir: Path) -> CaptureOverrides:
    """Instantiate and load the CaptureOverrides file on a worker thread."""
    overrides = CaptureOverrides(resolve_capture_overrides_path(data_dir))
    await asyncio.to_thread(overrides.load)
    return overrides


def _resolve_quarantine(request: Request) -> EndpointQuarantine:
    """Return the active §4.4.7 quarantine — registry-injected or singleton.

    Tests pass a fresh :class:`EndpointQuarantine` via ``app.state.quarantine``
    so cases don't bleed quarantine entries into each other. Production
    code falls through to the process-wide singleton so the dashboard sees
    the same store the cascade and watchdog mutate.
    """
    state_q: EndpointQuarantine | None = getattr(request.app.state, "quarantine", None)
    if state_q is not None:
        return state_q
    return get_default_quarantine()


def _voice_enabled(request: Request) -> bool:
    """Whether the voice pipeline is currently registered in the app."""
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        return False
    try:
        from sovyx.voice._capture_task import AudioCaptureTask

        return bool(registry.is_registered(AudioCaptureTask))
    except Exception:  # noqa: BLE001 — treat any registry introspection failure as "not enabled"
        return False


async def _resolve_vad(request: Request) -> SileroVAD | None:
    """Resolve the registered ``SileroVAD`` instance, or ``None`` if unavailable."""
    registry: ServiceRegistry | None = getattr(request.app.state, "registry", None)
    if registry is None:
        return None
    try:
        from sovyx.voice.vad import SileroVAD as _SileroVAD

        if not registry.is_registered(_SileroVAD):
            return None
        return await registry.resolve(_SileroVAD)
    except Exception:  # noqa: BLE001 — missing optional backend must not 500 the endpoint
        logger.warning("voice_health_vad_resolve_failed", exc_info=True)
        return None


def _combo_from_entry(
    store: ComboStore,
    overrides: CaptureOverrides,
    endpoint_guid: str,
) -> Combo | None:
    """Pick the combo to reprobe: pinned override > ComboStore entry."""
    pinned = overrides.get(endpoint_guid)
    if pinned is not None:
        return pinned
    entry = store.get(endpoint_guid)
    if entry is not None:
        return entry.winning_combo
    return None


def _lookup_device_index_by_name(friendly_name: str) -> int | None:
    """Find the current PortAudio input index whose name matches.

    Endpoint GUIDs (Windows MMDevice ids, ALSA hw: ids, CoreAudio uids)
    are OS-level identifiers that PortAudio's host-API layer exposes as
    a rotating integer ``device`` index. The ComboStore intentionally
    does not persist the numeric index — it would go stale after any
    hot-plug — so the reprobe handler resolves it from the stored
    ``device_friendly_name`` at call time.

    Returns ``None`` when no input device matches, or when
    ``sounddevice`` cannot be imported (no PortAudio available, e.g.
    in headless CI containers).
    """
    if not friendly_name:
        return None
    try:
        import sounddevice as sd
    except (ImportError, OSError):
        return None
    try:
        devices = sd.query_devices()
    except Exception:  # noqa: BLE001 — PortAudio hiccup must not 500 the route
        return None
    needle = friendly_name.strip().lower()
    for idx, dev in enumerate(devices):
        name = str(dev.get("name", "")).strip().lower()
        input_channels = int(dev.get("max_input_channels", 0) or 0)
        if input_channels > 0 and name == needle:
            return idx
    return None


async def _resolve_device_index(
    store: ComboStore,
    overrides: CaptureOverrides,
    endpoint_guid: str,
) -> int | None:
    """Resolve a stable endpoint GUID to a current PortAudio input index.

    Preference order for the friendly name used in the lookup:

    1. Pinned override (most intentional; user explicitly pinned it).
    2. ComboStore entry (last-known-good).
    """
    friendly_name = ""
    pinned = overrides.get(endpoint_guid)
    if pinned is not None:
        override_entry = overrides.get_entry(endpoint_guid)
        if override_entry is not None:
            friendly_name = override_entry.device_friendly_name
    if not friendly_name:
        entry = store.get(endpoint_guid)
        if entry is not None:
            friendly_name = entry.device_friendly_name
    if not friendly_name:
        return None
    return await asyncio.to_thread(_lookup_device_index_by_name, friendly_name)


# ── Endpoints ───────────────────────────────────────────────────────────


@router.get("", response_model=HealthSnapshotResponse)
async def get_voice_health(request: Request) -> HealthSnapshotResponse:
    """Return the current ComboStore + CaptureOverrides snapshot."""
    data_dir = _resolve_data_dir(request)
    try:
        store = await _load_combo_store(data_dir)
        overrides = await _load_capture_overrides(data_dir)
    except Exception as exc:  # noqa: BLE001 — filesystem failure maps to 500
        logger.error("voice_health_snapshot_failed", exc_info=True)
        raise HTTPException(
            status_code=HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to read voice health state: {exc}",
        ) from exc

    quarantine = _resolve_quarantine(request)
    return HealthSnapshotResponse(
        combo_store=[ComboEntryModel.from_domain(e) for e in store.entries()],
        overrides=[OverrideEntryModel.from_domain(e) for e in overrides.entries()],
        quarantine_count=len(quarantine),
        data_dir=str(data_dir),
        voice_enabled=_voice_enabled(request),
    )


@router.get("/quarantine", response_model=QuarantineSnapshotResponse)
async def get_voice_health_quarantine(request: Request) -> QuarantineSnapshotResponse:
    """Return the §4.4.7 kernel-invalidated quarantine snapshot.

    Each entry is an endpoint that the cascade has stopped probing because
    its IAudioClient is in an invalidated state. Operators can use this to:

    * confirm that sovyx detected the problem the user is reporting,
    * see which endpoints have been auto-failed-over,
    * decide whether to suggest a USB replug or a reboot.
    """
    quarantine = _resolve_quarantine(request)
    now = time.monotonic()
    snapshot = quarantine.snapshot()
    return QuarantineSnapshotResponse(
        entries=[
            QuarantineEntryModel.from_domain(
                e,
                now_monotonic=now,
                quarantine_s=quarantine.quarantine_s,
            )
            for e in snapshot
        ],
        count=len(snapshot),
    )


@router.get("/failover-history", response_model=FailoverHistoryResponse)
async def get_voice_failover_history(
    request: Request,  # noqa: ARG001 — kept for symmetry + future auth context
) -> FailoverHistoryResponse:
    """Return the runtime failover-ladder history ring (Mission C3 §T2.9).

    The ring is populated by
    :func:`sovyx.voice.health._runtime_failover._try_runtime_failover`
    on every ladder completion. Each entry carries:

    * ``ladder_id`` — uuid4 hex (12 chars) for correlation with the
      ``voice.failover.*`` LogEvents in ``sovyx.log``.
    * ``verdict`` — ``"succeeded" | "exhausted" | "in_progress"``.
    * ``candidates`` — per-candidate detail with verdict, error_class,
      elapsed_ms, and skipped_reason where applicable.

    Returns 200 with an empty ``entries`` list when no ladder has yet
    run (common on a fresh daemon boot — the ring is process-local).
    """
    history = get_default_failover_history()
    entries = [
        FailoverHistoryEntryModel.model_validate(
            {
                "ladder_id": run.ladder_id,
                "started_monotonic": run.started_monotonic,
                "completed_monotonic": run.completed_monotonic,
                "verdict": run.verdict,
                "candidates_tried": run.candidates_tried,
                "succeeded_index": run.succeeded_index,
                "candidates": [
                    {
                        "index": c.index,
                        "target_endpoint": c.target_endpoint,
                        "target_friendly_name": c.target_friendly_name,
                        "verdict": c.verdict,
                        "error_class": c.error_class,
                        "error_detail": c.error_detail,
                        "elapsed_ms": c.elapsed_ms,
                        "skipped_reason": c.skipped_reason,
                    }
                    for c in run.candidates
                ],
                "from_endpoint": run.from_endpoint,
                "elapsed_ms": run.elapsed_ms,
                "derived_reason": run.derived_reason,
                "mind_id": run.mind_id,
            },
        )
        for run in history.entries()
    ]
    return FailoverHistoryResponse(
        entries=entries,
        ring_capacity=history.capacity,
    )


@router.post("/reprobe", response_model=ReprobeResponse)
async def post_voice_reprobe(
    request: Request,
    body: ReprobeRequest,
) -> ReprobeResponse:
    """Run a single probe against ``body.endpoint_guid`` and return the result.

    Combo resolution order:

    1. Request ``combo`` field when provided (explicit caller choice).
    2. Pinned override for the endpoint.
    3. ComboStore entry for the endpoint.

    Warm mode requires the voice pipeline to be enabled so a warmed-up
    ``SileroVAD`` is available. Cold mode has no VAD dependency.
    """
    data_dir = _resolve_data_dir(request)
    try:
        store = await _load_combo_store(data_dir)
        overrides = await _load_capture_overrides(data_dir)
    except Exception as exc:  # noqa: BLE001
        logger.error("voice_health_reprobe_store_failed", exc_info=True)
        raise HTTPException(
            status_code=HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to read voice health state: {exc}",
        ) from exc

    combo: Combo | None
    if body.combo is not None:
        try:
            combo = body.combo.to_domain()
        except ValueError as exc:
            raise HTTPException(
                status_code=HTTP_409_CONFLICT,
                detail=f"Invalid combo: {exc}",
            ) from exc
    else:
        combo = _combo_from_entry(store, overrides, body.endpoint_guid)
        if combo is None:
            raise HTTPException(
                status_code=HTTP_404_NOT_FOUND,
                detail=(
                    f"No combo known for endpoint {body.endpoint_guid!r} — "
                    "provide `combo` explicitly or run the setup wizard first."
                ),
            )

    device_index = body.device_index
    if device_index is None:
        device_index = await _resolve_device_index(store, overrides, body.endpoint_guid)
    if device_index is None:
        raise HTTPException(
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"Could not resolve a PortAudio input index for endpoint "
                f"{body.endpoint_guid!r} — the device may be unplugged or "
                "the audio subsystem is unavailable."
            ),
        )

    mode = ProbeMode.WARM if body.mode == "warm" else ProbeMode.COLD
    vad = None
    if mode is ProbeMode.WARM:
        vad = await _resolve_vad(request)
        if vad is None:
            raise HTTPException(
                status_code=HTTP_409_CONFLICT,
                detail=(
                    "Warm probe requires the voice pipeline to be enabled "
                    "(SileroVAD not registered). Enable voice or request "
                    '`mode="cold"`.'
                ),
            )

    try:
        result = await probe(
            combo=combo,
            mode=mode,
            device_index=device_index,
            duration_ms=body.duration_ms,
            vad=vad,
        )
    except Exception as exc:  # noqa: BLE001 — probe may raise ValueError for bad DI, etc.
        logger.error(
            "voice_health_reprobe_failed",
            endpoint=body.endpoint_guid,
            exc_info=True,
        )
        raise HTTPException(
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Probe failed: {exc}",
        ) from exc

    logger.info(
        "voice_health_reprobe_completed",
        endpoint=body.endpoint_guid,
        mode=mode.value,
        diagnosis=result.diagnosis.value,
        duration_ms=result.duration_ms,
    )
    return ReprobeResponse(
        endpoint_guid=body.endpoint_guid,
        result=ProbeResultModel.from_domain(result),
    )


@router.post("/forget", response_model=ForgetResponse)
async def post_voice_forget(
    request: Request,
    body: ForgetRequest,
) -> ForgetResponse:
    """Invalidate a ComboStore entry. Returns ``invalidated=False`` if absent."""
    data_dir = _resolve_data_dir(request)
    try:
        store = await _load_combo_store(data_dir)
    except Exception as exc:  # noqa: BLE001
        logger.error("voice_health_forget_load_failed", exc_info=True)
        raise HTTPException(
            status_code=HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to read ComboStore: {exc}",
        ) from exc

    existed = store.get(body.endpoint_guid) is not None
    if existed:
        try:
            await asyncio.to_thread(store.invalidate, body.endpoint_guid, body.reason)
        except Exception as exc:  # noqa: BLE001
            logger.error("voice_health_forget_invalidate_failed", exc_info=True)
            raise HTTPException(
                status_code=HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to invalidate ComboStore entry: {exc}",
            ) from exc

    logger.info(
        "voice_health_forget_completed",
        endpoint=body.endpoint_guid,
        invalidated=existed,
        reason=body.reason,
    )
    return ForgetResponse(endpoint_guid=body.endpoint_guid, invalidated=existed)


@router.post("/pin", response_model=PinResponse)
async def post_voice_pin(
    request: Request,
    body: PinRequest,
) -> PinResponse:
    """Pin a combo to ``capture_overrides.json`` for the given endpoint."""
    data_dir = _resolve_data_dir(request)
    try:
        overrides = await _load_capture_overrides(data_dir)
    except Exception as exc:  # noqa: BLE001
        logger.error("voice_health_pin_load_failed", exc_info=True)
        raise HTTPException(
            status_code=HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to read CaptureOverrides: {exc}",
        ) from exc

    try:
        combo = body.combo.to_domain()
    except ValueError as exc:
        raise HTTPException(
            status_code=HTTP_409_CONFLICT,
            detail=f"Invalid combo: {exc}",
        ) from exc

    try:
        await asyncio.to_thread(
            overrides.pin,
            body.endpoint_guid,
            device_friendly_name=body.device_friendly_name,
            combo=combo,
            source=body.source,
            reason=body.reason,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=HTTP_409_CONFLICT,
            detail=f"Invalid pin request: {exc}",
        ) from exc
    except Exception as exc:  # noqa: BLE001
        logger.error("voice_health_pin_write_failed", exc_info=True)
        raise HTTPException(
            status_code=HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to write CaptureOverrides: {exc}",
        ) from exc

    logger.info(
        "voice_health_pin_completed",
        endpoint=body.endpoint_guid,
        source=body.source,
    )
    return PinResponse(endpoint_guid=body.endpoint_guid, pinned=True)


# `Diagnosis` exported for frontend OpenAPI generators; unused inline but kept
# in __all__ so downstream type generation picks the canonical enum up.
__all__ = [
    "ComboEntryModel",
    "ComboModel",
    "Diagnosis",
    "ForgetRequest",
    "ForgetResponse",
    "HealthSnapshotResponse",
    "FailoverCandidateModel",
    "FailoverHistoryEntryModel",
    "FailoverHistoryResponse",
    "OverrideEntryModel",
    "PinRequest",
    "PinResponse",
    "ProbeResultModel",
    "QuarantineEntryModel",
    "QuarantineSnapshotResponse",
    "ReprobeRequest",
    "ReprobeResponse",
    "router",
]
