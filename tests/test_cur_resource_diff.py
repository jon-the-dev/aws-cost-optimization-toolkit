from __future__ import annotations

import cur_resource_diff as diff
import pytest
from cur_common import add_month_column
from cur_fixtures import diff_dir, row, write_parquet


@pytest.fixture
def june_vs_may(tmp_path):
    """The two-month diff fixture, loaded and month-bucketed."""
    return add_month_column(diff._load_parquet(diff_dir(tmp_path)))


def _ids(records):
    return {r["resource_id"] for r in records}


@pytest.mark.parametrize(
    ("month", "expected"),
    [("2026-06", "2026-05"), ("2026-01", "2025-12"), ("2026-12", "2026-11")],
)
def test_prior_month_of_rolls_back_across_the_year_boundary(month, expected):
    assert diff.prior_month_of(month) == expected


def test_new_resources_are_those_first_billed_this_month(june_vs_may):
    result = diff.classify_resources(june_vs_may, "2026-06", "2026-05")

    assert _ids(result["new"]) == {"i-new"}


def test_disappeared_resources_were_billed_last_month_only(june_vs_may):
    result = diff.classify_resources(june_vs_may, "2026-06", "2026-05")

    assert _ids(result["disappeared"]) == {"i-gone"}


def test_min_cost_zero_keeps_every_continuing_resource_including_flat_ones(june_vs_may):
    # The floor is `abs(delta) < min_cost`, so the default of 0.0 admits a
    # zero-delta resource. Pass a floor to get a list of actual movement.
    result = diff.classify_resources(june_vs_may, "2026-06", "2026-05")

    assert _ids(result["movers"]) == {"i-grew", "i-shrank", "i-stable"}


def test_any_nonzero_floor_drops_flat_resources(june_vs_may):
    result = diff.classify_resources(june_vs_may, "2026-06", "2026-05", min_cost=0.01)

    assert _ids(result["movers"]) == {"i-grew", "i-shrank"}


def test_mover_deltas_are_signed(june_vs_may):
    result = diff.classify_resources(june_vs_may, "2026-06", "2026-05")
    deltas = {r["resource_id"]: r["delta"] for r in result["movers"]}

    assert deltas["i-grew"] == pytest.approx(15.0)
    assert deltas["i-shrank"] == pytest.approx(-20.0)


def test_account_level_charges_are_excluded_from_the_resource_diff(june_vs_may):
    # A blank resource id is an account-level charge with nothing to diff; it
    # would otherwise collapse into a single phantom "resource".
    result = diff.classify_resources(june_vs_may, "2026-06", "2026-05")

    assert "" not in _ids(result["new"]) | _ids(result["disappeared"]) | _ids(result["movers"])


def test_account_level_spend_is_still_reported_as_a_total(june_vs_may):
    result = diff.classify_resources(june_vs_may, "2026-06", "2026-05")

    assert result["account_level_month"] == pytest.approx(15.0)
    assert result["account_level_prior"] == pytest.approx(15.0)


def test_month_totals_include_account_level_spend(june_vs_may):
    result = diff.classify_resources(june_vs_may, "2026-06", "2026-05")

    assert result["month_total"] == pytest.approx(92.0)
    assert result["prior_total"] == pytest.approx(93.0)


def test_min_cost_filters_small_movements(june_vs_may):
    result = diff.classify_resources(june_vs_may, "2026-06", "2026-05", min_cost=18.0)

    # i-grew moved $15, below the floor; i-shrank moved $20 and survives.
    assert _ids(result["movers"]) == {"i-shrank"}


def test_min_cost_compares_absolute_delta_not_signed(june_vs_may):
    result = diff.classify_resources(june_vs_may, "2026-06", "2026-05", min_cost=16.0)

    assert "i-shrank" in _ids(result["movers"])


def test_a_missing_prior_month_is_reported_rather_than_crashing(june_vs_may):
    result = diff.classify_resources(june_vs_may, "2026-06", "2025-01")

    assert result["prior_exists"] is False
    assert result["disappeared"] == []


def test_a_resource_returning_after_a_gap_is_flagged_returned_not_first_ever(tmp_path):
    # i-seasonal billed in April, went quiet in May, came back in June. It is
    # new relative to last month but not new to the account, and conflating the
    # two makes a restarted workload look like unexplained growth.
    root = tmp_path / "cur"
    write_parquet(root, "2026-04", [row("2026-04", 5.0, "i-seasonal")])
    write_parquet(root, "2026-05", [row("2026-05", 5.0, "i-other")])
    write_parquet(root, "2026-06", [row("2026-06", 5.0, "i-seasonal")])
    df = add_month_column(diff._load_parquet(root))

    result = diff.classify_resources(df, "2026-06", "2026-05")

    assert [(r["resource_id"], r["status"]) for r in result["new"]] == [("i-seasonal", "returned")]


def test_a_genuinely_first_seen_resource_is_flagged_first_ever(june_vs_may):
    result = diff.classify_resources(june_vs_may, "2026-06", "2026-05")

    assert [(r["resource_id"], r["status"]) for r in result["new"]] == [("i-new", "first-ever")]


def test_render_html_is_self_contained(june_vs_may):
    html = diff.render_html(diff.classify_resources(june_vs_may, "2026-06", "2026-05"))

    assert html.lstrip().startswith("<!DOCTYPE html")
    assert "i-new" in html
