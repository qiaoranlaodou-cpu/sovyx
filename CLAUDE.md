# CLAUDE.md — Sovyx Development Guide

## North Star

These principles override defaults when in conflict. They are enforced via memory entries (`feedback_*`) that carry the same authority as this file.

1. **Enterprise-grade, no band-aids AND no over-engineering.** Fix root causes; stop where marginal value < marginal risk. (`feedback_enterprise_only`)
2. **Zero speculation.** Only state what is verified at HEAD. Mark unverified claims explicitly. (`feedback_no_speculation`)
3. **Staged adoption.** Foundation → wire-up → default-flip across separate commits. New validators ship LENIENT; flip STRICT after one minor cycle of telemetry. (`feedback_staged_adoption`)
4. **Full autonomous decision authority on technical scope.** Operator delegates architecture, migration, testing strategy. `AskUserQuestion` is reserved for product scope, priority, or UX phrasing — never technical. (`feedback_full_autonomous_authority`)
5. **Validation batched at tag milestones.** Ship code autonomously between checkpoints; operator validates against `OPERATOR-VALIDATION-BACKLOG-2026.md`. (`feedback_validation_batching`)
6. **Don't watch CI after tag push.** Skip `gh run watch` on `publish.yml`. (`feedback_ci_watching`)
7. **No paliative shell scripts in chat.** Diagnostic scripts ship as committed `.sh` files with download URL — never inline heredocs. (`feedback_no_inline_scripts_in_chat`)

## Rule Precedence

When two rules conflict, apply in this order:

1. **`feedback_*` memories** — operator's explicit guidance, same authority as this file.
2. **Anti-patterns below** — incidents already paid for in production.
3. **Conventions** — style + idiom.
4. **Stack defaults** — what the framework gives you.

Lower-priority rules cannot override higher-priority ones. If you're tempted to violate a higher rule for a lower-rule benefit, stop and surface the conflict.

## What is Sovyx
Sovereign Minds Engine — persistent AI companion with real memory, cognitive loop, and brain graph. Python library + CLI daemon + React dashboard.

## Stack
- **Backend:** Python 3.11 / 3.12 (CI matrix), structlog, pydantic v2, pydantic-settings, FastAPI, aiosqlite, ONNX Runtime, httpx, argon2-cffi, PyJWT
- **Frontend:** React 19, TypeScript, Vite, Tailwind CSS, Zustand, TanStack Virtual, zod (runtime response validation), i18next
- **Build:** uv (Python, `uv.lock` committed), npm (dashboard), Hatch (packaging) with `hatchling` backend
- **CI:** GitHub Actions on self-hosted `sovyx-4core` → ruff + mypy + bandit + pytest (3.11 & 3.12) + vitest + tsc + Docker + PyPI
- **CLI:** `sovyx` entry point (`sovyx.cli.main:app`), plugin entry points under `sovyx.plugins` group

## Quality Gates (MANDATORY before any commit)

**Mechanical forcing function — `git push` is REJECTED without proof:**

```bash
./scripts/install_hooks.sh       # one-time setup per clone — installs pre-push hook
./scripts/verify_gates.sh        # runs all 7 gates + writes .git/.last-gates-pass marker
git push                         # hook validates marker fresh + HEAD-matched, else REJECTS
```

The pre-push hook at `.githooks/pre-push` (activated by `install_hooks.sh` via `git config core.hooksPath .githooks`) makes the discipline mechanically impossible to bypass — every `git push` checks `.git/.last-gates-pass` for a HEAD-matching marker within the last 30 min (override via `SOVYX_GATES_MAX_AGE_SEC` env var).

