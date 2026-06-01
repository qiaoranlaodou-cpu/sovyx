"""Hypothesis property tests for ``ResourceCohortMetrics`` (Mission C C.6 §4).

Augments the Mission C C.6 §1 paired boundary tests in
``tests/integration/dashboard/test_engine_resources_boundary.py`` with
random-input fuzz coverage on the dotted-key alias surface + the
``extra="allow"`` passthrough contract:

* Every ``int`` cohort field accepts any non-negative int (cohort
  registry counters are monotonic non-negative since process start).
* ``list[str]`` / ``list[int]`` / ``dict[str, int]`` fields accept
  arbitrarily-shaped lists / dicts within their type bounds.
* ``populate_by_name=True`` — every dotted-alias field is reachable
  via either the wire alias (``"to_thread.pool_size"``) or the
  python attribute name (``"to_thread_pool_size"``).
* ``extra="allow"`` — arbitrary unknown top-level keys round-trip
  cleanly through ``__pydantic_extra__`` (the H4 Phase 1.E +
  D-class forward-additive contract).

Mission anchor:
``docs-internal/MISSION-C-REMEDIATION-PLAN-2026-05-21.md`` §3 Phase C.6
sub-sequence step 4 (Hypothesis property tests).
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from sovyx.dashboard.routes.engine_resources import ResourceCohortMetrics

# Set of dotted-alias prefixes and full dotted aliases that the model
# OWNS via Field(alias=...). Any random "unknown" key that collides
# with these MUST NOT be used in passthrough fuzz (they'd land in the
# typed field rather than __pydantic_extra__).
_CANONICAL_DOTTED_ALIAS_PREFIXES: tuple[str, ...] = (
    "process.",
    "asyncio.",
    "to_thread.",
    "lock_dict.",
    "onnx.",
    "gc.",
    "tracemalloc.",
    "exception_cohort.",
)

# Set of python attribute names the model OWNS (populate_by_name=True
# accepts these too — they'd land in the typed field, not extras).
_CANONICAL_ATTR_NAME_PREFIXES: tuple[str, ...] = (
    "process_",
    "asyncio_",
    "to_thread_",
    "lock_dict_",
    "onnx_",
    "gc_",
    "tracemalloc_",
    "exception_cohort_",
)


def _is_canonical_key(key: str) -> bool:
    return any(key.startswith(p) for p in _CANONICAL_DOTTED_ALIAS_PREFIXES) or any(
        key.startswith(p) for p in _CANONICAL_ATTR_NAME_PREFIXES
    )


# ── Int cohort fields ──────────────────────────────────────────────────


@given(
    pool_size=st.integers(min_value=0, max_value=2**31 - 1),
    queue_depth=st.integers(min_value=0, max_value=2**31 - 1),
    max_workers=st.integers(min_value=0, max_value=2**31 - 1),
)
@settings(max_examples=100, deadline=None)
def test_to_thread_canonical_int_fields_round_trip(
    pool_size: int,
    queue_depth: int,
    max_workers: int,
) -> None:
    """Any non-negative int triple validates + round-trips via the
    canonical post-A.1 freshness-suffixed keys."""
    payload: dict[str, Any] = {
        "to_thread.pool_size_at_last_dispatch": pool_size,
        "to_thread.queue_depth_at_last_dispatch": queue_depth,
        "to_thread.max_workers_at_last_dispatch": max_workers,
    }
    cohorts = ResourceCohortMetrics.model_validate(payload)
    assert cohorts.to_thread_pool_size_at_last_dispatch == pool_size
    assert cohorts.to_thread_queue_depth_at_last_dispatch == queue_depth
    assert cohorts.to_thread_max_workers_at_last_dispatch == max_workers


@given(
    cumulative_bytes=st.integers(min_value=0, max_value=2**63 - 1),
    cumulative_groups=st.integers(min_value=0, max_value=2**31 - 1),
    window_bytes=st.integers(min_value=0, max_value=2**63 - 1),
    window_groups=st.integers(min_value=0, max_value=2**31 - 1),
)
@settings(max_examples=80, deadline=None)
def test_exception_cohort_int_fields_round_trip(
    cumulative_bytes: int,
    cumulative_groups: int,
    window_bytes: int,
    window_groups: int,
) -> None:
    """Mission A.1 F-002+F-003 cumulative-vs-window split — both halves
    accept any non-negative int (the cumulative half is monotonic since
    process start; the window half decays with the deque)."""
    payload: dict[str, Any] = {
        "exception_cohort.cumulative_retained_bytes_since_start": cumulative_bytes,
        "exception_cohort.cumulative_distinct_group_id_count": cumulative_groups,
        "exception_cohort.window_retained_bytes": window_bytes,
        "exception_cohort.window_distinct_group_id_count": window_groups,
    }
    cohorts = ResourceCohortMetrics.model_validate(payload)
    assert cohorts.exception_cohort_cumulative_retained_bytes_since_start == cumulative_bytes
    assert cohorts.exception_cohort_cumulative_distinct_group_id_count == cumulative_groups
    assert cohorts.exception_cohort_window_retained_bytes == window_bytes
    assert cohorts.exception_cohort_window_distinct_group_id_count == window_groups


@given(
    onnx_count=st.integers(min_value=0, max_value=10_000),
    lock_dict_count=st.integers(min_value=0, max_value=10_000),
    gc_count=st.integers(min_value=0, max_value=2**31 - 1),
)
@settings(max_examples=60, deadline=None)
def test_misc_int_cohort_fields_round_trip(
    onnx_count: int,
    lock_dict_count: int,
    gc_count: int,
) -> None:
    payload: dict[str, Any] = {
        "onnx.session_count": onnx_count,
        "lock_dict.instance_count": lock_dict_count,
        "gc.objects_count": gc_count,
    }
    cohorts = ResourceCohortMetrics.model_validate(payload)
    assert cohorts.onnx_session_count == onnx_count
    assert cohorts.lock_dict_instance_count == lock_dict_count
    assert cohorts.gc_objects_count == gc_count


# ── list / dict cohort fields ──────────────────────────────────────────


@given(
    labels=st.lists(
        st.text(min_size=1, max_size=40),
        min_size=0,
        max_size=20,
        unique=True,
    ),
)
@settings(max_examples=80, deadline=None)
def test_onnx_session_labels_accepts_any_string_list(labels: list[str]) -> None:
    payload = {"onnx.session_labels": labels}
    cohorts = ResourceCohortMetrics.model_validate(payload)
    assert cohorts.onnx_session_labels == labels


@given(
    task_names=st.lists(
        st.text(min_size=1, max_size=40),
        min_size=0,
        max_size=20,
    ),
)
@settings(max_examples=60, deadline=None)
def test_asyncio_all_task_names_accepts_any_string_list(
    task_names: list[str],
) -> None:
    """Mission A.1 F-005 / ADR-D15 closure — the snapshot field
    accepts the raw task-name list emitted by the snapshotter."""
    payload = {"asyncio.all_task_names": task_names}
    cohorts = ResourceCohortMetrics.model_validate(payload)
    assert cohorts.asyncio_all_task_names == task_names


@given(
    per_owner=st.dictionaries(
        keys=st.text(min_size=1, max_size=40),
        values=st.integers(min_value=0, max_value=2**31 - 1),
        max_size=20,
    ),
)
@settings(max_examples=60, deadline=None)
def test_lock_dict_per_owner_accepts_arbitrary_str_int_dict(
    per_owner: dict[str, int],
) -> None:
    payload = {"lock_dict.per_owner": per_owner}
    cohorts = ResourceCohortMetrics.model_validate(payload)
    assert cohorts.lock_dict_per_owner == per_owner


@given(
    per_label=st.dictionaries(
        keys=st.text(min_size=1, max_size=40),
        values=st.integers(min_value=0, max_value=2**31 - 1),
        max_size=20,
    ),
)
@settings(max_examples=60, deadline=None)
def test_to_thread_dispatch_count_per_label_accepts_arbitrary_str_int_dict(
    per_label: dict[str, int],
) -> None:
    payload = {"to_thread.dispatch_count_per_label": per_label}
    cohorts = ResourceCohortMetrics.model_validate(payload)
    assert cohorts.to_thread_dispatch_count_per_label == per_label


# ── populate_by_name parity ────────────────────────────────────────────


@given(value=st.integers(min_value=0, max_value=2**31 - 1))
@settings(max_examples=40, deadline=None)
def test_populate_by_name_alias_and_attr_name_parity(value: int) -> None:
    """``ConfigDict(populate_by_name=True)`` — pydantic accepts EITHER
    the dotted wire alias OR the underscore python attribute name.
    Both paths MUST produce the same model value."""
    by_alias = ResourceCohortMetrics.model_validate(
        {"to_thread.pool_size_at_last_dispatch": value},
    )
    by_name = ResourceCohortMetrics.model_validate(
        {"to_thread_pool_size_at_last_dispatch": value},
    )
    assert by_alias.to_thread_pool_size_at_last_dispatch == value
    assert by_name.to_thread_pool_size_at_last_dispatch == value
    assert (
        by_alias.to_thread_pool_size_at_last_dispatch
        == by_name.to_thread_pool_size_at_last_dispatch
    )


# ── extra="allow" passthrough ──────────────────────────────────────────


@given(
    unknown_key=st.text(
        alphabet=st.characters(
            min_codepoint=ord("a"),
            max_codepoint=ord("z"),
        ),
        min_size=4,
        max_size=40,
    ),
    unknown_value=st.one_of(
        st.integers(),
        st.text(),
        st.booleans(),
        st.lists(st.integers(), max_size=5),
        st.dictionaries(st.text(min_size=1, max_size=10), st.integers(), max_size=5),
    ),
)
@settings(max_examples=80, deadline=None)
def test_passthrough_accepts_arbitrary_unknown_key(
    unknown_key: str,
    unknown_value: Any,
) -> None:
    """``extra="allow"`` — any non-canonical key lands in
    ``__pydantic_extra__`` (the H4 Phase 1.E forward-additive
    contract). Filter out keys that collide with the canonical
    field-name / alias namespaces so we test the actual passthrough
    branch, not typed-field validation."""
    assume(not _is_canonical_key(unknown_key))
    # Also skip keys that look like dotted aliases the schema might own
    # in a future minor (defensive — the assume above already covers
    # current canonicals).
    assume("." not in unknown_key)
    payload: dict[str, Any] = {unknown_key: unknown_value}
    cohorts = ResourceCohortMetrics.model_validate(payload)
    extra = cohorts.__pydantic_extra__ or {}
    assert unknown_key in extra
    assert extra[unknown_key] == unknown_value


@given(
    pool_size=st.integers(min_value=0, max_value=2**31 - 1),
    extra_count=st.integers(min_value=0, max_value=10),
)
@settings(max_examples=40, deadline=None)
def test_passthrough_does_not_interfere_with_canonical_typed_fields(
    pool_size: int,
    extra_count: int,
) -> None:
    """A canonical field + extras coexist: the canonical land typed,
    the extras land in ``__pydantic_extra__``."""
    payload: dict[str, Any] = {
        "to_thread.pool_size_at_last_dispatch": pool_size,
    }
    for i in range(extra_count):
        payload[f"future_governor_field_{i}"] = i * 10
    cohorts = ResourceCohortMetrics.model_validate(payload)
    assert cohorts.to_thread_pool_size_at_last_dispatch == pool_size
    extra = cohorts.__pydantic_extra__ or {}
    for i in range(extra_count):
        assert f"future_governor_field_{i}" in extra
        assert extra[f"future_governor_field_{i}"] == i * 10


# ── Type-mismatch rejection ────────────────────────────────────────────


def _pydantic_int_coercible(s: str) -> bool:
    """True if pydantic v2 (lax) would coerce ``s`` to ``int``.

    Pydantic accepts more than pure-digit strings: it is whitespace-tolerant
    (``"0\\r"`` / ``" 5"`` / ``"7\\n"``) AND it accepts integral *float*
    spellings (``"0.0"`` / ``"-3.0"``) by parsing the float and accepting when
    ``.is_integer()``. A rejection test must exclude EVERY coercible spelling,
    else Hypothesis keeps surfacing the next one (``"0\\r"`` pre-fix → ``"0.0"``
    next). The earlier filter only checked ``.isdigit()`` and missed floats.
    """
    t = s.strip()
    try:
        int(t)
        return True
    except ValueError:
        pass
    try:
        return float(t).is_integer()
    except (ValueError, OverflowError):
        return False


@given(value=st.text(min_size=1, max_size=20).filter(lambda s: not _pydantic_int_coercible(s)))
@settings(max_examples=80, deadline=None)
def test_int_field_rejects_non_numeric_strings(value: str) -> None:
    """``onnx.session_count`` is ``int`` — pydantic coerces numeric
    strings by default, but pure-text strings MUST reject."""
    payload = {"onnx.session_count": value}
    with pytest.raises(ValidationError):
        ResourceCohortMetrics.model_validate(payload)


@given(
    value=st.one_of(
        st.lists(st.integers(), min_size=1, max_size=5),
        st.dictionaries(st.text(min_size=1, max_size=5), st.integers(), min_size=1, max_size=3),
    ),
)
@settings(max_examples=40, deadline=None)
def test_int_field_rejects_list_or_dict(value: Any) -> None:
    """Compound types for an int field — reject."""
    payload = {"onnx.session_count": value}
    with pytest.raises(ValidationError):
        ResourceCohortMetrics.model_validate(payload)
