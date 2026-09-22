"""Explicit-clock schedule evaluation; returned actions are inert descriptors."""

from __future__ import annotations

import calendar
import hashlib
import io
import re
from datetime import datetime, timedelta, timezone
from importlib import resources
from zoneinfo import ZoneInfo

from _computation_expression import ComputationInvalid, require
from _evidence_revision import content_id

UNITS = {"seconds": 1, "minutes": 60, "hours": 3600, "days": 86400}


def zone_identity(name):
    require(type(name) is str and re.fullmatch(r"[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*", name) is not None,
            "computation_timezone_invalid")
    if name == "UTC":
        return timezone.utc, {"name": "UTC", "provider": "fixed-offset", "sha256": hashlib.sha256(b"UTC+00:00").hexdigest()}
    try:
        # A single bundled data source makes OS zone databases irrelevant.
        import tzdata
        data = resources.files("tzdata.zoneinfo").joinpath(*name.split("/")).read_bytes()
        require(0 < len(data) <= 131_072, "computation_timezone_bound")
        zone = ZoneInfo.from_file(io.BytesIO(data), key=name)
        return zone, {"name": name, "provider": "tzdata:" + tzdata.__version__, "sha256": hashlib.sha256(data).hexdigest()}
    except (ImportError, ValueError, OSError):
        raise ComputationInvalid("computation_timezone_unavailable") from None


def parsed_time(value, *, aware):
    require(type(value) is str and 10 <= len(value) <= 40, "computation_clock_invalid")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ComputationInvalid("computation_clock_invalid") from None
    require(result.microsecond == 0 and (not aware or result.tzinfo is not None), "computation_clock_offset_required")
    return result


def local_instant(value, zone, policy):
    naive = parsed_time(value, aware=False) if type(value) is str else value
    if naive.tzinfo is not None:
        return naive.astimezone(timezone.utc)
    options = sorted({instant for fold in (0, 1)
                      if (instant := naive.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc))
                      .astimezone(zone).replace(tzinfo=None) == naive})
    if not options:
        require(policy["nonexistent"] == "skip", "computation_clock_nonexistent")
        return None
    if len(options) == 2:
        require(policy["ambiguous"] != "refuse", "computation_clock_ambiguous")
    return options[-1] if policy["ambiguous"] == "later" else options[0]


def cron_fields(expression):
    require(type(expression) is str and len(expression) <= 256, "computation_cron_invalid")
    fields = expression.split()
    require(len(fields) == 5, "computation_cron_invalid")
    result = []
    for text, lower, upper in zip(fields, (0, 0, 1, 1, 0), (59, 23, 31, 12, 6), strict=True):
        values = set()
        parts = text.split(",")
        require(len(parts) <= 32, "computation_cron_bound")
        for part in parts:
            match = re.fullmatch(r"(\*|[0-9]{1,2}(?:-[0-9]{1,2})?)(?:/([0-9]{1,2}))?", part)
            require(match is not None, "computation_cron_invalid")
            term, step = match.group(1), int(match.group(2) or 1)
            require(match.group(2) is None or term == "*" or "-" in term, "computation_cron_step_requires_range")
            require(1 <= step <= upper - lower + 1, "computation_cron_invalid")
            start, end = (lower, upper) if term == "*" else tuple(map(int, term.split("-"))) if "-" in term else (int(term), int(term))
            require(lower <= start <= end <= upper, "computation_cron_invalid")
            values.update(range(start, end + 1, step))
        result.append(sorted(values))
    return result


def validate_clock(definition):
    clock = definition["clock"]
    zone, _identity = zone_identity(clock["timezone"])
    if clock["as_of"] is not None:
        parsed_time(clock["as_of"], aware=True)
    for item in definition["cadence"].values():
        trigger = item["trigger"]
        if trigger["type"] == "cron":
            cron_fields(trigger["expression"])
        elif trigger["type"] in {"fixed_date", "elapsed_units"}:
            local_instant(trigger["at"] if trigger["type"] == "fixed_date" else trigger["anchor"], zone, clock)


