"""Shared helpers for the AWS cost optimization toolkit scripts."""

from __future__ import annotations

import csv
import io
import json
import sys
from argparse import ArgumentParser, Namespace
from collections.abc import Callable, Iterator, Sequence
from typing import Any, NoReturn

import boto3

OUTPUT_FORMATS = ("table", "markdown", "csv", "json")


def add_common_args(parser: ArgumentParser) -> None:
    """Add the --profile/--region/--output arguments every script accepts."""
    parser.add_argument(
        "--profile", help="AWS named profile to use (default: environment credentials)"
    )
    parser.add_argument(
        "--region", help="AWS region to query (default: profile/environment region)"
    )
    parser.add_argument(
        "--output",
        choices=OUTPUT_FORMATS,
        default="table",
        help="Output format (default: table)",
    )


def make_session(args: Namespace) -> boto3.Session:
    """Build a boto3 session from the common CLI arguments."""
    return boto3.Session(profile_name=args.profile, region_name=args.region)


def render(rows: Sequence[dict[str, Any]], columns: Sequence[str], fmt: str) -> str:
    """Render rows in the requested output format.

    Args:
        rows: Records to render. Missing keys render as an empty cell.
        columns: Column keys, in display order.
        fmt: One of ``table``, ``markdown``, ``csv``, or ``json``.

    Returns:
        The rendered text, without a trailing newline.
    """
    if fmt == "json":
        return json.dumps([{c: r.get(c) for c in columns} for r in rows], indent=2, default=str)
    if fmt == "csv":
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        return buffer.getvalue().rstrip("\n")

    cells = [[_fmt(r.get(c)) for c in columns] for r in rows]
    widths = [
        max(len(str(c)), *(len(row[i]) for row in cells)) if cells else len(str(c))
        for i, c in enumerate(columns)
    ]

    if fmt == "markdown":
        lines = [
            "| " + " | ".join(str(c).ljust(w) for c, w in zip(columns, widths, strict=True)) + " |",
            "| " + " | ".join("-" * w for w in widths) + " |",
        ]
        lines += [
            "| " + " | ".join(v.ljust(w) for v, w in zip(row, widths, strict=True)) + " |"
            for row in cells
        ]
        return "\n".join(lines)

    lines = ["  ".join(str(c).ljust(w) for c, w in zip(columns, widths, strict=True)).rstrip()]
    lines.append("  ".join("-" * w for w in widths))
    lines += [
        "  ".join(v.ljust(w) for v, w in zip(row, widths, strict=True)).rstrip() for row in cells
    ]
    return "\n".join(lines)


def _fmt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:,.2f}"
    return str(value)


def next_token_pages(
    call: Callable[..., dict[str, Any]], result_key: str, **kwargs: Any
) -> Iterator[dict[str, Any]]:
    """Yield every item under ``result_key`` by following ``nextToken`` manually.

    Several AWS APIs (Compute Optimizer among them) ship without botocore paginators,
    so the token loop has to be written by hand.
    """
    token: str | None = None
    while True:
        response = call(**kwargs, **({"nextToken": token} if token else {}))
        yield from response.get(result_key, [])
        token = response.get("nextToken")
        if not token:
            return


def die(message: str) -> NoReturn:
    """Print an actionable error to stderr and exit non-zero."""
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)
