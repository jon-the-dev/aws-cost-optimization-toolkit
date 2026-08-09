from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

import fixtures
import instance_scheduler as scheduler
import pytest

NY = ZoneInfo("America/New_York")
OFFICE_HOURS = scheduler.parse_schedule("mon-fri 08:00-18:00")


def test_parse_schedule_expands_a_day_range():
    schedule = scheduler.parse_schedule("mon-fri 08:00-18:00")

    assert schedule.days == frozenset({0, 1, 2, 3, 4})
    assert schedule.start == time(8, 0)
    assert schedule.end == time(18, 0)


def test_parse_schedule_accepts_a_comma_list_and_a_single_day():
    assert scheduler.parse_schedule("mon,wed,fri 09:30-17:15").days == frozenset({0, 2, 4})
    assert scheduler.parse_schedule("sat 10:00-14:00").days == frozenset({5})


def test_parse_schedule_wraps_a_range_across_the_weekend():
    assert scheduler.parse_schedule("fri-mon 08:00-18:00").days == frozenset({4, 5, 6, 0})


@pytest.mark.parametrize(
    ("spec", "fragment"),
    [
        ("mon-fri", "mon-fri 08:00-18:00"),
        ("funday 08:00-18:00", "unknown day"),
        ("mon-fri 0800-1800", "24-hour HH:MM"),
        ("mon-fri 08:00", "08:00-18:00"),
        ("mon-fri 18:00-08:00", "overnight windows are not supported"),
        ("mon-fri 09:00-09:00", "overnight windows are not supported"),
    ],
)
def test_parse_schedule_rejects_bad_specs_with_a_useful_message(spec, fragment):
    with pytest.raises(ValueError, match=fragment):
        scheduler.parse_schedule(spec)


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        (datetime(2026, 8, 5, 12, 0, tzinfo=NY), "running"),  # Wednesday midday
        (datetime(2026, 8, 5, 7, 59, tzinfo=NY), "stopped"),  # just before the window
        (datetime(2026, 8, 5, 8, 0, tzinfo=NY), "running"),  # boundary: start is inclusive
        (datetime(2026, 8, 5, 18, 0, tzinfo=NY), "stopped"),  # boundary: end is exclusive
        (datetime(2026, 8, 8, 12, 0, tzinfo=NY), "stopped"),  # Saturday
        (datetime(2026, 8, 9, 12, 0, tzinfo=NY), "stopped"),  # Sunday
    ],
)
def test_desired_state_at_the_window_boundaries(when, expected):
    assert scheduler.desired_state(OFFICE_HOURS, when) == expected


def test_plan_marks_only_the_instances_that_need_to_change():
    instances = [
        {"instance_id": "i-1", "name": "a", "state": "running"},
        {"instance_id": "i-2", "name": "b", "state": "stopped"},
    ]

    rows = scheduler.plan(instances, "running")

    assert [r["action"] for r in rows] == ["none", "start"]


def test_plan_stopping_inverts_the_actions():
    instances = [
        {"instance_id": "i-1", "name": "a", "state": "running"},
        {"instance_id": "i-2", "name": "b", "state": "stopped"},
    ]

    rows = scheduler.plan(instances, "stopped")

    assert [r["action"] for r in rows] == ["stop", "none"]


def test_find_instances_filters_on_the_tag_and_actionable_states(stubbed):
    client, stubber = stubbed("ec2")
    stubber.add_response(
        "describe_instances",
        fixtures.DESCRIBE_INSTANCES,
        {
            "Filters": [
                {"Name": "tag:Schedule", "Values": ["office-hours"]},
                {"Name": "instance-state-name", "Values": ["running", "stopped"]},
            ]
        },
    )

    instances = scheduler.find_instances(client, "Schedule", "office-hours")

    stubber.assert_no_pending_responses()
    assert [i["instance_id"] for i in instances] == [
        "i-055ee66ff77aa88bb",
        "i-099cc00dd11ee22ff",
        "i-0aa11bb22cc33dd44",
    ]


def test_find_instances_tolerates_an_instance_with_no_name_tag(stubbed):
    client, stubber = stubbed("ec2")
    stubber.add_response("describe_instances", fixtures.DESCRIBE_INSTANCES)

    by_id = {
        i["instance_id"]: i for i in scheduler.find_instances(client, "Schedule", "office-hours")
    }

    assert by_id["i-099cc00dd11ee22ff"]["name"] == ""


def test_find_instances_returns_empty_when_nothing_is_tagged(stubbed):
    client, stubber = stubbed("ec2")
    stubber.add_response("describe_instances", {"Reservations": []})

    assert scheduler.find_instances(client, "Schedule", "office-hours") == []


def test_apply_plan_batches_starts_and_stops(stubbed):
    client, stubber = stubbed("ec2")
    stubber.add_response(
        "start_instances", {"StartingInstances": []}, {"InstanceIds": ["i-1", "i-3"]}
    )
    stubber.add_response("stop_instances", {"StoppingInstances": []}, {"InstanceIds": ["i-2"]})

    scheduler.apply_plan(
        client,
        [
            {"instance_id": "i-1", "action": "start"},
            {"instance_id": "i-2", "action": "stop"},
            {"instance_id": "i-3", "action": "start"},
            {"instance_id": "i-4", "action": "none"},
        ],
    )

    stubber.assert_no_pending_responses()


def test_apply_plan_makes_no_call_when_nothing_changes(stubbed):
    client, stubber = stubbed("ec2")

    scheduler.apply_plan(client, [{"instance_id": "i-1", "action": "none"}])

    stubber.assert_no_pending_responses()


def test_dry_run_is_the_default_and_apply_is_opt_in():
    assert scheduler.parse_args([]).apply is False
    assert scheduler.parse_args(["--apply"]).apply is True
