# CLI Reference

The `sovyx` command is the single entry point for the daemon, the
interactive REPL, the diagnostic suite, and every subsystem management
surface. All commands are typer-driven; every subcommand accepts
`--help` for usage detail.

```
sovyx [OPTIONS] COMMAND [ARGS]...
```

| Option | Description |
|---|---|
| `--version`, `-v` | Show installed Sovyx version. |
| `--install-completion` | Install shell completion for the current shell (bash / zsh / fish / PowerShell). |
| `--show-completion` | Print completion code for the current shell — copy / customize / source manually. |
| `--help` | Show the top-level usage block + command list. |

The default config + data path is `~/.sovyx/`. Override with
`SOVYX_DATA_DIR` (see [`configuration.md`](configuration.md)).

---

## Top-level commands

| Command | Purpose |
|---|---|
| [`init`](#sovyx-init) | Bootstrap a fresh `~/.sovyx/` (config + data dir). |
| [`start`](#sovyx-start) | Start the daemon (REST + WebSocket + cogloop + voice). |
| [`stop`](#sovyx-stop) | Stop a running daemon. |
| [`status`](#sovyx-status) | One-shot daemon status snapshot. |
| [`token`](#sovyx-token) | Show / copy the dashboard authentication token. |
| [`chat`](#sovyx-chat) | Interactive REPL with the active mind. |
| [`logs`](#sovyx-logs) | Query / filter the structured log file. |
| [`brain`](#sovyx-brain) | Brain memory commands (analyze / search / export). |
| [`mind`](#sovyx-mind) | Mind management (list / create / set-default). |
| [`dashboard`](#sovyx-dashboard) | Dashboard management — show access info; bundle integrity doctor. |
| [`doctor`](#sovyx-doctor) | Cross-subsystem health checks + auto-fix tools. |
| [`plugin`](#sovyx-plugin) | Plugin management (list / install / disable). |
| [`voice`](#sovyx-voice) | Voice-data lifecycle (forget / export). |
| [`audit`](#sovyx-audit) | Tamper-evident audit log inspection. |
| [`kb`](#sovyx-kb) | Mixer-profile knowledge base inspector. |

---

## `sovyx init`

Create `~/.sovyx/system.yaml` + `~/.sovyx/logs/` + a default mind directory.
Idempotent — re-running prints dim "already exists" lines for everything
already in place.

```bash
sovyx init
```

After `init`, run `sovyx start` and `sovyx token` to get the dashboard
URL + auth token.

---

## `sovyx start`

Start the daemon. Brings up the bridge channels, registers the cognitive
loop, mounts the FastAPI dashboard at `127.0.0.1:7777`, and (when
configured) starts the voice pipeline.

```bash
sovyx start
```

On Linux, integration with systemd is documented at
[`voice-setup-linux-mint.md`](voice-setup-linux-mint.md).

The first start may download voice ONNX models on demand; the dashboard
banner surfaces download progress.

---

## `sovyx stop`

Stop a running daemon gracefully (drains in-flight TTS, releases audio
devices, closes WebSocket connections, drains the bridge channel queues).

---

## `sovyx status`

One-shot snapshot of daemon state: running / stopped + uptime + mind
summary. Output mirrors `GET /api/status`.

---

## `sovyx token`

Print the dashboard auth token (32 url-safe bytes generated on first
start, stored at `~/.sovyx/token` with `0o600`).

```bash
sovyx token              # print to stdout
sovyx token --clipboard  # also copy to clipboard (where available)
```

NEVER paste this token into chat logs / screenshots. It grants full
control of the local daemon.

---

## `sovyx chat`

Open an interactive REPL with the active mind. Uses the LLM provider
selected by the current `MindConfig` (see
[`llm-router.md`](llm-router.md)). Slash-commands inside the REPL:

| Command | Effect |
|---|---|
| `/help` | List commands. |
| `/clear` | Clear the local REPL history (server-side conversation intact). |
| `/exit` | Quit the REPL. |
| `/model <name>` | Override the model for the next turn. |
| `/save` | Persist the conversation to disk under `~/.sovyx/exports/`. |

---

## `sovyx logs`

Query and filter the structured log file. Supports time-range filters,
JSON-Pointer field selectors, and Bash-friendly piping.

```bash
sovyx logs --since 30m
sovyx logs --level WARN --since 1h
sovyx logs --grep "voice.failover"
sovyx logs --json | jq 'select(.event | startswith("voice.frame"))'
```

| Option | Description |
|---|---|
| `--since <duration>` | Tail since the given duration (e.g. `30m`, `2h`, `1d`). |
| `--level <LEVEL>` | One of DEBUG / INFO / WARN / ERROR. |
| `--grep <pattern>` | Substring match against the rendered event field. |
| `--json` | Emit the raw JSON-per-line stream. |
| `--follow`, `-f` | Stream new entries as they arrive. |

---

## `sovyx brain`

Brain memory inspection. Subcommands:

| Subcommand | Description |
|---|---|
| `sovyx brain analyze` | Run the brain analyzer (per-mind concept + relation report). |
| `sovyx brain search "<query>"` | Hybrid lexical + vector search. |
| `sovyx brain export` | Export the per-mind brain graph to JSON / GraphML. |

---

## `sovyx mind`

Mind management:

| Subcommand | Description |
|---|---|
| `sovyx mind list` | List configured minds (id + display name + language + provider). |
| `sovyx mind create <id>` | Bootstrap a new mind directory under the active `data_dir`. |
| `sovyx mind set-default <id>` | Set the default mind for CLI + bridge channels. |

---

## `sovyx dashboard`

Dashboard management. The default invocation (no subcommand) prints the
current dashboard URL + token-reveal flag.

```bash
sovyx dashboard                # prints URL + "Token: use --token to reveal"
sovyx dashboard --token        # also prints the token
sovyx dashboard --token -t     # short form
```

### `sovyx dashboard doctor`

(Mission C5 §T3.3) — Verify the SPA bundle integrity of the installed
dashboard. Runs the four-state classifier (`FULLY_PRESENT` / `PARTIAL` /
`INDEX_HTML_MISSING` / `STATIC_DIR_MISSING` /
`LEGACY_INDEX_HTML_NO_ASSETS`) against
`~/.local/share/pipx/venvs/sovyx/.../sovyx/dashboard/static/` (or the
equivalent install path).

```bash
sovyx dashboard doctor                 # human-readable report
sovyx dashboard doctor --json | jq .   # parseable JSON
```

Exit codes:

| Code | Meaning |
|---|---|
| `0` | `FULLY_PRESENT` — every chunk referenced by `index.html` exists on disk. |
| `1` | Any non-`FULLY_PRESENT` verdict — bundle integrity violated. |

JSON output schema:

```json
{
  "verdict": "fully_present | partial | index_html_missing | static_dir_missing | legacy_index_html_no_assets",
  "static_dir": "<absolute POSIX path>",
  "index_html_path": "<absolute POSIX path>",
  "referenced_count": 42,
  "missing_count": 0,
  "orphan_count": 0,
  "missing_assets": [],
  "orphan_assets": [],
  "scan_duration_ms": 4.213
}
```

Triage workflow when the verdict is not `FULLY_PRESENT`:

1. `PARTIAL` — some referenced chunks are absent. Run
   `pipx reinstall sovyx` (or `npm run build` inside `dashboard/` when
   developing from a checkout).
2. `INDEX_HTML_MISSING` — the SPA entry point is absent. Same fix.
3. `STATIC_DIR_MISSING` — the entire `static/` dir is missing.
   Reinstall is the only path.
4. `LEGACY_INDEX_HTML_NO_ASSETS` — `index.html` exists but `assets/`
   is empty (typically a stale or interrupted developer build). Run
   `npm run build` in `dashboard/`.

See [`docs/modules/dashboard-distribution-integrity.md`](modules/dashboard-distribution-integrity.md)
for the full mission context, the related `dashboard.distribution.*`
OpenTelemetry events, and the tuning knobs under
`EngineConfig.tuning.dashboard`.

---

## `sovyx doctor`

Aggregate health-check command. Runs subsystem-specific diagnostics +
renders the operator-visible composite degraded-banner surfaces alongside
the Phase 1.C dashboard integrity surface (Mission C5 §T3.4).

```bash
sovyx doctor                  # runs the default subcommand suite
sovyx doctor --json           # machine-readable output
```

### Subcommands

| Subcommand | Description |
|---|---|
| `sovyx doctor voice` | Voice subsystem health checks (PortAudio + Linux mixer sanity + APO bypass + capture-integrity probe). |
| `sovyx doctor cascade` | Run the startup self-diagnosis cascade. |
| `sovyx doctor linux_session_manager_grab` | Detect whether another audio client holds the capture hardware. |
| `sovyx doctor voice_capture_apo` | Scan Windows capture-APO chain for Voice Clarity (Mission F2-M07). |
| `sovyx doctor piper_locale_match` | Check whether a locale has a curated Piper voice (F2-M03↑). |
| `sovyx doctor platform` | Cross-OS platform-diagnostics report. |

### Composite surfaces rendered by `sovyx doctor` (no args)

When invoked without a subcommand, the aggregate doctor flow renders
the following sections in order (Mission C4 §T3.6 + Mission C5 §T3.4):

1. **Voice — quarantine surface** — endpoints quarantined by the
   capture-integrity coordinator (Mission C1).
2. **Voice — failover history** — recent runtime-failover ladder runs
   (Mission C3 §T2.9).
3. **Voice — degraded banner** — cross-axis `EngineDegradedStore`
   snapshot + composite severity + per-axis action chips (Mission C4
   §T3.6).
4. **Dashboard — bundle integrity** — SPA bundle verdict + missing-chunk
   sample + remediation hint (Mission C5 §T3.4).

CLI-only operators see the same picture as the dashboard's composite
banner — no log-grep required.

---

## `sovyx plugin`

Plugin management:

| Subcommand | Description |
|---|---|
| `sovyx plugin list` | List installed plugins + entry-point source. |
| `sovyx plugin info <name>` | Show plugin manifest + permissions. |
| `sovyx plugin enable <name>` | Enable a previously-disabled plugin. |
| `sovyx plugin disable <name>` | Disable without uninstalling. |

---

## `sovyx voice`

Voice-data lifecycle commands. GDPR / LGPD compliance surface — see
[`compliance.md`](compliance.md).

| Subcommand | Description |
|---|---|
| `sovyx voice forget` | Erase voice-derived data for a mind (audio fragments + transcripts + per-utterance metadata). |
| `sovyx voice export` | Export per-mind voice data as a portable archive. |

---

## `sovyx audit`

Tamper-evident audit log inspection. Subcommands:

| Subcommand | Description |
|---|---|
| `sovyx audit show` | Render the audit log with cryptographic chain verification. |
| `sovyx audit verify` | Run the integrity checker — non-zero exit on tamper. |
| `sovyx audit export` | Export the audit log as a portable archive. |

---

## `sovyx kb`

Inspect the mixer-profile knowledge base (the corpus of audio-mixer
configuration heuristics shipped under `voice/health/_mixer_kb/`):

| Subcommand | Description |
|---|---|
| `sovyx kb list` | List installed KB profiles. |
| `sovyx kb verify` | Verify the signing chain (Mission KB-signing — `trusted_keys/`). |
| `sovyx kb show <id>` | Render a profile's match conditions + remediation steps. |

---

## Environment variables

The CLI respects every environment variable consumed by `EngineConfig`
and its sub-configs. Notable for CLI-driven workflows:

| Variable | Effect |
|---|---|
| `SOVYX_DATA_DIR` | Override the `~/.sovyx/` data path. |
| `SOVYX_TUNING__VOICE__*` | Voice tunable knobs (see [`configuration.md`](configuration.md)). |
| `SOVYX_TUNING__DASHBOARD__INTEGRITY_REACTIVE_ENABLED` | Toggle the dashboard bundle integrity reactive on-404 arm (default `True`). Mission C5 §T2.5. |
| `SOVYX_TUNING__DASHBOARD__INTEGRITY_REACTIVE_DEBOUNCE_SEC` | Reactive-arm debounce in seconds (default `60.0`, bounded `[10, 600]`). |
| `SOVYX_TUNING__DASHBOARD__INTEGRITY_ACTION_CHIP_REINSTALL_URL` | Override the operator-action chip reinstall target (default `https://sovyx.dev/docs/install/troubleshooting#reinstall`). |
| `SOVYX_TUNING__DASHBOARD__INTEGRITY_ACTION_CHIP_DOCTOR_URL` | Override the doctor docs URL (default `https://sovyx.dev/docs/cli/doctor#dashboard`). |
| `SOVYX_GATES_MAX_AGE_SEC` | Pre-push hook marker max age (default `1800` = 30 min). See [`contributing.md`](contributing.md) §Quality Gates. |

For the exhaustive variable catalog, see
[`docs/configuration.md`](configuration.md).

---

## Exit code contract

All `sovyx` commands follow a single contract:

| Code | Meaning |
|---|---|
| `0` | Success — the command's invariants hold. |
| `1` | Subsystem reported a failure (e.g. `sovyx dashboard doctor` on a partial install). |
| `2` | Argument or configuration error (typer's default). |
| `6` | (Voice subsystem only) `EXIT_DOCTOR_VOICE_NOT_CONFIGURED` — operator must run `sovyx voice setup`. |

Aggregate doctor flows return the number of failing subsystem checks
when invoked without `--fix` (preserving the v0.21.2 contract — see
[`docs-internal/missions/MISSION-voice-final-skype-grade-2026.md`](../docs-internal/missions/) §Phase 1 for the historical rationale; the file lives in the
internal mission archive in the operator's checkout).

---

## See also

* [`configuration.md`](configuration.md) — full `EngineConfig` reference.
* [`api-reference.md`](api-reference.md) — HTTP + WebSocket API.
* [`modules/dashboard-distribution-integrity.md`](modules/dashboard-distribution-integrity.md) — Mission C5 operator playbook.
* [`observability.md`](observability.md) — structured logging + OpenTelemetry semconv.
