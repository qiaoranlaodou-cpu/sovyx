"""CI gate — assert observability hot-path latency stays within budget.

Stand-alone enforcement of §23 of IMPL-OBSERVABILITY-001 ("perf
budgets"). Runs ``benchmarks/bench_observability.py`` multiple times
to measure the per-emit latency of the structlog pipeline under three
configurations (minimal / redacted / async), then enforces two
complementary checks against the **trimmed-mean p99** latencies
across runs:

  1. **Absolute ceiling** — a generous upper bound (default 10 ms p99)
     that catches catastrophic regressions where a refactor turned the
     pipeline 100× slower. The exact wall-clock floor varies wildly
     between developer laptops and shared CI runners, so the absolute
     check is intentionally loose; it exists only to fail on
     order-of-magnitude regressions.

  2. **Self-relative ratio** — the ratio of redacted-pipeline latency
     to minimal-pipeline latency (and async-pipeline to minimal). The
     PII redactor and clamp processors should add bounded overhead;
     if redacted/minimal grows past 3.0× the chain has either
     introduced a quadratic processor or accidentally widened the
     critical section. Likewise async/minimal > 2.0× means the queue
     enqueue path lost its ``put_nowait`` fast path.

Why trimmed-mean-of-N
=====================

Single-shot p99 on a shared CI runner is not noise-invariant — a
single GC pause or noisy-neighbour hiccup puts one sample at the
top of the 99.x percentile bucket, and with 20k samples a single
outlier at 700 µs pushes the p99 ratio past the gate. p99 is
explicitly tail-sensitive: it is the right number for production
SLO monitoring but the wrong number for CI ratio-budget enforcement
unless aggregated across runs.

Aggregation history on this gate
--------------------------------

* **v0.27.0** — single-shot p99 flaked under runner contention. Fix:
  median-of-3 (the canonical ``cargo bench`` / criterion.rs
  approach).
* **v0.45.7** — median-of-3 flaked when parallel
  ``CI / Perf Regression Gate`` and
  ``Publish to PyPI / CI Gate / Perf Regression Gate`` jobs landed
  on the same physical ``sovyx-4core`` host. Fix: bumped
  ``_DEFAULT_REPEATS`` 3 → 5 AND added the ``perf-regression-gate-global``
  ``concurrency`` group in ``ci.yml`` so the two workflow_call instances
  serialize.
* **v0.49.34** — median-of-5 flaked again, this time with the
  concurrency group already in place: third-party tenants on a
  GitHub-managed Larger Runner caused 3 of 5 async-p99 samples to
  spike to 600–820 µs while minimal stayed at 170–210 µs (median
  ratio 3.46× > 2.0× budget). Three of five noisy runs means the
  median is itself a noisy sample — median's robustness only kicks in
  if a majority of runs are clean. Fix: bumped ``_DEFAULT_REPEATS``
  5 → 7 AND switched aggregation from ``statistics.median`` to a
  **trimmed mean** that drops the slowest AND the fastest run before
  averaging the middle five. Trimmed-mean now survives up to 2 noisy
  runs of 7 (vs. the median-of-5's 2-of-5 breakpoint), which is the
  next discrete layer of noise headroom obtainable without
  recalibrating the 2.0× / 3.0× ratio budgets.

We take **trimmed mean**, not minimum. Minimum is maximally permissive
and would mask a genuinely flaky hot path. Trimmed-mean keeps the cost
asymmetry (one or two bad runs cost nothing, three+ bad runs are a
real regression).

Wired into ``.github/workflows/ci.yml`` as the
``perf-regression-gate`` job after ``metrics-cardinality-gate``.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess  # noqa: S404 — controlled subprocess of our own benchmark.
import sys
from pathlib import Path
from typing import Any

# ── Budgets ───────────────────────────────────────────────────────────
# The redactor adds ~50 µs of work per record on a developer laptop;
# 3× headroom gives CI runners ample slack while still catching a
# regression where the chain accidentally grew quadratic. Async should
# be FASTER than minimal (queue enqueue beats direct file write), so
# 2× the minimal floor is already a generous regression ceiling.
_REDACTED_VS_MINIMAL_MAX: float = 3.0
_ASYNC_VS_MINIMAL_MAX: float = 2.0

# Order-of-magnitude sanity floor in microseconds. Any modern x86 CPU
# emits a JSON log line in well under 1 ms; 10 ms p99 means something
# has gone catastrophically wrong (sync IO on every record, infinite
# retry, lock contention) and warrants a hard fail regardless of the
# self-relative ratio.
_ABSOLUTE_P99_CEILING_US: float = 10_000.0

# Default benchmark sample count per run — large enough for a stable
# p99 but fast enough that the CI step finishes in a few seconds.
_DEFAULT_BENCH_ITERATIONS: int = 20_000

# Default independent-run count for trimmed-mean-of-N computation.
# Trimmed-mean drops the slowest AND the fastest run before averaging
# the middle samples, so the gate survives ``_TRIM_COUNT`` noisy runs
# without firing.
#
# Sizing history (see module docstring "Aggregation history"):
#   * v0.27.0 — 1 → 3 (median-of-3, single noisy run absorbed).
#   * v0.45.7 — 3 → 5 (median-of-5, two noisy runs absorbed)
#     paired with ``perf-regression-gate-global`` concurrency group.
#   * v0.49.34 — 5 → 7 (trimmed-mean of inner 5 of 7, two noisy runs
#     absorbed AND the median-itself-is-noisy mode eliminated).
#
# Odd numbers preserve a clear "middle sample" semantic when a caller
# overrides ``--repeats`` to a low value (the trim helper falls back to
# the median when there are not enough samples to trim).
_DEFAULT_REPEATS: int = 7

# Outlier samples dropped from each tail before averaging the middle.
# With ``_DEFAULT_REPEATS = 7`` and ``_TRIM_COUNT = 1`` we discard the
# single slowest and the single fastest run and average the inner five.
# Bumping this to 2 (drop two from each side) is the next escalation
# step if anti-pattern #31 recurs again — leaves three central samples
# at the default 7-repeat setting.
_TRIM_COUNT: int = 1


def _run_benchmark(*, iterations: int, repo_root: Path) -> list[dict[str, Any]]:
    """Spawn the benchmark script and return its parsed JSON output.

    We invoke the benchmark via ``subprocess`` (instead of importing
    it) so the measurement runs in a fresh interpreter with no warm
    caches from the gate's own setup. That keeps the percentiles
    representative of a cold daemon process.
    """
    bench = repo_root / "benchmarks" / "bench_observability.py"
    if not bench.is_file():
        msg = f"benchmark script not found: {bench}"
        raise FileNotFoundError(msg)

    out_path = repo_root / "benchmarks" / "_perf_run.json"
    proc = subprocess.run(  # noqa: S603 — ``bench`` is repo-controlled.
        [
            sys.executable,
            str(bench),
            "--iterations",
            str(iterations),
            "--out",
            str(out_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        msg = (
            f"benchmark exited with status {proc.returncode}\n"
            f"--- stdout ---\n{proc.stdout}\n"
            f"--- stderr ---\n{proc.stderr}"
        )
        raise RuntimeError(msg)

    try:
        payload = json.loads(out_path.read_text(encoding="utf-8"))
    finally:
        out_path.unlink(missing_ok=True)

    if not isinstance(payload, list):
        msg = f"benchmark output is not a JSON list: {type(payload).__name__}"
        raise TypeError(msg)
    return payload


def _index(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Return ``results`` indexed by the ``benchmark`` field."""
    return {entry["benchmark"]: entry for entry in results}


