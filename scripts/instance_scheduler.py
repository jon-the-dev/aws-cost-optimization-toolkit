#!/usr/bin/env python3
"""Start and stop tagged EC2 instances on an office-hours schedule.

Non-production instances that only get used during working hours are the cheapest
savings available: stopping them nights and weekends removes roughly 70% of their
instance-hours. This script drives that from a tag.

Dry run is the default. Nothing starts or stops without --apply.

Example:
    # what would happen right now, given a Mon-Fri 08:00-18:00 New York schedule
    python scripts/instance_scheduler.py --schedule "mon-fri 08:00-18:00" \
        --timezone America/New_York

    # actually enforce it
    python scripts/instance_scheduler.py --schedule "mon-fri 08:00-18:00" \
        --timezone America/New_York --apply
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import add_common_args, die, make_session, render  # noqa: E402

COLUMNS = ("instance_id", "name", "state", "desired", "action")
DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
ACTIONABLE_STATES = {"running", "stopped"}


@dataclass(frozen=True)
class Schedule:
    """A weekly running window: which weekdays, and the start/end times on those days."""

    days: frozenset[int]
    start: time
    end: time


def parse_schedule(spec: str) -> Schedule:
    """Parse a schedule string such as ``mon-fri 08:00-18:00``.

    Days accept a single day (``sat``), a range (``mon-fri``), or a comma list
    (``mon,wed,fri``). Times are 24-hour ``HH:MM``.

    Raises:
        ValueError: If the spec is malformed or the window does not close on the same day.
    """
    parts = spec.strip().lower().split()
    if len(parts) != 2:
        raise ValueError(f"schedule must look like 'mon-fri 08:00-18:00', got {spec!r}")
    day_spec, time_spec = parts

    days: set[int] = set()
    for chunk in day_spec.split(","):
        if "-" in chunk:
            first, last = chunk.split("-", 1)
            days.update(_day_range(_day_index(first), _day_index(last)))
        else:
            days.add(_day_index(chunk))

    start_text, _, end_text = time_spec.partition("-")
    if not end_text:
        raise ValueError(f"time window must look like '08:00-18:00', got {time_spec!r}")
    start, end = _parse_time(start_text), _parse_time(end_text)
    if end <= start:
        raise ValueError(
            f"window {time_spec!r} ends at or before it starts; "
            "overnight windows are not supported. "
            "Split them into two schedules, one per calendar day."
        )
    return Schedule(frozenset(days), start, end)


def _day_index(name: str) -> int:
    try:
        return DAY_NAMES.index(name.strip()[:3])
    except ValueError:
        raise ValueError(f"unknown day {name!r}; expected one of {', '.join(DAY_NAMES)}") from None


def _day_range(first: int, last: int) -> list[int]:
    return (
        list(range(first, last + 1))
        if first <= last
        else list(range(first, 7)) + list(range(0, last + 1))
    )


def _parse_time(text: str) -> time:
    try:
        hour, minute = (int(p) for p in text.strip().split(":", 1))
        return time(hour, minute)
    except ValueError:
        raise ValueError(f"invalid time {text!r}; expected 24-hour HH:MM") from None


def desired_state(schedule: Schedule, now: datetime) -> str:
    """Return ``running`` if ``now`` falls inside the schedule window, else ``stopped``."""
    if now.weekday() not in schedule.days:
        return "stopped"
    return "running" if schedule.start <= now.time() < schedule.end else "stopped"


def find_instances(ec2: Any, tag_key: str, tag_value: str) -> list[dict[str, Any]]:
    """Return tagged instances that are in a state we can act on."""
    instances = []
    paginator = ec2.get_paginator("describe_instances")
    filters = [
        {"Name": f"tag:{tag_key}", "Values": [tag_value]},
        {"Name": "instance-state-name", "Values": sorted(ACTIONABLE_STATES)},
    ]
    for page in paginator.paginate(Filters=filters):
        for reservation in page.get("Reservations", []):
            for instance in reservation.get("Instances", []):
                tags = {t["Key"]: t["Value"] for t in instance.get("Tags", [])}
                instances.append(
                    {
                        "instance_id": instance["InstanceId"],
                        "name": tags.get("Name", ""),
                        "state": instance["State"]["Name"],
                    }
                )
    return sorted(instances, key=lambda i: i["instance_id"])


def plan(instances: list[dict[str, Any]], target: str) -> list[dict[str, Any]]:
    """Annotate each instance with its desired state and the action needed to get there."""
    rows = []
    for instance in instances:
        action = (
            "none" if instance["state"] == target else ("start" if target == "running" else "stop")
        )
        rows.append({**instance, "desired": target, "action": action})
    return rows


def apply_plan(ec2: Any, rows: list[dict[str, Any]]) -> None:
    """Issue the start/stop calls for every row whose action is not ``none``."""
    to_start = [r["instance_id"] for r in rows if r["action"] == "start"]
    to_stop = [r["instance_id"] for r in rows if r["action"] == "stop"]
    if to_start:
        ec2.start_instances(InstanceIds=to_start)
    if to_stop:
        ec2.stop_instances(InstanceIds=to_stop)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    add_common_args(parser)
    parser.add_argument(
        "--tag-key", default="Schedule", help="Tag key that opts an instance in (default: Schedule)"
    )
    parser.add_argument(
        "--tag-value", default="office-hours", help="Tag value to match (default: office-hours)"
    )
    parser.add_argument(
        "--schedule",
        default="mon-fri 08:00-18:00",
        help="Running window, e.g. 'mon-fri 08:00-18:00' (default: %(default)s)",
    )
    parser.add_argument(
        "--timezone", default="UTC", help="IANA timezone the schedule is written in (default: UTC)"
    )
    parser.add_argument(
        "--force",
        choices=("start", "stop"),
        help="Ignore the schedule and drive every matched instance to this state",
    )
    parser.add_argument(
        "--apply", action="store_true", help="Actually start/stop instances (default: dry run)"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.force:
        target = "running" if args.force == "start" else "stopped"
    else:
        try:
            schedule = parse_schedule(args.schedule)
            tz = ZoneInfo(args.timezone)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            die(str(exc))
        target = desired_state(schedule, datetime.now(tz))

    ec2 = make_session(args).client("ec2")
    rows = plan(find_instances(ec2, args.tag_key, args.tag_value), target)
    if not rows:
        print(
            f"No instances found with tag {args.tag_key}={args.tag_value} "
            "in a startable/stoppable state."
        )
        return 0

    print(render(rows, COLUMNS, args.output))
    changes = [r for r in rows if r["action"] != "none"]
    if not changes:
        print(f"\nAll {len(rows)} instances already {target}.")
        return 0

    if args.apply:
        apply_plan(ec2, rows)
        print(f"\nApplied {len(changes)} state changes.")
    else:
        print(
            f"\nDry run: {len(changes)} instances would change state. "
            "Re-run with --apply to enforce."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
