#!/usr/bin/env python3
"""Collect, normalize, summarize, and compare Quartus timing results.

Quartus-specific objects are collected by the sibling Tcl scripts.  This
wrapper deliberately keeps analysis outside Quartus' Tcl runtime so that the
output schema and historical comparisons are easy to test and maintain.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_ROOT = Path("logs/timing-analysis")
SOURCE_SUFFIXES = {".scala", ".v", ".sv", ".sdc", ".qsf", ".tcl"}


def read_json(path: Path, default: Any = None) -> Any:
    try:
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            # Quartus report panels use the process locale and may contain
            # Latin-1 trademark characters even when all node names are ASCII.
            text = raw.decode("latin-1")
        return json.loads(text)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not path.exists():
        return records
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"Invalid JSONL at {path}:{line_number}: {error}") from error
        if isinstance(value, dict):
            records.append(value)
    return records


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def numeric(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = re.search(r"[-+]?\d+(?:\.\d+)?", value.replace(",", ""))
        if match:
            try:
                return float(match.group(0))
            except ValueError:
                pass
    return None


def format_number(value: Any, digits: int = 3, suffix: str = "") -> str:
    number = numeric(value)
    if number is None:
        return "—"
    return f"{number:.{digits}f}{suffix}"


def markdown_cell(value: Any) -> str:
    return str(value if value not in (None, "") else "—").replace("|", "\\|").replace("\n", " ")


def markdown_table(headers: list[str], rows: Iterable[Iterable[Any]], limit: int | None = None) -> str:
    selected = list(rows)
    if limit is not None:
        selected = selected[:limit]
    if not headers:
        return ""
    lines = [
        "| " + " | ".join(markdown_cell(item) for item in headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in selected:
        values = list(row)
        values = (values + [""] * len(headers))[: len(headers)]
        lines.append("| " + " | ".join(markdown_cell(item) for item in values) + " |")
    return "\n".join(lines)


def run_logged(command: list[str], cwd: Path, log_path: Path) -> None:
    print(f"[timing] running: {' '.join(command)}", flush=True)
    warning_count = 0
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
        assert process.stdout is not None
        for line in process.stdout:
            log.write(line)
            if "Warning" in line:
                warning_count += 1
                if warning_count <= 5:
                    print(line.rstrip(), flush=True)
            elif any(token in line for token in ("Error", "successful", "collector wrote", "collector exported")):
                print(line.rstrip(), flush=True)
        result = process.wait()
    if warning_count > 5:
        print(f"[timing] {warning_count} warning lines; first 5 shown, see {log_path}", flush=True)
    if result != 0:
        raise RuntimeError(f"Command failed with exit code {result}; see {log_path}")


def extract_marked_section(log_path: Path, output_path: Path, begin: str, end: str) -> bool:
    captured: list[str] = []
    active = False
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip() == begin:
            active = True
            continue
        if line.strip() == end:
            if active:
                output_path.write_text("\n".join(captured) + "\n", encoding="utf-8")
                return True
            continue
        if active:
            captured.append(line)
    return False


def source_files(project_root: Path, project: str) -> list[Path]:
    roots = [
        project_root / "src/main/scala",
        project_root / "partition-chisel/src/main/scala",
        project_root / "build/rtl",
        project_root / "constraints",
    ]
    files: list[Path] = [project_root / "Makefile", project_root / f"{project}.qsf"]
    for root in roots:
        if root.exists():
            files.extend(
                path
                for path in root.rglob("*")
                if path.is_file() and path.suffix.lower() in SOURCE_SUFFIXES
            )
    return sorted({path.resolve() for path in files if path.exists()})


def source_fingerprint(project_root: Path, project: str) -> dict[str, Any]:
    digest = hashlib.sha256()
    files = source_files(project_root, project)
    newest_path: Path | None = None
    newest_mtime = 0.0
    for path in files:
        relative = path.relative_to(project_root.resolve())
        stat = path.stat()
        digest.update(str(relative).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
        if stat.st_mtime > newest_mtime:
            newest_mtime = stat.st_mtime
            newest_path = relative
    return {
        "sha256": digest.hexdigest(),
        "file_count": len(files),
        "newest_mtime": newest_mtime or None,
        "newest_file": str(newest_path) if newest_path else None,
    }


def compilation_state(project_root: Path, project: str, fingerprint: dict[str, Any]) -> dict[str, Any]:
    reports = {}
    for suffix in ("sta.rpt", "fit.rpt", "fit.fastforward.rpt", "syn.rpt"):
        path = project_root / f"{project}.{suffix}"
        if path.exists():
            reports[suffix] = {"path": str(path.relative_to(project_root)), "mtime": path.stat().st_mtime}
    sta_mtime = reports.get("sta.rpt", {}).get("mtime")
    source_mtime = fingerprint.get("newest_mtime")
    stale = bool(sta_mtime and source_mtime and source_mtime > sta_mtime)
    return {
        "reports": reports,
        "sta_older_than_sources": stale,
        "sta_age_vs_sources_seconds": (source_mtime - sta_mtime) if stale else 0,
    }


def normalize_node_name(name: str) -> str:
    name = re.sub(r"\[\d+\]", "[*]", name)
    name = re.sub(r"(?<![A-Za-z])i\d+", "i*", name)
    name = re.sub(r"~\d+", "~*", name)
    return name


def hierarchy_group(name: str, depth: int = 3) -> str:
    parts = name.split("|")
    if len(parts) <= 1:
        return normalize_node_name(name)
    implementation_markers = ("Memory_rtl", "auto_generated", "altera_syncram", "ram_block")
    marker_index = next(
        (index for index, part in enumerate(parts) if any(marker in part for marker in implementation_markers)),
        None,
    )
    parent = parts[:marker_index] if marker_index is not None else parts[:-1]
    return "|".join(parent[-depth:])


def point_metrics(path: dict[str, Any]) -> dict[str, Any]:
    route_delay = 0.0
    cell_delay = 0.0
    max_delay = 0.0
    max_delay_node = ""
    max_fanout = 0
    max_fanout_node = ""
    type_counts: Counter[str] = Counter()
    points = path.get("points", [])
    startpoint = str(path.get("from", ""))
    start_index = next(
        (index for index, point in enumerate(points) if str(point.get("node", "")) == startpoint),
        None,
    )
    if start_index is None:
        start_index = next(
            (index for index, point in enumerate(points) if str(point.get("type", "")).lower() == "utco"),
            0,
        )
    for point in points[start_index:]:
        point_type = str(point.get("type", ""))
        type_counts[point_type] += 1
        delay = numeric(point.get("incremental_delay_ns")) or 0.0
        lowered = point_type.lower()
        if lowered in {"ic", "re", "interconnect", "routing element"} or "interconnect" in lowered:
            route_delay += delay
        else:
            cell_delay += delay
        if delay > max_delay:
            max_delay = delay
            max_delay_node = str(point.get("node", ""))
        fanout = int(numeric(point.get("fanout")) or 0)
        if fanout > max_fanout:
            max_fanout = fanout
            max_fanout_node = str(point.get("node", ""))
    accounted = route_delay + cell_delay
    route_ratio = route_delay / accounted if accounted > 0 else None
    if route_ratio is None:
        classification = "unknown"
    elif route_ratio >= 0.60:
        classification = "routing-limited"
    elif route_ratio <= 0.35:
        classification = "logic-limited"
    else:
        classification = "mixed"
    return {
        "route_delay_ns": route_delay,
        "cell_delay_ns": cell_delay,
        "route_ratio": route_ratio,
        "classification": classification,
        "max_incremental_delay_ns": max_delay,
        "max_delay_node": max_delay_node,
        "max_fanout": max_fanout,
        "max_fanout_node": max_fanout_node,
        "point_types": dict(type_counts),
    }


def panel_rows(panel: Any) -> tuple[list[str], list[list[Any]]]:
    if not isinstance(panel, dict):
        return [], []
    rows = panel.get("rows", [])
    columns = panel.get("columns", [])
    if not isinstance(rows, list):
        return [], []
    if rows and isinstance(rows[0], list) and not columns:
        columns = rows[0]
        rows = rows[1:]
    if isinstance(columns, list) and columns and isinstance(columns[0], dict):
        columns = [item.get("name", "") for item in columns]
    normalized_rows = [list(row) for row in rows if isinstance(row, list)]
    return [str(item) for item in columns], normalized_rows


def load_selected_panels(run_dir: Path) -> list[dict[str, Any]]:
    result = []
    for entry in read_json(run_dir / "selected_panels.json", []) or []:
        if not isinstance(entry, dict) or "file" not in entry:
            continue
        data = read_json(run_dir / entry["file"], {})
        columns, rows = panel_rows(data)
        result.append({"name": entry.get("name", ""), "columns": columns, "rows": rows})
    return result


def panels_matching(panels: list[dict[str, Any]], pattern: str) -> list[dict[str, Any]]:
    regex = re.compile(pattern, re.IGNORECASE)
    return [panel for panel in panels if regex.search(str(panel.get("name", "")))]


def ascii_table(path: Path, header_pattern: str) -> tuple[list[str], list[list[str]]]:
    if not path.exists():
        return [], []
    candidate_rows: list[list[str]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not (stripped.startswith(";") and stripped.endswith(";")):
            continue
        cells = [cell.strip() for cell in stripped.strip(";").split(";")]
        candidate_rows.append(cells)
    header_index = next(
        (
            index
            for index, row in enumerate(candidate_rows)
            if len(row) > 1 and re.search(header_pattern, " ".join(row), re.I)
        ),
        None,
    )
    if header_index is None:
        return [], []
    header = candidate_rows[header_index]
    rows: list[list[str]] = []
    for row in candidate_rows[header_index + 1 :]:
        if len(row) != len(header):
            if rows:
                break
            continue
        if row == header or not any(row):
            continue
        rows.append(row)
    return header, rows


def bottleneck_table(path: Path, allowed_roots: set[str]) -> tuple[list[str], list[list[str]]]:
    columns = ["Rating", "Slack", "TNS", "Total Slack", "Fanouts", "Fanins", "Paths", "Failing Paths", "Node"]
    rows: list[list[str]] = []
    if not path.exists():
        return [], []
    row_pattern = re.compile(
        r"^Info:\s+([\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+"
        r"(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(.+)$"
    )
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = row_pattern.match(line)
        if not match:
            continue
        row = list(match.groups())
        root = row[-1].split("|", 1)[0]
        if allowed_roots and root not in allowed_roots:
            continue
        rows.append(row)
    return (columns, rows) if rows else ([], [])


def selected_panel_summary(panels: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    categories = {
        "fmax": r"Fmax Summary$",
        "setup": r"Setup Summary$",
        "hold": r"Hold Summary$",
        "recovery": r"Recovery Summary$",
        "removal": r"Removal Summary$",
        "fitter_summary": r"Fitter\|\|Fitter Summary$",
        "resources": r"Fitter Resource Usage Summary$",
        "routing_usage": r"Routing Usage Summary$",
        "peak_wire": r"Peak Wire Demand Summary$",
        "peak_wire_details": r"Peak Wire Demand Details$",
        "high_fanout": r"Non-Global High Fan-Out Signals$",
        "highest_wire_count": r"Nets with Highest Wire Count$",
        "duplication": r"Fitter Duplication Summary$",
        "retiming_limits": r"Retiming Limit Summary$",
        "fast_forward": r"Fast Forward Summary",
        "clock_transfers": r"Clock Transfers.*Transfers$",
        "unconstrained": r"Unconstrained Paths.*Summary$",
        "design_assistant": r"Design Assistant",
    }
    limits = {
        "setup": 100,
        "hold": 100,
        "recovery": 100,
        "removal": 100,
        "high_fanout": 50,
        "peak_wire_details": 100,
        "resources": 100,
        "routing_usage": 100,
        "clock_transfers": 100,
    }
    for category, pattern in categories.items():
        matches = panels_matching(panels, pattern)
        result[category] = [
            {
                "name": panel["name"],
                "columns": panel["columns"],
                "rows": panel["rows"][: limits.get(category, 500)],
            }
            for panel in matches
        ]
    return result


def summarize(run_dir: Path) -> dict[str, Any]:
    collection_metadata = read_json(run_dir / "collection_metadata.json", {}) or {}
    timing_metadata = read_json(run_dir / "timing_metadata.json", {}) or {}
    report_metadata = read_json(run_dir / "report_metadata.json", {}) or {}
    clocks = read_json(run_dir / "clocks.json", []) or []
    paths = read_jsonl(run_dir / "paths.jsonl")
    detailed_paths = read_jsonl(run_dir / "detailed_paths.jsonl")
    panels = load_selected_panels(run_dir)

    paths.sort(key=lambda item: numeric(item.get("slack_ns")) if numeric(item.get("slack_ns")) is not None else float("inf"))
    detailed_paths.sort(
        key=lambda item: numeric(item.get("slack_ns")) if numeric(item.get("slack_ns")) is not None else float("inf")
    )
    clock_summary = sorted(
        clocks,
        key=lambda item: numeric(item.get("collected_wns_ns"))
        if numeric(item.get("collected_wns_ns")) is not None
        else float("inf"),
    )

    endpoint_groups: dict[str, list[float]] = defaultdict(list)
    hierarchy_groups: dict[str, list[float]] = defaultdict(list)
    for path in paths:
        slack = numeric(path.get("slack_ns"))
        if slack is None:
            continue
        endpoint_groups[normalize_node_name(str(path.get("to", "")))].append(slack)
        hierarchy_groups[hierarchy_group(str(path.get("to", "")))].append(slack)

    def aggregate(groups: dict[str, list[float]], limit: int = 30) -> list[dict[str, Any]]:
        records = [
            {"name": name, "paths": len(values), "wns_ns": min(values), "sampled_tns_ns": sum(v for v in values if v < 0)}
            for name, values in groups.items()
        ]
        records.sort(key=lambda item: (item["wns_ns"], item["sampled_tns_ns"]))
        return records[:limit]

    detailed_metrics = []
    for path in detailed_paths:
        metrics = point_metrics(path)
        detailed_metrics.append(
            {
                "slack_ns": path.get("slack_ns"),
                "from": path.get("from"),
                "to": path.get("to"),
                "logic_levels": path.get("logic_levels"),
                **metrics,
            }
        )

    source_columns, source_rows = ascii_table(run_dir / "timing_by_source_files.rpt", r"source.*file")
    failing_roots = {
        str(path.get("from", "")).split("|", 1)[0]
        for path in paths
        if (numeric(path.get("slack_ns")) or 0) < 0
    } | {
        str(path.get("to", "")).split("|", 1)[0]
        for path in paths
        if (numeric(path.get("slack_ns")) or 0) < 0
    }
    failing_roots.discard("")
    bottleneck_columns, bottleneck_rows = bottleneck_table(run_dir / "bottlenecks.rpt", failing_roots)
    check_columns, check_rows = ascii_table(run_dir / "check_timing.rpt", r"number of issues found")
    async_cdc_columns, async_cdc_rows = ascii_table(run_dir / "asynch_cdc_summary.rpt", r"cdc count")
    project_root = str(collection_metadata.get("project_root", ""))
    if project_root:
        source_rows = [
            [cell.replace(project_root + os.sep, "") for cell in row]
            for row in source_rows
        ]

    warnings = []
    warning_path = run_dir / "collector_warnings.txt"
    if warning_path.exists():
        warnings = [line for line in warning_path.read_text(encoding="utf-8", errors="replace").splitlines() if line]

    diagnostic_files = {
        "cdc_viewer": "cdc_summary.rpt",
        "check_timing": "check_timing.rpt",
        "asynchronous_cdc": "asynch_cdc_summary.rpt",
        "logic_depth": "logic_depth.rpt",
        "neighbor_paths": "neighbor_paths.rpt",
        "register_spread": "register_spread.rpt",
        "net_delay": "net_delay.rpt",
        "route_nets_of_interest": "route_nets_of_interest.rpt",
        "pipelining_info": "pipelining_info.rpt",
        "retiming_restrictions": "retiming_restrictions.rpt",
    }
    diagnostic_files = {
        name: {"path": path, "bytes": (run_dir / path).stat().st_size}
        for name, path in diagnostic_files.items()
        if (run_dir / path).exists()
    }

    summary = {
        "schema_version": 1,
        "collection": collection_metadata,
        "timing_metadata": timing_metadata,
        "report_metadata": report_metadata,
        "clocks": clock_summary,
        "timing": {
            "collected_path_count": len(paths),
            "detailed_path_count": len(detailed_paths),
            "worst_paths": paths[:50],
            "endpoint_groups": aggregate(endpoint_groups),
            "hierarchy_groups": aggregate(hierarchy_groups),
            "detailed_path_metrics": detailed_metrics,
            "source_summary": {"columns": source_columns, "rows": source_rows[:100]},
            "bottlenecks": {"columns": bottleneck_columns, "rows": bottleneck_rows[:100]},
        },
        "reports": selected_panel_summary(panels),
        "diagnostics": {
            "check_timing": {"columns": check_columns, "rows": check_rows},
            "asynchronous_cdc": {"columns": async_cdc_columns, "rows": async_cdc_rows},
            "raw_files": diagnostic_files,
        },
        "selected_panel_count": len(panels),
        "collector_warnings": warnings,
    }
    write_json(run_dir / "summary.json", summary)
    (run_dir / "summary.md").write_text(render_markdown(summary), encoding="utf-8")
    return summary


def render_markdown(summary: dict[str, Any]) -> str:
    collection = summary.get("collection", {})
    state = collection.get("compilation_state", {})
    lines = ["# Quartus timing analysis", ""]
    lines.append(f"- Project/revision: `{timing_project(summary)}`")
    lines.append(f"- Collected: `{collection.get('collected_at', 'unknown')}`")
    lines.append(f"- Quartus: `{summary.get('timing_metadata', {}).get('quartus_version', 'unknown')}`")
    lines.append(f"- Source fingerprint: `{collection.get('source_fingerprint', {}).get('sha256', 'unknown')[:16]}`")
    if state.get("sta_older_than_sources"):
        lines.extend(
            [
                "",
                "> WARNING: final STA database is older than at least one RTL/configuration source. "
                "The report may not describe the current source tree.",
            ]
        )

    lines.extend(["", "## Clocks", ""])
    clock_rows = [
        [
            clock.get("name"),
            format_number(clock.get("period_ns")),
            format_number(clock.get("collected_wns_ns")),
            clock.get("collected_setup_paths", 0),
        ]
        for clock in summary.get("clocks", [])
    ]
    lines.append(markdown_table(["Clock", "Period ns", "Collected WNS ns", "Paths"], clock_rows))

    timing = summary.get("timing", {})
    lines.extend(["", "## Worst setup paths", ""])
    path_rows = [
        [
            format_number(path.get("slack_ns")),
            format_number(path.get("data_delay_ns")),
            path.get("logic_levels", ""),
            path.get("from", ""),
            path.get("to", ""),
        ]
        for path in timing.get("worst_paths", [])[:20]
    ]
    lines.append(markdown_table(["Slack ns", "Data ns", "Levels", "From", "To"], path_rows))

    lines.extend(["", "## Critical hierarchy groups", ""])
    hierarchy_rows = [
        [item.get("name"), item.get("paths"), format_number(item.get("wns_ns")), format_number(item.get("sampled_tns_ns"))]
        for item in timing.get("hierarchy_groups", [])[:20]
    ]
    lines.append(markdown_table(["Hierarchy", "Paths", "WNS ns", "Sampled TNS ns"], hierarchy_rows))

    lines.extend(["", "## Detailed path character", ""])
    metric_rows = [
        [
            format_number(item.get("slack_ns")),
            item.get("classification"),
            format_number((item.get("route_ratio") or 0) * 100, 1, "%"),
            format_number(item.get("route_delay_ns")),
            format_number(item.get("cell_delay_ns")),
            item.get("max_fanout"),
            item.get("to"),
        ]
        for item in timing.get("detailed_path_metrics", [])[:20]
    ]
    lines.append(
        markdown_table(
            ["Slack ns", "Class", "Route %", "Route ns", "Cell ns", "Max fanout", "Endpoint"], metric_rows
        )
    )

    source = timing.get("source_summary", {})
    if source.get("columns"):
        lines.extend(["", "## Timing by source file", ""])
        lines.append(markdown_table(source["columns"], source.get("rows", []), 30))

    bottlenecks = timing.get("bottlenecks", {})
    if bottlenecks.get("columns"):
        lines.extend(["", "## Bottlenecks", ""])
        lines.append(markdown_table(bottlenecks["columns"], bottlenecks.get("rows", []), 30))

    diagnostics = summary.get("diagnostics", {})
    check_timing = diagnostics.get("check_timing", {})
    if check_timing.get("columns"):
        lines.extend(["", "## Check timing", ""])
        lines.append(markdown_table(check_timing["columns"], check_timing.get("rows", []), 50))
    async_cdc = diagnostics.get("asynchronous_cdc", {})
    if async_cdc.get("columns"):
        lines.extend(["", "## Asynchronous CDC", ""])
        lines.append(markdown_table(async_cdc["columns"], async_cdc.get("rows", []), 50))

    report_sections = [
        ("Fmax panels", "fmax", 20),
        ("Fitter resources", "resources", 40),
        ("Routing usage", "routing_usage", 40),
        ("Peak wire demand", "peak_wire", 20),
        ("High fanout", "high_fanout", 20),
        ("Fast Forward", "fast_forward", 30),
        ("Retiming limits", "retiming_limits", 30),
        ("Unconstrained paths", "unconstrained", 30),
        ("Design Assistant", "design_assistant", 30),
    ]
    reports = summary.get("reports", {})
    for title, category, limit in report_sections:
        matches = reports.get(category, [])
        if not matches:
            continue
        lines.extend(["", f"## {title}", ""])
        for panel in matches:
            lines.extend([f"### {panel.get('name', category)}", ""])
            lines.append(markdown_table(panel.get("columns", []), panel.get("rows", []), limit))
            lines.append("")

    warnings = summary.get("collector_warnings", [])
    if warnings:
        lines.extend(["", "## Collector warnings", ""])
        lines.extend(f"- {warning}" for warning in warnings)
    lines.append("")
    return "\n".join(lines)


def timing_project(summary: dict[str, Any]) -> str:
    metadata = summary.get("timing_metadata", {})
    return f"{metadata.get('project', 'unknown')}/{metadata.get('revision', 'unknown')}"


def primary_clock(summary: dict[str, Any]) -> dict[str, Any] | None:
    clocks = [clock for clock in summary.get("clocks", []) if numeric(clock.get("collected_wns_ns")) is not None]
    return min(clocks, key=lambda item: numeric(item.get("collected_wns_ns")) or 0) if clocks else None


def resolve_summary(path: Path) -> tuple[Path, dict[str, Any]]:
    summary_path = path / "summary.json" if path.is_dir() else path
    data = read_json(summary_path)
    if not isinstance(data, dict):
        raise RuntimeError(f"Cannot read summary: {summary_path}")
    return summary_path, data


def compare_summaries(old_path: Path, new_path: Path) -> str:
    old_file, old = resolve_summary(old_path)
    new_file, new = resolve_summary(new_path)
    old_clock = primary_clock(old)
    new_clock = primary_clock(new)
    lines = ["# Quartus timing comparison", "", f"- Old: `{old_file}`", f"- New: `{new_file}`", ""]
    old_config = old.get("timing_metadata", {})
    new_config = new.get("timing_metadata", {})
    sampling_keys = ("paths_per_clock", "detailed_paths")
    if any(old_config.get(key) != new_config.get(key) for key in sampling_keys):
        lines.extend(
            [
                "> WARNING: collection sampling differs between the two runs. WNS is comparable, "
                "but path counts and sampled TNS/group totals are not directly comparable.",
                "",
            ]
        )
    if old_clock and new_clock:
        old_wns = numeric(old_clock.get("collected_wns_ns"))
        new_wns = numeric(new_clock.get("collected_wns_ns"))
        delta = new_wns - old_wns if old_wns is not None and new_wns is not None else None
        lines.extend(
            [
                "## Primary/worst clock",
                "",
                markdown_table(
                    ["Metric", "Old", "New", "Delta"],
                    [
                        ["Clock", old_clock.get("name"), new_clock.get("name"), "—"],
                        ["WNS ns", format_number(old_wns), format_number(new_wns), format_number(delta)],
                        [
                            "Collected paths",
                            old_clock.get("collected_setup_paths"),
                            new_clock.get("collected_setup_paths"),
                            int(new_clock.get("collected_setup_paths", 0)) - int(old_clock.get("collected_setup_paths", 0)),
                        ],
                    ],
                ),
                "",
            ]
        )
    old_groups = {item["name"]: item for item in old.get("timing", {}).get("hierarchy_groups", [])}
    new_groups = {item["name"]: item for item in new.get("timing", {}).get("hierarchy_groups", [])}
    changed = []
    for name in sorted(set(old_groups) | set(new_groups)):
        old_item = old_groups.get(name, {})
        new_item = new_groups.get(name, {})
        old_wns = numeric(old_item.get("wns_ns"))
        new_wns = numeric(new_item.get("wns_ns"))
        delta = new_wns - old_wns if old_wns is not None and new_wns is not None else None
        changed.append(
            [name, format_number(old_wns), format_number(new_wns), format_number(delta), old_item.get("paths", 0), new_item.get("paths", 0)]
        )
    lines.extend(["## Critical hierarchy changes", "", markdown_table(["Hierarchy", "Old WNS", "New WNS", "Delta", "Old paths", "New paths"], changed, 40), ""])
    return "\n".join(lines)


def run_directories(output_root: Path) -> list[Path]:
    if not output_root.exists():
        return []
    return sorted(
        path
        for path in output_root.iterdir()
        if path.is_dir() and not path.is_symlink() and (path / "summary.json").exists()
    )


def collect(args: argparse.Namespace) -> Path:
    project_root = Path(args.project_root).resolve()
    output_root = (project_root / args.output_root).resolve() if not Path(args.output_root).is_absolute() else Path(args.output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    run_name = args.run_name or dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = output_root / run_name
    if run_dir.exists():
        raise RuntimeError(f"Output directory already exists: {run_dir}")
    run_dir.mkdir()

    quartus_bin = Path(args.quartus_bin).resolve()
    quartus_sta = quartus_bin / "quartus_sta"
    quartus_sh = quartus_bin / "quartus_sh"
    for executable in (quartus_sta, quartus_sh):
        if not executable.is_file():
            raise RuntimeError(f"Missing Quartus executable: {executable}")

    fingerprint = source_fingerprint(project_root, args.project)
    state = compilation_state(project_root, args.project, fingerprint)
    metadata = {
        "schema_version": 1,
        "collected_at": dt.datetime.now(dt.timezone.utc).astimezone().isoformat(),
        "project_root": str(project_root),
        "source_fingerprint": fingerprint,
        "compilation_state": state,
        "command": " ".join(sys.argv),
    }
    write_json(run_dir / "collection_metadata.json", metadata)
    if state["sta_older_than_sources"]:
        print(
            f"[timing] WARNING: {args.project}.sta.rpt is older than {fingerprint['newest_file']}; "
            "results may describe stale RTL",
            flush=True,
        )

    timing_tcl = SCRIPT_DIR / "quartus_timing_collect.tcl"
    report_tcl = SCRIPT_DIR / "quartus_report_collect.tcl"
    run_logged(
        [
            str(quartus_sta),
            "-t",
            str(timing_tcl),
            args.project,
            args.revision,
            str(run_dir),
            str(args.paths_per_clock),
            str(args.detailed_paths),
        ],
        project_root,
        run_dir / "quartus_sta_collect.log",
    )
    extract_marked_section(
        run_dir / "quartus_sta_collect.log",
        run_dir / "bottlenecks.rpt",
        "QTA_BOTTLENECK_BEGIN",
        "QTA_BOTTLENECK_END",
    )
    run_logged(
        [str(quartus_sh), "-t", str(report_tcl), args.project, args.revision, str(run_dir)],
        project_root,
        run_dir / "quartus_report_collect.log",
    )

    summarize(run_dir)
    previous = run_directories(output_root)
    previous = [path for path in previous if path != run_dir]
    if previous:
        comparison = compare_summaries(previous[-1], run_dir)
        (run_dir / "comparison.md").write_text(comparison, encoding="utf-8")

    latest = output_root / "latest"
    temporary_link = output_root / f".latest.{os.getpid()}"
    if temporary_link.exists() or temporary_link.is_symlink():
        temporary_link.unlink()
    temporary_link.symlink_to(run_dir.name, target_is_directory=True)
    os.replace(temporary_link, latest)
    print(f"[timing] summary: {run_dir / 'summary.md'}", flush=True)
    if (run_dir / "comparison.md").exists():
        print(f"[timing] comparison: {run_dir / 'comparison.md'}", flush=True)
    return run_dir


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect_parser = subparsers.add_parser("collect", help="collect final STA/report data and generate a summary")
    collect_parser.add_argument("--project", default="vpart_pcie")
    collect_parser.add_argument("--revision", default=None)
    collect_parser.add_argument("--project-root", default=".")
    collect_parser.add_argument("--quartus-bin", required=True)
    collect_parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    collect_parser.add_argument("--run-name")
    collect_parser.add_argument("--paths-per-clock", type=int, default=50)
    collect_parser.add_argument("--detailed-paths", type=int, default=20)

    summarize_parser = subparsers.add_parser("summarize", help="regenerate summary files for one collection")
    summarize_parser.add_argument("run_dir", type=Path)

    compare_parser = subparsers.add_parser("compare", help="compare two timing collections")
    compare_parser.add_argument("old", type=Path)
    compare_parser.add_argument("new", type=Path)
    compare_parser.add_argument("--output", type=Path)

    latest_parser = subparsers.add_parser("compare-latest", help="compare the newest two collections")
    latest_parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    latest_parser.add_argument("--output", type=Path)

    args = parser.parse_args()
    if getattr(args, "revision", None) is None:
        args.revision = getattr(args, "project", "vpart_pcie")
    return args


def main() -> int:
    args = parse_args()
    try:
        if args.command == "collect":
            collect(args)
        elif args.command == "summarize":
            summarize(args.run_dir.resolve())
            print(args.run_dir.resolve() / "summary.md")
        elif args.command == "compare":
            output = compare_summaries(args.old.resolve(), args.new.resolve())
            if args.output:
                args.output.write_text(output, encoding="utf-8")
            else:
                print(output)
        elif args.command == "compare-latest":
            runs = run_directories(args.output_root.resolve())
            if len(runs) < 2:
                raise RuntimeError(f"Need at least two collections under {args.output_root}")
            output = compare_summaries(runs[-2], runs[-1])
            if args.output:
                args.output.write_text(output, encoding="utf-8")
            else:
                print(output)
        return 0
    except (OSError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