def _trimmed_mean(values: list[float], trim_count: int) -> float:
    """Return the mean of ``values`` after dropping ``trim_count`` from each tail.

    Falls back to ``statistics.median`` when there are not enough
    samples to perform the trim (i.e. ``len(values) < 2 * trim_count
    + 1``), which preserves sensible behaviour when a caller overrides
    ``--repeats`` to a low value such as 3 with the default
    ``_TRIM_COUNT = 1``: the inner slice would be a single sample and
    its mean equals itself, but for ``--repeats <= 2`` the inner slice
    is empty and median is the only defined fallback.
    """
    if trim_count < 0:
        msg = f"trim_count must be >= 0, got {trim_count}"
        raise ValueError(msg)
    if not values:
        msg = "cannot compute trimmed mean of empty sample"
        raise ValueError(msg)
    if len(values) < 2 * trim_count + 1:
        return float(statistics.median(values))
    sorted_values = sorted(values)
    inner = sorted_values[trim_count : len(sorted_values) - trim_count]
    return float(statistics.fmean(inner))


def _aggregate_p99s(
    runs: list[list[dict[str, Any]]],
) -> dict[str, float]:
    """Return ``{benchmark_name: trimmed_mean_p99_us}`` across independent runs.

    Expects each entry of ``runs`` to be a full benchmark output list
    (i.e. one item per config: minimal / redacted / async). Panics
    when any run is missing one of the required benchmarks.

    Aggregation is the **trimmed mean** of the per-run p99s — see the
    module docstring "Why trimmed-mean-of-N" for the rationale and the
    incident history that drove the switch from ``statistics.median``
    in v0.49.35.
    """
    expected = {"logging.emit.minimal", "logging.emit.redacted", "logging.emit.async"}
    per_name_p99s: dict[str, list[float]] = {name: [] for name in expected}
    for results in runs:
        by_name = _index(results)
        for name in expected:
            if name not in by_name:
                msg = f"benchmark run missing entry: {name!r}"
                raise KeyError(msg)
            per_name_p99s[name].append(float(by_name[name]["p99_us"]))
    return {
        name: _trimmed_mean(p99s, trim_count=_TRIM_COUNT)
        for name, p99s in per_name_p99s.items()
    }


