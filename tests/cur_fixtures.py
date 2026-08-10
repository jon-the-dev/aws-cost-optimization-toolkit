"""Synthetic CUR Parquet builders for the cost-and-usage-report tests.

Real CUR exports are hundreds of columns wide and account-specific. These
builders emit the handful of columns the scripts actually read, so the tests
stay readable and no real billing data is committed.

Column names follow the CUR Parquet convention: snake_cased line-item fields and
``resource_tags_user_<lowercased tag key>`` for cost-allocation tags.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

COST_COL = "line_item_unblended_cost"
RESOURCE_COL = "line_item_resource_id"
SERVICE_COL = "line_item_product_code"
DATE_COL = "line_item_usage_start_date"


def row(
    month: str,
    cost: float,
    resource: str = "",
    service: str = "AmazonEC2",
    day: int = 15,
    **tags: str,
) -> dict:
    """Build a single CUR line item.

    Args:
        month: ``YYYY-MM``.
        cost: Unblended cost for the line item.
        resource: Resource id. Empty string means an account-level charge.
        service: CUR product code.
        day: Day of month for the usage start timestamp.
        **tags: Extra columns, keyed by CUR tag column suffix. ``costcenter="20001"``
            becomes ``resource_tags_user_costcenter``.

    Returns:
        A dict suitable for :func:`write_parquet`.
    """
    record = {
        DATE_COL: pd.Timestamp(f"{month}-{day:02d} 00:00:00", tz="UTC"),
        COST_COL: cost,
        RESOURCE_COL: resource,
        SERVICE_COL: service,
    }
    record.update({f"resource_tags_user_{k}": v for k, v in tags.items()})
    return record


def write_parquet(root: Path, month: str, rows: list[dict], name: str = "part-0") -> Path:
    """Write ``rows`` into the Hive-style partition AWS uses for CUR exports.

    Real exports land under ``year=YYYY/month=M`` with the month un-padded, and
    tag_health_check derives the reporting month from that path, so the tests
    have to reproduce the layout rather than inventing a tidier one.

    Missing keys across rows become NaN columns, which mirrors how real CUR
    exports gain and lose tag columns between months.
    """
    year, month_num = month.split("-")
    directory = root / f"year={year}" / f"month={int(month_num)}"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.parquet"
    pd.DataFrame(rows).to_parquet(path, index=False)
    return path


def collision_dir(tmp_path: Path) -> Path:
    """A CUR export where CostCenter splits across two columns in one month.

    ``2026-05`` uses ``cost_center`` only. ``2026-06`` carries both
    ``cost_center`` and ``costcenter``, splitting $30 / $70 — the silent
    attribution bug tag_health_check exists to catch.
    """
    root = tmp_path / "cur"
    write_parquet(
        root,
        "2026-05",
        [
            row("2026-05", 10.0, "i-may1", cost_center="20001", application="api"),
            row("2026-05", 5.0, "i-may2", cost_center="20002", application="web"),
        ],
    )
    write_parquet(
        root,
        "2026-06",
        [
            row("2026-06", 30.0, "i-jun1", cost_center="20001", application="api"),
            row("2026-06", 70.0, "i-jun2", costcenter="20002", application="web"),
        ],
    )
    return root


def untagged_dir(tmp_path: Path) -> Path:
    """A single month mixing tagged, taggable-untagged, and account-level rows.

    Totals: $100 spend, of which $40 is untagged. Of that $40, $15 sits on real
    resources (fixable) and $25 is account-level charges with no resource id.
    """
    root = tmp_path / "cur"
    write_parquet(
        root,
        "2026-06",
        [
            row("2026-06", 60.0, "i-tagged", application="api"),
            row("2026-06", 10.0, "i-untagged1", application=""),
            row("2026-06", 5.0, "i-untagged2"),
            row("2026-06", 20.0, "", service="AWSCloudTrail"),
            row("2026-06", 5.0, "", service="AWSSupportBusiness"),
        ],
    )
    return root


def diff_dir(tmp_path: Path) -> Path:
    """Two months designed to exercise every resource-diff classification.

    ``i-stable`` is unchanged, ``i-grew`` doubles, ``i-shrank`` halves,
    ``i-new`` appears only in June, ``i-gone`` appears only in May, and each
    month carries an account-level charge that must be excluded from the diff.
    """
    root = tmp_path / "cur"
    write_parquet(
        root,
        "2026-05",
        [
            row("2026-05", 20.0, "i-stable"),
            row("2026-05", 10.0, "i-grew"),
            row("2026-05", 40.0, "i-shrank"),
            row("2026-05", 8.0, "i-gone"),
            row("2026-05", 15.0, "", service="AWSCloudTrail"),
        ],
    )
    write_parquet(
        root,
        "2026-06",
        [
            row("2026-06", 20.0, "i-stable"),
            row("2026-06", 25.0, "i-grew"),
            row("2026-06", 20.0, "i-shrank"),
            row("2026-06", 12.0, "i-new"),
            row("2026-06", 15.0, "", service="AWSCloudTrail"),
        ],
    )
    return root
