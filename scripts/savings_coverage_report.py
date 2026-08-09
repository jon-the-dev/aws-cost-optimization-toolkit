#!/usr/bin/env python3
"""Report Savings Plans utilization, coverage, and the current purchase recommendation.

Read-only. Answers three questions in one pass:

1. Are the Savings Plans we already own being used? (utilization)
2. How much compute spend is still on-demand? (coverage)
3. What would AWS have us commit to next? (purchase recommendation)

Cost Explorer bills roughly 0.01 USD per API request, so each run of this script costs a
few cents. Cost Explorer is a global service with a us-east-1 endpoint; the client below
pins that region regardless of --region.

Example:
    python scripts/savings_coverage_report.py --days 30 --output markdown
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import add_common_args, make_session, render  # noqa: E402

CE_REGION = "us-east-1"
COLUMNS = ("section", "metric", "value")
TERMS = ("ONE_YEAR", "THREE_YEARS")
PAYMENT_OPTIONS = ("NO_UPFRONT", "PARTIAL_UPFRONT", "ALL_UPFRONT")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    add_common_args(parser)
    parser.add_argument(
        "--days", type=int, default=30, help="Lookback window in days (default: 30)"
    )
    parser.add_argument(
        "--term",
        choices=TERMS,
        default="ONE_YEAR",
        help="Savings Plan term to price (default: ONE_YEAR)",
    )
    parser.add_argument(
        "--payment-option",
        choices=PAYMENT_OPTIONS,
        default="NO_UPFRONT",
        help="Savings Plan payment option to price (default: NO_UPFRONT)",
    )
    parser.add_argument(
        "--skip-recommendation",
        action="store_true",
        help="Skip the purchase recommendation call (saves one Cost Explorer request)",
    )
    args = parser.parse_args(argv)
    if args.days < 1:
        parser.error("--days must be at least 1")
    return args


def time_period(days: int, today: date) -> dict[str, str]:
    """Return an inclusive-start, exclusive-end Cost Explorer time period ending today."""
    return {"Start": (today - timedelta(days=days)).isoformat(), "End": today.isoformat()}


def lookback_period(days: int) -> str:
    """Map a day count onto the three lookback windows Cost Explorer accepts."""
    if days <= 7:
        return "SEVEN_DAYS"
    return "THIRTY_DAYS" if days <= 30 else "SIXTY_DAYS"


def _money(value: str | None) -> float:
    return round(float(value or 0.0), 2)


def utilization_rows(response: dict[str, Any]) -> list[dict[str, Any]]:
    total = response.get("Total") or {}
    utilization = total.get("Utilization") or {}
    savings = total.get("Savings") or {}
    if not utilization:
        return [
            {"section": "utilization", "metric": "savings_plans", "value": "none active in window"}
        ]
    return [
        {
            "section": "utilization",
            "metric": "total_commitment",
            "value": _money(utilization.get("TotalCommitment")),
        },
        {
            "section": "utilization",
            "metric": "used_commitment",
            "value": _money(utilization.get("UsedCommitment")),
        },
        {
            "section": "utilization",
            "metric": "unused_commitment",
            "value": _money(utilization.get("UnusedCommitment")),
        },
        {
            "section": "utilization",
            "metric": "utilization_pct",
            "value": _money(utilization.get("UtilizationPercentage")),
        },
        {
            "section": "utilization",
            "metric": "net_savings",
            "value": _money(savings.get("NetSavings")),
        },
    ]


def coverage_rows(response: dict[str, Any]) -> list[dict[str, Any]]:
    coverages = response.get("SavingsPlansCoverages") or []
    if not coverages:
        return [
            {
                "section": "coverage",
                "metric": "eligible_spend",
                "value": "no eligible compute spend in window",
            }
        ]
    covered = sum(
        _money(c.get("Coverage", {}).get("SpendCoveredBySavingsPlans")) for c in coverages
    )
    on_demand = sum(_money(c.get("Coverage", {}).get("OnDemandCost")) for c in coverages)
    total = covered + on_demand
    return [
        {"section": "coverage", "metric": "covered_spend", "value": round(covered, 2)},
        {"section": "coverage", "metric": "on_demand_spend", "value": round(on_demand, 2)},
        {
            "section": "coverage",
            "metric": "coverage_pct",
            "value": round(covered / total * 100, 2) if total else 0.0,
        },
    ]


def recommendation_rows(response: dict[str, Any]) -> list[dict[str, Any]]:
    summary = (response.get("SavingsPlansPurchaseRecommendation") or {}).get(
        "SavingsPlansPurchaseRecommendationSummary"
    ) or {}
    if not summary:
        return [
            {
                "section": "recommendation",
                "metric": "purchase",
                "value": "none returned for these parameters",
            }
        ]
    return [
        {
            "section": "recommendation",
            "metric": "hourly_commitment",
            "value": _money(summary.get("HourlyCommitmentToPurchase")),
        },
        {
            "section": "recommendation",
            "metric": "current_on_demand_spend",
            "value": _money(summary.get("CurrentOnDemandSpend")),
        },
        {
            "section": "recommendation",
            "metric": "est_monthly_savings",
            "value": _money(summary.get("EstimatedMonthlySavingsAmount")),
        },
        {
            "section": "recommendation",
            "metric": "est_savings_pct",
            "value": _money(summary.get("EstimatedSavingsPercentage")),
        },
        {
            "section": "recommendation",
            "metric": "est_roi_pct",
            "value": _money(summary.get("EstimatedROI")),
        },
    ]


def build_report(client: Any, args: argparse.Namespace, today: date) -> list[dict[str, Any]]:
    """Run the Cost Explorer calls and flatten every section into ``section/metric/value`` rows."""
    period = time_period(args.days, today)
    rows = utilization_rows(
        client.get_savings_plans_utilization(TimePeriod=period, Granularity="MONTHLY")
    )
    rows += coverage_rows(
        client.get_savings_plans_coverage(TimePeriod=period, Granularity="MONTHLY")
    )
    if not args.skip_recommendation:
        rows += recommendation_rows(
            client.get_savings_plans_purchase_recommendation(
                SavingsPlansType="COMPUTE_SP",
                TermInYears=args.term,
                PaymentOption=args.payment_option,
                LookbackPeriodInDays=lookback_period(args.days),
            )
        )
    return rows


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    client = make_session(args).client("ce", region_name=CE_REGION)
    rows = build_report(client, args, date.today())
    print(render(rows, COLUMNS, args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