def _aggregate_label(repeats: int) -> str:
    """Return the human-readable aggregation label for the given run count."""
    if repeats >= 2 * _TRIM_COUNT + 1:
        return f"trimmed-mean-of-{repeats}"
    return f"median-of-{repeats}"


def _check(runs: list[list[dict[str, Any]]]) -> list[str]:
    """Return human-readable violations; empty list means clean.

    Takes a list of benchmark runs and computes the trimmed-mean p99
    per config before applying the absolute + ratio checks.
    Trimmed-mean (dropping the slowest and the fastest of N runs and
    averaging the inner samples) is the noise-robust statistic for p99
    on shared runners — see module docstring for the why and the
    v0.27.0 → v0.45.7 → v0.49.34 incident history that drove the
    switch from ``statistics.median``.
    """
    violations: list[str] = []
    if not runs:
        violations.append("no benchmark runs were collected")
        return violations

    try:
        aggregated = _aggregate_p99s(runs)
    except KeyError as exc:
        violations.append(str(exc))
        return violations

    aggregate = _aggregate_label(len(runs))
    minimal_p99 = aggregated["logging.emit.minimal"]
    redacted_p99 = aggregated["logging.emit.redacted"]
    async_p99 = aggregated["logging.emit.async"]

    # Absolute ceiling — order-of-magnitude regression fails hard.
    for label, value in (
        ("logging.emit.minimal", minimal_p99),
        ("logging.emit.redacted", redacted_p99),
        ("logging.emit.async", async_p99),
    ):
        if value > _ABSOLUTE_P99_CEILING_US:
            violations.append(
                f"{label}: {aggregate} p99 = {value:.1f} µs exceeds "
                f"absolute ceiling {_ABSOLUTE_P99_CEILING_US:.0f} µs "
                "(catastrophic regression)"
            )

    # Self-relative ratios — derived from the aggregated p99s so a
    # single noisy run cannot bump a ratio through the budget.
    if minimal_p99 > 0:
        redacted_ratio = redacted_p99 / minimal_p99
        if redacted_ratio > _REDACTED_VS_MINIMAL_MAX:
            violations.append(
                f"redacted/minimal p99 ratio = {redacted_ratio:.2f}× "
                f"exceeds budget {_REDACTED_VS_MINIMAL_MAX:.1f}× "
                f"({aggregate} redacted={redacted_p99:.1f} µs, "
                f"{aggregate} minimal={minimal_p99:.1f} µs across {len(runs)} runs). "
                "Likely cause: a new processor in the redactor chain that scales "
                "with payload size, or a regex added without a fast-reject path."
            )

        async_ratio = async_p99 / minimal_p99
        if async_ratio > _ASYNC_VS_MINIMAL_MAX:
            violations.append(
                f"async/minimal p99 ratio = {async_ratio:.2f}× "
                f"exceeds budget {_ASYNC_VS_MINIMAL_MAX:.1f}× "
                f"({aggregate} async={async_p99:.1f} µs, "
                f"{aggregate} minimal={minimal_p99:.1f} µs across {len(runs)} runs). "
                "Likely cause: AsyncQueueHandler.enqueue lost its put_nowait "
                "fast path, or BackgroundLogWriter started doing work on the "
                "producer thread."
            )

    return violations


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — returns 0 on clean run, 1 on any violation."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--iterations",
        type=int,
        default=_DEFAULT_BENCH_ITERATIONS,
        help=f"Samples per benchmark (default: {_DEFAULT_BENCH_ITERATIONS})",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=_DEFAULT_REPEATS,
        help=(
            f"Independent benchmark runs for trimmed-mean-of-N "
            f"aggregation (default: {_DEFAULT_REPEATS}). Use 3-7 for "
            f"CI; runs below {2 * _TRIM_COUNT + 1} fall back to median."
        ),
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Repository root (default: current working directory)",
    )
    args = parser.parse_args(argv)

    if args.repeats < 1:
        print("error: --repeats must be >= 1", file=sys.stderr)
        return 2
    if not (args.root / "benchmarks").is_dir():
        print(
            f"error: {args.root} does not look like the sovyx repo (missing benchmarks/)",
            file=sys.stderr,
        )
        return 2

    runs: list[list[dict[str, Any]]] = []
    for attempt in range(1, args.repeats + 1):
        try:
            run_results = _run_benchmark(
                iterations=args.iterations,
                repo_root=args.root,
            )
        except (FileNotFoundError, RuntimeError, TypeError, json.JSONDecodeError) as exc:
            print(
                f"FAIL: could not run perf benchmark (attempt {attempt}/{args.repeats}): "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return 1
        runs.append(run_results)

    aggregate = _aggregate_label(len(runs))
    violations = _check(runs)
    if violations:
        print(
            f"\nFAIL: {len(violations)} perf regression(s) ({aggregate} of "
            f"{len(runs)} runs):",
            file=sys.stderr,
        )
        for line in violations:
            print(f"  - {line}", file=sys.stderr)
        print("\n  Per-run benchmark output:", file=sys.stderr)
        for idx, results in enumerate(runs, start=1):
            print(f"    run {idx}:", file=sys.stderr)
            for entry in results:
                print(f"      {entry}", file=sys.stderr)
        return 1

    aggregated = _aggregate_p99s(runs)
    print(
        f"OK: observability hot-path latency within budget ({aggregate} of "
        f"{len(runs)} runs).",
    )
    for name, value in aggregated.items():
        per_run = [float(next(e for e in r if e["benchmark"] == name)["p99_us"]) for r in runs]
        print(
            f"  {name}: {aggregate} p99={value:.1f} µs "
            f"(per-run p99: {', '.join(f'{v:.1f}' for v in per_run)})"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
