#!/usr/bin/env python3
"""Regenerate examples/output/ from the synthetic fixtures.

The committed sample output is produced by the same code paths the scripts use, against
the invented fixtures in tests/fixtures.py. Nothing here touches a real AWS account, so
the files are safe to commit and reproducible with `make samples`.
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import boto3
from botocore.stub import Stubber

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import fixtures  # noqa: E402
import instance_scheduler as scheduler  # noqa: E402
import rightsizing_report  # noqa: E402
import savings_coverage_report  # noqa: E402
from common import render  # noqa: E402

OUTPUT_DIR = ROOT / "examples" / "output"
SAMPLE_DATE = date(2026, 8, 8)


def _client(service: str) -> tuple[object, Stubber]:
    session = boto3.Session(
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        aws_session_token="testing",
        region_name="us-east-1",
    )
    client = session.client(service)
    stubber = Stubber(client)
    stubber.activate()
    return client, stubber


def sample_rightsizing() -> str:
    client, stubber = _client("compute-optimizer")
    stubber.add_response("get_ec2_instance_recommendations", fixtures.EC2_RECOMMENDATIONS, {})
    stubber.add_response("get_ebs_volume_recommendations", fixtures.EBS_RECOMMENDATIONS, {})
    stubber.add_response("get_lambda_function_recommendations", fixtures.LAMBDA_RECOMMENDATIONS, {})

    rows = rightsizing_report.build_report(client, ["ec2", "ebs", "lambda"], 0.0, False)
    total = sum(r["monthly_savings"] for r in rows)
    body = render(rows, rightsizing_report.COLUMNS, "markdown")
    return (
        "$ python scripts/rightsizing_report.py --region us-east-1 --output markdown\n\n"
        f"{body}\n\n"
        f"Estimated monthly savings across {len(rows)} recommendations: {total:,.2f} USD\n"
    )


def sample_savings_coverage() -> str:
    client, stubber = _client("ce")
    period = savings_coverage_report.time_period(30, SAMPLE_DATE)
    stubber.add_response(
        "get_savings_plans_utilization",
        fixtures.SP_UTILIZATION,
        {"TimePeriod": period, "Granularity": "MONTHLY"},
    )
    stubber.add_response(
        "get_savings_plans_coverage",
        fixtures.SP_COVERAGE,
        {"TimePeriod": period, "Granularity": "MONTHLY"},
    )
    stubber.add_response(
        "get_savings_plans_purchase_recommendation",
        fixtures.SP_PURCHASE_RECOMMENDATION,
        {
            "SavingsPlansType": "COMPUTE_SP",
            "TermInYears": "ONE_YEAR",
            "PaymentOption": "NO_UPFRONT",
            "LookbackPeriodInDays": "THIRTY_DAYS",
        },
    )

    args = savings_coverage_report.parse_args(["--output", "markdown"])
    rows = savings_coverage_report.build_report(client, args, SAMPLE_DATE)
    body = render(rows, savings_coverage_report.COLUMNS, "markdown")
    return f"$ python scripts/savings_coverage_report.py --days 30 --output markdown\n\n{body}\n"


def sample_scheduler() -> str:
    client, stubber = _client("ec2")
    stubber.add_response("describe_instances", fixtures.DESCRIBE_INSTANCES)

    # Saturday, so the Mon-Fri window is closed and everything should be stopped.
    saturday = datetime(2026, 8, 8, 12, 0, tzinfo=ZoneInfo("America/New_York"))
    target = scheduler.desired_state(scheduler.parse_schedule("mon-fri 08:00-18:00"), saturday)
    rows = scheduler.plan(scheduler.find_instances(client, "Schedule", "office-hours"), target)
    changes = [r for r in rows if r["action"] != "none"]
    body = render(rows, scheduler.COLUMNS, "markdown")
    return (
        '$ python scripts/instance_scheduler.py --schedule "mon-fri 08:00-18:00" \\\n'
        "      --timezone America/New_York --output markdown\n"
        f"# evaluated at Saturday 2026-08-08 12:00 America/New_York\n\n"
        f"{body}\n\n"
        f"Dry run: {len(changes)} instances would change state. Re-run with --apply to enforce.\n"
    )


SAMPLES = {
    "rightsizing-report.md": sample_rightsizing,
    "savings-coverage-report.md": sample_savings_coverage,
    "instance-scheduler-dry-run.md": sample_scheduler,
}


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for filename, builder in SAMPLES.items():
        path = OUTPUT_DIR / filename
        path.write_text(builder())
        print(f"wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
