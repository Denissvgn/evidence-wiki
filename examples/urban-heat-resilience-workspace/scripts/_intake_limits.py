#!/usr/bin/env python3
"""Shared intake budget helpers for workspace scripts."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DEFAULT_MAX_OPEN_QUESTIONS_TOTAL = 250
DEFAULT_MAX_INTAKE_PER_HOUR = 25
DEFAULT_MAX_MCP_INTAKE_BATCH_QUESTIONS = 100
INTAKE_WINDOW_SECONDS = 3600

_CREATED_AT_RE = re.compile(r"^- Created at: (?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\.\s*$")
_ADDED_RE = re.compile(r"^- Added: (?P<count>\d+) open question page\(s\)")


def positive_int_config(config: dict[str, Any], field: str, default: int) -> int:
    run_config = config.get("run") if isinstance(config.get("run"), dict) else {}
    value = run_config.get(field, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return default
    return value


def mcp_intake_batch_limit(config: dict[str, Any]) -> int:
    return positive_int_config(
        config,
        "max_mcp_intake_batch_questions",
        DEFAULT_MAX_MCP_INTAKE_BATCH_QUESTIONS,
    )


def timestamp_utc(moment: datetime | None = None) -> str:
    value = moment or datetime.now(timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc)


def _iter_intake_blocks(text: str) -> list[list[str]]:
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            if current is not None:
                blocks.append(current)
            current = [line] if "intake | Injected question batch" in line else None
            continue
        if current is not None:
            current.append(line)
    if current is not None:
        blocks.append(current)
    return blocks


def intake_events(log_path: Path) -> list[dict[str, Any]]:
    if not log_path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for block in _iter_intake_blocks(log_path.read_text(encoding="utf-8", errors="ignore")):
        created_at: datetime | None = None
        added = 0
        for line in block:
            created_match = _CREATED_AT_RE.match(line.strip())
            if created_match:
                created_at = parse_timestamp(created_match.group("timestamp"))
                continue
            added_match = _ADDED_RE.match(line.strip())
            if added_match:
                added = int(added_match.group("count"))
        if created_at is None:
            continue
        events.append({"created_at": created_at, "questions_created": added})
    return events


def recent_intake_summary(
    log_path: Path,
    *,
    now: datetime | None = None,
    window_seconds: int = INTAKE_WINDOW_SECONDS,
) -> dict[str, Any]:
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = current_time - timedelta(seconds=window_seconds)
    events = intake_events(log_path)
    recent = [
        event
        for event in events
        if cutoff <= event["created_at"].astimezone(timezone.utc) <= current_time
    ]
    last_event = max(events, key=lambda event: event["created_at"], default=None)
    return {
        "batches_last_hour": len(recent),
        "questions_created_last_hour": sum(int(event["questions_created"]) for event in recent),
        "window_seconds": window_seconds,
        "last_intake_at": timestamp_utc(last_event["created_at"]) if last_event else None,
    }
