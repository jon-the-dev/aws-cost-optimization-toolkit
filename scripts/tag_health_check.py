#!/usr/bin/env python3
"""
Tag Health Check

Deterministic guardrail that inspects cost-allocation tag health across the
local CUR Parquet files and flags drift that silently breaks cost attribution.

The primary motivating bug: a tag key gets renamed (e.g. ``costcenter`` ->
``cost_center``), producing two CUR columns that map to the same canonical tag.
Cost-center spend then splits across both columns and reporting silently uses
only one. This tool catches that and related classes of tag drift:

  1. Alias collisions  - two+ columns mapping to one canonical tag (primary).
  2. Disappearances    - a tag column present earlier, absent later.
  3. Non-canonical     - user tag columns whose suffix is unknown.
  4. Presence matrix   - which tag columns exist in which month.

Reads schemas cheaply via ``pyarrow.parquet.read_schema`` and only loads column
data for months where a collision is detected, to quantify the cost split.

Usage:
    python scripts/tag_health_check.py
    python scripts/tag_health_check.py --strict
    python scripts/tag_health_check.py --output reports/health.html
"""

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cur_common import DEFAULT_TAG_ALIASES as _DEFAULT_ALIASES  # noqa: E402
from cur_common import (  # noqa: E402
    SYSTEM_TAG_PREFIX,
    USER_TAG_PREFIX,
    add_cur_args,
    find_parquet_files,
    load_tag_aliases,
)

# Replaced by main() when --tag-aliases is passed.
_TAG_ALIASES = dict(_DEFAULT_ALIASES)
COST_COL = "line_item_unblended_cost"


# ---------------------------------------------------------------------------
# Pure detection functions (operate on {month: [user tag columns]})
# ---------------------------------------------------------------------------


def alias_to_canonical() -> dict:
    """Build a reverse map of tag-column suffix -> canonical PascalCase key.

    Returns:
        Dict mapping each accepted alias suffix to its canonical key.
    """
    lookup: dict = {}
    for canonical, aliases in _TAG_ALIASES.items():
        for alias in aliases:
            lookup[alias] = canonical
    return lookup


def canonical_of(column: str) -> str | None:
    """Map a ``resource_tags_user_*`` column to its canonical key, if known.

    Args:
        column: A user tag column name.

    Returns:
        Canonical PascalCase key, or None if the suffix is non-canonical.
    """
    if not column.startswith(USER_TAG_PREFIX):
        return None
    suffix = column[len(USER_TAG_PREFIX) :]
    return alias_to_canonical().get(suffix)


def find_collisions(month_columns: dict) -> dict:
    """Find canonical tags served by two+ columns in the same month.

    Args:
        month_columns: Mapping of ``YYYY-MM`` -> list of user tag columns.

    Returns:
        Mapping of canonical key -> ``{"columns": [...], "months": {month: [...]}}``
        for every canonical with a collision in at least one month. ``columns``
        is the union of colliding columns across all months (sorted).
    """
    per_month_groups: dict = {}
    for month, columns in month_columns.items():
        groups: dict = {}
        for col in columns:
            canonical = canonical_of(col)
            if canonical is not None:
                groups.setdefault(canonical, []).append(col)
        per_month_groups[month] = groups

    result: dict = {}
    for month in sorted(per_month_groups):
        for canonical, cols in per_month_groups[month].items():
            if len(cols) < 2:
                continue
            entry = result.setdefault(canonical, {"columns": set(), "months": {}})
            entry["columns"].update(cols)
            entry["months"][month] = sorted(cols)

    return {c: {"columns": sorted(v["columns"]), "months": v["months"]} for c, v in result.items()}


def find_disappearances(month_columns: dict) -> list:
    """Find columns present in an earlier month but absent in a later one.

    A column that only appears in later months (a new addition) is not flagged;
    only a column whose last appearance precedes a subsequent month is.

    Args:
        month_columns: Mapping of ``YYYY-MM`` -> list of user tag columns.

    Returns:
        List of ``{"column", "last_present", "missing_in"}`` dicts, sorted by
        column name.
    """
    months = sorted(month_columns)
    if not months:
        return []

    present: dict = {}
    for month in months:
        for col in month_columns[month]:
            present.setdefault(col, []).append(month)

    findings = []
    for col, seen in present.items():
        last_idx = months.index(seen[-1])
        later = months[last_idx + 1 :]
        if later:
            findings.append(
                {
                    "column": col,
                    "last_present": seen[-1],
                    "missing_in": later,
                }
            )

    return sorted(findings, key=lambda f: f["column"])


