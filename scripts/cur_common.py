"""Shared helpers for the Cost and Usage Report (CUR) analysis scripts.

The CUR scripts read AWS Cost and Usage Report Parquet exports from a local
directory rather than calling an AWS API. That is the only way to get
resource-level, tag-level detail: Cost Explorer aggregates too aggressively and
bills per request, while the CUR gives you every line item for free once it is
exported to S3.

See the README for how to export a CUR and sync it locally.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pandas as pd

USER_TAG_PREFIX = "resource_tags_user_"
SYSTEM_TAG_PREFIX = "resource_tags_aws_"

# Canonical cost-allocation tag names mapped to the lowercase CUR column suffixes
# that have been seen to represent them. AWS lowercases and underscores tag keys
# in CUR column names, so `CostCenter` and `cost_center` both land in this space
# and have to be reconciled by hand.
#
# Override with --tag-aliases pointing at a JSON file of the same shape, e.g.
#   {"Company": ["company", "customer"], "Team": ["team", "squad"]}
DEFAULT_TAG_ALIASES: dict[str, list[str]] = {
    "Application": ["application", "app"],
    "Environment": ["environment", "env"],
    "CostCenter": ["costcenter", "cost_center", "cost-center"],
    "Owner": ["owner", "techowner"],
    "Project": ["project"],
}


def load_tag_aliases(path: str | Path | None) -> dict[str, list[str]]:
    """Load a canonical-tag alias map, falling back to the built-in defaults.

    Args:
        path: JSON file mapping canonical tag names to lists of CUR column
            suffixes, or None to use DEFAULT_TAG_ALIASES.

    Returns:
        The alias mapping.

    Raises:
        ValueError: If the file is not a JSON object of string lists.
    """
    if path is None:
        return dict(DEFAULT_TAG_ALIASES)

    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict) or not all(
        isinstance(v, list) and all(isinstance(a, str) for a in v) for v in data.values()
    ):
        raise ValueError(
            f"{path}: expected a JSON object mapping tag names to lists of strings, "
            'e.g. {"CostCenter": ["costcenter", "cost_center"]}'
        )
    return {str(k): list(v) for k, v in data.items()}


def resolve_tag_column(
    columns: Iterable[str], canonical: str, aliases: dict[str, list[str]] | None = None
) -> str | None:
    """Return the first matching CUR tag column for a canonical tag name.

    Args:
        columns: Iterable of DataFrame column names.
        canonical: Canonical tag name, e.g. "Application".
        aliases: Alias map to consult. Defaults to DEFAULT_TAG_ALIASES.

    Returns:
        Column name like ``resource_tags_user_application`` if found, else None.
    """
    alias_map = DEFAULT_TAG_ALIASES if aliases is None else aliases
    candidates = alias_map.get(canonical, [canonical.lower()])
    col_set = set(columns)
    for alias in candidates:
        column = f"{USER_TAG_PREFIX}{alias}"
        if column in col_set:
            return column
    return None


def canonical_for_column(column: str, aliases: dict[str, list[str]] | None = None) -> str | None:
    """Map a ``resource_tags_user_*`` column back to its canonical tag name."""
    alias_map = DEFAULT_TAG_ALIASES if aliases is None else aliases
    suffix = column[len(USER_TAG_PREFIX) :]
    for canonical, candidates in alias_map.items():
        if suffix in candidates:
            return canonical
    return None


def add_month_column(df: pd.DataFrame) -> pd.DataFrame:
    """Add a ``month`` column (``YYYY-MM``) derived from usage start date.

    CUR timestamps are timezone-aware; they are converted to UTC before
    formatting so a run's local timezone cannot shift a line item into the
    neighbouring month.

    Args:
        df: DataFrame containing ``line_item_usage_start_date``.

    Returns:
        A copy of the DataFrame with a new string column ``month``.
    """
    timestamps = df["line_item_usage_start_date"]
    if hasattr(timestamps.dtype, "tz") and timestamps.dtype.tz is not None:
        timestamps = timestamps.dt.tz_convert("UTC").dt.tz_localize(None)
    df = df.copy()
    df["month"] = timestamps.dt.strftime("%Y-%m")
    return df


def find_parquet_files(cur_dir: str | Path) -> list[Path]:
    """Return every Parquet file under ``cur_dir``, sorted for deterministic runs.

    Raises:
        SystemExit: If the directory is missing or holds no Parquet files, with a
            message pointing at the sync command in the README.
    """
    root = Path(cur_dir)
    if not root.is_dir():
        raise SystemExit(
            f"error: CUR directory {root} does not exist. Sync your CUR export first:\n"
            f"  aws s3 sync s3://$CUR_BUCKET/$CUR_PREFIX/ {root}/"
        )
    files = sorted(root.rglob("*.parquet"))
    if not files:
        raise SystemExit(
            f"error: no .parquet files under {root}. Sync your CUR export first:\n"
            f"  aws s3 sync s3://$CUR_BUCKET/$CUR_PREFIX/ {root}/"
        )
    return files


def add_cur_args(parser: Any) -> None:
    """Add the --cur-dir/--tag-aliases arguments every CUR script accepts."""
    parser.add_argument(
        "--cur-dir",
        default="cur-data",
        help="Local directory holding the CUR Parquet export (default: cur-data)",
    )
    parser.add_argument(
        "--tag-aliases",
        help="JSON file overriding the canonical tag alias map (see cur_common.py)",
    )
