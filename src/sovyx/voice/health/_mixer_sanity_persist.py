"""L2.5 mixer-sanity persistence — alsactl + systemd unit delegate.

Phase 5.F.8 god-file extraction from
``voice/health/_mixer_sanity.py`` (anti-pattern #16). Owns the
``alsactl store`` / systemd-unit invocation surface — invariant I7
("the daemon never writes ``/var/lib/alsa/asound.state`` directly")
+ Paranoid-QA HIGH #6 ("PATH-influenced subprocess hijack").

Contents:

* :data:`_SYSTEMD_PERSIST_UNIT` — name of the packaged
  ``sovyx-audio-mixer-persist.service`` systemd unit.
* :data:`_SYSTEMCTL_PATHS` + :data:`_ALSACTL_PATHS` — fixed
  canonical-path whitelists; ``$PATH`` is intentionally NOT
  consulted to remove the hijack surface (Paranoid-QA HIGH #6).
* :func:`_find_trusted_binary` — symlink-aware whitelist resolver
  with structured-log audit trail.
* :func:`default_persist_via_alsactl` — tries the systemd delegate
  first, falls back to direct ``alsactl store`` when the daemon has
  write access. Never raises; ``False`` returns map to a
  ``MIXER_SANITY_PERSIST_FAILED`` error token at the L2.5
  orchestrator boundary.

Anti-pattern #20 covered: parent module
``voice/health/_mixer_sanity.py`` re-exports every symbol so the
single-test-file at ``tests/unit/voice/health/test_mixer_sanity.py``
patches via ``patch.object(mod, "_find_trusted_binary", ...)``
intercept correctly via the parent's attribute lookup.
"""

from __future__ import annotations

import asyncio
import os
import stat
import subprocess  # noqa: S404 — fixed-argv subprocess to trusted alsa-utils binaries
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sovyx.engine.config import VoiceTuningConfig

logger = get_logger(__name__)


_SYSTEMD_PERSIST_UNIT = "sovyx-audio-mixer-persist.service"
"""systemd unit (ships under ``packaging/systemd/``) that runs
``alsactl store`` as root with a tight sandbox. Invoked on-demand by
the L2.5 orchestrator after a successful heal."""


# Absolute-path binaries — paranoid-QA HIGH #6. A daemon whose PATH is
# influenced by the unit's ``Environment=`` directive (common operator
# mistake: ``PATH=$HOME/bin:$PATH``) would otherwise let an attacker
# with write access to that dir plant a shim for ``systemctl`` or
# ``alsactl`` and hijack the one runtime bridge to root (invariant I7
# relies on systemctl being the genuine systemd client). Hardcoding
# canonical absolute paths — with graceful fallthrough when absent —
# removes that hijack surface.
_SYSTEMCTL_PATHS: tuple[str, ...] = (
    "/usr/bin/systemctl",
    "/bin/systemctl",
)
_ALSACTL_PATHS: tuple[str, ...] = (
    "/usr/sbin/alsactl",
    "/sbin/alsactl",
    "/usr/bin/alsactl",
)


def _find_trusted_binary(candidates: tuple[str, ...]) -> str | None:
    """Return the first canonical-path binary that exists on disk.

    Does NOT consult ``PATH``. Any path outside the whitelist is
    refused even when present. Returns ``None`` when no canonical
    location holds the binary — caller falls through to the next
    persistence strategy or returns ``False``.

    Threat model (Paranoid-QA R2 MEDIUM #1):

    The classic TOCTOU concern (check via stat, use via subprocess)
    does not apply here because the candidate paths — ``/usr/bin``,
    ``/sbin``, ``/bin``, ``/usr/sbin`` — are writable only by root.
    An attacker who can replace ``/usr/bin/systemctl`` between
    resolution and exec already has root and can bypass any check
    we layer on top. For that reason we don't use ``O_NOFOLLOW``
    + ``fexecve`` here (which would also break legitimate
    ``/usr/bin/systemctl -> /sbin/systemctl`` symlinks on
    Arch-derivatives).

    We DO log at DEBUG when a candidate resolves through a symlink
    so operator-driven audits can spot unexpected indirection. The
    final ``subprocess.run`` relies on the same canonical path, so
    a symlink pointing outside the whitelist is detectable via
    ``resolve()`` and skipped.
    """
    for path in candidates:
        p = Path(path)
        try:
            # ``lstat`` inspects the link itself, not the target.
            # ``is_file`` follows symlinks — we want to know both.
            lstat_info = p.lstat()
            if not p.is_file() or not os.access(p, os.X_OK):
                continue
            if stat.S_ISLNK(lstat_info.st_mode):
                # Resolve the target and confirm it's either in the
                # whitelist or under one of the canonical system bin
                # directories. ``/usr/bin/systemctl -> /sbin/systemctl``
                # is legitimate; ``/usr/bin/systemctl -> /tmp/attacker``
                # is not.
                try:
                    resolved = str(p.resolve(strict=True))
                except (OSError, RuntimeError):
                    # Broken symlink or loop — skip.
                    continue
                trusted_prefixes = (
                    "/usr/bin/",
                    "/usr/sbin/",
                    "/bin/",
                    "/sbin/",
                    "/usr/local/bin/",
                    "/usr/local/sbin/",
                )
                if not any(resolved.startswith(prefix) for prefix in trusted_prefixes):
                    logger.warning(
                        "mixer_sanity_trusted_binary_symlink_escapes_whitelist",
                        candidate=str(p),
                        resolved=resolved,
                        note="refusing — symlink target is outside canonical bin dirs",
                    )
                    continue
                logger.debug(
                    "mixer_sanity_trusted_binary_symlink_resolved",
                    candidate=str(p),
                    resolved=resolved,
                )
            return str(p)
        except OSError:
            continue
    return None