def find_non_canonical(month_columns: dict) -> dict:
    """Find user tag columns whose suffix is not a known canonical alias.

    Args:
        month_columns: Mapping of ``YYYY-MM`` -> list of user tag columns.

    Returns:
        Mapping of non-canonical column name -> sorted list of months present.
    """
    result: dict = {}
    for month in sorted(month_columns):
        for col in month_columns[month]:
            if canonical_of(col) is None:
                result.setdefault(col, []).append(month)
    return {col: sorted(months) for col, months in sorted(result.items())}


def presence_matrix(month_columns: dict) -> tuple:
    """Build a column-by-month presence matrix.

    Args:
        month_columns: Mapping of ``YYYY-MM`` -> list of user tag columns.

    Returns:
        Tuple ``(months, columns, matrix)`` where ``matrix[column][month]`` is a
        bool. ``months`` and ``columns`` are sorted.
    """
    months = sorted(month_columns)
    columns: set = set()
    for cols in month_columns.values():
        columns.update(cols)
    columns_sorted = sorted(columns)

    matrix = {
        col: {month: col in month_columns[month] for month in months} for col in columns_sorted
    }
    return months, columns_sorted, matrix


# ---------------------------------------------------------------------------
# CUR scanning + cost quantification
# ---------------------------------------------------------------------------


def month_from_path(path) -> str | None:
    """Derive a ``YYYY-MM`` month from a CUR Parquet file path.

    Robust to single-digit (``month=7``) and zero-padded (``month=07``) path
    segments; normalizes to zero-padded ``YYYY-MM``.

    Args:
        path: File path containing ``year=YYYY`` and ``month=M`` segments.

    Returns:
        Normalized ``YYYY-MM`` string, or None if the pattern is absent.
    """
    match = re.search(r"year=(\d{4}).*?month=(\d{1,2})", str(path))
    if not match:
        return None
    year, month = match.group(1), int(match.group(2))
    return f"{year}-{month:02d}"


def scan_cur_dir(cur_dir: Path) -> dict:
    """Scan CUR Parquet schemas cheaply and group tag columns by month.

    Args:
        cur_dir: Root directory to glob for ``**/*.parquet``.

    Returns:
        Mapping of ``YYYY-MM`` -> ``{"files": [...], "user_tags": [...],
        "system_tags": [...]}``. Tag lists are the union across the month's files.
    """
    files = find_parquet_files(cur_dir)
    scan: dict = {}
    for path in files:
        month = month_from_path(path)
        if month is None:
            print(f"Warning: cannot derive month from {path}; skipping.", file=sys.stderr)
            continue
        names = pq.read_schema(path).names
        entry = scan.setdefault(month, {"files": [], "user_tags": set(), "system_tags": set()})
        entry["files"].append(path)
        entry["user_tags"].update(n for n in names if n.startswith(USER_TAG_PREFIX))
        entry["system_tags"].update(n for n in names if n.startswith(SYSTEM_TAG_PREFIX))

    return {
        month: {
            "files": entry["files"],
            "user_tags": sorted(entry["user_tags"]),
            "system_tags": sorted(entry["system_tags"]),
        }
        for month, entry in scan.items()
    }


def _column_costs(files: list, cols: list) -> dict:
    """Sum unblended cost carried by each column (non-null, non-empty).

    Args:
        files: Parquet file paths for a single month.
        cols: Colliding tag columns to quantify.

    Returns:
        Mapping of column name -> rounded cost sitting in that column.
    """
    needed = list(cols) + [COST_COL]
    frames = [pd.read_parquet(f, columns=needed) for f in files]
    df = pd.concat(frames, ignore_index=True)

    out: dict = {}
    for col in cols:
        mask = df[col].notna() & (df[col].astype(str) != "")
        out[col] = round(float(df.loc[mask, COST_COL].sum()), 2)
    return out


def quantify_collision_costs(scan: dict, collisions: dict) -> dict:
    """Quantify the per-column cost split for each collision month.

    Only loads data for months where a collision exists, projecting only the
    colliding columns plus the cost column.

    Args:
        scan: Output of :func:`scan_cur_dir`.
        collisions: Output of :func:`find_collisions`.

    Returns:
        Mapping ``canonical -> month -> {column: cost}``.
    """
    result: dict = {}
    for canonical, info in collisions.items():
        result[canonical] = {}
        for month, cols in info["months"].items():
            result[canonical][month] = _column_costs(scan[month]["files"], cols)
    return result


