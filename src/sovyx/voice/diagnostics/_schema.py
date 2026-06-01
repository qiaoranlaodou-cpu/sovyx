"""Single source of truth for the cross-toolkit diagnostic tarball contract.

W3.0 / AP #53 (MISSION-VOICE-DEEP-INVESTIGATION-2026-06-01).

The triage analyzer (:mod:`sovyx.voice.diagnostics.triage`) ingests a
``SUMMARY.json`` + section files from a tarball produced by one of three
forensic toolkits (Linux v4.3, Windows v2, macOS v1). Historically the field
names + section-dir paths lived as scattered string literals inside
``triage.py``'s hypothesis branches, with NO shared symbol — so a future
producer (the W3.1 Windows ETW/WASAPI capturer) would re-create them by hand
and drift silently the moment a name was mistyped (a counted-but-zeroed
hypothesis). This module is the contract both sides import.

The Windows-v2 contract below is the authoritative checklist a Windows
producer MUST satisfy; it is consumed by ``triage.py``'s ``toolkit ==
"windows"`` branches AND (W3.1) by the producer's SUMMARY.json serializer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import TypedDict

# ── Toolkit detection (SUMMARY.json ``tool`` / ``script_name`` substring) ──
TOOLKIT_WINDOWS_TOKENS: tuple[str, ...] = ("windows", "voice-diagnostic")
TOOLKIT_MACOS_TOKEN = "mac"

# ── Base SUMMARY.json fields (all toolkits) ──
SUMMARY_SCHEMA_VERSION = "schema_version"
ANALYZER_SELFTEST_STATUS = "analyzer_selftest_status"

# ── Cross-OS network-reachability records (``network_llm``) ──
NETWORK_LLM = "network_llm"
NETWORK_HOST = "host"
NETWORK_DNS_OK = "dns_ok"
NETWORK_TCP_OK = "tcp_ok"

# ── Windows-v2 SUMMARY.json contract (read by triage's windows branches) ──
WIN_AUDIO_ENDPOINTS = "audio_endpoints"
WIN_ENDPOINT_FRIENDLY_NAME = "friendly_name"
WIN_ENDPOINT_IS_ACTIVE = "is_active"
WIN_ENDPOINT_VOICE_CLARITY_ACTIVE = "voice_clarity_active"

WIN_LIVE_CAPTURES = "live_captures"
WIN_LIVE_VERDICT = "verdict"
WIN_LIVE_VERDICT_APO_CONFIRMED = "voice_clarity_destroying_apo_confirmed"
WIN_LIVE_VERDICT_APO_NOT_CULPRIT = "apo_not_culprit"
WIN_LIVE_DELTA_RMS = "delta_rms_dbfs"
WIN_LIVE_DELTA_VAD = "delta_vad"

WIN_CONSENT_STORE = "consent_store"
WIN_CONSENT_USER_GLOBAL = "user_global_value"
WIN_CONSENT_NONPACKAGED = "nonpackaged_apps"
WIN_CONSENT_APP_PATH = "app_path_enc"
WIN_CONSENT_APP_VALUE = "value"

# ── Section-dir relative paths read from the extracted tarball root ──
LINUX_TARGET_CARD_PATH = "C_alsa/target_card.txt"
MACOS_HAL_CLASSIFIER_PATH = "D_coreaudiod/hal_classifier.json"
MACOS_TCC_CONSENTS_PATH = "F_session/tcc_mic_consents.json"
MACOS_COREAUDIO_DUMP_PATH = "C_coreaudio/coreaudio_dump.json"
# NOTE (W3.0): the macOS section-dir prefixes are internally inconsistent
# (``C_coreaudio`` vs ``D_coreaudiod``) and ``F_session`` collides with the
# Linux toolkit's own session layer. Captured here AS-IS because no macOS
# producer emits them yet; a future macOS-producer mission should harmonise
# the scheme. Pinning them here at least stops the triage-side literals from
# drifting further.


if TYPE_CHECKING:

    class WindowsAudioEndpoint(TypedDict, total=False):
        """One Windows MMDevices capture endpoint in ``audio_endpoints``."""

        friendly_name: str
        is_active: bool
        voice_clarity_active: bool

    class WindowsLiveCaptures(TypedDict, total=False):
        """The shared-vs-exclusive WASAPI comparator verdict (``live_captures``)."""

        verdict: str
        delta_rms_dbfs: float
        delta_vad: float

    class WindowsConsentApp(TypedDict, total=False):
        """One non-packaged app row in ``consent_store.nonpackaged_apps``."""

        app_path_enc: str
        value: int

    class WindowsConsentStore(TypedDict, total=False):
        """Windows mic ConsentStore snapshot (``consent_store``)."""

        user_global_value: int
        nonpackaged_apps: list[WindowsConsentApp]

    class NetworkLlmProbe(TypedDict, total=False):
        """One provider reachability probe (``network_llm`` element)."""

        host: str
        dns_ok: bool
        tcp_ok: bool

    class WindowsSummaryV2(TypedDict, total=False):
        """The Windows-v2 ``SUMMARY.json`` a producer MUST emit for triage.

        ``schema_version`` is required (== 1); ``tool`` must contain a
        :data:`TOOLKIT_WINDOWS_TOKENS` token so ``_detect_toolkit`` routes to
        the Windows branches. The rest are read by hypotheses H2/H5/H9 + the
        cross-OS H6/H7.
        """

        schema_version: int
        tool: str
        tool_version: str
        host: str
        captured_at_utc: str
        os_descriptor: str
        status: str
        exit_code: str
        analyzer_selftest_status: str
        audio_endpoints: list[WindowsAudioEndpoint]
        live_captures: WindowsLiveCaptures
        consent_store: WindowsConsentStore
        network_llm: list[NetworkLlmProbe]


__all__ = [
    "ANALYZER_SELFTEST_STATUS",
    "LINUX_TARGET_CARD_PATH",
    "MACOS_COREAUDIO_DUMP_PATH",
    "MACOS_HAL_CLASSIFIER_PATH",
    "MACOS_TCC_CONSENTS_PATH",
    "NETWORK_DNS_OK",
    "NETWORK_HOST",
    "NETWORK_LLM",
    "NETWORK_TCP_OK",
    "SUMMARY_SCHEMA_VERSION",
    "TOOLKIT_MACOS_TOKEN",
    "TOOLKIT_WINDOWS_TOKENS",
    "WIN_AUDIO_ENDPOINTS",
    "WIN_CONSENT_APP_PATH",
    "WIN_CONSENT_APP_VALUE",
    "WIN_CONSENT_NONPACKAGED",
    "WIN_CONSENT_STORE",
    "WIN_CONSENT_USER_GLOBAL",
    "WIN_ENDPOINT_FRIENDLY_NAME",
    "WIN_ENDPOINT_IS_ACTIVE",
    "WIN_ENDPOINT_VOICE_CLARITY_ACTIVE",
    "WIN_LIVE_CAPTURES",
    "WIN_LIVE_DELTA_RMS",
    "WIN_LIVE_DELTA_VAD",
    "WIN_LIVE_VERDICT",
    "WIN_LIVE_VERDICT_APO_CONFIRMED",
    "WIN_LIVE_VERDICT_APO_NOT_CULPRIT",
]
