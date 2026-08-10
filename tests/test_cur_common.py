from __future__ import annotations

import json

import pandas as pd
import pytest
from cur_common import (
    DEFAULT_TAG_ALIASES,
    add_month_column,
    canonical_for_column,
    find_parquet_files,
    load_tag_aliases,
    resolve_tag_column,
)
from cur_fixtures import collision_dir


def test_resolve_tag_column_prefers_the_first_listed_alias():
    columns = ["resource_tags_user_cost_center", "resource_tags_user_costcenter"]

    # "costcenter" is listed before "cost_center" in the default aliases.
    assert resolve_tag_column(columns, "CostCenter") == "resource_tags_user_costcenter"


def test_resolve_tag_column_falls_back_to_the_lowercased_name_for_unknown_tags():
    assert resolve_tag_column(["resource_tags_user_squad"], "Squad") == "resource_tags_user_squad"


def test_resolve_tag_column_returns_none_when_the_tag_is_absent():
    assert resolve_tag_column(["line_item_unblended_cost"], "Application") is None


def test_resolve_tag_column_honours_a_custom_alias_map():
    columns = ["resource_tags_user_biz_unit"]

    assert resolve_tag_column(columns, "Company", {"Company": ["biz_unit"]}) == columns[0]
    assert resolve_tag_column(columns, "Company") is None


def test_canonical_for_column_maps_both_spellings_to_one_tag():
    assert canonical_for_column("resource_tags_user_costcenter") == "CostCenter"
    assert canonical_for_column("resource_tags_user_cost_center") == "CostCenter"


def test_canonical_for_column_returns_none_for_an_unknown_suffix():
    assert canonical_for_column("resource_tags_user_mystery") is None


def test_load_tag_aliases_defaults_to_the_builtin_map():
    assert load_tag_aliases(None) == DEFAULT_TAG_ALIASES


def test_load_tag_aliases_does_not_let_callers_mutate_the_default():
    load_tag_aliases(None)["Application"] = ["clobbered"]

    assert DEFAULT_TAG_ALIASES["Application"] == ["application", "app"]


def test_load_tag_aliases_reads_a_custom_file(tmp_path):
    path = tmp_path / "aliases.json"
    path.write_text(json.dumps({"Team": ["team", "squad"]}))

    assert load_tag_aliases(path) == {"Team": ["team", "squad"]}


@pytest.mark.parametrize(
    "payload", ['["not", "a", "map"]', '{"Team": "not-a-list"}', '{"Team": [1]}']
)
def test_load_tag_aliases_rejects_malformed_files(tmp_path, payload):
    path = tmp_path / "bad.json"
    path.write_text(payload)

    with pytest.raises(SystemExit, match="must be a JSON object"):
        load_tag_aliases(path)


def test_load_tag_aliases_reports_bad_json_without_a_traceback(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json")

    with pytest.raises(SystemExit, match="not valid JSON"):
        load_tag_aliases(path)


def test_load_tag_aliases_reports_a_missing_file(tmp_path):
    with pytest.raises(SystemExit, match="cannot read"):
        load_tag_aliases(tmp_path / "absent.json")


def test_load_tag_aliases_ignores_underscore_prefixed_comment_keys(tmp_path):
    # JSON has no comments, so config files grow a "_comment" key. Failing on it
    # is a papercut the first consumer of this toolkit hit immediately.
    path = tmp_path / "aliases.json"
    path.write_text(json.dumps({"_comment": "why these tags", "Team": ["team"]}))

    assert load_tag_aliases(path) == {"Team": ["team"]}


def test_load_tag_aliases_rejects_a_file_that_is_only_comments(tmp_path):
    path = tmp_path / "aliases.json"
    path.write_text(json.dumps({"_comment": "oops, no tags"}))

    with pytest.raises(SystemExit, match="defines no tags"):
        load_tag_aliases(path)


def test_add_month_column_buckets_by_utc_not_local_time():
    # 23:30 UTC on the last day of June must stay in June, not roll into July.
    df = pd.DataFrame(
        {"line_item_usage_start_date": [pd.Timestamp("2026-06-30 23:30:00", tz="UTC")]}
    )

    assert add_month_column(df)["month"].tolist() == ["2026-06"]


def test_add_month_column_converts_a_non_utc_timestamp_before_bucketing():
    # 20:00 on 30 June in New York is 00:00 on 1 July UTC. CUR reports in UTC,
    # so this line item belongs to July; reading the local wall clock would file
    # it under June and silently move spend between reporting months.
    df = pd.DataFrame(
        {"line_item_usage_start_date": [pd.Timestamp("2026-06-30 20:00:00", tz="America/New_York")]}
    )

    assert add_month_column(df)["month"].tolist() == ["2026-07"]


def test_add_month_column_handles_naive_timestamps():
    df = pd.DataFrame({"line_item_usage_start_date": [pd.Timestamp("2026-06-15 12:00:00")]})

    assert add_month_column(df)["month"].tolist() == ["2026-06"]


def test_add_month_column_does_not_mutate_the_input():
    df = pd.DataFrame({"line_item_usage_start_date": [pd.Timestamp("2026-06-15", tz="UTC")]})

    add_month_column(df)

    assert "month" not in df.columns


def test_find_parquet_files_recurses_and_sorts(tmp_path):
    root = collision_dir(tmp_path)

    files = find_parquet_files(root)

    assert [f.parent.name for f in files] == ["month=5", "month=6"]


def test_find_parquet_files_explains_how_to_populate_a_missing_directory(tmp_path):
    with pytest.raises(SystemExit, match="aws s3 sync"):
        find_parquet_files(tmp_path / "nope")


def test_find_parquet_files_explains_an_empty_directory(tmp_path):
    (tmp_path / "empty").mkdir()

    with pytest.raises(SystemExit, match="no .parquet files"):
        find_parquet_files(tmp_path / "empty")
