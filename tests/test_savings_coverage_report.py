from __future__ import annotations

from datetime import date

import fixtures
import pytest
import savings_coverage_report as report


def test_time_period_is_a_closed_open_window_ending_today():
    assert report.time_period(30, date(2026, 8, 8)) == {"Start": "2026-07-09", "End": "2026-08-08"}


@pytest.mark.parametrize(
    ("days", "expected"),
    [
        (1, "SEVEN_DAYS"),
        (7, "SEVEN_DAYS"),
        (8, "THIRTY_DAYS"),
        (30, "THIRTY_DAYS"),
        (31, "SIXTY_DAYS"),
    ],
)
def test_lookback_period_maps_onto_the_three_accepted_windows(days, expected):
    assert report.lookback_period(days) == expected


def test_utilization_rows_surface_unused_commitment():
    rows = report.utilization_rows(fixtures.SP_UTILIZATION)

    values = {r["metric"]: r["value"] for r in rows}
    assert values["unused_commitment"] == 539.28
    assert values["utilization_pct"] == 92.51
    assert values["net_savings"] == 2184.36


def test_utilization_rows_handle_an_account_with_no_savings_plans():
    rows = report.utilization_rows({"Total": {}})

    assert rows == [
        {"section": "utilization", "metric": "savings_plans", "value": "none active in window"}
    ]


def test_coverage_rows_compute_the_percentage_from_the_summed_spend():
    rows = report.coverage_rows(fixtures.SP_COVERAGE)

    values = {r["metric"]: r["value"] for r in rows}
    assert values["covered_spend"] == 6660.72
    assert values["on_demand_spend"] == 3120.44
    assert values["coverage_pct"] == 68.1


def test_coverage_rows_do_not_divide_by_zero_when_there_is_no_spend():
    response = {
        "SavingsPlansCoverages": [
            {"Coverage": {"SpendCoveredBySavingsPlans": "0", "OnDemandCost": "0", "TotalCost": "0"}}
        ]
    }

    values = {r["metric"]: r["value"] for r in report.coverage_rows(response)}

    assert values["coverage_pct"] == 0.0


def test_coverage_rows_handle_an_empty_response():
    rows = report.coverage_rows({"SavingsPlansCoverages": []})

    assert rows[0]["value"] == "no eligible compute spend in window"


def test_recommendation_rows_extract_the_summary():
    rows = report.recommendation_rows(fixtures.SP_PURCHASE_RECOMMENDATION)

    values = {r["metric"]: r["value"] for r in rows}
    assert values["hourly_commitment"] == 3.34
    assert values["est_monthly_savings"] == 681.72
    assert values["est_roi_pct"] == 27.94


def test_recommendation_rows_handle_no_recommendation():
    rows = report.recommendation_rows({"SavingsPlansPurchaseRecommendation": {}})

    assert rows[0]["value"] == "none returned for these parameters"


def test_build_report_issues_the_expected_cost_explorer_calls(stubbed):
    client, stubber = stubbed("ce")
    period = {"Start": "2026-07-09", "End": "2026-08-08"}
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

    args = report.parse_args([])
    rows = report.build_report(client, args, date(2026, 8, 8))

    stubber.assert_no_pending_responses()
    assert {r["section"] for r in rows} == {"utilization", "coverage", "recommendation"}


def test_skip_recommendation_avoids_the_third_billable_request(stubbed):
    client, stubber = stubbed("ce")
    period = {"Start": "2026-07-09", "End": "2026-08-08"}
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

    args = report.parse_args(["--skip-recommendation"])
    rows = report.build_report(client, args, date(2026, 8, 8))

    stubber.assert_no_pending_responses()
    assert {r["section"] for r in rows} == {"utilization", "coverage"}


def test_zero_days_is_rejected():
    with pytest.raises(SystemExit):
        report.parse_args(["--days", "0"])
