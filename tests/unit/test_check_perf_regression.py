"""Unit tests for ``scripts/check_perf_regression.py``.

The production path of this script is "run the observability benchmark
N times, compute trimmed-mean p99 per config, compare ratios against
budget". The benchmark subprocess itself is slow (seconds per run) so
these tests inject already-synthesised benchmark outputs and exercise
the pure logic:

* ``_trimmed_mean`` — drops min + max, averages the inner samples,
  falls back to median when there are not enough samples to trim.
* ``_aggregate_p99s`` — correct trimmed-mean across N runs, raises on
  missing benchmark entries.
* ``_check`` — no-violation on clean inputs, reports every individual
  budget breach, picks the right wording for the
  trimmed-mean-of-N / median-of-N framing.

No subprocess is invoked; the tests are millisecond-fast and run on
every platform (the production script is Linux-only in CI, but its
internals are pure Python).
"""

from __future__ import annotations

import importlib.util
import statistics
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from types import ModuleType


_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_perf_regression.py"


def _load_script_module() -> ModuleType:
    """Load ``scripts/check_perf_regression.py`` as an importable module.

    The scripts/ directory is not a package, so we load by file path.
    Caches on ``sys.modules`` so pytest collection doesn't re-import
    on every test.
    """
    name = "_sovyx_check_perf_regression_testshim"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None, f"failed to build spec for {_SCRIPT_PATH}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _entry(benchmark: str, p99_us: float, p50_us: float = 100.0) -> dict[str, Any]:
    return {
        "benchmark": benchmark,
        "p50_us": p50_us,
        "p95_us": p99_us * 0.9,
        "p99_us": p99_us,
        "mean_us": p50_us,
        "samples": 20000.0,
    }


def _run(minimal: float, redacted: float, async_: float) -> list[dict[str, Any]]:
    return [
        _entry("logging.emit.minimal", minimal),
        _entry("logging.emit.redacted", redacted),
        _entry("logging.emit.async", async_),
    ]


# ---------------------------------------------------------------------------
# _trimmed_mean
# ---------------------------------------------------------------------------


class TestTrimmedMean:
    def test_drops_min_and_max_then_averages_inner(self) -> None:
        script = _load_script_module()
        # 5 samples, trim 1 → drop 100 and 500, average (200, 300, 400) = 300.
        value = script._trimmed_mean([100.0, 200.0, 300.0, 400.0, 500.0], trim_count=1)  # noqa: SLF001
        assert value == pytest.approx(300.0)

    def test_seven_samples_trim_one_drops_outermost(self) -> None:
        script = _load_script_module()
        # Inner 5 of [10, 20, 30, 40, 50, 60, 70] is [20, 30, 40, 50, 60]; mean = 40.
        value = script._trimmed_mean(
            [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0],
            trim_count=1,
        )  # noqa: SLF001
        assert value == pytest.approx(40.0)

    def test_two_noisy_runs_of_seven_absorbed(self) -> None:
        """The v0.49.34 contention pattern, restaged as a 7-run trimmed-mean.

        Three of five median-of-5 runs were noisy (~600 µs while the
        clean baseline was ~200 µs) so median itself was a noisy
        sample. With trimmed-mean-of-7 the slowest AND fastest sample
        are discarded before averaging, so the same 3 noisy runs in 7
        plus 4 clean runs leave the inner-5 average dominated by the
        clean cluster.
        """
        script = _load_script_module()
        # 4 clean (~200) + 3 noisy (~600) + 1 of each dropped → inner 5
        # = [200, 200, 200, 600, 600], mean = 360 µs (vs. median-of-5
        # of the same shape = 600 µs noisy sample).
        samples = [200.0, 200.0, 200.0, 200.0, 600.0, 600.0, 600.0]
        value = script._trimmed_mean(samples, trim_count=1)  # noqa: SLF001
        # Drop fastest (200) + slowest (600) → inner = [200, 200, 200, 600, 600]
        assert value == pytest.approx(statistics.fmean([200.0, 200.0, 200.0, 600.0, 600.0]))

    def test_falls_back_to_median_when_insufficient_samples(self) -> None:
        script = _load_script_module()
        # 2 samples, trim 1 → would leave 0 samples → fallback to median.
        value = script._trimmed_mean([100.0, 200.0], trim_count=1)  # noqa: SLF001
        assert value == pytest.approx(statistics.median([100.0, 200.0]))

    def test_single_sample_returns_itself(self) -> None:
        script = _load_script_module()
        value = script._trimmed_mean([42.0], trim_count=1)  # noqa: SLF001
        assert value == pytest.approx(42.0)

    def test_three_samples_trim_one_returns_middle(self) -> None:
        script = _load_script_module()
        # Inner 1 of [100, 200, 300] is [200]; mean of a single sample is itself.
        value = script._trimmed_mean([100.0, 200.0, 300.0], trim_count=1)  # noqa: SLF001
        assert value == pytest.approx(200.0)

    def test_empty_raises(self) -> None:
        script = _load_script_module()
        with pytest.raises(ValueError, match="empty sample"):
            script._trimmed_mean([], trim_count=1)  # noqa: SLF001

    def test_negative_trim_count_raises(self) -> None:
        script = _load_script_module()
        with pytest.raises(ValueError, match="trim_count must be >= 0"):
            script._trimmed_mean([1.0, 2.0, 3.0], trim_count=-1)  # noqa: SLF001


