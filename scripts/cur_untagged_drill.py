#!/usr/bin/env python3
"""
Untagged Cost Drill-Down

Turns the monthly review's big "UNTAGGED" lump into an actionable, resource-level
punch-list. Most untagged spend is NOT fixable — it is account-level charges
(Registrar, tax, CloudTrail, GuardDuty, data transfer) with no resource to tag.
This script splits untagged spend for a canonical tag into two buckets:

  * TAGGABLE          — rows missing the tag but with a real resource id → fix these.
  * ACCOUNT-LEVEL     — rows missing the tag AND with a blank resource id → informational.

Usage:
    python scripts/cur_untagged_drill.py --month 2026-06 --tag Application
    python scripts/cur_untagged_drill.py --tag CostCenter --top 25
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

# Reuse the shared CUR helpers so tag/alias/month logic stays consistent across
sys.path.insert(0, str(Path(__file__).resolve().parent))
from cur_common import (  # noqa: E402
    add_cur_args,
    add_month_column,
    find_parquet_files,
    load_tag_aliases,
    resolve_tag_column,
)

COST_COL = "line_item_unblended_cost"
RESOURCE_COL = "line_item_resource_id"
SERVICE_COL = "line_item_product_code"
STACK_COL = "resource_tags_aws_cloudformation_stack_name"


# ---------------------------------------------------------------------------
# Core analysis (pure functions)
# ---------------------------------------------------------------------------


def analyze_untagged(
    df_month: pd.DataFrame,
    tag_col: str | None,
    tag: str = "Application",
    top_n: int = 50,
) -> dict:
    """Split untagged spend into taggable resources and account-level charges.

    A row is "untagged" when ``tag_col`` is absent, NaN, or an empty string.
    Untagged rows are then partitioned by whether they carry a real
    ``line_item_resource_id`` (taggable) or a blank one (account-level).

    Args:
        df_month: CUR rows for a single calendar month.
        tag_col: Resolved CUR tag column for the canonical tag, or None if the
            column does not exist for this month (then every row is untagged).
        tag: Canonical tag name, used only for labelling the result.
        top_n: Maximum number of taggable resources to return.

    Returns:
        Dict with spend totals and the taggable / account-level breakdowns.
    """
    total_cost = round(float(df_month[COST_COL].sum()), 2)

    untagged_mask = _untagged_mask(df_month, tag_col)
    untagged = df_month[untagged_mask]
    untagged_cost = round(float(untagged[COST_COL].sum()), 2)
    untagged_pct = round(untagged_cost / total_cost * 100, 2) if total_cost else 0.0

    res = untagged[RESOURCE_COL]
    has_res = res.notna() & (res.astype(str).str.strip() != "")
    taggable = untagged[has_res]
    account_level = untagged[~has_res]

    taggable_cost = round(float(taggable[COST_COL].sum()), 2)
    account_cost = round(float(account_level[COST_COL].sum()), 2)

    return {
        "tag": tag,
        "tag_column": tag_col,
        "tag_column_present": tag_col is not None,
        "total_cost": total_cost,
        "untagged_cost": untagged_cost,
        "untagged_pct": untagged_pct,
        "taggable_cost": taggable_cost,
        "account_level_cost": account_cost,
        "taggable_resources": _top_taggable_resources(taggable, top_n),
        "account_level_services": _account_level_by_service(account_level),
    }


def _untagged_mask(df: pd.DataFrame, tag_col: str | None) -> pd.Series:
    """Return a boolean mask of rows that are untagged for ``tag_col``."""
    if tag_col is None or tag_col not in df.columns:
        return pd.Series(True, index=df.index)
    col = df[tag_col]
    return col.isna() | (col.astype(str).str.strip() == "")


def _top_taggable_resources(taggable: pd.DataFrame, top_n: int) -> list:
    """Group taggable untagged rows by resource id, sort by cost, take top N."""
    if taggable.empty:
        return []

    cc_col = resolve_tag_column(taggable.columns, "CostCenter")
    rows = []
    for resource_id, grp in taggable.groupby(RESOURCE_COL, sort=False):
        rows.append(
            {
                "resource_id": str(resource_id),
                "cost": round(float(grp[COST_COL].sum()), 2),
                "service": _dominant_value(grp, SERVICE_COL),
                "stack": _dominant_value(grp, STACK_COL),
                "cost_center": _dominant_value(grp, cc_col),
            }
        )
    rows.sort(key=lambda r: r["cost"], reverse=True)
    return rows[:top_n]


def _account_level_by_service(account_level: pd.DataFrame) -> list:
    """Group account-level (blank resource id) untagged rows by service."""
    if account_level.empty:
        return []
    series = account_level.groupby(SERVICE_COL)[COST_COL].sum().sort_values(ascending=False)
    return [
        {"service": str(name), "cost": round(float(cost), 2)}
        for name, cost in series.items()
        if round(float(cost), 2) != 0.0
    ]


def _dominant_value(grp: pd.DataFrame, col: str | None) -> str:
    """Return the value carrying the most cost in ``col``, or '-' if unusable."""
    if not col or col not in grp.columns:
        return "-"
    tagged = grp[grp[col].notna() & (grp[col].astype(str).str.strip() != "")]
    if tagged.empty:
        return "-"
    series = tagged.groupby(col)[COST_COL].sum().sort_values(ascending=False)
    return str(series.index[0])


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------


def render_html(result: dict, month: str, is_partial: bool) -> str:
    """Render a self-contained HTML untagged drill-down report.

    Args:
        result: Analysis dict from :func:`analyze_untagged`.
        month: Focus month (``YYYY-MM``).
        is_partial: True when the month is still accumulating data.

    Returns:
        HTML document string.
    """
    return "\n".join(
        [
            _html_head(result, month, is_partial),
            _html_cards(result),
            _html_note(result),
            _html_taggable_table(result),
            _html_account_table(result),
            _html_footer(),
        ]
    )


def _html_head(result: dict, month: str, is_partial: bool) -> str:
    partial = " (partial)" if is_partial else ""
    tag = result["tag"]
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Untagged Drill-Down – {tag} – {month}{partial}</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;
                background: #f5f5f5; padding: 20px; line-height: 1.6; }}
        .container {{ max-width: 1200px; margin: 0 auto; }}
        .header {{ background: linear-gradient(135deg, #856404 0%, #b8860b 100%);
                   color: white; padding: 40px; border-radius: 10px; margin-bottom: 30px; }}
        .header h1 {{ font-size: 2.2em; margin-bottom: 10px; }}
        .summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
                    gap: 20px; margin-bottom: 30px; }}
        .card {{ background: white; padding: 26px; border-radius: 10px;
                 box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        .card h3 {{ color: #666; font-size: 0.85em; margin-bottom: 8px;
                    text-transform: uppercase; }}
        .card .value {{ font-size: 1.9em; font-weight: bold; color: #333; }}
        .card.actionable {{ border-left: 5px solid #856404; }}
        .card.actionable .value {{ color: #856404; }}
        .card.unfixable {{ border-left: 5px solid #999; }}
        .section {{ background: white; padding: 30px; border-radius: 10px;
                    margin-bottom: 30px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        .section h2 {{ font-size: 1.6em; margin-bottom: 16px; color: #333;
                       border-bottom: 3px solid #856404; padding-bottom: 10px; }}
        .note {{ background: #fff3cd; border-left: 5px solid #856404; padding: 16px 20px;
                 border-radius: 6px; margin-bottom: 30px; color: #856404; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
        th, td {{ padding: 9px 12px; text-align: left; border-bottom: 1px solid #ddd; }}
        th {{ background: #f8f9fa; font-weight: 600; color: #333; }}
        tr:hover {{ background: #f8f9fa; }}
        td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
        code {{ font-family: 'SF Mono', Menlo, monospace; font-size: 0.85em; }}
        .footer {{ text-align: center; color: #666; margin-top: 40px;
                   padding: 20px; font-size: 0.9em; }}
    </style>
</head>
<body>
<div class="container">
    <div class="header">
        <h1>Untagged Cost Drill-Down</h1>
        <p>Tag: <strong>{tag}</strong> &nbsp;|&nbsp; Month: <strong>{month}{partial}</strong>
           &nbsp;|&nbsp; Generated: {datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")}</p>
    </div>
"""


