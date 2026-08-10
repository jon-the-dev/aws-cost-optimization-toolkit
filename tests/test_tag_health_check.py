from __future__ import annotations

import pytest
import tag_health_check as thc
from cur_fixtures import collision_dir, write_parquet


def test_find_collisions_flags_two_columns_serving_one_canonical_tag():
    month_columns = {
        "2026-06": [
            "resource_tags_user_costcenter",
            "resource_tags_user_cost_center",
            "resource_tags_user_application",
        ]
    }

    collisions = thc.find_collisions(month_columns)

    assert list(collisions) == ["CostCenter"]
    assert collisions["CostCenter"]["columns"] == [
        "resource_tags_user_cost_center",
        "resource_tags_user_costcenter",
    ]


def test_find_collisions_is_quiet_when_each_tag_has_one_column():
    month_columns = {"2026-06": ["resource_tags_user_costcenter", "resource_tags_user_application"]}

    assert thc.find_collisions(month_columns) == {}


def test_a_tag_split_across_months_is_not_a_collision():
    # cost_center in May, costcenter in June. Never both at once, so attribution
    # is never split within a single month — that is a rename, not a collision.
    month_columns = {
        "2026-05": ["resource_tags_user_cost_center"],
        "2026-06": ["resource_tags_user_costcenter"],
    }

    assert thc.find_collisions(month_columns) == {}


def test_find_collisions_records_every_affected_month():
    month_columns = {
        "2026-05": ["resource_tags_user_costcenter", "resource_tags_user_cost_center"],
        "2026-06": ["resource_tags_user_costcenter", "resource_tags_user_cost_center"],
    }

    assert sorted(thc.find_collisions(month_columns)["CostCenter"]["months"]) == [
        "2026-05",
        "2026-06",
    ]


def test_find_disappearances_flags_a_column_that_vanishes_later():
    month_columns = {
        "2026-05": ["resource_tags_user_owner", "resource_tags_user_application"],
        "2026-06": ["resource_tags_user_application"],
    }

    disappeared = thc.find_disappearances(month_columns)

    assert [d["column"] for d in disappeared] == ["resource_tags_user_owner"]


def test_find_disappearances_ignores_a_column_added_partway_through():
    month_columns = {
        "2026-05": ["resource_tags_user_application"],
        "2026-06": ["resource_tags_user_application", "resource_tags_user_owner"],
    }

    assert thc.find_disappearances(month_columns) == []


def test_find_non_canonical_reports_unknown_tag_suffixes():
    month_columns = {"2026-06": ["resource_tags_user_application", "resource_tags_user_mystery"]}

    non_canonical = thc.find_non_canonical(month_columns)

    assert "resource_tags_user_mystery" in non_canonical
    assert "resource_tags_user_application" not in non_canonical


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("cur/year=2026/month=6/x.parquet", "2026-06"),
        ("cur/year=2026/month=06/x.parquet", "2026-06"),
        ("cur/year=2026/month=12/x.parquet", "2026-12"),
        # A bare YYYY-MM directory is not the layout AWS exports, and is not parsed.
        ("cur-data/2026-06/part-0.parquet", None),
        ("cur-data/no-date-here/part-0.parquet", None),
    ],
)
def test_month_from_path_reads_common_cur_partition_layouts(path, expected):
    assert thc.month_from_path(path) == expected


def test_scan_cur_dir_groups_tag_columns_by_month(tmp_path):
    scan = thc.scan_cur_dir(collision_dir(tmp_path))

    assert sorted(scan) == ["2026-05", "2026-06"]
    assert scan["2026-06"]["user_tags"] == [
        "resource_tags_user_application",
        "resource_tags_user_cost_center",
        "resource_tags_user_costcenter",
    ]


def test_build_findings_quantifies_how_the_cost_split(tmp_path):
    findings = thc.build_findings(thc.scan_cur_dir(collision_dir(tmp_path)))

    june = findings["collision_cost_split"]["CostCenter"]["2026-06"]

    assert june["resource_tags_user_cost_center"] == pytest.approx(30.0)
    assert june["resource_tags_user_costcenter"] == pytest.approx(70.0)
    # May used one column only, so it is not a collision month and is not costed.
    assert "2026-05" not in findings["collision_cost_split"]["CostCenter"]


def test_collisions_are_blocking_findings(tmp_path):
    findings = thc.build_findings(thc.scan_cur_dir(collision_dir(tmp_path)))

    assert thc.has_blocking_findings(findings) is True


def test_a_clean_export_produces_no_blocking_findings(tmp_path):
    from cur_fixtures import row

    root = tmp_path / "cur"
    write_parquet(root, "2026-06", [row("2026-06", 5.0, "i-1", application="api")])

    findings = thc.build_findings(thc.scan_cur_dir(root))

    assert findings["collisions"] == {}
    assert thc.has_blocking_findings(findings) is False


def test_system_tags_are_never_treated_as_collisions(tmp_path):
    from cur_fixtures import row

    root = tmp_path / "cur"
    rows = [row("2026-06", 5.0, "i-1", application="api")]
    rows[0]["resource_tags_aws_cloudformation_stack_name"] = "my-stack"
    write_parquet(root, "2026-06", rows)

    scan = thc.scan_cur_dir(root)

    assert scan["2026-06"]["system_tags"] == ["resource_tags_aws_cloudformation_stack_name"]
    assert thc.find_collisions({m: v["user_tags"] for m, v in scan.items()}) == {}


def test_render_html_is_self_contained(tmp_path):
    findings = thc.build_findings(thc.scan_cur_dir(collision_dir(tmp_path)))

    html = thc.render_html(findings)

    assert html.lstrip().startswith("<!DOCTYPE html")
    assert "CostCenter" in html
    assert "<script src=" not in html
