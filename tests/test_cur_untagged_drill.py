from __future__ import annotations

import cur_untagged_drill as drill
import pytest
from cur_common import add_month_column, resolve_tag_column
from cur_fixtures import row, untagged_dir, write_parquet


@pytest.fixture
def june(tmp_path):
    """The untagged fixture month, loaded and month-bucketed."""
    df = add_month_column(drill.load_cur(untagged_dir(tmp_path)))
    return df[df["month"] == "2026-06"]


def _analyze(df, tag="Application"):
    return drill.analyze_untagged(df, resolve_tag_column(df.columns, tag), tag=tag)


def test_untagged_spend_splits_into_fixable_and_account_level(june):
    result = _analyze(june)

    assert result["total_cost"] == pytest.approx(100.0)
    assert result["untagged_cost"] == pytest.approx(40.0)
    assert result["taggable_cost"] == pytest.approx(15.0)
    assert result["account_level_cost"] == pytest.approx(25.0)


def test_the_two_buckets_account_for_all_untagged_spend(june):
    result = _analyze(june)

    assert result["taggable_cost"] + result["account_level_cost"] == pytest.approx(
        result["untagged_cost"]
    )


def test_untagged_percentage_is_of_total_spend(june):
    assert _analyze(june)["untagged_pct"] == pytest.approx(40.0)


def test_an_empty_string_tag_counts_as_untagged(june):
    # i-untagged1 carries application="" — present column, no value.
    taggable_ids = {r["resource_id"] for r in _analyze(june)["taggable_resources"]}

    assert "i-untagged1" in taggable_ids


def test_a_missing_tag_value_counts_as_untagged(june):
    # i-untagged2 has no application key at all, so the column is NaN.
    taggable_ids = {r["resource_id"] for r in _analyze(june)["taggable_resources"]}

    assert "i-untagged2" in taggable_ids


def test_tagged_resources_are_excluded(june):
    taggable_ids = {r["resource_id"] for r in _analyze(june)["taggable_resources"]}

    assert "i-tagged" not in taggable_ids


def test_account_level_rows_never_appear_in_the_fixable_list(june):
    # Charges with a blank resource id cannot be tagged; listing them sends
    # people chasing work that cannot be done.
    resources = _analyze(june)["taggable_resources"]

    assert all(r["resource_id"].strip() for r in resources)


def test_a_missing_tag_column_makes_every_row_untagged(june):
    result = drill.analyze_untagged(june, None, tag="Nonexistent")

    assert result["untagged_cost"] == pytest.approx(result["total_cost"])


def test_top_n_caps_the_listed_resources(june):
    result = drill.analyze_untagged(june, resolve_tag_column(june.columns, "Application"), top_n=1)

    assert len(result["taggable_resources"]) == 1
    # Capping the list must not change the totals.
    assert result["taggable_cost"] == pytest.approx(15.0)


def test_taggable_resources_are_sorted_by_cost_descending(june):
    costs = [r["cost"] for r in _analyze(june)["taggable_resources"]]

    assert costs == sorted(costs, reverse=True)


def test_a_fully_tagged_month_reports_zero_untagged(tmp_path):
    root = tmp_path / "cur"
    write_parquet(root, "2026-06", [row("2026-06", 10.0, "i-1", application="api")])
    df = add_month_column(drill.load_cur(root))

    result = _analyze(df)

    assert result["untagged_cost"] == pytest.approx(0.0)
    assert result["untagged_pct"] == pytest.approx(0.0)
    assert result["taggable_resources"] == []


def test_a_zero_cost_month_does_not_divide_by_zero(tmp_path):
    root = tmp_path / "cur"
    write_parquet(root, "2026-06", [row("2026-06", 0.0, "i-1")])
    df = add_month_column(drill.load_cur(root))

    assert _analyze(df)["untagged_pct"] == 0.0


def test_render_html_is_self_contained_and_shows_the_split(june):
    html = drill.render_html(_analyze(june), "2026-06", is_partial=False)

    assert html.lstrip().startswith("<!DOCTYPE html")
    assert "Taggable" in html
