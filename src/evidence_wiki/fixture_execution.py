"""Closed execution-window helpers for explicitly selected reference fixtures.

These helpers retain the original frozen policy and window. They do not grant
production authority or extend an existing run.
"""

from __future__ import annotations


def fixture_authority(value, frozen_at, expires_at, *, current_utc=None):
    """Bind initial fixture issuance to the existing frozen execution owner."""
    from ._native_owner_execution import fixture_authority as owner

    return owner(value, frozen_at, expires_at, current_utc=current_utc)


def validate_fixture_authority(value, *, current_utc=None):
    """Recheck retained fixture issuance without renewing its original window."""
    from ._native_owner_execution import validate_fixture_authority as owner

    return owner(value, current_utc=current_utc)