async def default_persist_via_alsactl(
    cards: Sequence[int],
    tuning: VoiceTuningConfig,
) -> bool:
    """Persist the current mixer state via ``alsactl store``.

    Tries two strategies in order (invariant I7 — the daemon never
    writes ``/var/lib/alsa/asound.state`` directly):

    1. **systemd delegate** — ``systemctl start --no-block
       sovyx-audio-mixer-persist.service``. This is the production
       path: the packaged unit runs ``alsactl store -f`` as root
       with the same capability-bounded sandbox as the runtime_pm
       oneshot. ``--no-block`` returns as soon as systemd accepts
       the start request; the actual store takes ~30 ms on a
       single-card laptop but we don't need to wait.
    2. **Direct alsactl fallback** — useful in containers / dev
       environments where the daemon runs as root AND the systemd
       unit isn't installed (``pipx install sovyx`` before
       ``sudo postinstall_admin.sh``). The daemon's own alsactl
       invocation succeeds when the process has write access to
       ``/var/lib/alsa/asound.state``; otherwise it logs and
       returns ``False``.

    Returns ``True`` when strategy (1) accepted the start request OR
    strategy (2) exited 0 for every card. ``False`` when neither
    strategy is available or both fail. Never raises.

    A ``False`` return is not fatal: the L2.5 orchestrator still
    reports ``HEALED`` with an ``error=MIXER_SANITY_PERSIST_FAILED``
    token — the preset lives in-memory until reboot and re-applies
    on the next boot cascade.

    ``cards`` is ignored by strategy (1) — ``alsactl store -f``
    persists every card in one call. Strategy (2) passes the list
    verbatim to preserve backward compatibility with the pre-
    systemd-delegate behaviour.
    """
    if sys.platform != "linux":
        return False
    # Paranoid-QA HIGH #6: resolve subprocess binaries via a fixed
    # canonical-path whitelist. ``shutil.which`` honours $PATH and
    # would let an operator's ill-configured unit-level
    # ``Environment=PATH=$HOME/bin:$PATH`` redirect ``systemctl`` to
    # an attacker-controlled shim.
    systemctl_path = _find_trusted_binary(_SYSTEMCTL_PATHS)
    # Paranoid-QA LOW: clamp subprocess timeout so a bad env override
    # (``SOVYX_TUNING__VOICE__LINUX_MIXER_SUBPROCESS_TIMEOUT_S=0``)
    # cannot DoS the persist path by timing out instantly, nor block
    # the event loop for minutes at the other extreme.
    timeout_s = max(0.5, min(tuning.linux_mixer_subprocess_timeout_s, 30.0))
    # Strategy 1: systemd delegate.
    if systemctl_path is not None:
        # ``start`` without ``--no-block`` so the real exit code of the
        # unit (success / failure during ExecStart) propagates to us.
        # The unit's ``TimeoutStartSec=5s`` bounds wall-clock at the
        # systemd side; our own ``timeout_s`` bounds us.
        argv_sd = [
            systemctl_path,
            "start",
            _SYSTEMD_PERSIST_UNIT,
        ]
        try:
            proc = await asyncio.to_thread(
                subprocess.run,  # noqa: S603 — fixed argv, no shell, timeout enforced
                argv_sd,
                capture_output=True,
                timeout=timeout_s,
                check=False,
                text=True,
                errors="replace",
            )
        except (subprocess.SubprocessError, OSError) as exc:
            logger.debug(
                "mixer_sanity_systemd_persist_subprocess_failed",
                detail=str(exc)[:200],
            )
        else:
            if proc.returncode == 0:
                logger.info(
                    "mixer_sanity_persist_delegated_to_systemd",
                    unit=_SYSTEMD_PERSIST_UNIT,
                )
                return True
            logger.debug(
                "mixer_sanity_systemd_persist_nonzero",
                returncode=proc.returncode,
                stderr=(proc.stderr or "").strip()[:200],
                note="unit probably not installed; falling back to direct alsactl",
            )

    # Strategy 2: direct alsactl — only works when daemon has write
    # access to /var/lib/alsa/asound.state (typically means running
    # as root, which is rare in Sovyx deployments).
    alsactl_path = _find_trusted_binary(_ALSACTL_PATHS)
    if alsactl_path is None:
        logger.debug("mixer_sanity_alsactl_missing")
        return False
    all_ok = True
    for card_index in cards:
        argv = [alsactl_path, "store", "-f", "-c", str(card_index)]
        try:
            proc = await asyncio.to_thread(
                subprocess.run,  # noqa: S603 — fixed argv, no shell, timeout enforced
                argv,
                capture_output=True,
                timeout=timeout_s,
                check=False,
                text=True,
                errors="replace",
            )
        except (subprocess.SubprocessError, OSError) as exc:
            logger.warning(
                "mixer_sanity_alsactl_store_subprocess_failed",
                card_index=card_index,
                detail=str(exc)[:200],
            )
            all_ok = False
            continue
        if proc.returncode != 0:
            logger.warning(
                "mixer_sanity_alsactl_store_nonzero",
                card_index=card_index,
                returncode=proc.returncode,
                stderr=(proc.stderr or "").strip()[:200],
            )
            all_ok = False
    return all_ok


__all__ = [
    "_ALSACTL_PATHS",
    "_SYSTEMCTL_PATHS",
    "_SYSTEMD_PERSIST_UNIT",
    "_find_trusted_binary",
    "default_persist_via_alsactl",
]
