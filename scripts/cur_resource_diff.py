#!/usr/bin/env python3
"""
CUR Resource Diff — "What's new this month".

Diffs the set of billed resources between a reporting month and its prior month
from local AWS Cost & Usage Report (CUR) Parquet data. Surfaces resources that
are NEW (first billed this month), DISAPPEARED (in prior, gone this month), and
CONTINUING resources whose cost moved materially.

Usage:
    python scripts/cur_resource_diff.py
    python scripts/cur_resource_diff.py --month 2026-06 --prior 2026-05
    python scripts/cur_resource_diff.py --min-cost 1.0 --json out.json
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

# Reuse the shared month-bucketing helper (handles tz-aware CUR timestamps).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from cur_common import add_month_column, find_parquet_files  # noqa: E402

COST_COL = "line_item_unblended_cost"
RES_COL = "line_item_resource_id"
PRODUCT_COL = "line_item_product_code"
START_COL = "line_item_usage_start_date"
STACK_COL = "resource_tags_aws_cloudformation_stack_name"
CC_COL = "resource_tags_user_costcenter"


# ---------------------------------------------------------------------------
# Classification (pure functions — no disk I/O, unit-testable)
# ---------------------------------------------------------------------------


def prior_month_of(month: str) -> str:
    """Return the calendar month immediately before ``month``.

    Args:
        month: Month string in ``YYYY-MM`` form.

    Returns:
        The preceding month as ``YYYY-MM``.
    """
    year, mon = (int(part) for part in month.split("-"))
    mon -= 1
    if mon == 0:
        year -= 1
        mon = 12
    return f"{year:04d}-{mon:02d}"


def _first_nonblank(series: pd.Series) -> str:
    """Return the first non-null, non-blank value in a Series as a string."""
    for value in series[series.notna()]:
        text = str(value).strip()
        if text:
            return text
    return ""


def _col_or_blank(df: pd.DataFrame, col: str) -> pd.Series:
    """Return ``df[col]`` if present, otherwise an all-None Series of matching length."""
    if col in df.columns:
        return df[col]
    return pd.Series([None] * len(df), index=df.index)


def _split_account_level(df: pd.DataFrame) -> tuple:
    """Split rows into (resource-bearing, account-level) frames.

    Account-level rows have an empty/blank ``line_item_resource_id`` (registrar,
    tax, and data-transfer lines that map to no taggable resource).

    Args:
        df: CUR DataFrame containing ``line_item_resource_id``.

    Returns:
        Tuple ``(resource_df, account_df)``.
    """
    res = df[RES_COL]
    has_res = res.notna() & (res.astype(str).str.strip() != "")
    return df[has_res], df[~has_res]


def _resource_frame(df_slice: pd.DataFrame) -> pd.DataFrame:
    """Aggregate a month slice to one row per resource id.

    Args:
        df_slice: Resource-bearing rows for a single month.

    Returns:
        DataFrame indexed by resource id with cost, first_billed, service,
        stack, and costcenter columns. Empty frame if the slice is empty.
    """
    if df_slice.empty:
        return pd.DataFrame(columns=["cost", "first_billed", "service", "stack", "costcenter"])
    work = df_slice[[RES_COL, COST_COL, START_COL]].copy()
    work["service"] = _col_or_blank(df_slice, PRODUCT_COL).values
    work["stack"] = _col_or_blank(df_slice, STACK_COL).values
    work["costcenter"] = _col_or_blank(df_slice, CC_COL).values
    grouped = work.groupby(RES_COL, sort=False)
    return grouped.agg(
        cost=(COST_COL, "sum"),
        first_billed=(START_COL, "min"),
        service=("service", _first_nonblank),
        stack=("stack", _first_nonblank),
        costcenter=("costcenter", _first_nonblank),
    )


def _record(rid: str, row: pd.Series, extra: dict) -> dict:
    """Build a serialisable resource record from an aggregated row."""
    return {
        "resource_id": str(rid),
        "service": row.get("service", ""),
        "stack": row.get("stack", ""),
        "costcenter": row.get("costcenter", ""),
        "first_billed": str(row.get("first_billed", "")),
        **extra,
    }


def _classify_new(month_frame, prior_ids, before_ids, min_cost) -> list:
    """Build NEW resource records (present this month, absent last month)."""
    new = []
    for rid in set(month_frame.index) - prior_ids:
        row = month_frame.loc[rid]
        cost = round(float(row["cost"]), 2)
        if cost < min_cost:
            continue
        status = "returned" if str(rid) in before_ids else "first-ever"
        new.append(_record(rid, row, {"cost": cost, "status": status}))
    return sorted(new, key=lambda r: r["cost"], reverse=True)


def _classify_disappeared(prior_frame, month_ids, min_cost) -> list:
    """Build DISAPPEARED records (present last month, gone this month)."""
    gone = []
    for rid in set(prior_frame.index) - month_ids:
        row = prior_frame.loc[rid]
        cost = round(float(row["cost"]), 2)
        if cost < min_cost:
            continue
        gone.append(_record(rid, row, {"prior_cost": cost}))
    return sorted(gone, key=lambda r: r["prior_cost"], reverse=True)


def _classify_movers(month_frame, prior_frame, min_cost) -> list:
    """Build CONTINUING mover records whose absolute cost delta >= min_cost."""
    movers = []
    for rid in set(month_frame.index) & set(prior_frame.index):
        cost_month = round(float(month_frame.loc[rid, "cost"]), 2)
        cost_prior = round(float(prior_frame.loc[rid, "cost"]), 2)
        delta = round(cost_month - cost_prior, 2)
        if abs(delta) < min_cost:
            continue
        row = month_frame.loc[rid]
        movers.append(
            _record(
                rid,
                row,
                {
                    "cost": cost_month,
                    "prior_cost": cost_prior,
                    "delta": delta,
                },
            )
        )
    return sorted(movers, key=lambda r: abs(r["delta"]), reverse=True)


def classify_resources(
    df: pd.DataFrame,
    month: str,
    prior: str,
    min_cost: float = 0.0,
) -> dict:
    """Classify resources into NEW, DISAPPEARED, and CONTINUING movers.

    Args:
        df: CUR DataFrame that already carries a ``month`` (``YYYY-MM``) column.
        month: Reporting month to analyse.
        prior: Comparison month (typically the month before ``month``).
        min_cost: Minimum month cost or absolute delta for inclusion.

    Returns:
        Dict with month/prior totals, account-level totals, ``prior_exists``,
        and ``new`` / ``disappeared`` / ``movers`` record lists.
    """
    resource_df, account_df = _split_account_level(df)
    month_frame = _resource_frame(resource_df[resource_df["month"] == month])
    prior_frame = _resource_frame(resource_df[resource_df["month"] == prior])

    before_ids = set(resource_df.loc[resource_df["month"] < month, RES_COL].astype(str))
    month_ids = set(month_frame.index)
    prior_ids = set(prior_frame.index)

    return {
        "month": month,
        "prior": prior,
        "prior_exists": bool((df["month"] == prior).any()),
        "month_total": round(float(df.loc[df["month"] == month, COST_COL].sum()), 2),
        "prior_total": round(float(df.loc[df["month"] == prior, COST_COL].sum()), 2),
        "account_level_month": round(
            float(account_df.loc[account_df["month"] == month, COST_COL].sum()), 2
        ),
        "account_level_prior": round(
            float(account_df.loc[account_df["month"] == prior, COST_COL].sum()), 2
        ),
        "new": _classify_new(month_frame, prior_ids, before_ids, min_cost),
        "disappeared": _classify_disappeared(prior_frame, month_ids, min_cost),
        "movers": _classify_movers(month_frame, prior_frame, min_cost),
    }


# ---------------------------------------------------------------------------
# Console output
# ---------------------------------------------------------------------------


def _print_table(title: str, rows: list, columns: list) -> None:
    """Print a titled fixed-width table of up to 15 rows to stdout."""
    print(f"\n{title}")
    print("=" * 100)
    if not rows:
        print("  (none)")
        return
    header = "  " + "".join(f"{label:<{width}}" for label, _, width in columns)
    print(header)
    print("-" * 100)
    for row in rows[:15]:
        line = "  " + "".join(f"{fmt(row):<{width}}" for _, fmt, width in columns)
        print(line)


def print_summary(result: dict) -> None:
    """Print a console summary of the resource diff.

    Args:
        result: Output of :func:`classify_resources`.
    """
    month, prior = result["month"], result["prior"]
    print(f"\nResource diff: {month} vs {prior}")
    print(
        f"Month total ${result['month_total']:,.2f}  |  "
        f"Prior total ${result['prior_total']:,.2f}  |  "
        f"prior_month_present={result['prior_exists']}"
    )
    print(
        f"Counts:  NEW={len(result['new'])}  "
        f"DISAPPEARED={len(result['disappeared'])}  "
        f"MOVERS={len(result['movers'])}"
    )

    _print_table(
        "Top NEW resources (first billed this month)",
        result["new"],
        [
            ("RESOURCE", lambda r: _short(r["resource_id"]), 46),
            ("SERVICE", lambda r: r["service"][:22], 24),
            ("COST", lambda r: f"${r['cost']:,.2f}", 14),
            ("STATUS", lambda r: r["status"], 12),
        ],
    )
    _print_table(
        "Top DISAPPEARED resources (billed last month, gone this month)",
        result["disappeared"],
        [
            ("RESOURCE", lambda r: _short(r["resource_id"]), 46),
            ("SERVICE", lambda r: r["service"][:22], 24),
            ("PRIOR COST", lambda r: f"${r['prior_cost']:,.2f}", 14),
        ],
    )
    _print_table(
        "Biggest cost MOVERS (continuing resources)",
        result["movers"],
        [
            ("RESOURCE", lambda r: _short(r["resource_id"]), 46),
            ("SERVICE", lambda r: r["service"][:22], 24),
            ("DELTA", lambda r: f"{'+' if r['delta'] >= 0 else ''}${r['delta']:,.2f}", 16),
        ],
    )
    print(
        f"\nFootnote: account-level (no resource id) spend — "
        f"{month}: ${result['account_level_month']:,.2f}, "
        f"{prior}: ${result['account_level_prior']:,.2f}"
    )


def _short(resource_id: str, width: int = 44) -> str:
    """Truncate long resource ids/ARNs from the left, keeping the tail visible."""
    if len(resource_id) <= width:
        return resource_id
    return "..." + resource_id[-(width - 3) :]


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------


def _rows_html(records: list, fields: list) -> str:
    """Render table body rows from records using (key, is_cost) field specs."""
    body = ""
    for rec in records:
        cells = ""
        for key, is_cost in fields:
            val = rec.get(key, "")
            if is_cost:
                cells += f'<td data-sort="{val}" class="num">${val:,.2f}</td>'
            elif isinstance(val, (int, float)):
                cells += f'<td data-sort="{val}" class="num">{val}</td>'
            else:
                cells += f'<td data-sort="{val}">{val}</td>'
        body += f"<tr>{cells}</tr>\n"
    return body


def render_html(result: dict) -> str:
    """Render a self-contained HTML resource-diff report.

    Args:
        result: Output of :func:`classify_resources`.

    Returns:
        HTML document string.
    """
    month, prior = result["month"], result["prior"]
    delta_total = round(result["month_total"] - result["prior_total"], 2)
    new_rows = _rows_html(
        result["new"],
        [
            ("resource_id", False),
            ("service", False),
            ("stack", False),
            ("costcenter", False),
            ("status", False),
            ("first_billed", False),
            ("cost", True),
        ],
    )
    gone_rows = _rows_html(
        result["disappeared"],
        [
            ("resource_id", False),
            ("service", False),
            ("stack", False),
            ("costcenter", False),
            ("prior_cost", True),
        ],
    )
    mover_rows = _rows_html(
        result["movers"],
        [
            ("resource_id", False),
            ("service", False),
            ("stack", False),
            ("costcenter", False),
            ("cost", True),
            ("prior_cost", True),
            ("delta", True),
        ],
    )
    return _HTML_TEMPLATE.format(
        month=month,
        prior=prior,
        generated=datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        month_total=f"{result['month_total']:,.2f}",
        prior_total=f"{result['prior_total']:,.2f}",
        delta_total=f"{'+' if delta_total >= 0 else ''}{delta_total:,.2f}",
        n_new=len(result["new"]),
        n_gone=len(result["disappeared"]),
        n_movers=len(result["movers"]),
        acct_month=f"{result['account_level_month']:,.2f}",
        acct_prior=f"{result['account_level_prior']:,.2f}",
        new_rows=new_rows,
        gone_rows=gone_rows,
        mover_rows=mover_rows,
    )


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CUR Resource Diff – {month} vs {prior}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;
         background: #f5f5f5; padding: 20px; line-height: 1.5; color: #333; }}
  .container {{ max-width: 1400px; margin: 0 auto; }}
  .header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white;
            padding: 32px; border-radius: 10px; margin-bottom: 24px; }}
  .header h1 {{ font-size: 2em; margin-bottom: 8px; }}
  .summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
             gap: 16px; margin-bottom: 24px; }}
  .card {{ background: white; padding: 20px; border-radius: 10px;
          box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
  .card h3 {{ color: #666; font-size: 0.8em; text-transform: uppercase; margin-bottom: 8px; }}
  .card .value {{ font-size: 1.6em; font-weight: bold; }}
  .section {{ background: white; padding: 24px; border-radius: 10px; margin-bottom: 24px;
             box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
  .section h2 {{ font-size: 1.4em; margin-bottom: 12px; border-bottom: 3px solid #667eea;
                padding-bottom: 8px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.85em; }}
  th, td {{ padding: 8px 10px; text-align: left; border-bottom: 1px solid #eee;
           white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 320px; }}
  th {{ background: #f8f9fa; cursor: pointer; user-select: none; position: sticky; top: 0; }}
  th:hover {{ background: #eef0ff; }}
  td.num, th.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  tr:hover {{ background: #f8f9fa; }}
  .footer {{ text-align: center; color: #666; margin-top: 24px; font-size: 0.85em; }}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>CUR Resource Diff</h1>
    <p>Reporting month <strong>{month}</strong> vs prior <strong>{prior}</strong>
       &nbsp;|&nbsp; Generated {generated}</p>
  </div>
  <div class="summary">
    <div class="card"><h3>{month} Total</h3><div class="value">${month_total}</div></div>
    <div class="card"><h3>{prior} Total</h3><div class="value">${prior_total}</div></div>
    <div class="card"><h3>Total Change</h3><div class="value">${delta_total}</div></div>
    <div class="card"><h3>New</h3><div class="value">{n_new}</div></div>
    <div class="card"><h3>Disappeared</h3><div class="value">{n_gone}</div></div>
    <div class="card"><h3>Movers</h3><div class="value">{n_movers}</div></div>
  </div>

  <div class="section">
    <h2>New Resources ({n_new})</h2>
    <table class="sortable">
      <thead><tr>
        <th>Resource ID</th><th>Service</th><th>Stack</th><th>Cost Center</th>
        <th>Status</th><th>First Billed</th><th class="num">Cost</th>
      </tr></thead>
      <tbody>{new_rows}</tbody>
    </table>
  </div>

  <div class="section">
    <h2>Disappeared Resources ({n_gone})</h2>
    <table class="sortable">
      <thead><tr>
        <th>Resource ID</th><th>Service</th><th>Stack</th><th>Cost Center</th>
        <th class="num">Prior Cost</th>
      </tr></thead>
      <tbody>{gone_rows}</tbody>
    </table>
  </div>

  <div class="section">
    <h2>Cost Movers ({n_movers})</h2>
    <table class="sortable">
      <thead><tr>
        <th>Resource ID</th><th>Service</th><th>Stack</th><th>Cost Center</th>
        <th class="num">Cost</th><th class="num">Prior Cost</th><th class="num">Delta</th>
      </tr></thead>
      <tbody>{mover_rows}</tbody>
    </table>
  </div>

  <div class="footer">
    <p>Account-level (no resource id) spend &mdash;
       {month}: ${acct_month} &nbsp;|&nbsp; {prior}: ${acct_prior}</p>
    <p>Data: AWS Cost and Usage Report (CUR) &nbsp;|&nbsp; Metric: UnblendedCost</p>
  </div>
</div>
<script>
document.querySelectorAll('table.sortable th').forEach(function (th, idx) {{
  th.addEventListener('click', function () {{
    var tbody = th.closest('table').querySelector('tbody');
    var rows = Array.from(tbody.querySelectorAll('tr'));
    var asc = !(th.dataset.asc === 'true');
    th.dataset.asc = asc;
    rows.sort(function (a, b) {{
      var av = a.children[idx].dataset.sort || a.children[idx].textContent;
      var bv = b.children[idx].dataset.sort || b.children[idx].textContent;
      var an = parseFloat(av), bn = parseFloat(bv);
      if (!isNaN(an) && !isNaN(bn)) {{ return asc ? an - bn : bn - an; }}
      return asc ? av.localeCompare(bv) : bv.localeCompare(av);
    }});
    rows.forEach(function (r) {{ tbody.appendChild(r); }});
  }});
}});
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _load_parquet(cur_dir: Path) -> pd.DataFrame:
    """Load and concat all Parquet files under ``cur_dir`` (schema-tolerant).

    Args:
        cur_dir: Directory to glob for ``**/*.parquet``.

    Returns:
        Concatenated DataFrame; exits the process if no files are readable.
    """
    files = find_parquet_files(cur_dir)
    print(f"Loading {len(files)} Parquet file(s) from {cur_dir} ...")
    frames = []
    for path in files:
        try:
            frames.append(pd.read_parquet(path))
        except Exception as exc:  # noqa: BLE001 - report and skip unreadable files
            print(f"Warning: failed to load {path}: {exc}", file=sys.stderr)
    if not frames:
        print("Failed to load any Parquet files.", file=sys.stderr)
        sys.exit(1)
    return pd.concat(frames, ignore_index=True)


def main() -> None:
    """CLI entry point for the CUR resource diff report."""
    today = datetime.now(UTC)
    default_output = f"reports/{today.strftime('%Y-%m-%d')}_resource_diff.html"

    parser = argparse.ArgumentParser(
        description="Diff billed resources between a month and its prior month.",
    )
    parser.add_argument("--month", help="Reporting month YYYY-MM (default: latest in data)")
    parser.add_argument("--prior", help="Comparison month YYYY-MM (default: month before --month)")
    parser.add_argument(
        "--cur-dir",
        default="cur-data",
        help="Local directory holding the CUR Parquet export (default: cur-data)",
    )
    parser.add_argument(
        "--min-cost",
        type=float,
        default=0.0,
        help="Minimum month cost or absolute delta to include",
    )
    parser.add_argument("--output", default=default_output, help="HTML output path")
    parser.add_argument("--json", dest="json_out", help="Optional JSON dump path")
    args = parser.parse_args()

    df = add_month_column(_load_parquet(Path(args.cur_dir)))
    months_present = sorted(df["month"].dropna().unique())
    if not months_present:
        print("No valid month data found in CUR files.", file=sys.stderr)
        sys.exit(1)

    month = args.month or months_present[-1]
    prior = args.prior or prior_month_of(month)
    if month not in months_present:
        print(
            f"Month {month} not found in data. Available: {', '.join(months_present)}",
            file=sys.stderr,
        )
        sys.exit(1)

    result = classify_resources(df, month, prior, args.min_cost)
    if not result["prior_exists"]:
        print(f"Note: prior month {prior} has no data; every resource is treated as NEW.")

    print_summary(result)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_html(result))
    print(f"\nHTML report: {output_path}")

    if args.json_out:
        json_path = Path(args.json_out)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(result, indent=2, default=str))
        print(f"JSON dump: {json_path}")


if __name__ == "__main__":
    main()