def build_findings(scan: dict) -> dict:
    """Run all detections and assemble a findings dict.

    Args:
        scan: Output of :func:`scan_cur_dir`.

    Returns:
        Findings dict with presence matrix, collisions (with cost split),
        disappearances, non-canonical columns, and system tags.
    """
    month_columns = {month: entry["user_tags"] for month, entry in scan.items()}
    months, columns, matrix = presence_matrix(month_columns)

    collisions = find_collisions(month_columns)
    cost_split = quantify_collision_costs(scan, collisions)

    system_tags = sorted({t for entry in scan.values() for t in entry["system_tags"]})

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "months": months,
        "columns": columns,
        "presence_matrix": matrix,
        "collisions": collisions,
        "collision_cost_split": cost_split,
        "disappearances": find_disappearances(month_columns),
        "non_canonical": find_non_canonical(month_columns),
        "system_tags": system_tags,
    }


def has_blocking_findings(findings: dict) -> bool:
    """Return True if any collision or disappearance was found (for --strict)."""
    return bool(findings["collisions"]) or bool(findings["disappearances"])


# ---------------------------------------------------------------------------
# Console rendering
# ---------------------------------------------------------------------------


def _print_presence_matrix(findings: dict) -> None:
    months = findings["months"]
    columns = findings["columns"]
    print("\n🗂️  Cost-Allocation Tag Presence Matrix")
    print("=" * 80)
    if not columns:
        print("  (no user tag columns found)")
        return
    width = max(len(c) for c in columns) + 2
    print(f"{'column'.ljust(width)}{'  '.join(months)}")
    for col in columns:
        cells = "  ".join(" ✅ " if findings["presence_matrix"][col][m] else " ·· " for m in months)
        canonical = canonical_of(col) or "NON-CANONICAL"
        print(f"{col.ljust(width)}{cells}   -> {canonical}")


def _print_collisions(findings: dict) -> None:
    collisions = findings["collisions"]
    print("\n🚨 Alias Collisions (multiple columns -> one canonical tag)")
    print("=" * 80)
    if not collisions:
        print("  ✅ None. Each canonical tag is served by at most one column per month.")
        return
    split = findings["collision_cost_split"]
    for canonical, info in collisions.items():
        print(f"\n  ❌ {canonical}: {len(info['columns'])} columns -> {info['columns']}")
        for month in sorted(info["months"]):
            costs = split.get(canonical, {}).get(month, {})
            total = sum(costs.values())
            print(f"     {month}: cost split across columns (total ${total:,.2f})")
            for col, cost in sorted(costs.items(), key=lambda kv: kv[1], reverse=True):
                pct = cost / total * 100 if total else 0.0
                print(f"        {col:<45} ${cost:>12,.2f} ({pct:5.1f}%)")


def _print_disappearances(findings: dict) -> None:
    disappearances = findings["disappearances"]
    print("\n⚠️  Column Disappearances (attribution silently regressed)")
    print("=" * 80)
    if not disappearances:
        print("  ✅ None. No tag column vanished in a later month.")
        return
    for item in disappearances:
        print(
            f"  ❌ {item['column']} last seen {item['last_present']}, "
            f"missing in {', '.join(item['missing_in'])}"
        )


def _print_non_canonical(findings: dict) -> None:
    non_canonical = findings["non_canonical"]
    print("\n🔍 Non-Canonical User Tag Columns")
    print("=" * 80)
    if not non_canonical:
        print("  ✅ None. Every user tag column maps to a known canonical key.")
        return
    for col, months in non_canonical.items():
        print(f"  ⚠️  {col}  (present: {', '.join(months)})")


def render_console(findings: dict) -> None:
    """Print the full tag-health report to stdout."""
    print("\n" + "=" * 80)
    print("AWS FinOps — Cost-Allocation Tag Health Check")
    print(f"Generated: {findings['generated_at']}  |  Months: {', '.join(findings['months'])}")
    print("=" * 80)
    _print_presence_matrix(findings)
    _print_collisions(findings)
    _print_disappearances(findings)
    _print_non_canonical(findings)
    if findings["system_tags"]:
        print("\nℹ️  System tags (ignored for collisions): " + ", ".join(findings["system_tags"]))
    print()


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------


def _html_matrix(findings: dict) -> str:
    months = findings["months"]
    head = "".join(f"<th>{m}</th>" for m in months)
    rows = ""
    for col in findings["columns"]:
        canonical = canonical_of(col) or '<span class="bad">NON-CANONICAL</span>'
        cells = "".join(
            f'<td class="{"ok" if findings["presence_matrix"][col][m] else "miss"}">'
            f"{'✓' if findings['presence_matrix'][col][m] else '·'}</td>"
            for m in months
        )
        rows += f"<tr><td class='mono'>{col}</td>{cells}<td>{canonical}</td></tr>"
    return f"""<h2>Tag Presence Matrix</h2>
    <table><thead><tr><th>Column</th>{head}<th>Canonical</th></tr></thead>
    <tbody>{rows}</tbody></table>"""


