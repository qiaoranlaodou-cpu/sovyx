"""Sovyx Dashboard — static file serving and API routes."""

from pathlib import Path

from sovyx.dashboard._integrity import (
    BundleIntegrityReport,
    BundleVerdict,
    scan_bundle_integrity,
)

STATIC_DIR = Path(__file__).parent / "static"

__all__ = [
    "BundleIntegrityReport",
    "BundleVerdict",
    "STATIC_DIR",
    "scan_bundle_integrity",
]