# ---------------------------------------------------------------------------
# _aggregate_p99s
# ---------------------------------------------------------------------------


class TestAggregateP99s:
    def test_single_run_returns_that_runs_values(self) -> None:
        script = _load_script_module()
        runs = [_run(100.0, 200.0, 150.0)]
        aggregated = script._aggregate_p99s(runs)  # noqa: SLF001
        # With 1 sample we fall back to median, which equals the sample.
        assert aggregated == {
            "logging.emit.minimal": 100.0,
            "logging.emit.redacted": 200.0,
            "logging.emit.async": 150.0,
        }

    def test_three_runs_trim_to_middle_sample(self) -> None:
        script = _load_script_module()
        # 3 samples, trim 1 → inner 1 sample, mean of 1 sample = itself.
        runs = [
            _run(100.0, 200.0, 150.0),
            _run(110.0, 210.0, 160.0),
            _run(90.0, 190.0, 140.0),
        ]
        aggregated = script._aggregate_p99s(runs)  # noqa: SLF001
        assert aggregated["logging.emit.minimal"] == pytest.approx(100.0)
        assert aggregated["logging.emit.redacted"] == pytest.approx(200.0)
        assert aggregated["logging.emit.async"] == pytest.approx(150.0)

    def test_seven_runs_drop_outermost_two_then_average(self) -> None:
        script = _load_script_module()
        # The async column has a single outlier at 800; trimmed-mean
        # drops it (as the max) and averages the inner 5 of 7.
        runs = [
            _run(100.0, 200.0, 150.0),
            _run(110.0, 210.0, 160.0),
            _run(90.0, 190.0, 140.0),
            _run(105.0, 205.0, 155.0),
            _run(115.0, 215.0, 165.0),
            _run(95.0, 195.0, 145.0),
            _run(100.0, 200.0, 800.0),  # <-- single outlier, dropped
        ]
        aggregated = script._aggregate_p99s(runs)  # noqa: SLF001
        # Async inner 5 (after dropping min 140 + max 800) =
        # [145, 150, 155, 160, 165]; mean = 155.
        assert aggregated["logging.emit.async"] == pytest.approx(155.0)

    def test_two_noisy_runs_of_seven_no_violation(self) -> None:
        """v0.49.34 bimodal contention, restaged as trimmed-mean-of-7.

        With median-of-5 and 3 of 5 noisy samples the median is itself
        the noisy sample (3.46× budget breach). Trimmed-mean-of-7
        absorbs the same noise level because the slowest sample is
        dropped before averaging — the gate stays clean.
        """
        script = _load_script_module()
        runs = [
            _run(175.0, 200.0, 235.0),
            _run(166.0, 195.0, 248.0),
            _run(174.0, 198.0, 250.0),
            _run(180.0, 205.0, 260.0),
            _run(197.0, 220.0, 603.0),  # noisy
            _run(209.0, 230.0, 643.0),  # noisy
            _run(165.0, 192.0, 200.0),
        ]
        # async inner 5 (drop min 200, drop max 643) =
        # [235, 248, 250, 260, 603]; mean = 319.2 µs.
        # minimal inner 5 (drop min 165, drop max 209) =
        # [166, 174, 175, 180, 197]; mean = 178.4 µs.
        # ratio 319.2 / 178.4 = 1.79× — under the 2.0× budget.
        violations = script._check(runs)  # noqa: SLF001
        assert violations == []

    def test_missing_entry_raises(self) -> None:
        script = _load_script_module()
        # Delete the async entry from the single run.
        runs = [_run(100.0, 200.0, 150.0)]
        runs[0].pop()  # drop async
        with pytest.raises(KeyError, match="logging.emit.async"):
            script._aggregate_p99s(runs)  # noqa: SLF001


# ---------------------------------------------------------------------------
# _check
# ---------------------------------------------------------------------------