def _html_cards(result: dict) -> str:
    return f"""    <div class="summary">
        <div class="card">
            <h3>Total Month Spend</h3>
            <div class="value">${result["total_cost"]:,.2f}</div>
        </div>
        <div class="card">
            <h3>Untagged Spend</h3>
            <div class="value">{result["untagged_pct"]:.1f}%</div>
            <div style="color:#666;font-size:0.9em;">${result["untagged_cost"]:,.2f}</div>
        </div>
        <div class="card actionable">
            <h3>Taggable (Actionable)</h3>
            <div class="value">${result["taggable_cost"]:,.2f}</div>
            <div style="color:#666;font-size:0.9em;">
              {len(result["taggable_resources"])} resources listed</div>
        </div>
        <div class="card unfixable">
            <h3>Account-Level (Unfixable)</h3>
            <div class="value">${result["account_level_cost"]:,.2f}</div>
            <div style="color:#666;font-size:0.9em;">no resource to tag</div>
        </div>
    </div>
"""


def _html_note(result: dict) -> str:
    if not result["tag_column_present"]:
        return f"""    <div class="note">
        <strong>Note:</strong> the <code>{result["tag"]}</code> tag column does not exist in this
        month's CUR data — it was not cost-allocation-activated yet, so <em>every</em> row counts as
        untagged. Activate the tag in the Billing console to begin attributing spend.
    </div>
"""
    return ""