Escape hatch: `git push --no-verify` (git's standard contract). NEVER use without explicit operator approval + commit-body rationale documenting why the gate was skipped.

The script (`set -euo pipefail` + explicit `grep` on summary lines) replaces the ad-hoc invocation pattern below. Pre-v0.42.2 the ad-hoc pattern `pytest ... 2>&1 | tail -N` masked 6 real test failures across 4 cycles because the harness's exit-code reporting was unreliable when piped to tail without `pipefail`. See `feedback_ci_preflight.md` Addendum 2026-05-14 + `feedback_no_speculation.md` Addendum 2026-05-14 for the forensic detail.

The 7 gates the script runs (in order):

```bash
# Python (from repo root)
uv run ruff check src/ tests/                                          # 1. lint
uv run ruff format --check src/ tests/                                 # 2. format
uv run mypy src/                                                       # 3. type (strict)
uv run bandit -r src/sovyx/ --configfile pyproject.toml                # 4. security
uv run python -m pytest tests/ --ignore=tests/smoke --timeout=30 -q    # 5. tests
# Dashboard (from dashboard/)
npx tsc -b tsconfig.app.json                                           # 6. dashboard type
npx vitest run --reporter=dot                                          # 7. dashboard tests
```

Plus `uv lock --check` (verified separately when bumping). If running gates ad-hoc instead of via the script, you MUST grep the summary line — never trust the harness exit code alone:

```bash
# WRONG (4-cycle red precedent):
uv run python -m pytest tests/ ... 2>&1 | tail -15        # tail eats pytest's exit 1

# RIGHT:
uv run python -m pytest tests/ ... 2>&1 | tee /tmp/log    # full output captured
grep -qE '[0-9]+ failed' /tmp/log && echo "RED" && exit 1 # GREP exit is the gate
```

If ANY gate fails, fix before committing. Never skip.

**Version bump gotcha:** any change to `pyproject.toml` `version` requires `uv lock` to regenerate `uv.lock` — CI enforces `uv lock --check`.

**Post-tag CI verification:** after `git push origin <tag>`, run `gh run list --workflow=publish.yml --limit 3` to confirm the previous tag passed BEFORE bumping the next one. Skipping this step shipped 6 tags atop a broken pipeline in the v0.41.x cycle.

## Repo Layout

```
src/sovyx/
├── engine/              # Config, bootstrap, lifecycle, events, registry, RPC
│   └── _lock_dict.py    # LRULockDict — bounded asyncio.Lock dict (use this, never raw defaultdict)
├── cognitive/           # Perceive → Attend → Think → Act → Reflect loop
│   ├── safety/          # Pattern catalogs per language + classifier
│   └── reflect/         # Concept extraction + episode encoding
├── brain/               # Concepts, episodes, relations, embedding, scoring, retrieval
├── bridge/              # Inbound/outbound messaging
│   └── channels/        # telegram.py, signal.py
├── persistence/         # SQLite pool manager (WAL, round-robin readers), migrations
├── observability/       # Logging (structlog), health checks, alerts, SLOs, tracing
├── llm/                 # Multi-provider router (Anthropic, OpenAI, Google, Ollama)
├── mind/                # Mind config, personality
├── context/             # Context assembly for LLM calls
├── cli/                 # Typer CLI: sovyx start/stop/init/logs/doctor
├── dashboard/           # FastAPI server
│   ├── server.py        # Wires routers only
│   └── routes/          # APIRouter modules per domain (activity, brain, voice, …)
├── tiers.py             # ServiceTier enum, feature/mind-limit maps (informational)
├── license.py           # LicenseValidator (Ed25519 public-key JWT, offline)
├── voice/               # STT, TTS, VAD, wake word, Wyoming
│   │                    # Per-mind voice identity is configurable per MindConfig
│   │                    # (see Phase 8 of MISSION-voice-final-skype-grade-2026.md).
│   ├── _capture_task.py # Orchestration root: AudioCaptureTask composes mixins from capture/
│   ├── capture/         # Mixins: ring buffer + lifecycle + loop + restart strategies
│   └── pipeline/        # State machine + output queue + barge-in
├── plugins/             # Plugin loader, sandbox, SDK
│   ├── sandbox_http.py  # SandboxedHttpClient (all official plugins MUST use this)
│   ├── sandbox_fs.py    # Filesystem sandbox
│   └── official/        # First-party plugins (financial_math, weather, web_intelligence, knowledge)
├── upgrade/             # Doctor, importer, blue-green, backup manager
└── benchmarks/          # Budget baselines

dashboard/               # React SPA — part of the main repo (NOT a submodule)
├── src/pages/           # Route pages
├── src/stores/          # Zustand store (dashboard.ts + slices/)
├── src/components/      # dashboard/, ui/, auth/, chat/, settings/, layout/, common
├── src/hooks/           # use-auth, use-websocket, use-mobile, use-onboarding, use-resolved-mind-id
├── src/types/           # api.ts (compile-time) + schemas.ts (zod runtime)
└── src/lib/             # api.ts (apiFetch + api.{get,post,…}), safe-json.ts, format.ts, i18n.ts

tests/
├── unit/                # Fast, isolated; mirrors src/sovyx/
├── integration/         # Cross-component
├── dashboard/           # Backend API + adversarial (use create_app)
├── plugins/             # Plugin + sandbox tests
├── property/            # Hypothesis property-based tests
├── security/            # Security-specific tests
├── stress/              # Load/performance tests
└── smoke/               # Excluded from CI via --ignore=tests/smoke

docs/                    # Public docs — MkDocs source
docs-internal/           # Internal planning, audits, missions, ADRs — gitignored
```

## Conventions

### Python
- **Logging:** Always `from sovyx.observability.logging import get_logger` then `logger = get_logger(__name__)`. Never `print()` or `logging.getLogger()` directly.
- **Config:** All config via `EngineConfig` (pydantic-settings). Env vars: `SOVYX_*` prefix, `__` for nesting (e.g., `SOVYX_LOG__LEVEL=DEBUG`). Tuning knobs live under `EngineConfig.tuning.{safety,brain,voice}` — overridable via `SOVYX_TUNING__VOICE__AUTO_SELECT_MIN_GPU_VRAM_MB=...`.
- **Errors:** Custom exceptions in `engine/errors.py`. Always include `context` dict.
- **Type hints:** All functions fully typed. `from __future__ import annotations` in every file.
- **Imports:** `TYPE_CHECKING` block for type-only imports. Ruff enforces `TCH` rules.
- **Async:** All database/IO operations are async. Sync CPU-bound work (ONNX, boto3) MUST be wrapped in `asyncio.to_thread()`. Tests use `pytest-asyncio` with `mode=auto`.
- **Docstrings:** Every public class/function. First line = imperative summary. Default to no comments otherwise (well-named identifiers do that work); only add a comment when WHY is non-obvious.

### Dashboard (TypeScript)
- **Types:** Compile-time in `src/types/api.ts`; runtime zod schemas in `src/types/schemas.ts`. Pass `{ schema }` to `api.get/post/put/patch/delete` to validate the response (safeParse — logs mismatch, returns payload).
- **State:** Zustand store in `src/stores/dashboard.ts` with slices pattern.
- **API calls:** ALWAYS via `src/lib/api.ts` — `api.*` for JSON, `apiFetch(path, init, overrideToken?)` for raw `Response` (binary/FormData). Defaults: 30 s timeout, retry w/ exp backoff on 429/503/5xx for idempotent verbs.
- **Auth token:** `sessionStorage` + in-memory fallback. NEVER `localStorage`.
- **Hot-path memoization:** `React.memo` on rows in virtualized lists (log-row, chat-bubble, plugin-card, timeline-row, tool-item). `useMemo`/`useCallback` for derived values + stable props.
- **i18n:** All user-visible strings via `useTranslation()`.
- **Mind id:** Use `useResolvedMindId` hook — never hardcode `"default"` (anti-pattern #35). ESLint rule guards this.
- **Tests:** Colocated `*.test.tsx` next to each page/component.

### Git
- **Commits:** Conventional commits (`feat:`, `fix:`, `refactor:`, `test:`, `chore:`, `perf:`, `docs:`).
- **Tags:** `vX.Y.Z` triggers `publish.yml` — runs full CI gate, then PyPI (OIDC trusted publishing) + Docker + GitHub Release. Tag version must match `pyproject.toml` version or publish fails.
- **Dashboard:** part of the main repo; stage dashboard changes alongside backend changes in the same commit when they're related.
- **Branch:** Always `main`. No feature branches (fast iteration, CI validates).

## Anti-Patterns (bugs that already happened)

Each entry is **rule + why + pointer**. Forensic detail lives in the referenced commit/mission/file. Cross-references in memories and commits use the entry number — preserve numbering when adding new entries (append, never renumber).

**Index by category:**
- **Logging & Config:** 1, 3, 4, 5, 6, 7, 17, 23, 35
- **Imports & Test Patches:** 2, 11, 20, 36, 38
- **Concurrency & Async:** 14, 15, 30
- **Cross-Platform:** 21, 22, 24
- **Voice Subsystem:** 25, 26, 27, 28, 29, 39
- **Tests:** 8, 9, 10, 12, 31
- **Architecture & Design:** 13, 16, 18, 19, 32, 33, 34, 37, 39

---

1. **Circular imports in `observability/__init__.py`:** Uses `__getattr__` lazy loading. Never add eager imports there.

2. **`sys.modules` stubs miss already-imported modules:** `import X as Y` captures the real module at import time — `sys.modules` patches don't reach the alias. Patch the aliased attribute directly: `patch.object(real_module, "attr", mock)`. Use `sys.modules` only for genuinely first-time imports inside the function under test.

3. **`LoggingConfig.console_format` (not `format`):** Renamed v0.5.24; legacy YAML `format:` auto-migrates. File handler ALWAYS writes JSON.

4. **`log_file` resolved by `EngineConfig` model_validator:** `LoggingConfig.log_file` defaults to `None`; `EngineConfig` resolves to `data_dir/logs/sovyx.log`. Never hardcode log paths.

5. **Dashboard `EngineConfig` from registry:** Dashboard resolves config via `ServiceRegistry`, never `EngineConfig()` instantiation.

6. **httpx logs to WARNING in `setup_logging()`:** Raw HTTP lines in console = `setup_logging()` wasn't called.

7. **`LogEntry` has 4 required fields:** `timestamp`, `level`, `logger`, `event`. Backend normalizes `ts→timestamp`, `severity→level`, `message→event`, `module→logger`.

8. **xdist class identity:** pytest-xdist can reimport modules → duplicate classes. Never `pytest.raises(InternalClass)`; use `pytest.raises(Exception)` + `assert type(exc).__name__ == "X"`. In production code, dispatch on `type(exc).__name__`, never `isinstance()`.

9. **Enums are StrEnum:** Every enum with string values inherits from `StrEnum`, never plain `Enum`. Guarantees value-based comparison + immune to xdist namespace duplication.

10. **Auth in tests via `create_app(token="...")`:** Never monkeypatch `_ensure_token` or `_server_token`. The `token` parameter bypasses filesystem + global state.

11. **Prefer `patch.object` over string-path patches:** `patch("module.attr")` can resolve to different module objects under xdist or after refactors. `patch.object(imported_module, "attr")` is stable.

12. **Defense-in-depth in tests is a smell:** If 3 layers make a test pass, you don't know which one works. One layer, understood > three layers, mysterious. If a fix makes a workaround unnecessary, delete the workaround in the same commit.

13. **Plugin imports via `SandboxedHttpClient`, never raw httpx:** Every official plugin instantiates `SandboxedHttpClient` and calls `.get()` / `.post()` on it. Raw `httpx.AsyncClient(...)` from plugin code bypasses allowed-domains + rate-limit + size-cap and turns the sandbox into theater.

14. **Sync CPU-bound in `async def` blocks the event loop:** ONNX inference (Piper, Kokoro, Silero, Moonshine, OpenWakeWord), `boto3` calls, any blocking CPU/IO MUST be wrapped in `asyncio.to_thread(fn, *args)`. A naked `self._sess.run(...)` in an async handler stalls every other coroutine for the inference duration.

15. **Unbounded `defaultdict(asyncio.Lock)` leaks memory:** One-lock-per-key patterns use `sovyx.engine._lock_dict.LRULockDict(maxsize=N)` so unused keys evict. Raw `defaultdict(asyncio.Lock)` grows forever in a long-lived daemon.

16. **God files (>500 LOC with mixed responsibilities):** Split into a subpackage. `__init__.py` re-exports public surface; underscore-prefixed sub-files signal "internal, accessed via parent". Migrate test patches in the same commit (#20). References to study: `cognitive/safety/`, `cognitive/reflect/`, `voice/pipeline/`, `voice/capture/` (5-mixin host on a single `AudioCaptureTask` class, the most aggressive worked example), `dashboard/routes/`.

17. **Hardcoded tuning constants:** Thresholds, timeouts, URLs, SHAs go in `EngineConfig.tuning.{safety,brain,voice}`. Module-level `_CONST = _TuningCls().field` keeps import-time access while allowing `SOVYX_TUNING__*` env override. Never hardcode in a `.py` literal.

18. **Raw `fetch()` in the frontend:** Every network call goes through `src/lib/api.ts` — `api.*` for JSON (auth + retry + timeout + schema validation), `apiFetch` for raw `Response` (binary/FormData). A loose `fetch("/api/…")` drifts from auth header injection and 401 handler.

19. **`localStorage` for auth tokens is XSS-exposed:** Use `sessionStorage` (tab-scoped) + in-memory fallback (already in `src/lib/api.ts`). A boot-time migrator reads any legacy `localStorage` entries.

20. **Test patches must follow module splits:** Extracting a helper turns every `patch("old.module.X")` into a silent no-op — patch resolves to a non-existent attribute, the real implementation runs, the mock looks fine. Migrate patch paths in the same commit as the split. Extends to: lazy `from X import Y` (#38); `caplog.set_level(logger=...)` widening when loggers move; `patch.object(mod, "sys", ...)` style patches across submodule boundaries.

21. **Windows capture APOs corrupt mic before PortAudio sees it:** Voice Clarity (`VocaEffectPack` / `voiceclarityep`, ships via Windows Update) is a per-endpoint capture APO that destroys Silero VAD input — max speech probability < 0.01 despite healthy RMS. Fix: WASAPI exclusive mode via `capture_wasapi_exclusive` (bypasses APO chain). Auto-detected at startup (`sovyx.voice._apo_detector`); auto-bypasses on repeated deaf heartbeats (`voice_clarity_autofix=True`, default). Never tune VAD threshold or add AGC — band-aids; signal is destroyed *upstream* of user-space. Surfaces: `sovyx doctor voice_capture_apo`, `GET /api/voice/capture-diagnostics`.

22. **Windows `time.monotonic()` ticks at ~15.6 ms without `timeBeginPeriod`:** `time.sleep(0.01)` can yield zero-tick delta. Timer-sensitive tests: sleeps ≥ 50 ms or fake clock; for perf measurement use `time.perf_counter`. Linux sub-µs masks this on CI; surfaces only on Windows dev hosts.

23. **`EngineConfig.data_dir` defaults to `~/.sovyx`; bootstrap re-seeds env from it:** `bootstrap()` reads `<data_dir>/{channel,secrets}.env` into the process env. Tests passing only `database=DatabaseConfig(data_dir=tmp_path)` leave `data_dir` at home default → dev host's production secrets re-seeded mid-test. Always pass BOTH `data_dir=tmp_path` AND `database=DatabaseConfig(data_dir=tmp_path)`. Use `monkeypatch.delenv` (auto-restored), not `os.environ.pop` (leaks). Bootstrap auto-detect checks 9 cloud-LLM keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`, `XGROK_API_KEY`, `DEEPSEEK_API_KEY`, `MISTRAL_API_KEY`, `GROQ_API_KEY`, `TOGETHER_API_KEY`, `FIREWORKS_API_KEY`) — one leftover masks the path under test.

24. **Strict `>` on `time.monotonic()` deadlines is silently wrong on coarse clocks:** When `now` and deadline share a tick, `>` never fires. Symptom: `ttl_sec=0` never expires. Prefer `>=` for deadline/TTL — inclusive matches user intuition + coarse-clock-safe.

25. **Frame-typed pipeline as observability layer, NOT state-machine rewrite:** Hybrid Option C — typed frames (`PipelineFrame` + 8 subclasses in `voice/pipeline/_frame_types.py`) instrument transitions/cancellations with structured metadata, but authoritative state stays in `VoicePipelineState` + boolean flags. Frames go into a bounded ring buffer (256 entries) via `PipelineStateMachine.record_frame`; exposed via `GET /api/voice/frame-history`. Never couple production logic to frame presence — buffer evicts. Full Pipecat state-machine rewrite deferred to v0.24.0+; doing it as a single mission would force 200+ test rewrites.

26. **KB profile signing: dev key in repo, production rotation via HSM:** `voice/health/_mixer_kb/_trusted_keys/v1.pub` is the dev signing key. Private key (`.signing-keys/sovyx_kb_v1.priv`) gitignored + STAYS LOCAL. Loader stays `Mode.LENIENT` for v0.23.x; flips `Mode.STRICT` after one minor cycle of telemetry-validated lenient mode (per `feedback_staged_adoption`). Production rotation: HSM-backed key (YubiKey / AWS KMS / GCP Cloud KMS), multi-key trust store with overlapping windows. Compromise response: 24h advisory + emergency v2 roll + `Mode.STRICT` flip + community PR queue purge. Procedure: `docs/contributing/voice-kb-rotation.md`.

27. **`contextlib.suppress` + `logger.debug(..._skipped, reason=…)` is the canonical "intentional ignore":** Replaces raw `try/except: pass` for genuinely benign failures (best-effort cleanup, optional import probe, malformed-field skip). Explicit intent + observability without prod cost (debug-level filter strips it). Rejected alternatives: silent suppression with no log, WARN-level floods, raising errors callers can't handle.

28. **Cold probe MUST validate signal energy, not just callback count (Furo W-1):** APOs leave PortAudio callbacks firing while delivering exact-zero PCM. Pre-v0.24.0 `_diagnose_cold` returned `HEALTHY` whenever `callbacks_fired > 0`, and `ComboStore` then persisted the silent winner deterministically every boot. v0.24.0 fix: read `rms_db`; in strict mode return `Diagnosis.NO_SIGNAL` when `rms_db < probe_rms_db_no_signal`. Lenient mode emits `voice.probe.cold_silence_rejected{mode=lenient_passthrough}` for telemetry-only calibration. **Generalizes:** any acceptance gate downstream of a real-world signal source MUST verify the signal itself, not just the wrapping mechanics. Don't accept "callback fired" as proxy for "signal is alive".

29. **`CaptureRestartFrame` is observability, NOT state-machine rewrite:** Same hybrid-Option-C contract as #25. Every restart method (`request_exclusive_restart`, `request_alsa_hw_direct_restart`, …) emits a `CaptureRestartFrame` BEFORE the ring-buffer epoch increments; orchestrator records it via `PipelineStateMachine.record_frame`. Dashboard renders `GET /api/voice/restart-history`. Schema fields stay `.optional()` for one minor cycle before promotion. Don't couple production logic to frame presence — ring buffer is bounded.

30. **`psutil.open_files()` / `net_connections()` hang during async teardown on Windows:** psutil iterates kernel handle table + calls `os.stat()` per handle. Closing handles cause `os.stat()` to block indefinitely — `try/except` catches raised exceptions, NOT blocked syscalls. Windows CI symptom: 6+ minute timeout in `_capture_psutil_metrics → proc.open_files() → psutil/_pswindows.py::isfile_strict`. Linux unaffected (sub-µs `os.stat`). Fix: `skip_expensive: bool` keyword-only flag on the metrics-emit path; cheap fields (`rss/vms/cpu/threads/handles_or_fds`) still flow on shutdown. Site fixed: `observability/resources.py::_capture_psutil_metrics` + `_emit_snapshot(final=True)` (commit `003a63f`). **Generalizes:** any metrics-emit path on a shutdown / cancellation hook MUST avoid handle-iterating syscalls or wrap in `asyncio.wait_for` with a strict deadline.

31. **Perf gate p99 ratio is tail-sensitive even with median-of-3:** `scripts/check_perf_regression.py` runs `bench_observability.py` 3× and takes median p99. Sustained GitHub Linux contention can blow all 3 runs → median = noise → gate fails on commits unrelated to logging. **Triage:** if `git diff` doesn't touch `observability/logging.py`, `_async_handler.py`, or the structlog processor chain, very high prior the failure is contention. If it does, suspect lost `put_nowait` fast path on `AsyncQueueHandler.enqueue` or `BackgroundLogWriter` doing work on the producer thread. Hardening: bump `_DEFAULT_REPEATS` 3 → 5 or trimmed-mean (drop 1 highest + 1 lowest).

32. **Mixin method-via-MRO stubs silently shadow real methods that live AFTER the calling mixin:** A `def foo(self) -> None: ...` stub on `MixinA` is a real Python method (the `...` body returns `None`) and WINS MRO over the real `foo` on a `MixinB` that comes later in the host's bases. The shadowed call returns `None` silently — invisible to mypy/ruff/bandit, surfaces as runtime "method did nothing". Two safe patterns: (a) target lives BEFORE caller in MRO → naked `def stub(...): ...` is fine (real method found first); (b) target lives AFTER caller in MRO → declare cross-mixin reference inside `if TYPE_CHECKING:` (type-check-only, erased at runtime → MRO falls through to real method). Documented inline in `voice/capture/_loop_mixin.py`.

33. **Per-mind config from RPC handlers: best-effort YAML, never assume registry methods exist:** `MagicMock`-typed `registry.resolve(...).method(...)` returns `Any` and masks `AttributeError` at test time → production blows up at first invocation. Before `await registry.resolve(X).method(y)`, grep `class X:` for `def method`. Privacy-sensitive paths (retention) MUST fall through to global defaults on malformed config — operator's compliance posture > perfect resolution. Reference: `_load_mind_config_best_effort` in `engine/_rpc_handlers.py`.

34. **Schedulers with kill-switch flags default OFF + skip instantiation when disabled:** Default-OFF means default-ABSENT, not default-PRESENT-but-no-op. Bootstrap: `if config.X.enabled: register_instance(...)`. Lifecycle: `if registry.is_registered(X): start ...`. Always-instantiate-+-start-time-check leaks no-op tasks into the asyncio loop + no-op entries in the registry, observable in logs/metrics, confusing for triage. Same pattern: ConsolidationScheduler / DreamScheduler / RetentionScheduler.

35. **Cross-layer config defaults are sentinels, not values:** A field like `VoicePipelineConfig.mind_id: str = "default"` is a sentinel upstream callers MUST overwrite; every caller path that omits the field is a silent bug. Prior incident: voice pipeline launched under phantom `"default"` mind because `dashboard/routes/voice.py` read `getattr(request.app.state, "mind_id", "default")` while no production code ever assigned `app.state.mind_id`. Two safe patterns: (a) **make field required** (no default — type-check enforces); preferred for NEW fields. (b) **detect sentinel at top wire-up + emit structured WARN**; safe migration when the sentinel already shipped. Pattern (b) lives in `voice/factory/__init__.py` (`voice.factory.mind_id_default_sentinel`) + `dashboard/_shared.resolve_active_mind_id_for_request`. **Recurring offender — has surfaced 5+ times in voice flow.** Frontend has dedicated `useResolvedMindId` hook + ESLint rule.

36. **`patch.object` on async functions auto-detects `AsyncMock`; string-path `patch` follows the same autodetect when the import resolves at patch time:** Python 3.8+ inspects targets with `inspect.iscoroutinefunction` and substitutes `AsyncMock` (whose `return_value` is awaitable) instead of `MagicMock` (whose isn't, and crashes the awaiter with `TypeError: object Foo can't be used in 'await' expression`). Prefer `patch.object(module, "name", return_value=X)` over `patch("path", new_callable=AsyncMock, return_value=X)` for async patches — autodetect is documented and load-bearing for clean async test code.

37. **Cryptographic verifier verdict ordering: cheapest + most-common-failure FIRST, dependency invariants asserted BEFORE invoking dependent ops:** In a 5-way verdict (`ACCEPTED / REJECTED_NO_SIGNATURE / REJECTED_BAD_SIGNATURE / REJECTED_MALFORMED_SIGNATURE / REJECTED_NO_TRUSTED_KEY`), order: (1) `pubkey is None` (else later `pubkey.verify(...)` crashes with `AttributeError`); (2) `signature is None` (cheap, avoids canonicalisation); (3) signature shape malformed (b64 invalid OR length != 64; cheap, avoids less-informative `InvalidSignature`); (4) actual `pubkey.verify` (expensive). Site: `_persistence.py::_verify_calibration_signature`.

38. **Lazy `from X import Y` inside a function body invalidates module-level patches:** The lazy import resolves on the SOURCE module at call-time, not on the caller's top-level binding. Patch `X.Y` (source module attr), NOT `caller.Y`. Mixed cases: a single test may patch BOTH `caller.eager_attr` (top-level import) AND `source.lazy_attr` (function-body import). Extends #20 to lazy-import boundaries. **Cross-platform corollary:** when production references a POSIX-only attribute (e.g. `signal.SIGKILL`), Windows tests patching `sys.platform="linux"` MUST also `patch.object(target, "ATTR", value, create=True)` — else `AttributeError` before production logic runs. Same pattern for `os.killpg` and any POSIX symbol guarded by `sys.platform != "win32"`.

39. **Probe-verdict misrouting + observability event-name drift across platforms.** Two paired subrules.

    **(a) Verdict-disjoint remediation.** Acceptance gates and remediation routers MUST consume the probe **verdict** (a categorical classification), not the wrapping symptom. `vad_mute` (user not speaking) and `no_signal` (driver silent) are orthogonal failure classes; routing both to the same ladder loses the operator's working hardware. Sibling of #28 (verify signal energy, not callback count). Generalises: any router whose input is a multi-class verdict MUST dispatch per class, with disjoint remediation paths. Site: pre-mission `voice/health/capture_integrity.py` `handle_deaf_signal` funneled every non-HEALTHY verdict to a single strategy ladder; v0.44.0 verdict-router (Mission C1 T1.3) restored the disjoint dispatch with `assert_never` exhaustiveness. Carries the `is_recheck_eligible`/`is_apo_class_reason` consultation-of-derived-reason corollary at LENIENT (commit `c5791e40`): when a verdict-disjoint field is added during staged adoption, every classifier consumer MUST consult the new field first, with fallback to legacy for pre-mission data — bare reads of the legacy field at LENIENT silently disable the dispatch. Mission anchor: `docs-internal/missions/MISSION-c1-vad-mute-reclassification-2026-05-14.md`.

    **(b) Cross-platform event-name drift.** Cross-platform event names MUST be neutral; platform-specific terminology (`apo.*`, `wasapi.*`, `dsound.*`) MUST be gated by `sys.platform` or live behind a neutral wrapper. Strategies can be platform-specific without the wrapping event needing to be. Sibling of #21 (APO is Windows-only). Site: pre-H2 mission, `audio.apo.bypassed` and `voice_apo_bypass_ineffective` fired on Linux hosts where `voice_clarity_active=False`. Generalises: an event's name is part of its public API for operators, dashboards, and downstream triage tooling; renaming has the same cost discipline as code refactors. Mission anchor: separate v0.43.3 mission (sibling of C1).

## Testing Patterns

```python
# Test class naming
class TestFeatureName:
    """Short description of what's being tested."""

    def test_specific_behavior(self, tmp_path: Path) -> None:
        """What should happen in this scenario."""
        ...

# Async tests (no decorator needed — asyncio_mode=auto)
class TestAsyncFeature:
    @pytest.mark.asyncio()
    async def test_async_behavior(self) -> None: ...

# File handler cleanup fixture
@pytest.fixture(autouse=True)
def _clean_handlers() -> Generator[None, None, None]:
    yield
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, logging.handlers.RotatingFileHandler):
            h.close()
    root.handlers.clear()

# Property-based tests with Hypothesis
from hypothesis import given, settings
from hypothesis import strategies as st

@given(level=st.sampled_from(["DEBUG", "INFO", "WARNING", "ERROR"]))
@settings(max_examples=20)
def test_any_valid_level(self, level: str) -> None: ...

# Auth in dashboard/API tests — use token parameter, never monkeypatch
_TOKEN = "test-token-fixo"

@pytest.fixture()
def app() -> FastAPI:
    return create_app(token=_TOKEN)

@pytest.fixture()
def client(app: FastAPI) -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})

# Exception assertions — xdist-safe, never pytest.raises(InternalException)
with pytest.raises(Exception) as exc_info:
    do_something_that_raises()
assert type(exc_info.value).__name__ == "LLMError"
assert "expected message" in str(exc_info.value)

# Mocking SandboxedHttpClient-based plugins
# SandboxedHttpClient internally calls ._client.request(METHOD, url, ...) — NOT .get().
# Tests that patch httpx.AsyncClient MUST mock .request, not .get, and wire
# MockClient.return_value to the configured mock (NOT the async-with __aenter__ path).
with patch("httpx.AsyncClient") as MockClient:
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(return_value=mock_resp)
    mock_client.aclose = AsyncMock()
    MockClient.return_value = mock_client
    result = await my_plugin_func()

# Patching a module-level aliased import (e.g. `import onnxruntime as ort`)
# sys.modules patches DON'T work — the alias captures the real module at
# import time. Patch the real module's attribute directly:
import onnxruntime
with patch.object(onnxruntime, "InferenceSession", return_value=mock_sess):
    ...

# Patch targets after a module split: `from sovyx.brain.embedding import ModelDownloader`
# was moved to sovyx.brain._model_downloader. Tests must patch the NEW path:
with patch("sovyx.brain._model_downloader.httpx.AsyncClient", ...):
    ...
```

## Debugging Rules

When investigating bugs:
1. **Audit first** — before fixing anything, grep the full codebase for ALL instances of the same pattern. Map the size of the problem before solving any single instance.
2. **Group by root cause** — if 28 tests fail, find out how many distinct root causes exist. Fix causes, not symptoms.
3. **Don't band-aid** — understand the root cause. If you can't explain WHY a fix works, it's not ready.
4. **One commit per root cause** — all fixes for the same root cause go in one commit. No partial pushes to CI for incremental testing.
5. **No shotgun debugging** — if you're setting the same value in 3 places hoping one sticks, stop and trace the actual read path.
6. **Local suite before push** — run the full affected test suite locally before pushing to CI. Each CI round-trip wastes minutes and fragments reasoning.
7. **Check the full chain** — a config bug might affect CLI, dashboard, and API.
8. **Write regression tests** — the bug must never recur.
9. **If you're in the third fix→push→CI-fail cycle for the same problem, STOP** — the approach is wrong. Step back, reassess the strategy.
10. **Windows mypy noise:** local `uv run mypy src/` on Windows reports platform-specific `AF_UNIX` / `os.sysconf` / `getrusage` / `open_unix_server` errors. Those 9 are false positives on Windows; only count errors OUTSIDE that list as real regressions. CI runs Linux — the true baseline.
11. **Closure protocol on a bug class** — when fixing one site of a bug class (e.g. anti-pattern #35 surfacing on `VoiceStep.tsx`), grep ALL consumers of the same flag/sentinel before declaring the fix complete. State the closure assertion in the commit body. Bug classes that surface across siblings invariably do so in waves; each unaudited consumer is the next RC.

## Working Style

When given a task:
1. **Understand the scope** — read relevant source files, understand dependencies.
2. **Check for existing patterns** — look at similar code in the repo for conventions.
3. **Implement** — write code following conventions above.
4. **Write tests** — ≥95 % coverage on modified files, include edge cases.
5. **Run ALL quality gates** — ruff (+ format), mypy (strict), bandit, pytest, vitest, tsc.
6. **Commit with conventional message** — descriptive body explaining WHY.

When modifying tests:
1. **Never introduce workarounds** — if a test needs patching to pass, the production code might need a better interface (e.g., `create_app(token=...)` instead of monkeypatching globals).
2. **Prefer explicit parameters over mocking** — dependency injection beats monkeypatch.
3. **One assertion pattern** — use the xdist-safe patterns documented above consistently.
4. **Remove dead code** — if a fix makes a workaround unnecessary, delete the workaround in the same commit.

When splitting a god file:
1. **Public surface stays stable** — `__init__.py` re-exports everything so callers don't break.
2. **One responsibility per sub-file** — underscore-prefixed modules (`_event_emitter.py`, `_model_downloader.py`) signal "internal, accessed via parent package".
3. **Migrate tests in the same commit** — any `patch("old.module.X")` target becomes a silent no-op once the split lands (anti-pattern #20).
4. **Preserve the public docstring** — move it to the parent module's `__init__.py` if the original class was the face of the module.

## Deploy Flow

1. Bump `version` in `pyproject.toml` (single source of truth — `src/sovyx/__init__.py` reads it via `importlib.metadata.version`).
2. `uv lock` to refresh `uv.lock` (CI enforces `uv lock --check`).
3. `git commit` + `git tag vX.Y.Z` + `git push origin main` + `git push origin vX.Y.Z`.
4. Tag push triggers `publish.yml`:
   - **CI gate** — full ci.yml (lint + typecheck + security + dashboard + Python 3.11 & 3.12 tests) must pass.
   - **Build** — dashboard `npm run build` bakes static assets into `src/sovyx/dashboard/static/`; `uv build` produces sdist + wheel. Publish fails if tag version ≠ pyproject.toml version.
   - **Publish to PyPI** — OIDC trusted publishing, no API token.
   - **GitHub Release** — auto-generated release notes + artifacts.
   - **Docker** — `docker.yml` builds + pushes image in parallel.
5. If CI fails on a tagged commit, fix + commit + re-tag with `git tag -d vX.Y.Z && git tag vX.Y.Z && git push origin vX.Y.Z --force`.

Per `feedback_ci_watching`: don't `gh run watch` after tag push — the operator will surface real failures via the validation backlog.

### Two-Tier GA Strategy (voice subsystem)

The voice subsystem ships in two GA tiers per master mission `MISSION-voice-final-skype-grade-2026.md`:

- **v0.30.0 — single-mind production GA.** Phase 1-7 complete (cold-probe, bypass tiers wire-up, telemetry/IMM listener, multi-platform Win/Linux/macOS). Operators MAY ship v0.30.0 without waiting for Phase 8.
- **v0.31.0 — FINAL multi-mind GA.** Phase 8 complete (per-mind wake word, voice ID, language, accent, cadence — see Phase 8 task block in master mission).

Phase 8 work goes into v0.30.x patches OR directly v0.31.0 — never blocking v0.30.0 release. Operators choose tier per their mind topology.

## Mission Lifecycle

Sovyx coordinates multi-version work via long-running structured missions.

- **Active** missions live in `docs-internal/missions/MISSION-*.md` with task IDs (T1.1, T1.2, …) and Phase boundaries mapped to versions.
- **ADRs** live in `docs-internal/ADR-*.md` and are CANONICAL — referenced from code docstrings. Never delete; supersede via a new ADR that references the old one.
- **Completed / superseded** missions are ARCHIVED to `docs-internal/archive/missions-completed/` with an `## Archive Footer` block (status, code references, predecessor / successor). Update `docs-internal/archive/INDEX.md`.
- **Forensic resolution docs** (post-incident ADRs, RCA closures) go to `docs-internal/archive/forensics-resolved/` with the same footer convention.
- **Never delete** a mission or ADR that produced shipped code — reference value > workspace cleanliness. Pure orphans (planning docs that produced no code, byte-identical duplicates) are the only valid DELETE targets.

When closing a mission task in a commit, reference the mission file + task ID in the body (e.g. `Mission: docs-internal/missions/MISSION-voice-final-skype-grade-2026.md §Phase 1.T2`) and update the mission spec to mark the task ✅ shipped in a follow-up `docs(mission):` commit. Forensic trail intact even when later tasks block.

## Deep Reference
- Public docs (MkDocs): `docs/` — architecture, getting-started, configuration, api-reference, security, per-module specs under `docs/modules/`.
- Internal planning + audits: `docs-internal/` (gitignored, local only).
- Backend specs (IMPL/SPE/ADR): live under `docs-internal/`, searchable by number.
- Code patterns: existing tests are the canonical examples — `tests/unit/` mirrors `src/sovyx/`.
- Frontend types: `dashboard/src/types/api.ts` (compile-time) + `dashboard/src/types/schemas.ts` (runtime).

## Persistent Memory

Sovyx development uses an auto-memory system that persists across sessions:

- **Location:** `C:\Users\guipe\.claude\projects\E--sovyx\memory\`
- **Index file:** `MEMORY.md` — load every linked entry at session start. Keep index lines ≤ 150 chars; detail lives in the linked file.
- **Authority:** memories tagged `feedback_*` carry the SAME authority as CLAUDE.md instructions and OVERRIDE default behavior (see `## Rule Precedence` above). The North Star section is the canonical summary of the current `feedback_*` set.
- **Project memories** (`project_*`) carry historical context: ongoing missions, incidents, paranoid investigations.
- **User memories** (`user_*`) carry preferences and role context.
- **Reference memories** (`reference_*`) point to external systems.

Before recommending from memory, verify the referenced file/function still exists (memories can drift). **Memory state at write time ≠ current state.** When a memory recommends a flag/file/path, grep the codebase before relying on it.
