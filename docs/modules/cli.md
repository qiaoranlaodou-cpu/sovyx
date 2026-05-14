# Module: cli

## What it does

`sovyx.cli` is the Typer-based command-line interface. It manages the daemon lifecycle (`start` / `stop` / `status`), exposes brain queries, controls plugins, and provides an interactive REPL (`sovyx chat`) over the existing JSON-RPC Unix socket.

## Key classes

| Name | Responsibility |
|---|---|
| `app` | Typer root with nested sub-apps (brain, mind, plugin, dashboard, logs). |
| `DaemonClient` | JSON-RPC 2.0 client over Unix socket (`~/.sovyx/sovyx.sock`). |
| `chat` | Interactive REPL with prompt_toolkit (history, slash commands). |

## Commands

Top-level commands and sub-app groups are listed below. For full
syntax of any command, run `sovyx <command> --help`. The CLI uses
Typer; auto-completion is available via
`sovyx --install-completion`.

### Root commands

| Command | What it does |
|---|---|
| `sovyx init <name>` | Create `~/.sovyx/<name>/` with `mind.yaml`. Since v0.39.0 the command invokes `sovyx voice setup` inline after mind creation so the operator can configure the input device interactively; pass `--skip-voice-setup` to preserve the pre-v0.39.0 non-interactive flow (useful for CI / scripted installs). |
| `sovyx start [--foreground]` | Launch the daemon + dashboard (`:7777`). Resolves the active mind via the shared resolver (`--mind-id` flag / config / sentinel-fallback). |
| `sovyx stop` | Stop the daemon. |
| `sovyx status` | Daemon health summary. |
| `sovyx token [--copy]` | Print or copy the dashboard bearer token. |
| `sovyx chat` | Interactive REPL with prompt_toolkit (history + slash commands). |

### `sovyx doctor` — diagnostic checks (sub-app)

| Command | What it does |
|---|---|
| `sovyx doctor` | Run the 10+ default diagnostic check matrix. |
| `sovyx doctor voice [--calibrate] [--non-interactive] [--input-device <name>] [--mind-id <id>] [--full-diag]` | Voice subsystem checks. `--calibrate` runs the full slow-path calibration; the prereq gate is **STRICT since v0.40.0** — exits with code `EXIT_DOCTOR_VOICE_NOT_CONFIGURED=6` when no input device is configured on the resolved mind. `--input-device "<name>"` is the escape hatch: inline-configures the named device (substring match against the PortAudio list; non-interactive sessions require exactly one match), persists it to `mind.yaml`, then continues. |
| `sovyx doctor cascade` | Probe the Linux device cascade planner against the operator's audio stack. |
| `sovyx doctor linux_session_manager_grab` | Verify PipeWire / PulseAudio session-manager grab semantics. |
| `sovyx doctor voice_capture_apo` | Detect Windows capture-side APOs (Voice Clarity etc.) per anti-pattern #21. |
| `sovyx doctor piper_locale_match` | Flag drift between the operator's spoken language and the auto-selected Piper voice (F2-M03). |
| `sovyx doctor platform` | Cross-platform parity summary (Linux / Windows / macOS detection + delta to baseline). |

### `sovyx voice` — voice data lifecycle (sub-app)