def cron_neighbors(trigger, now, zone, clock, budget):
    minutes, hours, days, months, weekdays = cron_fields(trigger["expression"])
    today = now.astimezone(zone).date()
    previous = following = None
    for distance in range(clock["search_days"] + 1):
        for direction in ([0] if distance == 0 else [-1, 1]):
            if direction < 0 and previous is not None or direction > 0 and following is not None:
                continue
            budget.tick()
            try:
                day = today + timedelta(days=direction * distance)
            except OverflowError:
                continue
            day_match, weekday_match = day.day in days, (day.weekday() + 1) % 7 in weekdays
            if trigger["day_match"] == "and":
                matches = day_match and weekday_match
            elif len(days) == 31:
                matches = weekday_match
            elif len(weekdays) == 7:
                matches = day_match
            else:
                matches = day_match or weekday_match
            if day.month not in months or not matches:
                continue
            for hour in hours:
                for minute in minutes:
                    budget.tick()
                    instant = local_instant(datetime(day.year, day.month, day.day, hour, minute), zone, clock)
                    if instant is None:
                        continue
                    if instant <= now and (previous is None or previous < instant):
                        previous = instant
                    if now < instant and (following is None or instant < following):
                        following = instant
        if previous is not None and following is not None:
            break
    return previous, following


def elapsed_neighbors(trigger, now, zone, clock):
    anchor = local_instant(trigger["anchor"], zone, clock)
    if anchor is None:
        return None, None
    if now < anchor:
        return None, anchor
    unit, amount = trigger["unit"], trigger["amount"]
    if unit in UNITS:
        interval = amount * UNITS[unit]
        delta = now - anchor
        count = (delta.days * 86400 + delta.seconds) // interval
        return anchor + timedelta(seconds=count * interval), anchor + timedelta(seconds=(count + 1) * interval)
    months = amount * (12 if unit == "years" else 1)
    start, current = anchor.astimezone(zone), now.astimezone(zone)
    count = max(0, ((current.year - start.year) * 12 + current.month - start.month) // months)
    def occurrence(index):
        total = start.year * 12 + start.month - 1 + index * months
        year, month_index = divmod(total, 12)
        require(1 <= year <= 9999, "computation_calendar_bound")
        month = month_index + 1
        last = calendar.monthrange(year, month)[1]
        require(start.day <= last or trigger["calendar_policy"] == "clamp", "computation_calendar_day_invalid")
        naive = start.replace(tzinfo=None, year=year, month=month, day=min(start.day, last))
        return local_instant(naive, zone, clock)
    previous = occurrence(count)
    if previous is None:
        return None, None
    if previous > now:
        count -= 1
        previous = occurrence(count) if count >= 0 else None
    return previous, occurrence(count + 1)


def evaluate_schedules(definition, as_of, evaluator, resolve_inputs, input_id, *, completed=frozenset()):
    clock = definition["clock"]
    effective = as_of if as_of is not None else clock["as_of"]
    require(effective is not None or not definition["cadence"], "computation_clock_required")
    zone, identity = zone_identity(clock["timezone"])
    now = parsed_time(effective, aware=True).astimezone(timezone.utc) if effective is not None else None
    policy_id = content_id("computation-schedule-policy/v1", {**definition, "clock": {**clock, "as_of": None}})
    rows = []
    for key, item in sorted(definition["cadence"].items()):
        trigger, previous, following = item["trigger"], None, None
        lineage, condition = [], None
        if trigger["type"] == "condition":
            result = evaluator.evaluate(trigger["expression"], resolve_inputs(trigger["inputs"]))
            require(type(result.value) is bool, "computation_boolean_required")
            condition, lineage = result.value, sorted(result.lineage)
            previous = now if condition else None
        elif trigger["type"] == "cron":
            previous, following = cron_neighbors(trigger, now, zone, clock, evaluator.budget)
        elif trigger["type"] == "elapsed_units":
            previous, following = elapsed_neighbors(trigger, now, zone, clock)
        else:
            instant = local_instant(trigger["at"], zone, clock)
            if instant is not None:
                previous, following = (instant, None) if instant <= now else (None, instant)
        due = previous is not None
        occurrence = content_id("computation-occurrence/v1", {"policy": policy_id, "cadence": key,
            "basis": input_id if trigger["type"] == "condition" else previous.isoformat()}) if due else None
        state = "completed" if occurrence in completed else "due" if previous == now and due else "overdue" if due else "pending" if following else "inactive" if condition is False else "unknown"
        alerts = []
        if following is not None:
            delta = following - now
            seconds = delta.days * 86400 + delta.seconds
            alerts = [alert for alert in item["lead_alerts"] if seconds <= alert["amount"] * UNITS[alert["unit"]]]
        rows.append({"id": key, "state": state, "due_at": previous.isoformat() if previous else None,
                     "next_at": following.isoformat() if following else None, "lead_alerts": alerts,
                     "occurrence_id": occurrence, "action": item["action"], "lineage": lineage,
                     "condition": condition, "catch_up": "latest-occurrence-only"})
    return {"as_of": now.isoformat() if now else None, "timezone": identity, "schedules": rows}
