#!/usr/bin/env python3
"""Summarise AWS Compute Optimizer rightsizing recommendations and their monthly savings.

Read-only. Queries EC2 instance, EBS volume, and Lambda function recommendations in one
region and prints them sorted by estimated monthly savings.

Example:
    python scripts/rightsizing_report.py --region us-east-1 --output markdown
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import add_common_args, die, make_session, next_token_pages, render  # noqa: E402

COLUMNS = (
    "resource_type",
    "resource_id",
    "finding",
    "current",
    "recommended",
    "monthly_savings",
    "savings_pct",
)
RESOURCE_TYPES = ("ec2", "ebs", "lambda")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    add_common_args(parser)
    parser.add_argument(
        "--resource-type",
        action="append",
        choices=RESOURCE_TYPES,
        help="Limit the report to one resource type. Repeatable. Default: all three.",
    )
    parser.add_argument(
        "--min-savings",
        type=float,
        default=0.0,
        help="Hide recommendations below this estimated monthly saving (default: 0)",
    )
    parser.add_argument(
        "--include-optimized",
        action="store_true",
        help="Include resources Compute Optimizer already considers optimized",
    )
    return parser.parse_args(argv)


def _best_option(options: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the recommendation option with the largest estimated monthly saving."""
    if not options:
        return None
    return max(
        options,
        key=lambda o: (
            o.get("savingsOpportunity", {}).get("estimatedMonthlySavings", {}).get("value", 0.0)
        ),
    )


def _savings(option: dict[str, Any]) -> tuple[float, float]:
    opportunity = option.get("savingsOpportunity", {})
    return (
        opportunity.get("estimatedMonthlySavings", {}).get("value", 0.0),
        opportunity.get("savingsOpportunityPercentage", 0.0),
    )


def collect_ec2(client: Any) -> list[dict[str, Any]]:
    rows = []
    for rec in next_token_pages(client.get_ec2_instance_recommendations, "instanceRecommendations"):
        option = _best_option(rec.get("recommendationOptions", []))
        if option is None:
            continue
        value, pct = _savings(option)
        rows.append(
            {
                "resource_type": "ec2",
                "resource_id": rec.get("instanceName") or rec["instanceArn"].rsplit("/", 1)[-1],
                "finding": rec.get("finding", ""),
                "current": rec.get("currentInstanceType", ""),
                "recommended": option.get("instanceType", ""),
                "monthly_savings": value,
                "savings_pct": pct,
            }
        )
    return rows


def _volume_desc(config: dict[str, Any]) -> str:
    return f"{config.get('volumeType', '?')} {config.get('volumeSize', '?')}GiB"


def collect_ebs(client: Any) -> list[dict[str, Any]]:
    rows = []
    for rec in next_token_pages(client.get_ebs_volume_recommendations, "volumeRecommendations"):
        option = _best_option(rec.get("volumeRecommendationOptions", []))
        if option is None:
            continue
        value, pct = _savings(option)
        current = rec.get("currentConfiguration", {})
        target = option.get("configuration", {})
        rows.append(
            {
                "resource_type": "ebs",
                "resource_id": rec["volumeArn"].rsplit("/", 1)[-1],
                "finding": rec.get("finding", ""),
                "current": _volume_desc(current),
                "recommended": _volume_desc(target),
                "monthly_savings": value,
                "savings_pct": pct,
            }
        )
    return rows


def collect_lambda(client: Any) -> list[dict[str, Any]]:
    rows = []
    for rec in next_token_pages(
        client.get_lambda_function_recommendations, "lambdaFunctionRecommendations"
    ):
        option = _best_option(rec.get("memorySizeRecommendationOptions", []))
        if option is None:
            continue
        value, pct = _savings(option)
        rows.append(
            {
                "resource_type": "lambda",
                "resource_id": rec["functionArn"].rsplit(":function:", 1)[-1],
                "finding": rec.get("finding", ""),
                "current": f"{rec.get('currentMemorySize', '?')}MB",
                "recommended": f"{option.get('memorySize', '?')}MB",
                "monthly_savings": value,
                "savings_pct": pct,
            }
        )
    return rows


COLLECTORS = {"ec2": collect_ec2, "ebs": collect_ebs, "lambda": collect_lambda}


def check_enrollment(client: Any) -> None:
    """Exit with an actionable message unless Compute Optimizer is enrolled and ready."""
    status = client.get_enrollment_status()
    if status.get("status") != "Active":
        die(
            f"Compute Optimizer enrollment status is {status.get('status', 'unknown')!r} "
            f"({status.get('statusReason', 'no reason given')}). "
            "Enable it with: aws compute-optimizer update-enrollment-status --status Active"
        )


def build_report(
    client: Any, resource_types: list[str], min_savings: float, include_optimized: bool
) -> list[dict[str, Any]]:
    """Collect, filter, and sort recommendations across the requested resource types."""
    rows: list[dict[str, Any]] = []
    for resource_type in resource_types:
        rows.extend(COLLECTORS[resource_type](client))
    if not include_optimized:
        rows = [r for r in rows if r["finding"] not in ("Optimized", "NotOptimized_Skipped")]
    rows = [r for r in rows if r["monthly_savings"] >= min_savings]
    return sorted(rows, key=lambda r: r["monthly_savings"], reverse=True)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    client = make_session(args).client("compute-optimizer")
    check_enrollment(client)

    rows = build_report(
        client, args.resource_type or list(RESOURCE_TYPES), args.min_savings, args.include_optimized
    )
    if not rows:
        print("No rightsizing recommendations matched the filters.")
        return 0

    print(render(rows, COLUMNS, args.output))
    if args.output in ("table", "markdown"):
        total = sum(r["monthly_savings"] for r in rows)
        print(f"\nEstimated monthly savings across {len(rows)} recommendations: {total:,.2f} USD")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