| Command | What it does |
|---|---|
| `sovyx voice setup [--mind-id <id>] [--input-device <substring>] [--non-interactive]` | Configure the active mind's input device. Renders an interactive picker over the PortAudio device list (or applies `--input-device` substring match). Persists the choice to `mind.yaml` under `voice_input_device_name`. Shipped v0.39.0 as part of MISSION-voice-config-calibrate-enterprise Phase 2. |
| `sovyx voice forget [--mind-id <id>] [--scope conversations\|episodes\|all]` | Erase voice-derived data per the GDPR / LGPD lifecycle. |
| `sovyx voice history [--mind-id <id>]` | List voice-data records currently retained. |
| `sovyx voice train-wake-word [--mind-id <id>] [--unattached] [--word <word>]` | Train a sub-second ONNX wake-word model for the resolved mind. `--unattached` skips mind resolution (used for test hermeticity per anti-pattern #23). |
| `sovyx voice generate-signing-key [--mind-id <id>] [--out <path>]` | Generate an Ed25519 signing key for the calibration / KB profile signing flow (per anti-pattern #26). |

### `sovyx brain` — brain memory queries (sub-app)

| Command | What it does |
|---|---|
| `sovyx brain search <query>` | Hybrid (KNN + FTS5 + RRF) search across the brain graph. |
| `sovyx brain stats` | Concept / episode / relation counts. |
| `sovyx brain analyze scores` | Importance + confidence score distribution. |

### `sovyx mind` — mind management (sub-app)

| Command | What it does |
|---|---|
| `sovyx mind list` | List configured minds. |
| `sovyx mind status` | Active mind details. |
| `sovyx mind forget <id>` | Delete a mind (concepts + episodes + relations + voice data). |
| `sovyx mind retention prune [--mind-id <id>] [--dry-run]` | Apply the retention policy now (delete records older than the configured TTL). |
| `sovyx mind retention status [--mind-id <id>]` | Show retention-policy state + next-prune ETA. |

### `sovyx plugin` — plugin management (sub-app)

| Command | What it does |
|---|---|
| `sovyx plugin list` | Installed plugins with state. |
| `sovyx plugin info <name>` | Manifest, permissions, tools, risk levels. |
| `sovyx plugin install <path> [--allow-unsafe]` | AST-scan + copy to `data_dir/plugins`. |
| `sovyx plugin enable <name>` / `disable <name>` | Toggle. |
| `sovyx plugin remove <name>` | Uninstall. |
| `sovyx plugin validate <path>` | Run quality gates (manifest, AST, permissions) without installing. |
| `sovyx plugin create <name>` | Scaffold a new plugin skeleton. |

### `sovyx kb` — KB profile inspection (sub-app)

Used for the voice mixer KB profile signing flow (anti-pattern #26).

| Command | What it does |
|---|---|
| `sovyx kb list` | List trusted-key profiles on disk. |
| `sovyx kb inspect <profile>` | Show profile content + signature + verification verdict. |
| `sovyx kb validate <profile>` | Strict-mode signature validation; non-zero exit on failure. |
| `sovyx kb fixtures` | Generate dev / test fixtures for the trusted-key store. |

### `sovyx audit` — tamper-evident audit log (sub-app)

| Command | What it does |
|---|---|
| `sovyx audit verify-chain [--mind-id <id>]` | Walk the audit chain and verify hashes from genesis to head. Non-zero exit if any entry tampered. |

### `sovyx logs` + `sovyx dashboard`

| Command | What it does |
|---|---|
| `sovyx logs [--level] [--follow]` | Tail / filter daemon logs. |
| `sovyx dashboard [--open]` | Print or open the dashboard URL. |

## Interactive REPL

`sovyx chat` opens a prompt_toolkit session that talks to the daemon over JSON-RPC (not HTTP). Works even when the dashboard is disabled.

Features:
- Persistent history at `~/.sovyx/history` (chmod 0600).
- Word-completer over the slash-command vocabulary.
- History search (Ctrl+R).
- Seven slash commands: `/help`, `/exit`, `/quit`, `/new`, `/clear`, `/status`, `/minds`, `/config`.

## RPC protocol

The daemon listens on a Unix socket (`~/.sovyx/sovyx.sock`). `DaemonClient` sends JSON-RPC 2.0 requests and reads responses. Stale socket detection via probe (connect + immediate close).

5 methods currently wired: `status`, `shutdown`, `chat`, `mind.list`, `config.get`. Brain and plugin subcommands fall back to dashboard HTTP endpoints when the RPC method is not registered.

## Configuration

No dedicated CLI config — reads `EngineConfig` from `system.yaml` and env vars. Socket path from `EngineConfig.socket.socket_path`.

## Roadmap

- Admin utilities (DB inspect, config reset, user/mind management).
- Migrate remaining brain/plugin commands from HTTP to RPC.

## See also

- Source: `src/sovyx/cli/main.py`, `src/sovyx/cli/commands/`, `src/sovyx/cli/chat.py`, `src/sovyx/cli/rpc_client.py`.
- Tests: `tests/unit/cli/`.
- Related: [`engine`](./engine.md) (RPC server side), [`dashboard`](./dashboard.md) (HTTP fallback for some commands).
