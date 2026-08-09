from __future__ import annotations

import json

import pytest
from common import render

ROWS = [
    {"name": "alpha", "cost": 1234.5},
    {"name": "b", "cost": 7.0},
]
COLUMNS = ("name", "cost")


def test_table_pads_columns_to_the_widest_cell():
    lines = render(ROWS, COLUMNS, "table").splitlines()

    assert lines[0] == "name   cost"
    assert lines[2] == "alpha  1,234.50"


def test_markdown_emits_a_header_separator_row():
    lines = render(ROWS, COLUMNS, "markdown").splitlines()

    assert lines[0].startswith("| name")
    assert set(lines[1].replace("|", "").replace(" ", "")) == {"-"}
    assert len(lines) == len(ROWS) + 2


def test_csv_is_parseable_and_keeps_raw_numbers():
    output = render(ROWS, COLUMNS, "csv")

    assert output.splitlines()[0] == "name,cost"
    assert "1234.5" in output


def test_json_round_trips():
    assert json.loads(render(ROWS, COLUMNS, "json")) == [
        {"name": "alpha", "cost": 1234.5},
        {"name": "b", "cost": 7.0},
    ]


@pytest.mark.parametrize("fmt", ["table", "markdown", "csv", "json"])
def test_empty_rows_do_not_crash_any_format(fmt):
    render([], COLUMNS, fmt)


def test_missing_keys_render_as_blank_cells():
    assert "  " in render([{"name": "alpha"}], COLUMNS, "table")


def test_extra_keys_are_ignored():
    output = render([{"name": "a", "cost": 1.0, "secret": "arn:aws:iam::x"}], COLUMNS, "csv")

    assert "secret" not in output