class TestCheckClean:
    def test_empty_runs_list_reports_violation(self) -> None:
        script = _load_script_module()
        assert script._check([]) == [  # noqa: SLF001
            "no benchmark runs were collected",
        ]

    def test_clean_single_run_passes(self) -> None:
        script = _load_script_module()
        runs = [_run(200.0, 300.0, 220.0)]
        assert script._check(runs) == []  # noqa: SLF001

    def test_clean_three_runs_passes(self) -> None:
        script = _load_script_module()
        runs = [
            _run(200.0, 300.0, 220.0),
            _run(210.0, 320.0, 230.0),
            _run(195.0, 290.0, 215.0),
        ]
        assert script._check(runs) == []  # noqa: SLF001

    def test_single_noisy_async_run_dropped_at_seven_repeats(self) -> None:
        """A single async outlier in 7 runs gets discarded as the max."""
        script = _load_script_module()
        runs = [
            _run(200.0, 230.0, 195.0),
            _run(205.0, 235.0, 190.0),
            _run(198.0, 232.0, 192.0),
            _run(202.0, 234.0, 196.0),
            _run(207.0, 236.0, 193.0),
            _run(201.0, 233.0, 194.0),
            _run(189.3, 227.2, 698.0),  # <-- the original v0.27.0 CI flake
        ]
        assert script._check(runs) == []  # noqa: SLF001


class TestCheckRatioViolations:
    def test_async_ratio_exceeded_in_all_runs_fails(self) -> None:
        script = _load_script_module()
        # Every run shows async at 3× minimal — trimmed-mean is 3×, gate fires.
        runs = [
            _run(100.0, 200.0, 300.0),
            _run(110.0, 210.0, 330.0),
            _run(90.0, 190.0, 270.0),
        ]
        violations = script._check(runs)  # noqa: SLF001
        assert len(violations) == 1
        assert "async/minimal" in violations[0]
        assert "3.00×" in violations[0]

    def test_redacted_ratio_exceeded_in_all_runs_fails(self) -> None:
        script = _load_script_module()
        # Redacted runs at 4× minimal; budget is 3×.
        runs = [
            _run(100.0, 400.0, 150.0),
            _run(110.0, 440.0, 160.0),
            _run(90.0, 360.0, 140.0),
        ]
        violations = script._check(runs)  # noqa: SLF001
        assert len(violations) == 1
        assert "redacted/minimal" in violations[0]
        assert "4.00×" in violations[0]

    def test_both_ratios_exceeded_fails_with_two_violations(self) -> None:
        script = _load_script_module()
        runs = [
            _run(100.0, 400.0, 300.0),
            _run(110.0, 440.0, 330.0),
            _run(90.0, 360.0, 270.0),
        ]
        violations = script._check(runs)  # noqa: SLF001
        assert len(violations) == 2

    def test_absolute_ceiling_breached(self) -> None:
        script = _load_script_module()
        # All three runs have minimal p99 above 10 ms — catastrophic.
        runs = [
            _run(11_000.0, 12_000.0, 10_500.0),
            _run(11_100.0, 12_100.0, 10_600.0),
            _run(10_900.0, 11_900.0, 10_400.0),
        ]
        violations = script._check(runs)  # noqa: SLF001
        # All three configs tripped the absolute ceiling.
        assert len(violations) == 3
        for line in violations:
            assert "absolute ceiling" in line


class TestCheckMessageFraming:
    def test_violation_message_mentions_run_count_and_trimmed_mean(self) -> None:
        """The trimmed-mean-of-N wording is part of the gate's user
        experience — a contributor reading the failure should see
        whether this is a fallback median or a true trimmed-mean.
        """
        script = _load_script_module()
        runs = [
            _run(100.0, 200.0, 300.0),  # async ratio 3×
            _run(100.0, 200.0, 300.0),
            _run(100.0, 200.0, 300.0),
            _run(100.0, 200.0, 300.0),
            _run(100.0, 200.0, 300.0),
        ]
        violations = script._check(runs)  # noqa: SLF001
        assert violations
        assert "across 5 runs" in violations[0]
        assert "trimmed-mean" in violations[0].lower()

    def test_low_repeat_message_falls_back_to_median_wording(self) -> None:
        """With fewer samples than 2 * _TRIM_COUNT + 1 the script
        reports the aggregation as ``median-of-N`` because
        ``_trimmed_mean`` falls back to the median.
        """
        script = _load_script_module()
        runs = [_run(100.0, 200.0, 300.0), _run(100.0, 200.0, 300.0)]
        violations = script._check(runs)  # noqa: SLF001
        assert violations
        assert "across 2 runs" in violations[0]
        assert "median-of-2" in violations[0]


# ---------------------------------------------------------------------------
# _aggregate_label
# ---------------------------------------------------------------------------


class TestAggregateLabel:
    def test_label_switches_at_trim_threshold(self) -> None:
        script = _load_script_module()
        # _TRIM_COUNT = 1, so threshold is 2 * 1 + 1 = 3 runs.
        assert script._aggregate_label(1) == "median-of-1"  # noqa: SLF001
        assert script._aggregate_label(2) == "median-of-2"  # noqa: SLF001
        assert script._aggregate_label(3) == "trimmed-mean-of-3"  # noqa: SLF001
        assert script._aggregate_label(5) == "trimmed-mean-of-5"  # noqa: SLF001
        assert script._aggregate_label(7) == "trimmed-mean-of-7"  # noqa: SLF001
