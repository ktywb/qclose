#!/usr/bin/env python3
"""Show the fitted data-path nodes behind qclose's Worst setup paths."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from quartus_timing_analyze import data_path_points, read_json, read_jsonl


SUMMARY_PATH_LIMIT = 20  # The Worst setup paths table in summary.md.
PIN_NAMES = {"combout", "cout", "cin", "sumout", "q", "d", "ena"}


def path_key(path: dict[str, Any]) -> tuple[Any, ...]:
    """Match a Markdown row to its detailed record without guessing by slack."""
    return tuple(
        path.get(field)
        for field in (
            "from", "to", "from_clock", "to_clock", "corner",
            "slack_ns", "data_delay_ns",
        )
    )


def compact_node(node: str) -> str:
    parts = node.split("|")
    if parts[-1] in PIN_NAMES or (
        len(parts[-1]) == 5 and parts[-1].startswith("data") and parts[-1][-1] in "abcdef"
    ):
        parts.pop()
    # Quartus appends fit/replica and technology suffixes after '~'.  Keep the
    # source-like signal name, but never claim it is an exact RTL source line.
    base = "|".join(parts).split("~", 1)[0].split("|")
    if base[:2] == ["vpart", "engine"]:
        base = base[2:]
    if "ram_ext" in base or "Memory_rtl_0" in base:
        owner = base[0] if base else "RAM"
        return f"{owner}.RAM.{base[-1]}"
    return ".".join(base)


def skeleton(path: dict[str, Any]) -> list[tuple[int, str]]:
    """Keep logical cells and the RAM port, dropping clock and route resources."""
    result: list[tuple[int, str]] = []
    for point in data_path_points(path):
        point_type = str(point.get("type", "")).lower()
        node = str(point.get("node", ""))
        is_ram_port = point_type == "ic" and "|port" in node
        if point_type != "cell" and not is_ram_port:
            continue
        if not node:
            continue
        name = compact_node(node)
        if not result or result[-1][1] != name:
            result.append((int(point.get("index", 0)), name))
    return result


def physical_points(path: dict[str, Any], include_routing: bool) -> list[str]:
    """Preserve Quartus point names and indices for auditing a compact path."""
    lines: list[str] = []
    for point in data_path_points(path):
        point_type = str(point.get("type", "")).lower()
        if not include_routing and point_type not in {"cell", "ic", "utco"}:
            continue
        node = str(point.get("node", ""))
        if not node:
            continue
        lines.append(
            f"    [{point.get('index')}] {point_type} {node} "
            f"(+{point.get('incremental_delay_ns', 0)} ns, "
            f"total={point.get('total_delay_ns', 0)} ns, "
            f"fanout={point.get('fanout', 0)})"
        )
    return lines


def render(
    summary_path: Path,
    ranks: list[int] | None,
    details: bool = False,
    routing: bool = False,
) -> str:
    if summary_path.is_dir():
        run_dir = summary_path
    elif summary_path.name in {"summary.md", "summary.json"}:
        run_dir = summary_path.parent
    else:
        raise ValueError("input must be a qclose run directory or its summary.md")

    summary = read_json(run_dir / "summary.json")
    if not isinstance(summary, dict):
        raise ValueError(f"missing or invalid {run_dir / 'summary.json'}")
    worst_paths = summary.get("timing", {}).get("worst_paths", [])[:SUMMARY_PATH_LIMIT]
    if not worst_paths:
        raise ValueError("summary has no Worst setup paths")
    selected = ranks if ranks is not None else list(range(1, len(worst_paths) + 1))
    for rank in selected:
        if rank < 1 or rank > len(worst_paths):
            raise ValueError(f"rank {rank} is outside summary.md's 1..{len(worst_paths)} range")

    detailed = read_jsonl(run_dir / "detailed_paths.jsonl")
    by_key: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for path in detailed:
        by_key.setdefault(path_key(path), []).append(path)

    lines: list[str] = []
    for rank in selected:
        row = worst_paths[rank - 1]
        matches = by_key.get(path_key(row), [])
        lines.append(
            f"#{rank} slack={row.get('slack_ns')} ns "
            f"data={row.get('data_delay_ns')} ns "
            f"levels={row.get('logic_levels')}"
        )
        lines.append(f"  from: {row.get('from', '')}")
        lines.append(f"  to:   {row.get('to', '')}")
        if not matches:
            lines.append(
                "  No detailed points for this row. Collect more --detailed-paths "
                "in a new run, then inspect that run."
            )
        else:
            if len(matches) > 1:
                lines.append(
                    f"  {len(matches)} detailed paths have the same summary fields; "
                    "showing every match."
                )
            for alternative, match in enumerate(matches, 1):
                if len(matches) > 1:
                    lines.append(f"  alternative {alternative}:")
                for index, name in skeleton(match):
                    lines.append(f"    [{index}] {name}")
                if details or routing:
                    lines.append("  Physical points (clock tree omitted):")
                    lines.extend(physical_points(match, include_routing=routing))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("summary", type=Path, help="qclose summary.md or run directory")
    parser.add_argument(
        "--rank", type=int, action="append", help="1-based Worst setup paths row; repeatable"
    )
    parser.add_argument(
        "--details", action="store_true",
        help="also show every data-path cell, input pin, and clock-to-Q point",
    )
    parser.add_argument(
        "--routing", action="store_true",
        help="also show all data-path routing points and their delays",
    )
    args = parser.parse_args(argv)
    try:
        sys.stdout.write(render(args.summary, args.rank, args.details, args.routing))
    except ValueError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
