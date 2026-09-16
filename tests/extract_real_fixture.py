#!/usr/bin/env python3
"""Minimize an existing qclose run into a real-data regression fixture."""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
from pathlib import Path
from typing import Any


ANALYZER_PATH = Path(__file__).resolve().parents[1] / "quartus_timing_analyze.py"
SPEC = importlib.util.spec_from_file_location("quartus_timing_analyze", ANALYZER_PATH)
assert SPEC and SPEC.loader
QTA = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(QTA)


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main_path_blocks(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    starts = [index for index, line in enumerate(lines) if re.fullmatch(r"Path #\d+", line.strip())]
    blocks = []
    for position, start in enumerate(starts):
        stop = starts[position + 1] if position + 1 < len(starts) else len(lines)
        for index in range(start + 1, stop):
            if re.fullmatch(r"Path #\d+ - Path (?:Before|After) #\d+", lines[index].strip()):
                stop = index
                break
        blocks.append("\n".join(lines[start:stop]).rstrip() + "\n")
    return blocks


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_run", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--paths", type=int, default=10)
    args = parser.parse_args()

    source = args.source_run.resolve()
    destination = args.destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)

    detailed = QTA.read_jsonl(source / "detailed_paths.jsonl")
    detailed.sort(key=lambda item: QTA.numeric(item.get("slack_ns")) or float("inf"))
    selected = detailed[: args.paths]
    (destination / "detailed_paths.jsonl").write_text(
        "".join(json.dumps(item, sort_keys=True) + "\n" for item in selected), encoding="utf-8"
    )

    blocks = main_path_blocks(source / "neighbor_paths.rpt")
    if len(blocks) < len(selected):
        raise RuntimeError("neighbor_paths.rpt contains fewer main paths than the selected detailed paths")
    (destination / "neighbor_paths.rpt").write_text("\n".join(blocks[: len(selected)]), encoding="utf-8")
    (destination / "bottlenecks.rpt").write_text(
        (source / "bottlenecks.rpt").read_text(encoding="utf-8", errors="replace"), encoding="utf-8"
    )

    summary = QTA.read_json(source / "summary.json", {})
    structured = summary.get("diagnostics", {}).get("structured", {})
    identities = [
        node["identity"]
        for path in selected
        for node in QTA.logical_path_node_records(path)
    ]
    evidence = {}
    for category in (
        "high_fanout",
        "register_spread",
        "route_nets",
        "highest_wire_count",
        "peak_wire_details",
        "retiming_restrictions",
        "retiming_limits",
    ):
        records = []
        for record in structured.get(category, {}).get("records", []):
            confidence, _ = QTA.node_match_confidence(record, identities, "")
            if confidence >= 0.60 or category == "retiming_limits":
                records.append(record)
        if records:
            evidence[category] = {
                "source": structured[category].get("source"),
                "columns": structured[category].get("columns", []),
                "record_count": len(records),
                "records": records,
                "truncated": False,
            }
    write_json(destination / "structured_evidence.json", evidence)

    normalization_samples = []
    spread_columns, spread_rows = QTA.ascii_table(
        source / "register_spread.rpt", r"register name.*register location"
    )
    if "Register Name" in spread_columns:
        node_index = spread_columns.index("Register Name")
        normalization_samples = [
            row[node_index]
            for row in spread_rows
            if node_index < len(row) and re.search(r"~(?:RTM|ERTM|DUPLICATE)|(?:~_|_)Duplicate", row[node_index], re.I)
        ][:10]
    write_json(destination / "normalization_samples.json", normalization_samples)

    # Preserve real false-positive controls alongside positive evidence.  The
    # selected names must all occur in the source run; extraction fails rather
    # than silently replacing them with synthetic examples.
    all_nodes = {
        str(point.get("node", ""))
        for path in detailed
        for point in path.get("points", [])
        if point.get("node")
    }
    bottleneck_columns, bottleneck_rows = QTA.bottleneck_table(source / "bottlenecks.rpt", set())
    if "Node" in bottleneck_columns:
        node_index = bottleneck_columns.index("Node")
        all_nodes.update(row[node_index] for row in bottleneck_rows if node_index < len(row))
    all_nodes.update(normalization_samples)

    def require(pattern: str) -> str:
        match = next((node for node in sorted(all_nodes) if re.search(pattern, node)), None)
        if not match:
            raise RuntimeError(f"real negative-control node missing: {pattern}")
        return match

    add16 = require(r"pes_0\|writeComb\|add_0~16\|cin$")
    add56 = require(r"pes_0\|writeComb\|add_0~56\|cin$")
    lane280 = require(r"procDataReg\[280\]~ENA_dff$")
    lane287 = require(r"procDataReg\[287\]~ENA_dff$")
    negative_controls = [
        {
            "name": "similar generated name 16 versus 56",
            "record": add16,
            "path_node": add56,
            "hierarchy": "",
            "expected_method": "none",
            "expected_confidence": 0.0,
        },
        {
            "name": "different bus lane is normalized evidence only",
            "record": lane280,
            "path_node": lane287,
            "hierarchy": "",
            "expected_method": "normalized-node",
            "expected_confidence": 0.65,
        },
    ]
    write_json(destination / "negative_controls.json", negative_controls)

    timing_metadata = QTA.read_json(source / "timing_metadata.json", {})
    write_json(
        destination / "timing_metadata.json",
        {
            key: timing_metadata.get(key)
            for key in ("project", "revision", "quartus_version", "paths_per_clock", "detailed_paths")
        },
    )
    write_json(
        destination / "fixture_metadata.json",
        {
            "source_run": source.name,
            "origin": "Quartus Prime Pro 25.1 final fitted Agilex 7 database",
            "selected_paths": len(selected),
            "minimized_by": "tests/extract_real_fixture.py",
        },
    )


if __name__ == "__main__":
    main()