def _html_taggable_table(result: dict) -> str:
    resources = result["taggable_resources"]
    if not resources:
        body = "<tr><td colspan='5'>No taggable untagged resources found.</td></tr>"
    else:
        body = "".join(
            f"<tr><td><code>{r['resource_id']}</code></td><td>{r['service']}</td>"
            f"<td>{r['stack']}</td><td>{r['cost_center']}</td>"
            f"<td class='num'>${r['cost']:,.2f}</td></tr>"
            for r in resources
        )
    return f"""    <div class="section">
        <h2>Taggable Resources — Remediation Punch-List</h2>
        <p style="color:#666;margin-bottom:10px;">
            These untagged rows have a real resource id, so they can be tagged directly.
            Fixing them recovers <strong>${result["taggable_cost"]:,.2f}</strong> of attribution.
        </p>
        <table>
            <thead><tr><th>Resource ID</th><th>Service</th><th>CFN Stack</th>
                       <th>CostCenter</th><th class="num">Cost</th></tr></thead>
            <tbody>{body}</tbody>
        </table>
    </div>
"""


def _html_account_table(result: dict) -> str:
    services = result["account_level_services"]
    if not services:
        body = "<tr><td colspan='2'>No account-level charges found.</td></tr>"
    else:
        body = "".join(
            f"<tr><td>{s['service']}</td><td class='num'>${s['cost']:,.2f}</td></tr>"
            for s in services
        )
    labels = json.dumps([s["service"] for s in services[:12]])
    data = json.dumps([s["cost"] for s in services[:12]])
    return f"""    <div class="section">
        <h2>Account-Level Charges — Unassignable</h2>
        <p style="color:#666;margin-bottom:10px;">
            These charges have no resource id and cannot be tagged
            (${result["account_level_cost"]:,.2f} total). Informational only.
        </p>
        <div style="position:relative;height:320px;margin-bottom:20px;">
            <canvas id="account_chart"></canvas>
        </div>
        <table>
            <thead><tr><th>Service</th><th class="num">Cost</th></tr></thead>
            <tbody>{body}</tbody>
        </table>
    </div>
    <script>
    new Chart(document.getElementById('account_chart'), {{
        type: 'bar',
        data: {{labels: {labels},
                datasets: [{{label: 'Cost', data: {data}, backgroundColor: '#999'}}]}},
        options: {{
            responsive: true, maintainAspectRatio: false, indexAxis: 'y',
            plugins: {{legend: {{display: false}}}},
            scales: {{x: {{beginAtZero: true, ticks: {{callback: v => '$' + v.toLocaleString()}}}}}}
        }}
    }});
    </script>
"""


