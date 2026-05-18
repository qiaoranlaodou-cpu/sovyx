# Voice troubleshooting — Linux (capture-chain integrity)

Mission H2 v0.49.7 — companion to
[voice-troubleshooting-windows.md](voice-troubleshooting-windows.md).
The Windows playbook documents the Voice Clarity APO bypass cascade;
this page documents the Linux equivalent surface, anchored on the
neutral `voice.capture_integrity.*` event family that Mission H2
introduces.

## Linux capture-chain processing

Linux audio capture goes through one or more processing layers between
the kernel driver and Sovyx:

| Layer | Where | Sovyx bypass strategy prefix |
|---|---|---|
| ALSA mixer + capture chain | Kernel-side | `linux.alsa_*` |
| PulseAudio module-echo-cancel | User-space sound server | `linux.module_echo_cancel_*` |
| PipeWire filter chain | User-space sound server | `linux.pipewire_*` |
| WirePlumber session-manager default-source | Policy layer above PipeWire | `linux.wireplumber_*`, `linux.session_manager_*` |

When Sovyx's voice pipeline emits sustained `voice_pipeline_deaf_warning`
events (capture stream wedged), the bypass coordinator dispatches a
cascade of these strategies. Mission H2 renames the dispatch's
observability events to platform-neutral names so operators reading
Linux logs see correct platform attribution.

## Observable events

| Event | Description |
|---|---|
| `voice.capture_integrity.bypass_activated` (Mission H2 neutral) / `voice_apo_bypass_activated` (legacy) | A bypass strategy succeeded in recovering the capture signal. |
| `voice.capture_integrity.bypass_ineffective` (neutral) / `voice_apo_bypass_ineffective` (legacy) | Every strategy in the cascade failed; endpoint quarantined. |
| `voice.capture_integrity.bypass_failed` (neutral) / `voice_apo_bypass_failed` (legacy) | The coordinator callback itself raised. |
| `voice.capture_integrity.bypassed` (neutral) / `audio.apo.bypassed` (legacy) | Terminal verdict event (`voice.verdict` = `success` / `failure` / `partial`). |

The neutral events carry three additional metadata fields not present
on the legacy twins:

* `voice.platform: "linux" | "windows" | "darwin" | "other"` —
  auto-resolved from `sys.platform`. On Linux this is always `"linux"`.
* `voice.bypass_family: str` — resolved via majority-vote across the
  strategy-name prefixes. On Linux the typical values are
  `alsa_capture_chain`, `pipewire_filter_chain`,
  `wireplumber_default_source`, or `module_echo_cancel`.
* `voice.event_schema_version: "2.0.0"` — the v2.0.0 schema marker.

## Remediation playbook

When `voice.capture_integrity.bypass_ineffective` fires on Linux:

1. **Check `sovyx doctor voice`** — it surfaces the capture-chain state
   and suggests remediations specific to your platform.
2. **Inspect ALSA mixer state** — `amixer scontrols` lists capture
   controls; common offenders are `Capture` muted or boost set wrong.
3. **Inspect PipeWire/PulseAudio modules** — `pactl list modules`
   shows loaded filter modules. `module-echo-cancel` is the most
   common signal-destroying offender on Linux.
4. **Reconnect the USB microphone** — physical replug is the cure for
   driver-level wedge states.
5. **Restart PipeWire** — `systemctl --user restart pipewire pipewire-pulse`
   forces a clean session-manager state.

## Cross-platform terminology

`apo` in legacy event names is a Windows-platform term (Audio
Processing Object). On Linux the equivalent processing happens via
PulseAudio/PipeWire modules — there are no APOs. Mission H2 closes
the cross-platform terminology drift by routing every operator log
through the neutral `voice.capture_integrity.*` namespace. Legacy
events continue firing through v0.51.0 STRICT for playbook compatibility;
operator runbooks SHOULD migrate to the neutral names during the
v0.49.x..v0.50.x window.