def _html_collisions(findings: dict) -> str:
    collisions = findings["collisions"]
    if not collisions:
        return '<h2>Alias Collisions</h2><p class="ok">None detected.</p>'
    split = findings["collision_cost_split"]
    blocks = ""
    for canonical, info in collisions.items():
        rows = ""
        for month in sorted(info["months"]):
            costs = split.get(canonical, {}).get(month, {})
            total = sum(costs.values()) or 0.0
            for col, cost in sorted(costs.items(), key=lambda kv: kv[1], reverse=True):
                pct = cost / total * 100 if total else 0.0
                rows += (
                    f"<tr><td>{month}</td><td class='mono'>{col}</td>"
                    f"<td class='num'>${cost:,.2f}</td><td class='num'>{pct:.1f}%</td></tr>"
                )
        blocks += f"""<h3 class="bad">❌ {canonical} — {len(info["columns"])} columns</h3>
        <p class="mono">{", ".join(info["columns"])}</p>
        <table><thead><tr><th>Month</th><th>Column</th><th class='num'>Cost</th>
        <th class='num'>Share</th></tr></thead><tbody>{rows}</tbody></table>"""
    return f"<h2 class='bad'>Alias Collisions</h2>{blocks}"


def _html_lists(findings: dict) -> str:
    disappearances = findings["disappearances"]
    if disappearances:
        dis_items = "".join(
            f"<li class='mono'>{d['column']} — last {d['last_present']}, "
            f"missing in {', '.join(d['missing_in'])}</li>"
            for d in disappearances
        )
        dis = f"<ul>{dis_items}</ul>"
    else:
        dis = '<p class="ok">None detected.</p>'

    non_canonical = findings["non_canonical"]
    if non_canonical:
        nc_items = "".join(
            f"<li class='mono'>{col} (present: {', '.join(months)})</li>"
            for col, months in non_canonical.items()
        )
        nc = f"<ul>{nc_items}</ul>"
    else:
        nc = '<p class="ok">None detected.</p>'

    return f"<h2>Column Disappearances</h2>{dis}<h2>Non-Canonical Columns</h2>{nc}"


def render_html(findings: dict) -> str:
    """Render a self-contained HTML tag-health report.

    Args:
        findings: Output of :func:`build_findings`.

    Returns:
        HTML document string.
    """
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AWS Tag Health Check</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;
         background:#f5f5f5; padding:24px; color:#222; line-height:1.5; }}
  .container {{ max-width:1200px; margin:0 auto; }}
  h1 {{ margin-bottom:4px; }}
  .meta {{ color:#666; margin-bottom:24px; }}
  h2 {{ border-bottom:3px solid #667eea; padding-bottom:6px; margin-top:32px; }}
  table {{ width:100%; border-collapse:collapse; margin:12px 0; background:#fff; }}
  th, td {{ padding:8px 10px; border-bottom:1px solid #ddd; text-align:left; }}
  th {{ background:#f0f0f5; }}
  .num {{ text-align:right; }}
  .mono {{ font-family: ui-monospace, Menlo, monospace; font-size:0.9em; }}
  .ok {{ color:#155724; }}
  .bad {{ color:#721c24; }}
  td.ok {{ color:#155724; text-align:center; }}
  td.miss {{ color:#bbb; text-align:center; }}
</style></head>
<body><div class="container">
<h1>AWS FinOps — Cost-Allocation Tag Health Check</h1>
<p class="meta">Generated {findings["generated_at"]}
&nbsp;|&nbsp; Months: {", ".join(findings["months"])}</p>
{_html_collisions(findings)}
{_html_lists(findings)}
{_html_matrix(findings)}
<p class="meta">System tags (ignored for collisions):
{", ".join(findings["system_tags"]) or "none"}</p>
</div></body></html>
"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    """CLI entry point for the tag health check."""
    parser = argparse.ArgumentParser(
        description="Inspect cost-allocation tag health across local CUR Parquet files.",
    )
    add_cur_args(parser)
    parser.add_argument("--output", help="Optional HTML report output path")
    parser.add_argument("--json", dest="json_out", help="Optional JSON findings output path")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 1 if any collision or disappearance is found (CI guardrail)",
    )
    args = parser.parse_args()

    global _TAG_ALIASES
    _TAG_ALIASES = load_tag_aliases(args.tag_aliases)

    scan = scan_cur_dir(Path(args.cur_dir))
    findings = build_findings(scan)

    render_console(findings)

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_html(findings))
        print(f"HTML report: {out}")

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(findings, indent=2, default=str))
        print(f"JSON findings: {out}")

    if args.strict and has_blocking_findings(findings):
        print("STRICT: blocking findings detected (collision or disappearance).", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