def _html_footer() -> str:
    return f"""    <div class="footer">
        <p>Generated {datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")} UTC
           &nbsp;|&nbsp; Data: AWS Cost and Usage Report (CUR)
           &nbsp;|&nbsp; Metric: UnblendedCost</p>
    </div>
</div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Console output + CLI
# ---------------------------------------------------------------------------


def print_console_summary(result: dict, month: str) -> None:
    """Print a console summary highlighting the actionable vs unfixable split."""
    print(f"\nUntagged Drill-Down [{month}] tag={result['tag']}")
    print("=" * 78)
    print(f"  Total month spend      ${result['total_cost']:>12,.2f}")
    print(
        f"  Untagged spend         ${result['untagged_cost']:>12,.2f}"
        f"  ({result['untagged_pct']:.1f}%)"
    )
    print(f"  -> Taggable (fixable)  ${result['taggable_cost']:>12,.2f}  <- work on this")
    print(f"  -> Account-level       ${result['account_level_cost']:>12,.2f}  (cannot be tagged)")
    if not result["tag_column_present"]:
        print(f"\n  NOTE: '{result['tag']}' tag column absent this month — all spend is untagged.")

    resources = result["taggable_resources"]
    print(f"\n  Top {len(resources)} taggable resources:")
    print("-" * 78)
    for r in resources[:20]:
        rid = r["resource_id"]
        rid = rid if len(rid) <= 48 else rid[:45] + "..."
        print(f"  ${r['cost']:>10,.2f}  {r['service']:<20.20}  {rid}")
    if not resources:
        print("  (none)")
    print("=" * 78)


def load_cur(cur_dir: Path) -> pd.DataFrame:
    """Load and concat all local CUR Parquet files, unioning varying schemas.

    Args:
        cur_dir: Root directory globbed recursively for ``**/*.parquet``.

    Returns:
        Concatenated DataFrame (missing columns filled with NaN across files).
    """
    files = find_parquet_files(cur_dir)
    print(f"Loading {len(files)} Parquet file(s) from {cur_dir} ...")
    dfs = []
    for f in files:
        try:
            dfs.append(pd.read_parquet(f))
        except Exception as exc:  # noqa: BLE001 - report and skip unreadable files
            print(f"Warning: failed to load {f}: {exc}", file=sys.stderr)
    if not dfs:
        print("Failed to load any Parquet files.", file=sys.stderr)
        sys.exit(1)
    return pd.concat(dfs, ignore_index=True)


def _build_parser() -> argparse.ArgumentParser:
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    parser = argparse.ArgumentParser(
        description="Drill untagged CUR spend into taggable vs account-level buckets.",
    )
    parser.add_argument("--month", help="Focus month YYYY-MM (default: latest in data)")
    parser.add_argument(
        "--tag", default="Application", help="Canonical tag to measure untagged spend against"
    )
    add_cur_args(parser)
    parser.add_argument("--top", type=int, default=50, help="Max taggable resources to list")
    parser.add_argument(
        "--output", default=f"reports/{today}_untagged_drill.html", help="HTML output path"
    )
    parser.add_argument("--json", dest="json_out", help="Optional JSON dump path")
    return parser


def main() -> None:
    """CLI entry point for the untagged drill-down report."""
    args = _build_parser().parse_args()
    aliases = load_tag_aliases(args.tag_aliases)
    current_ym = datetime.now(UTC).strftime("%Y-%m")

    df = add_month_column(load_cur(Path(args.cur_dir)))
    months = sorted(df["month"].dropna().unique())
    if not months:
        print("No valid month data found in CUR files.", file=sys.stderr)
        sys.exit(1)

    month = args.month or months[-1]
    if month not in months:
        print(f"Month {month} not in data. Available: {', '.join(months)}", file=sys.stderr)
        sys.exit(1)

    df_month = df[df["month"] == month]
    tag_col = resolve_tag_column(df_month.columns, args.tag, aliases)
    result = analyze_untagged(df_month, tag_col, tag=args.tag, top_n=args.top)

    print_console_summary(result, month)

    html = render_html(result, month, is_partial=(month == current_ym))
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html)
    print(f"\nHTML report: {out}")

    if args.json_out:
        json_path = Path(args.json_out)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps({"month": month, **result}, indent=2, default=str))
        print(f"JSON dump:   {json_path}")


if __name__ == "__main__":
    main()
