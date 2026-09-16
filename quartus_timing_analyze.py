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
SCHEMA_VERSION = 4
DELAY_TOLERANCE_NS = 0.002
ISSUE_WNS_CHANGE_NS = 0.005
QUARTUS_SNAPSHOTS = ("planned", "placed", "routed", "retimed", "final")


def snapshot_capabilities(snapshot: str) -> dict[str, Any]:
    """Conservative stage matrix; availability is confirmed by each collector run."""
    rank = {name: index for index, name in enumerate(QUARTUS_SNAPSHOTS)}.get(snapshot, 4)
    return {
        "snapshot": snapshot,
        "timing_paths": {"status": "supported", "minimum_stage": "planned"},
        "routing_detail": {
            "status": "supported" if rank >= 2 else "unavailable-for-stage",
            "minimum_stage": "routed",
        },
        "register_spread": {
            "status": "supported" if rank >= 1 else "unavailable-for-stage",
            "minimum_stage": "placed",
        },
        "retiming": {
            "status": "supported" if rank >= 3 else "unavailable-for-stage",
            "minimum_stage": "retimed",
        },
        "compilation_report_db": {
            "status": "supported" if snapshot == "final" else "not-collected-for-intermediate-stage",
            "minimum_stage": "final",
        },
        "basis": "Quartus Prime Pro 25.1 --help=snapshot plus conservative report-stage requirements",
    }


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
    """Normalize bus lanes only; fitter-generated numeric names stay distinct.

    Replacing anonymous ``i123`` or ``~42`` fragments caused unrelated fitted
    cells in the same hierarchy to compare equal.  Bus-lane normalization is
    retained as medium-strength evidence, never as an exact identity.
    """
    name = re.sub(r"\[\d+\]", "[*]", name)
    return name


def base_node_name(name: str) -> str:
    """Remove common post-fit replica/location suffixes without losing hierarchy."""
    value = name.strip()
    pin_suffix = r"(?=(?:\|(?:q|d|clk))?$)"
    value = re.sub(rf"(?:~_|_)Duplicate(?:_\d+)*{pin_suffix}", "", value, flags=re.I)
    value = re.sub(rf"~(?:RTM|ERTM|DUPLICATE)[A-Za-z0-9_]*{pin_suffix}", "", value, flags=re.I)
    value = re.sub(r"_(?:R\d+|C\d+)_X\d+_Y\d+_N\d+_I\d+_(?:dff|lut)$", "", value, flags=re.I)
    return value


def node_identity(name: str) -> dict[str, str]:
    exact = name.strip()
    base = base_node_name(exact)
    return {
        "exact": exact,
        "base": base,
        "normalized": normalize_node_name(base),
        "hierarchy": hierarchy_group(base),
    }


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


def data_path_points(path: dict[str, Any]) -> list[dict[str, Any]]:
    points = [point for point in path.get("points", []) if isinstance(point, dict)]
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
    return points[start_index:]


def logical_path_node_records(path: dict[str, Any]) -> list[dict[str, Any]]:
    """Return logical/cell path points, excluding anonymous routing resources."""
    records: dict[str, dict[str, Any]] = {}
    route_types = {"ic", "interconnect", "re", "routing element"}
    for position, point in enumerate(data_path_points(path)):
        point_type = str(point.get("type", "")).lower()
        if point_type in route_types or "routing element" in point_type or "local interconnect" in point_type:
            continue
        node = str(point.get("node", "")).strip()
        if not node:
            continue
        fanout = int(numeric(point.get("fanout")) or 0)
        delay = numeric(point.get("incremental_delay_ns")) or 0.0
        current = records.get(node)
        if current is None:
            records[node] = {
                "node": node,
                "identity": node_identity(node),
                "position": position,
                "max_fanout": fanout,
                "max_incremental_delay_ns": delay,
            }
        else:
            current["max_fanout"] = max(current["max_fanout"], fanout)
            current["max_incremental_delay_ns"] = max(current["max_incremental_delay_ns"], delay)
    return list(records.values())


def point_metrics(path: dict[str, Any]) -> dict[str, Any]:
    route_delay = 0.0
    cell_delay = 0.0
    logic_cell_delay = 0.0
    launch_delay = 0.0
    max_delay = 0.0
    max_delay_node = ""
    max_fanout = 0
    max_fanout_node = ""
    type_counts: Counter[str] = Counter()
    path_points = data_path_points(path)
    previous_total: float | None = None
    total_delay_decreases = 0
    for point in path_points:
        point_type = str(point.get("type", ""))
        type_counts[point_type] += 1
        delay = numeric(point.get("incremental_delay_ns")) or 0.0
        lowered = point_type.lower()
        if lowered in {"ic", "interconnect"} or "local interconnect" in lowered:
            route_delay += delay
        elif lowered in {"re", "routing element"} or "routing element" in lowered:
            route_delay += delay
        elif lowered == "utco":
            launch_delay += delay
            cell_delay += delay
        else:
            logic_cell_delay += delay
            cell_delay += delay
        if delay > max_delay:
            max_delay = delay
            max_delay_node = str(point.get("node", ""))
        fanout = int(numeric(point.get("fanout")) or 0)
        if fanout > max_fanout:
            max_fanout = fanout
            max_fanout_node = str(point.get("node", ""))
        total = numeric(point.get("total_delay_ns"))
        if total is not None and previous_total is not None and total + DELAY_TOLERANCE_NS < previous_total:
            total_delay_decreases += 1
        if total is not None:
            previous_total = total
    accounted = route_delay + cell_delay
    data_delay = numeric(path.get("data_delay_ns"))
    delay_error = abs(accounted - data_delay) if data_delay is not None else None
    arrival = numeric(path.get("arrival_time_ns"))
    required = numeric(path.get("required_time_ns"))
    slack = numeric(path.get("slack_ns"))
    slack_error = abs((required - arrival) - slack) if None not in (arrival, required, slack) else None
    delay_ok = delay_error is not None and delay_error <= DELAY_TOLERANCE_NS
    slack_ok = slack_error is not None and slack_error <= DELAY_TOLERANCE_NS
    if delay_ok and slack_ok and path_points and total_delay_decreases == 0:
        consistency_status = "pass"
        classification_confidence = "medium"
    elif delay_ok and path_points:
        consistency_status = "warning"
        classification_confidence = "medium"
    else:
        consistency_status = "fail"
        classification_confidence = "low"
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
        "local_ic_delay_ns": None,
        "fabric_ic_delay_ns": None,
        "cell_delay_ns": cell_delay,
        "logic_cell_delay_ns": logic_cell_delay,
        "launch_delay_ns": launch_delay,
        "route_ratio": route_ratio,
        "classification": classification,
        "max_incremental_delay_ns": max_delay,
        "max_delay_node": max_delay_node,
        "max_fanout": max_fanout,
        "max_fanout_node": max_fanout_node,
        "point_types": dict(type_counts),
        "consistency": {
            "status": consistency_status,
            "delay_sum_error_ns": delay_error,
            "slack_equation_error_ns": slack_error,
            "total_delay_decreases": total_delay_decreases,
            "tolerance_ns": DELAY_TOLERANCE_NS,
        },
        "classification_confidence": classification_confidence,
    }


def neighbor_path_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    starts = [index for index, line in enumerate(lines) if re.fullmatch(r"Path #\d+", line.strip())]
    records = []
    wanted = {
        "From Node",
        "To Node",
        "Launch Clock",
        "Latch Clock",
        "Setup Operating Conditions",
        "Setup Slack",
        "[c] uTco",
        "[a] Cell Delay",
        "[b] Local IC Delay",
        "[D] Fabric IC Delay",
        "Logic Levels",
        "Max Fanout",
        "Number of Wires",
        "Route Stage Congestion Impact",
        "Source/Destination Bounding Box",
        "Cell Bounding Box",
        "Interconnect Bounding Box",
    }
    for position, start in enumerate(starts):
        stop = starts[position + 1] if position + 1 < len(starts) else len(lines)
        path_column: int | None = None
        fields: dict[str, str] = {}
        for line in lines[start:stop]:
            stripped = line.strip()
            if not (stripped.startswith(";") and stripped.endswith(";")):
                continue
            cells = [cell.strip() for cell in stripped.strip(";").split(";")]
            if "Path" in cells and cells[0] in {"", "Property"}:
                path_column = cells.index("Path")
                continue
            if path_column is None or not cells:
                continue
            label = re.sub(r"^\s+", "", cells[0])
            if label in wanted and path_column < len(cells):
                fields[label] = cells[path_column]
        from_node = fields.get("From Node", "")
        to_node = fields.get("To Node", "")
        if not from_node or not to_node:
            continue
        records.append(
            {
                "source": f"rpt:{path.name}",
                "path_number": position + 1,
                "from": from_node,
                "to": to_node,
                "from_clock": fields.get("Launch Clock", ""),
                "to_clock": fields.get("Latch Clock", ""),
                "corner": fields.get("Setup Operating Conditions", ""),
                "from_identity": node_identity(from_node),
                "to_identity": node_identity(to_node),
                "slack_ns": numeric(fields.get("Setup Slack")),
                "utco_ns": numeric(fields.get("[c] uTco")),
                "cell_delay_ns": numeric(fields.get("[a] Cell Delay")),
                "local_ic_delay_ns": numeric(fields.get("[b] Local IC Delay")),
                "fabric_ic_delay_ns": numeric(fields.get("[D] Fabric IC Delay")),
                "logic_levels": numeric(fields.get("Logic Levels")),
                "max_fanout": numeric(fields.get("Max Fanout")),
                "number_of_wires": numeric(fields.get("Number of Wires")),
                "congestion_impact": fields.get("Route Stage Congestion Impact", ""),
                "bounding_boxes": {
                    "source_destination": fields.get("Source/Destination Bounding Box", ""),
                    "cell": fields.get("Cell Bounding Box", ""),
                    "interconnect": fields.get("Interconnect Bounding Box", ""),
                },
            }
        )
    return records


def match_neighbor_path(path: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any] | None:
    candidates = [
        record
        for record in records
        if record.get("from") == path.get("from") and record.get("to") == path.get("to")
    ]
    if not candidates:
        return None
    corner = str(path.get("corner", ""))
    if corner:
        same_corner = [record for record in candidates if record.get("corner") == corner]
        if not same_corner:
            return None
        candidates = same_corner
    slack = numeric(path.get("slack_ns"))
    match = min(
        candidates,
        key=lambda record: abs((numeric(record.get("slack_ns")) or 0.0) - (slack or 0.0)),
    )
    # report_neighbor_paths and get_timing_paths can use different -nworst
    # samples.  From/to equality is not sufficient to identify a physical
    # path when several alternatives share the same endpoints.  Refuse a
    # cross-check unless the rounded slacks identify the same path.
    matched_slack = numeric(match.get("slack_ns"))
    if slack is not None and matched_slack is not None and abs(matched_slack - slack) > DELAY_TOLERANCE_NS:
        return None
    return match


def match_neighbor_paths(
    paths: list[dict[str, Any]], records: list[dict[str, Any]]
) -> list[dict[str, Any] | None]:
    """Match report rows one-to-one in timing order.

    Quartus can return distinct physical paths with identical endpoints, corner,
    and rounded slack.  Consuming each report row once prevents one
    report_neighbor_paths sample from validating several get_timing_paths
    samples.
    """
    remaining = list(records)
    matches: list[dict[str, Any] | None] = []
    for path in paths:
        match = match_neighbor_path(path, remaining)
        matches.append(match)
        if match is not None:
            remaining.remove(match)
    return matches


def apply_neighbor_validation(
    metrics: dict[str, Any],
    neighbor: dict[str, Any] | None,
    path: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if neighbor is None:
        metrics["quartus_breakdown_validation"] = {"status": "unavailable", "source": None}
        return metrics
    local_ic = numeric(neighbor.get("local_ic_delay_ns"))
    fabric_ic = numeric(neighbor.get("fabric_ic_delay_ns"))
    quartus_cell = numeric(neighbor.get("cell_delay_ns"))
    quartus_utco = numeric(neighbor.get("utco_ns"))
    quartus_route = local_ic + fabric_ic if local_ic is not None and fabric_ic is not None else None
    route_error = abs((numeric(metrics.get("route_delay_ns")) or 0) - quartus_route) if quartus_route is not None else None
    logic_cell_error = (
        abs((numeric(metrics.get("logic_cell_delay_ns")) or 0) - quartus_cell)
        if quartus_cell is not None
        else None
    )
    launch_error = (
        abs((numeric(metrics.get("launch_delay_ns")) or 0) - quartus_utco)
        if quartus_utco is not None
        else None
    )
    path = path or {}
    quartus_data = (
        quartus_utco + quartus_cell + local_ic + fabric_ic
        if None not in (quartus_utco, quartus_cell, local_ic, fabric_ic)
        else None
    )
    path_data = numeric(path.get("data_delay_ns"))
    path_slack = numeric(path.get("slack_ns"))
    neighbor_slack = numeric(neighbor.get("slack_ns"))
    data_error = abs(path_data - quartus_data) if None not in (path_data, quartus_data) else None
    slack_error = abs(path_slack - neighbor_slack) if None not in (path_slack, neighbor_slack) else None
    level_error = (
        abs((numeric(path.get("logic_levels")) or 0) - (numeric(neighbor.get("logic_levels")) or 0))
        if numeric(path.get("logic_levels")) is not None and numeric(neighbor.get("logic_levels")) is not None
        else None
    )
    identity_checks = {
        "from": not path or path.get("from") == neighbor.get("from"),
        "to": not path or path.get("to") == neighbor.get("to"),
        "from_clock": not path or path.get("from_clock") == neighbor.get("from_clock"),
        "to_clock": not path or path.get("to_clock") == neighbor.get("to_clock"),
        "corner": not path or path.get("corner") == neighbor.get("corner"),
    }
    delay_errors = [error for error in (route_error, logic_cell_error, launch_error, data_error, slack_error) if error is not None]
    required_error_count = 3 if not path else 5
    if len(delay_errors) != required_error_count or (path and level_error is None):
        status = "unavailable"
    else:
        status = (
            "pass"
            if max(delay_errors) <= DELAY_TOLERANCE_NS
            and (level_error or 0) == 0
            and all(identity_checks.values())
            else "fail"
        )
    metrics["local_ic_delay_ns"] = local_ic
    metrics["fabric_ic_delay_ns"] = fabric_ic
    metrics["quartus_breakdown_validation"] = {
        "status": status,
        "source": neighbor.get("source"),
        "route_sum_error_ns": route_error,
        "logic_cell_error_ns": logic_cell_error,
        "launch_utco_error_ns": launch_error,
        "data_delay_error_ns": data_error,
        "slack_error_ns": slack_error,
        "logic_levels_error": level_error,
        "identity_checks": identity_checks,
        "tolerance_ns": DELAY_TOLERANCE_NS,
    }
    if status == "pass" and metrics.get("consistency", {}).get("status") == "pass":
        metrics["classification_confidence"] = "high"
    elif status == "fail":
        metrics["classification_confidence"] = "low"
    return metrics


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


def normalized_quartus_guidance(reports: dict[str, Any]) -> dict[str, Any]:
    """Normalize only Quartus guidance already present in Report DB panels."""
    design_panels = reports.get("design_assistant", [])
    design_rules = []
    for panel in design_panels:
        columns = panel.get("columns", [])
        for row in panel.get("rows", []):
            fields = row_as_fields(columns, row)
            violation_count = int(numeric(first_field(fields, ("Violations", "Violation Count"))) or 0)
            rule_text = first_field(fields, ("Rule", "Rule Name"))
            rule_match = re.match(r"([^ ]+)\s+-\s+(.+)", rule_text)
            design_rules.append(
                {
                    "rule": rule_match.group(1) if rule_match else rule_text,
                    "severity": first_field(fields, ("Severity",)),
                    "stage": "elaborated" if "Elaborated" in panel.get("name", "") else "unknown",
                    "category": first_field(fields, ("Tags", "Category")),
                    "description": rule_match.group(2) if rule_match else rule_text,
                    "recommendation": first_field(fields, ("Recommendation",)),
                    "node": first_field(fields, ("Node", "Node Name", "Entity")),
                    "path": first_field(fields, ("Path", "Location")),
                    "source": f"report_db:{panel.get('name', 'Design Assistant')}",
                    "violations": violation_count,
                    "waived": int(numeric(first_field(fields, ("Waived",))) or 0),
                }
            )
    if design_panels:
        design_status = "violations" if any(item["violations"] > 0 for item in design_rules) else "pass"
    else:
        design_status = "unavailable-or-not-run"

    fast_forward = []
    for panel in reports.get("fast_forward", []):
        columns = panel.get("columns", [])
        domain = panel.get("name", "").rsplit("Clock Domain ", 1)[-1]
        for row in panel.get("rows", []):
            fields = row_as_fields(columns, row)
            fast_forward.append(
                {
                    "clock_domain": domain,
                    "step": first_field(fields, ("Step",)),
                    "optimization": first_field(fields, ("Fast Forward Optimizations Analyzed",)),
                    "estimated_fmax": first_field(fields, ("Estimated Fmax",)),
                    "slack": first_field(fields, ("Slack",)),
                    "relationship": first_field(fields, ("Relationship",)),
                    "source": f"report_db:{panel.get('name', 'Fast Forward Summary')}",
                }
            )

    retiming = []
    for panel in reports.get("retiming_limits", []):
        columns = panel.get("columns", [])
        for row in panel.get("rows", []):
            fields = row_as_fields(columns, row)
            retiming.append(
                {
                    "clock_transfer": first_field(fields, ("Clock Transfer",)),
                    "limiting_reason": first_field(fields, ("Limiting Reason",)),
                    "recommendation": first_field(fields, ("Recommendation",)),
                    "source": f"report_db:{panel.get('name', 'Retiming Limit Summary')}",
                }
            )
    return {
        "design_assistant": {
            "status": design_status,
            "rules_checked": len(design_rules),
            "violations": [item for item in design_rules if item["violations"] > 0],
            "source": "report_db" if design_panels else None,
        },
        "fast_forward": {
            "status": "available" if reports.get("fast_forward") else "unavailable-or-not-run",
            "records": fast_forward,
        },
        "retiming_limits": {
            "status": "available" if reports.get("retiming_limits") else "unavailable-or-not-run",
            "records": retiming,
        },
    }


def build_health(
    reports: dict[str, Any],
    check_columns: list[str],
    check_rows: list[list[str]],
    cdc_columns: list[str],
    cdc_rows: list[list[str]],
    metric_aggregate: dict[str, Any],
    parser_status: dict[str, Any],
    collector_warnings: list[str],
    sta_warning_count: int,
    guidance: dict[str, Any],
) -> dict[str, Any]:
    reasons: list[str] = []
    check_issues = {}
    for row in check_rows:
        fields = row_as_fields(check_columns, row)
        name = first_field(fields, ("Check",))
        count = int(numeric(first_field(fields, ("Number of Issues Found",))) or 0)
        if name and count:
            check_issues[name] = count

    unconstrained = []
    for panel in reports.get("unconstrained", []):
        for row in panel.get("rows", []):
            fields = row_as_fields(panel.get("columns", []), row)
            prop = first_field(fields, ("Property",))
            if prop and sum(int(numeric(fields.get(key)) or 0) for key in ("Setup", "Hold")):
                unconstrained.append(fields)
            status = first_field(fields, ("Status",))
            if status and status.lower() != "constrained":
                unconstrained.append(fields)

    unsafe_transfers_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for panel in reports.get("clock_transfers", []):
        for row in panel.get("rows", []):
            fields = row_as_fields(panel.get("columns", []), row)
            classification = first_field(fields, ("Clock Pair Classification",))
            if "unsafe" in classification.lower():
                key = (
                    first_field(fields, ("From Clock",)),
                    first_field(fields, ("To Clock",)),
                    classification,
                )
                unsafe_transfers_by_key[key] = fields
    unsafe_transfers = list(unsafe_transfers_by_key.values())

    constraints_status = "pass"
    if check_issues or unconstrained or unsafe_transfers:
        constraints_status = "blocked"
        reasons.append(
            f"timing constraints require review: check_timing={sum(check_issues.values())}, "
            f"unconstrained={len(unconstrained)}, unsafe_clock_transfers={len(unsafe_transfers)}"
        )
    elif not check_columns or not reports.get("unconstrained"):
        constraints_status = "warning"
        reasons.append("constraint health is incomplete; missing check_timing or unconstrained-path data")

    cdc_findings = []
    benign = ("compliant", "false path", "inactive")
    for row in cdc_rows:
        fields = row_as_fields(cdc_columns, row)
        name = first_field(fields, ("CDC Type",))
        count = int(numeric(first_field(fields, ("CDC Count",))) or 0)
        if count and not any(token in name.lower() for token in benign):
            cdc_findings.append({"type": name, "count": count})
    cdc_status = "warning" if cdc_findings else "pass" if cdc_columns else "unavailable"
    if cdc_findings:
        reasons.append(f"CDC report contains {sum(item['count'] for item in cdc_findings)} non-compliant/unreviewed transfers")
    elif not cdc_columns:
        reasons.append("CDC report is unavailable; absence is not treated as clean")

    design = guidance.get("design_assistant", {})
    design_status = design.get("status", "unavailable-or-not-run")
    if design_status == "unavailable-or-not-run":
        reasons.append("Design Assistant was not run or its Report DB panel is unavailable")
    elif design_status == "violations":
        reasons.append(f"Design Assistant reports {len(design.get('violations', []))} violated rules")

    failed_parsers = [
        name
        for name, status in parser_status.items()
        if status.get("status") in ("warning", "unavailable")
    ]
    breakdown_failures = int(metric_aggregate.get("quartus_breakdown_failures", 0))
    consistency_failures = int(metric_aggregate.get("consistency_failures", 0))
    data_status = "pass"
    if breakdown_failures or consistency_failures:
        data_status = "blocked"
        reasons.append(
            f"timing data validation failed: point_sum={consistency_failures}, breakdown={breakdown_failures}"
        )
    elif failed_parsers or collector_warnings or sta_warning_count:
        data_status = "warning"
        reasons.append(
            f"collector/parser/STA warnings: "
            f"{len(collector_warnings) + len(failed_parsers) + sta_warning_count}"
        )

    overall = "blocked" if "blocked" in (constraints_status, data_status) else "warning" if (
        "warning" in (constraints_status, cdc_status, data_status)
        or cdc_status == "unavailable"
        or design_status in ("violations", "unavailable-or-not-run")
    ) else "pass"
    return {
        "status": overall,
        "constraints": {
            "status": constraints_status,
            "check_timing": check_issues,
            "unconstrained_records": unconstrained,
            "unsafe_clock_transfers": unsafe_transfers,
        },
        "cdc": {"status": cdc_status, "findings": cdc_findings},
        "design_assistant": design,
        "data_quality": {
            "status": data_status,
            "point_sum_failures": consistency_failures,
            "breakdown_failures": breakdown_failures,
            "parser_warnings": failed_parsers,
            "collector_warnings": len(collector_warnings),
            "sta_warning_count": sta_warning_count,
        },
        "reasons": reasons,
    }


def row_as_fields(columns: list[str], row: list[Any]) -> dict[str, Any]:
    return {column: row[index] if index < len(row) else "" for index, column in enumerate(columns)}


def first_field(fields: dict[str, Any], candidates: Iterable[str]) -> str:
    lowered = {key.lower(): key for key in fields}
    for candidate in candidates:
        key = lowered.get(candidate.lower())
        if key is not None and str(fields[key]).strip():
            return str(fields[key]).strip()
    return ""


def normalized_records(
    source: str,
    columns: list[str],
    rows: list[list[Any]],
    node_columns: Iterable[str],
    limit: int = 500,
) -> dict[str, Any]:
    records = []
    for row in rows[:limit]:
        fields = row_as_fields(columns, row)
        node = first_field(fields, node_columns)
        record: dict[str, Any] = {"source": source, "fields": fields}
        if node:
            record["node"] = node
            record["identity"] = node_identity(node)
        records.append(record)
    return {
        "source": source,
        "columns": columns,
        "record_count": len(rows),
        "records": records,
        "truncated": len(rows) > limit,
    }


def normalized_diagnostics(
    run_dir: Path,
    reports: dict[str, Any],
    relevant_nodes: list[dict[str, str]],
) -> dict[str, Any]:
    """Normalize useful report tables behind one schema, regardless of origin."""
    file_specs = {
        "logic_depth": ("logic_depth.rpt", r"clock name.*clock period", ("Clock Name",)),
        "register_spread": ("register_spread.rpt", r"register name.*register location", ("Register Name",)),
        "net_delay": ("net_delay.rpt", r"name.*slack.*required.*actual.*from.*to", ("From", "To")),
        "route_nets": ("route_nets_of_interest.rpt", r"net driver.*route effort", ("Net Driver",)),
        "pipelining": ("pipelining_info.rpt", r"full hierarchy name.*clock name", ("Full Hierarchy Name",)),
        "retiming_restrictions": (
            "retiming_restrictions.rpt",
            r"compilation hierarchy node.*full hierarchy name",
            ("Full Hierarchy Name", "Compilation Hierarchy Node"),
        ),
    }
    result: dict[str, Any] = {}
    for name, (filename, header, node_columns) in file_specs.items():
        columns, rows = ascii_table(run_dir / filename, header)
        if columns:
            original_row_count = len(rows)
            if name == "retiming_restrictions":
                node_column = next(
                    (columns.index(candidate) for candidate in node_columns if candidate in columns),
                    None,
                )
                if node_column is not None:
                    relevant_exact = {identity["exact"] for identity in relevant_nodes}
                    relevant_base = {identity["base"] for identity in relevant_nodes}
                    relevant_normalized = {identity["normalized"] for identity in relevant_nodes}

                    def relevance(row: list[Any]) -> int:
                        if node_column >= len(row):
                            return 0
                        identity = node_identity(str(row[node_column]))
                        if len(identity["exact"]) >= 12 and identity["exact"] in relevant_exact:
                            return 3
                        if len(identity["base"]) >= 12 and (
                            identity["base"] in relevant_base or identity["normalized"] in relevant_normalized
                        ):
                            return 2
                        if identity["hierarchy"] and any(
                            identity["hierarchy"] == item["hierarchy"]
                            for item in relevant_nodes
                        ):
                            return 1
                        return 0

                    scored_rows = [(relevance(row), row) for row in rows]
                    rows = [row for score, row in sorted(scored_rows, key=lambda item: item[0], reverse=True) if score > 0]
            normalized = normalized_records(f"rpt:{filename}", columns, rows, node_columns)
            if name == "retiming_restrictions":
                normalized["record_count"] = original_row_count
                normalized["selected_record_count"] = len(rows)
                normalized["selection"] = "records matching sampled detailed-path data nodes"
                normalized["truncated"] = len(rows) > 500
            result[name] = normalized

    panel_specs = {
        "high_fanout": ("high_fanout", ("Name",)),
        "highest_wire_count": ("highest_wire_count", ("Net",)),
        "peak_wire_details": ("peak_wire_details", ("Net Names",)),
        "retiming_limits": ("retiming_limits", ("Clock Transfer",)),
    }
    for name, (category, node_columns) in panel_specs.items():
        records: list[dict[str, Any]] = []
        columns: list[str] = []
        total = 0
        for panel in reports.get(category, []):
            panel_columns = [str(item) for item in panel.get("columns", [])]
            panel_rows = panel.get("rows", [])
            if not panel_columns:
                continue
            columns = panel_columns
            total += len(panel_rows)
            normalized = normalized_records(
                f"panel:{panel.get('name', category)}", panel_columns, panel_rows, node_columns
            )
            records.extend(normalized["records"])
        if columns:
            result[name] = {
                "source": "report_db",
                "columns": columns,
                "record_count": total,
                "records": records[:500],
                "truncated": len(records) > 500,
            }
    return result


def node_match_confidence(record: dict[str, Any], path_nodes: list[dict[str, str]], hierarchy: str) -> tuple[float, str]:
    identity = record.get("identity")
    if not isinstance(identity, dict):
        return 0.0, "none"
    exact = identity.get("exact", "")
    base = identity.get("base", "")
    normalized = identity.get("normalized", "")
    for path_node in path_nodes:
        if exact and exact == path_node.get("exact"):
            return 1.0, "exact-node"
        if base and len(base) >= 12 and base == path_node.get("base"):
            return 0.85, "base-node"
        if normalized and len(normalized) >= 12 and normalized == path_node.get("normalized"):
            return 0.65, "normalized-node"
    if identity.get("hierarchy") and identity.get("hierarchy") == hierarchy:
        return 0.2, "same-hierarchy"
    return 0.0, "none"


def compact_evidence(record: dict[str, Any], confidence: float, method: str) -> dict[str, Any]:
    return {
        "source": record.get("source"),
        "node": record.get("node", ""),
        "match_confidence": confidence,
        "match_method": method,
        "fields": record.get("fields", {}),
    }


def record_path_matches(record: dict[str, Any], entries: list[dict[str, Any]]) -> list[tuple[int, float, str]]:
    """Return sampled paths reached by node-level evidence.

    A hierarchy-only match is deliberately excluded: it is useful context but
    does not prove that the report record describes the sampled timing cone.
    """
    matches = []
    for index, entry in enumerate(entries):
        identities = [node["identity"] for node in entry["nodes"]]
        confidence, method = node_match_confidence(record, identities, "")
        if confidence >= 0.60:
            matches.append((index, confidence, method))
    return matches


def issue_secondary_anchors(
    entries: list[dict[str, Any]], structured: dict[str, Any], primary: dict[str, Any]
) -> list[dict[str, Any]]:
    """Keep credible alternate shared nodes instead of discarding them."""
    candidates: dict[tuple[str, str], dict[str, Any]] = {}
    for record in structured.get("bottlenecks", {}).get("records", []):
        matches = record_path_matches(record, entries)
        if len(matches) < 2:
            continue
        identity = record.get("identity", {})
        key = ("bottleneck-node", str(identity.get("base") or record.get("node", "")))
        candidates[key] = {
            "node": record.get("node", ""),
            "identity": identity,
            "selection_method": "bottleneck-node",
            "source": record.get("source"),
            "sampled_path_count": len(matches),
            "match_confidence": min(item[1] for item in matches),
            "fields": record.get("fields", {}),
        }
    common: dict[tuple[str, str], dict[str, Any]] = {}
    for index, entry in enumerate(entries):
        for node in entry["nodes"]:
            for kind, strength in (("exact", 1.0), ("base", 0.85), ("normalized", 0.65)):
                value = node["identity"].get(kind, "")
                if len(value) < 12:
                    continue
                item = common.setdefault(
                    (kind, value),
                    {"members": set(), "node": value, "identity": node_identity(value), "strength": strength},
                )
                item["members"].add(index)
    for (kind, value), item in common.items():
        if len(item["members"]) < 2:
            continue
        method = f"common-{kind}-node"
        candidates[(method, value)] = {
            "node": value,
            "identity": item["identity"],
            "selection_method": method,
            "source": "timing_api:detailed_path_points",
            "sampled_path_count": len(item["members"]),
            "match_confidence": item["strength"],
            "fields": {},
        }
    def canonical_cell(item: dict[str, Any]) -> str:
        name = str(item.get("node", ""))
        return normalize_node_name(re.sub(r"\|(?:q|d|clk)$", "", base_node_name(name), flags=re.I))

    primary_identity = primary.get("identity", {})
    primary_names = {
        str(primary_identity.get(kind, ""))
        for kind in ("exact", "base", "normalized")
        if primary_identity.get(kind)
    }
    values = []
    for item in candidates.values():
        identity = item.get("identity", {})
        names = {
            str(identity.get(kind, ""))
            for kind in ("exact", "base", "normalized")
            if identity.get(kind)
        }
        if primary_names.isdisjoint(names):
            values.append(item)
    values.sort(
        key=lambda item: (
            item["sampled_path_count"],
            item["selection_method"] == "bottleneck-node",
            item["match_confidence"],
        ),
        reverse=True,
    )
    unique = []
    seen_cells = {canonical_cell(primary)}
    for item in values:
        cell = canonical_cell(item)
        if not cell or cell in seen_cells:
            continue
        seen_cells.add(cell)
        unique.append(item)
        if len(unique) == 10:
            break
    return unique


def issue_recommendations(diagnoses: list[str]) -> list[str]:
    recommendations = []
    found = set(diagnoses)
    if "LOGIC_LIMITED" in found or "DEEP_LOGIC" in found:
        recommendations.append("Inspect the combinational cone for pipelining, predecode, or balanced-tree restructuring.")
    if "HIGH_FANOUT" in found:
        recommendations.append("Consider local registered control leaves or placement-aware register duplication.")
    if "PHYSICAL_SPREAD" in found:
        recommendations.append("Reduce producer-to-consumer spread; keep state/control close to its physical consumers.")
    if "ROUTING_PRESSURE" in found:
        recommendations.append("Inspect the matched high-wire/route-effort nets before changing unrelated logic depth.")
    if "RETIMING_RESTRICTED" in found or "CLOCK_DOMAIN_RETIMING_LIMIT" in found:
        recommendations.append("Inspect the matched retiming restriction or RTL loop; automatic retiming may not cross it.")
    return recommendations


def path_aggregate(metrics: list[dict[str, Any]]) -> dict[str, Any]:
    def average(key: str) -> float | None:
        values = [numeric(item.get(key)) for item in metrics]
        present = [value for value in values if value is not None]
        return sum(present) / len(present) if present else None

    slacks = [value for item in metrics if (value := numeric(item.get("slack_ns"))) is not None]
    return {
        "paths": len(metrics),
        "wns_ns": min(slacks) if slacks else None,
        "average_route_ratio": average("route_ratio"),
        "average_route_delay_ns": average("route_delay_ns"),
        "average_local_ic_delay_ns": average("local_ic_delay_ns"),
        "average_fabric_ic_delay_ns": average("fabric_ic_delay_ns"),
        "average_cell_delay_ns": average("cell_delay_ns"),
        "average_logic_cell_delay_ns": average("logic_cell_delay_ns"),
        "average_launch_delay_ns": average("launch_delay_ns"),
        "max_logic_levels": max((int(numeric(item.get("logic_levels")) or 0) for item in metrics), default=0),
        "max_fanout": max((int(numeric(item.get("max_fanout")) or 0) for item in metrics), default=0),
        "consistency_failures": sum(
            item.get("consistency", {}).get("status") == "fail" for item in metrics
        ),
        "quartus_breakdown_failures": sum(
            item.get("quartus_breakdown_validation", {}).get("status") == "fail" for item in metrics
        ),
        "quartus_breakdown_unavailable": sum(
            item.get("quartus_breakdown_validation", {}).get("status") == "unavailable" for item in metrics
        ),
    }


def cluster_issue_entries(
    detailed_paths: list[dict[str, Any]],
    detailed_metrics: list[dict[str, Any]],
    structured: dict[str, Any],
) -> list[dict[str, Any]]:
    """Cluster failing paths around a shared node, not their destination hierarchy."""
    entries = []
    for path, metrics in zip(detailed_paths, detailed_metrics):
        slack = numeric(path.get("slack_ns"))
        if slack is not None and slack < 0:
            entries.append({"path": path, "metrics": metrics, "nodes": logical_path_node_records(path)})
    unassigned = set(range(len(entries)))
    clusters: list[dict[str, Any]] = []

    # A Quartus bottleneck traversed by multiple sampled paths is the preferred
    # issue identity.  Hierarchy-only matches are deliberately excluded.
    bottleneck_candidates = []
    for record in structured.get("bottlenecks", {}).get("records", []):
        matched: dict[int, tuple[float, str]] = {}
        for index, entry in enumerate(entries):
            identities = [node["identity"] for node in entry["nodes"]]
            confidence, method = node_match_confidence(record, identities, "")
            if confidence >= 0.60:
                matched[index] = (confidence, method)
        if len(matched) < 2:
            continue
        fields = record.get("fields", {})
        bottleneck_candidates.append(
            {
                "record": record,
                "matched": matched,
                "score": (
                    len(matched),
                    min(confidence for confidence, _ in matched.values()),
                    numeric(fields.get("Rating")) or 0.0,
                ),
            }
        )
    for candidate in sorted(bottleneck_candidates, key=lambda item: item["score"], reverse=True):
        members = sorted(set(candidate["matched"]) & unassigned)
        if len(members) < 2:
            continue
        record = candidate["record"]
        methods = Counter(candidate["matched"][index][1] for index in members)
        minimum_match = min(candidate["matched"][index][0] for index in members)
        clusters.append(
            {
                "member_indices": members,
                "root": {
                    "node": record.get("node", ""),
                    "identity": record.get("identity", {}),
                    "selection_method": "bottleneck-node",
                    "source": record.get("source"),
                    "sampled_path_count": len(members),
                    "match_methods": dict(methods),
                    "match_confidence": minimum_match,
                    "grouping_confidence": 1.0 if minimum_match == 1.0 else 0.85,
                    "fields": record.get("fields", {}),
                },
            }
        )
        unassigned.difference_update(members)

    # For paths not covered by report_bottleneck, find a repeated logical node.
    # Exact/base common-cone identities outrank normalized bus-lane identities.
    common_candidates: dict[tuple[str, str], dict[str, Any]] = {}
    for index in unassigned:
        for node in entries[index]["nodes"]:
            for identity_kind, strength in (("exact", 1.0), ("base", 0.85), ("normalized", 0.65)):
                value = node["identity"].get(identity_kind, "")
                if len(value) < 12:
                    continue
                candidate = common_candidates.setdefault(
                    (identity_kind, value),
                    {
                        "identity_kind": identity_kind,
                        "node": value,
                        "identity": node_identity(value),
                        "members": set(),
                        "strength": strength,
                        "max_fanout": 0,
                        "max_delay": 0.0,
                    },
                )
                candidate["members"].add(index)
                candidate["max_fanout"] = max(candidate["max_fanout"], node["max_fanout"])
                candidate["max_delay"] = max(candidate["max_delay"], node["max_incremental_delay_ns"])
    candidates = [candidate for candidate in common_candidates.values() if len(candidate["members"]) >= 2]
    candidates.sort(
        key=lambda item: (
            len(item["members"]),
            item["strength"],
            item["max_fanout"],
            item["max_delay"],
            len(item["node"]),
        ),
        reverse=True,
    )
    for candidate in candidates:
        members = sorted(candidate["members"] & unassigned)
        if len(members) < 2:
            continue
        kind = candidate["identity_kind"]
        grouping_confidence = {"exact": 0.90, "base": 0.80, "normalized": 0.60}[kind]
        clusters.append(
            {
                "member_indices": members,
                "root": {
                    "node": candidate["node"],
                    "identity": candidate["identity"],
                    "selection_method": f"common-{kind}-node",
                    "source": "timing_api:detailed_path_points",
                    "sampled_path_count": len(members),
                    "match_methods": {f"{kind}-node": len(members)},
                    "match_confidence": candidate["strength"],
                    "grouping_confidence": grouping_confidence,
                    "fields": {
                        "Max Fanout": candidate["max_fanout"],
                        "Max Incremental Delay": candidate["max_delay"],
                    },
                },
            }
        )
        unassigned.difference_update(members)

    # Endpoint identity is a conservative fallback.  Normalized bus bits may be
    # grouped, but carry lower grouping confidence.  Hierarchy is display-only
    # unless an endpoint is absent.
    endpoint_groups: dict[tuple[str, str], list[int]] = defaultdict(list)
    for index in unassigned:
        endpoint = str(entries[index]["path"].get("to", ""))
        if endpoint:
            identity = node_identity(endpoint)
            endpoint_groups[("endpoint", identity["normalized"])].append(index)
        else:
            endpoint_groups[("hierarchy", hierarchy_group(str(entries[index]["path"].get("from", ""))))].append(index)
    for (kind, key), members in endpoint_groups.items():
        identity = node_identity(key)
        clusters.append(
            {
                "member_indices": sorted(members),
                "root": {
                    "node": key,
                    "identity": identity,
                    "selection_method": f"{kind}-fallback",
                    "source": "timing_api:get_timing_paths",
                    "sampled_path_count": len(members),
                    "match_methods": {"normalized-node" if kind == "endpoint" else "same-hierarchy": len(members)},
                    "match_confidence": 0.55 if kind == "endpoint" else 0.20,
                    "grouping_confidence": 0.50 if kind == "endpoint" else 0.25,
                    "fields": {},
                },
            }
        )
    for cluster in clusters:
        cluster["entries"] = [entries[index] for index in cluster.pop("member_indices")]
    return clusters


def component_confidence(statuses: Iterable[str], mapping: dict[str, float]) -> float:
    values = [mapping.get(status, 0.0) for status in statuses]
    return sum(values) / len(values) if values else 0.0


def build_issues(
    detailed_paths: list[dict[str, Any]],
    detailed_metrics: list[dict[str, Any]],
    structured: dict[str, Any],
) -> list[dict[str, Any]]:
    issues = []
    for cluster in cluster_issue_entries(detailed_paths, detailed_metrics, structured):
        entries = cluster["entries"]
        anchor = cluster["root"]
        metrics_list = [entry["metrics"] for entry in entries]
        aggregate = path_aggregate(metrics_list)
        hierarchy = anchor.get("identity", {}).get("hierarchy") or hierarchy_group(anchor.get("node", ""))
        path_nodes = {
            node["identity"]["exact"]: node["identity"]
            for entry in entries
            for node in entry["nodes"]
            if node["identity"]["exact"]
        }
        evidence: dict[str, list[dict[str, Any]]] = {}
        contextual_evidence: dict[str, list[dict[str, Any]]] = {}
        for category in (
            "bottlenecks",
            "high_fanout",
            "register_spread",
            "route_nets",
            "highest_wire_count",
            "peak_wire_details",
            "retiming_restrictions",
            "pipelining",
            "design_assistant",
        ):
            node_matches = []
            context_matches = []
            for record in structured.get(category, {}).get("records", []):
                path_matches = record_path_matches(record, entries)
                if path_matches:
                    confidence = min(match[1] for match in path_matches)
                    methods = Counter(match[2] for match in path_matches)
                    method = next(iter(methods)) if len(methods) == 1 else "mixed-node-match"
                    item = compact_evidence(record, confidence, method)
                    item["match_methods"] = dict(methods)
                    item["sampled_path_count"] = len(path_matches)
                    item["coverage"] = len(path_matches) / len(entries)
                    node_matches.append(item)
                    continue
                confidence, method = node_match_confidence(record, list(path_nodes.values()), hierarchy)
                item = compact_evidence(record, confidence, method)
                if confidence >= 0.20:
                    context_matches.append(item)
            node_matches.sort(key=lambda item: item["match_confidence"], reverse=True)
            if node_matches:
                evidence[category] = node_matches[:10]
            if context_matches:
                contextual_evidence[category] = context_matches[:5]

        # Fanout measured on a detailed timing point is already node-level
        # Quartus evidence; retain the node and affected sampled-path count.
        fanout_nodes: dict[str, dict[str, Any]] = {}
        for entry in entries:
            metrics = entry["metrics"]
            fanout = int(numeric(metrics.get("max_fanout")) or 0)
            node = str(metrics.get("max_fanout_node", ""))
            if fanout < 64 or not node:
                continue
            item = fanout_nodes.setdefault(
                node,
                {
                    "source": "timing_api:detailed_path_points",
                    "node": node,
                    "match_confidence": 1.0,
                    "match_method": "exact-node",
                    "sampled_path_count": 0,
                    "coverage": 0.0,
                    "fields": {"Max Fanout": fanout, "Sampled Failing Paths": 0},
                },
            )
            item["fields"]["Max Fanout"] = max(item["fields"]["Max Fanout"], fanout)
            item["fields"]["Sampled Failing Paths"] += 1
            item["sampled_path_count"] += 1
            item["coverage"] = item["sampled_path_count"] / len(entries)
        if fanout_nodes:
            evidence["path_point_fanout"] = sorted(
                fanout_nodes.values(), key=lambda item: item["fields"]["Max Fanout"], reverse=True
            )[:10]

        path_clocks = {str(entry["path"].get("to_clock", "")) for entry in entries}
        clock_context = []
        for record in structured.get("retiming_limits", {}).get("records", []):
            fields = record.get("fields", {})
            transfer = first_field(fields, ("Clock Transfer",))
            if any(clock and clock in transfer for clock in path_clocks):
                clock_context.append(compact_evidence(record, 0.35, "same-clock-context"))
        if clock_context:
            contextual_evidence["retiming_limits"] = clock_context[:10]

        classes = Counter(str(metrics.get("classification", "unknown")) for metrics in metrics_list)
        ratios = [value for metrics in metrics_list if (value := numeric(metrics.get("route_ratio"))) is not None]
        heterogeneous = "routing-limited" in classes and "logic-limited" in classes and max(ratios) - min(ratios) >= 0.30
        route_ratio = numeric(aggregate.get("average_route_ratio"))
        if heterogeneous:
            primary_diagnosis = "MIXED_ROOT_CAUSE"
        elif route_ratio is not None and route_ratio >= 0.60:
            primary_diagnosis = "ROUTING_LIMITED"
        elif route_ratio is not None and route_ratio <= 0.35:
            primary_diagnosis = "LOGIC_LIMITED"
        else:
            primary_diagnosis = "MIXED_DELAY"

        def has_node_evidence(category: str) -> bool:
            return any((numeric(item.get("match_confidence")) or 0) >= 0.60 for item in evidence.get(category, []))

        contributors = []
        if aggregate["max_logic_levels"] >= 8:
            contributors.append("DEEP_LOGIC")
        if has_node_evidence("path_point_fanout") or has_node_evidence("high_fanout"):
            contributors.append("HIGH_FANOUT")
        if has_node_evidence("register_spread"):
            contributors.append("PHYSICAL_SPREAD")
        if any(has_node_evidence(category) for category in ("route_nets", "highest_wire_count", "peak_wire_details")):
            contributors.append("ROUTING_PRESSURE")
        if has_node_evidence("retiming_restrictions"):
            contributors.append("RETIMING_RESTRICTED")
        elif contextual_evidence.get("retiming_limits"):
            contributors.append("CLOCK_DOMAIN_RETIMING_LIMIT")
        if has_node_evidence("design_assistant"):
            contributors.append("DESIGN_ASSISTANT_VIOLATION")
        uncertainties = []
        if heterogeneous:
            uncertainties.append("conflicting delay character inside one root cluster")
        if anchor["selection_method"] == "common-normalized-node":
            uncertainties.append("issue anchor relies on bus-index normalization")
        if anchor["selection_method"].endswith("fallback"):
            uncertainties.append(f"issue anchor uses {anchor['selection_method']}")
        consistency_statuses = [metrics.get("consistency", {}).get("status", "fail") for metrics in metrics_list]
        breakdown_statuses = [
            metrics.get("quartus_breakdown_validation", {}).get("status", "unavailable") for metrics in metrics_list
        ]
        if "fail" in consistency_statuses:
            uncertainties.append("timing point consistency failure")
        if "fail" in breakdown_statuses:
            uncertainties.append("Quartus delay breakdown mismatch")
        elif "unavailable" in breakdown_statuses:
            uncertainties.append("Quartus delay breakdown unavailable for part of sample")

        timing_confidence = component_confidence(consistency_statuses, {"pass": 1.0, "warning": 0.60, "fail": 0.0})
        breakdown_confidence = component_confidence(breakdown_statuses, {"pass": 1.0, "unavailable": 0.60, "fail": 0.0})
        # Anchor confidence is a separate component. Evidence coverage measures
        # independent diagnosis evidence, not the node used to form the issue.
        per_path_evidence = [0.0] * len(entries)
        for values in evidence.values():
            for item in values:
                record = {
                    "node": item.get("node", ""),
                    "identity": node_identity(str(item.get("node", ""))),
                }
                for index, match_confidence, _ in record_path_matches(record, entries):
                    per_path_evidence[index] = max(per_path_evidence[index], match_confidence)
        evidence_strength = sum(per_path_evidence) / len(per_path_evidence) if per_path_evidence else 0.0
        evidence_coverage = (
            sum(value >= 0.60 for value in per_path_evidence) / len(per_path_evidence)
            if per_path_evidence
            else 0.0
        )
        anchor_confidence = numeric(anchor.get("grouping_confidence")) or 0.0
        overall = (
            timing_confidence
            + breakdown_confidence
            + anchor_confidence
            + evidence_strength
            + evidence_coverage
        ) / 5.0
        if timing_confidence < 0.50:
            overall = min(overall, 0.25)
        elif timing_confidence < 1.0:
            overall = min(overall, 0.79)
        if breakdown_confidence == 0.0:
            overall = min(overall, 0.35)
        elif breakdown_confidence < 1.0:
            overall = min(overall, 0.79)
        if evidence_coverage < 0.50:
            overall = min(overall, 0.79)
        if heterogeneous:
            overall = min(overall, 0.54)
        if anchor["selection_method"] == "common-normalized-node":
            overall = min(overall, 0.79)
        if anchor["selection_method"] == "endpoint-fallback":
            overall = min(overall, 0.69)
        if anchor["selection_method"] == "hierarchy-fallback":
            overall = min(overall, 0.40)
        confidence = {
            "overall": overall,
            "level": "high" if overall >= 0.80 else "medium" if overall >= 0.55 else "low",
            "timing_consistency": timing_confidence,
            "quartus_breakdown_validation": breakdown_confidence,
            "anchor_confidence": anchor_confidence,
            "evidence_strength": evidence_strength,
            "evidence_coverage": evidence_coverage,
        }

        identity_kind = "normalized" if "normalized" in anchor["selection_method"] else "base"
        stable_anchor = anchor.get("identity", {}).get(identity_kind) or anchor.get("node", "")
        issue_key = f"{anchor['selection_method']}:{stable_anchor}"
        context = []
        if any(
            "Memory_rtl" in str(entry["path"].get("from", ""))
            or "Memory_rtl" in str(entry["path"].get("to", ""))
            for entry in entries
        ):
            context.append("MEMORY_ENDPOINT")
        diagnoses = [primary_diagnosis, *contributors]
        worst_entry = min(entries, key=lambda entry: numeric(entry["path"].get("slack_ns")) or 0)
        path_keys = sorted(
            {
                f"{node_identity(str(entry['path'].get('from', '')))['normalized']} -> "
                f"{node_identity(str(entry['path'].get('to', '')))['normalized']}"
                for entry in entries
            }
        )
        issues.append(
            {
                "issue_id": hashlib.sha1(issue_key.encode()).hexdigest()[:12],
                "issue_anchor": anchor,
                "root_cause_candidate": bool(
                    anchor.get("selection_method") == "bottleneck-node"
                    and (numeric(anchor.get("match_confidence")) or 0.0) >= 0.85
                    and int(anchor.get("sampled_path_count", 0)) >= 2
                    and timing_confidence == 1.0
                    and breakdown_confidence == 1.0
                ),
                "secondary_anchors": issue_secondary_anchors(entries, structured, anchor),
                "hierarchy": hierarchy,
                **aggregate,
                "primary_diagnosis": primary_diagnosis,
                "contributors": contributors,
                "context": context,
                "clock_domains": sorted(path_clocks),
                "uncertainties": uncertainties,
                "diagnoses": diagnoses,
                "recommendations": issue_recommendations(diagnoses),
                "confidence": confidence,
                "evidence": evidence,
                "contextual_evidence": contextual_evidence,
                "path_keys": path_keys,
                "worst_from": worst_entry["path"].get("from"),
                "worst_to": worst_entry["path"].get("to"),
            }
        )
    issues.sort(key=lambda item: numeric(item.get("wns_ns")) if numeric(item.get("wns_ns")) is not None else 0)
    return issues


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

    neighbor_records = neighbor_path_records(run_dir / "neighbor_paths.rpt")
    neighbor_matches = match_neighbor_paths(detailed_paths, neighbor_records)
    detailed_metrics = []
    for path, neighbor in zip(detailed_paths, neighbor_matches):
        metrics = apply_neighbor_validation(point_metrics(path), neighbor, path)
        detailed_metrics.append(
            {
                "slack_ns": path.get("slack_ns"),
                "data_delay_ns": path.get("data_delay_ns"),
                "from": path.get("from"),
                "to": path.get("to"),
                "from_clock": path.get("from_clock"),
                "to_clock": path.get("to_clock"),
                "corner": path.get("corner"),
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
    sta_log = run_dir / "quartus_sta_collect.log"
    sta_warning_count = 0
    if sta_log.exists():
        sta_warning_count = sum(
            line.startswith("Warning (")
            for line in sta_log.read_text(encoding="utf-8", errors="replace").splitlines()
        )

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

    report_summary = selected_panel_summary(panels)
    guidance = normalized_quartus_guidance(report_summary)
    relevant_nodes = [
        node["identity"]
        for path in detailed_paths
        for node in logical_path_node_records(path)
    ]
    structured = normalized_diagnostics(run_dir, report_summary, relevant_nodes)
    design_records = []
    for violation in guidance.get("design_assistant", {}).get("violations", []):
        node = str(violation.get("node", ""))
        record = {
            "source": violation.get("source"),
            "node": node,
            "fields": violation,
        }
        if node:
            record["identity"] = node_identity(node)
        design_records.append(record)
    if design_records:
        structured["design_assistant"] = {
            "source": "report_db",
            "columns": [],
            "record_count": len(design_records),
            "records": design_records,
            "truncated": False,
        }
    if neighbor_records:
        structured["neighbor_paths"] = {
            "source": "rpt:neighbor_paths.rpt",
            "columns": [],
            "record_count": len(neighbor_records),
            "records": neighbor_records,
            "truncated": False,
        }
    if bottleneck_columns:
        structured["bottlenecks"] = normalized_records(
            "rpt:bottlenecks.rpt", bottleneck_columns, bottleneck_rows, ("Node",), limit=100
        )
    parser_files = {
        "logic_depth": "logic_depth.rpt",
        "register_spread": "register_spread.rpt",
        "net_delay": "net_delay.rpt",
        "route_nets": "route_nets_of_interest.rpt",
        "pipelining": "pipelining_info.rpt",
        "retiming_restrictions": "retiming_restrictions.rpt",
        "neighbor_paths": "neighbor_paths.rpt",
        "bottlenecks": "bottlenecks.rpt",
    }
    parser_status = {}
    snapshot = str(timing_metadata.get("snapshot", "final"))
    capabilities = snapshot_capabilities(snapshot)
    stage_requirements = {
        "register_spread": "register_spread",
        "route_nets": "routing_detail",
        "net_delay": "routing_detail",
        "retiming_restrictions": "retiming",
        "pipelining": "retiming",
    }
    for name, filename in parser_files.items():
        report_path = run_dir / filename
        capability = stage_requirements.get(name)
        if capability and capabilities[capability]["status"] == "unavailable-for-stage":
            parser_status[name] = {
                "status": "unavailable-for-stage",
                "source": f"rpt:{filename}",
                "reason": f"requires {capabilities[capability]['minimum_stage']} snapshot",
            }
        elif not report_path.exists():
            parser_status[name] = {"status": "unavailable", "source": f"rpt:{filename}", "reason": "file missing"}
        elif name not in structured:
            parser_status[name] = {
                "status": "warning",
                "source": f"rpt:{filename}",
                "reason": "report exists but no recognized records were parsed",
            }
        else:
            parser_status[name] = {
                "status": "pass",
                "source": structured[name].get("source", f"rpt:{filename}"),
                "records": len(structured[name].get("records", [])),
            }
    capabilities["timing_paths"]["observed_status"] = "pass" if paths and detailed_paths else "warning"
    capabilities["routing_detail"]["observed_status"] = (
        "pass"
        if all(parser_status.get(name, {}).get("status") == "pass" for name in ("net_delay", "route_nets"))
        else capabilities["routing_detail"]["status"]
    )
    capabilities["register_spread"]["observed_status"] = (
        "pass"
        if parser_status.get("register_spread", {}).get("status") == "pass"
        else capabilities["register_spread"]["status"]
    )
    capabilities["retiming"]["observed_status"] = (
        "pass"
        if all(
            parser_status.get(name, {}).get("status") == "pass"
            for name in ("pipelining", "retiming_restrictions")
        )
        else capabilities["retiming"]["status"]
    )
    capabilities["compilation_report_db"]["observed_status"] = (
        "pass" if panels else capabilities["compilation_report_db"]["status"]
    )
    issues = build_issues(detailed_paths, detailed_metrics, structured)
    metric_aggregate = path_aggregate(detailed_metrics)
    health = build_health(
        report_summary,
        check_columns,
        check_rows,
        async_cdc_columns,
        async_cdc_rows,
        metric_aggregate,
        parser_status,
        warnings,
        sta_warning_count,
        guidance,
    )

    summary = {
        "schema_version": SCHEMA_VERSION,
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
            "detailed_path_aggregate": metric_aggregate,
            "source_summary": {"columns": source_columns, "rows": source_rows[:100]},
            "bottlenecks": {"columns": bottleneck_columns, "rows": bottleneck_rows[:100]},
        },
        "reports": report_summary,
        "quartus_guidance": guidance,
        "health": health,
        "capabilities": capabilities,
        "issues": issues,
        "diagnostics": {
            "check_timing": {"columns": check_columns, "rows": check_rows},
            "asynchronous_cdc": {"columns": async_cdc_columns, "rows": async_cdc_rows},
            "structured": structured,
            "parser_status": parser_status,
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
    health = summary.get("health", {})
    lines.append(f"- Preflight health: **{health.get('status', 'unknown')}**")
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
            format_number(item.get("logic_cell_delay_ns")),
            format_number(item.get("launch_delay_ns")),
            item.get("max_fanout"),
            item.get("classification_confidence"),
            item.get("to"),
        ]
        for item in timing.get("detailed_path_metrics", [])[:20]
    ]
    lines.append(
        markdown_table(
            [
                "Slack ns",
                "Class",
                "Route %",
                "Route ns",
                "Logic cell ns",
                "uTco ns",
                "Max fanout",
                "Confidence",
                "Endpoint",
            ],
            metric_rows,
        )
    )

    aggregate = timing.get("detailed_path_aggregate", {})
    lines.extend(["", "## Detailed path validation", ""])
    lines.append(
        markdown_table(
            [
                "Paths",
                "Point-sum failures",
                "Quartus-breakdown failures",
                "Breakdown unavailable",
                "Avg route %",
                "Avg local IC ns",
                "Avg fabric IC ns",
                "Avg logic cell ns",
                "Avg uTco ns",
            ],
            [[
                aggregate.get("paths", 0),
                aggregate.get("consistency_failures", 0),
                aggregate.get("quartus_breakdown_failures", 0),
                aggregate.get("quartus_breakdown_unavailable", 0),
                format_number((aggregate.get("average_route_ratio") or 0) * 100, 1, "%"),
                format_number(aggregate.get("average_local_ic_delay_ns")),
                format_number(aggregate.get("average_fabric_ic_delay_ns")),
                format_number(aggregate.get("average_logic_cell_delay_ns")),
                format_number(aggregate.get("average_launch_delay_ns")),
            ]],
        )
    )

    issues = summary.get("issues", [])
    if issues:
        lines.extend(["", "## Correlated timing issues", ""])
        issue_rows = [
            [
                issue_anchor(issue).get("node", issue.get("hierarchy")),
                format_number(issue.get("wns_ns")),
                issue.get("paths", 0),
                format_number((issue.get("average_route_ratio") or 0) * 100, 1, "%"),
                issue.get("max_logic_levels", 0),
                issue.get("max_fanout", 0),
                issue.get("primary_diagnosis", "UNKNOWN"),
                ", ".join(issue.get("contributors", [])) or "—",
                issue.get("confidence", {}).get("level", "unknown"),
                format_number(issue.get("confidence", {}).get("overall"), 2),
            ]
            for issue in issues[:20]
        ]
        lines.append(
            markdown_table(
                [
                    "Issue anchor",
                    "WNS ns",
                    "Paths",
                    "Route %",
                    "Levels",
                    "Fanout",
                    "Primary",
                    "Contributors",
                    "Confidence",
                    "Score",
                ],
                issue_rows,
            )
        )
        lines.extend(["", "### Issue evidence and recommendations", ""])
        for issue in issues[:10]:
            evidence_counts = ", ".join(
                f"{name}={len(records)}" for name, records in sorted(issue.get("evidence", {}).items())
            ) or "path metrics only"
            root = issue_anchor(issue)
            lines.append(
                f"- `{root.get('node', issue.get('hierarchy'))}` "
                f"({root.get('selection_method', 'legacy-group')}): {evidence_counts}."
            )
            confidence = issue.get("confidence", {})
            lines.append(
                "  - Confidence: "
                f"timing={format_number(confidence.get('timing_consistency'), 2)}, "
                f"breakdown={format_number(confidence.get('quartus_breakdown_validation'), 2)}, "
                f"anchor={format_number(confidence.get('anchor_confidence'), 2)}, "
                f"strength={format_number(confidence.get('evidence_strength'), 2)}, "
                f"coverage={format_number(confidence.get('evidence_coverage'), 2)}."
            )
            if issue.get("uncertainties"):
                lines.append(f"  - Uncertainties: {', '.join(issue['uncertainties'])}.")
            for recommendation in issue.get("recommendations", []):
                lines.append(f"  - {recommendation}")

    structured = summary.get("diagnostics", {}).get("structured", {})
    if structured:
        lines.extend(["", "## Structured diagnostic coverage", ""])
        lines.append(
            markdown_table(
                ["Dataset", "Source", "Records", "Selected", "Stored", "Truncated"],
                [
                    [
                        name,
                        data.get("source"),
                        data.get("record_count", 0),
                        data.get("selected_record_count", data.get("record_count", 0)),
                        len(data.get("records", [])),
                        data.get("truncated", False),
                    ]
                    for name, data in sorted(structured.items())
                ],
            )
        )

    parser_warnings = [
        (name, status)
        for name, status in summary.get("diagnostics", {}).get("parser_status", {}).items()
        if status.get("status") != "pass"
    ]
    if parser_warnings:
        lines.extend(["", "## Parser warnings", ""])
        lines.append(
            markdown_table(
                ["Dataset", "Status", "Source", "Reason"],
                [
                    [name, status.get("status"), status.get("source"), status.get("reason")]
                    for name, status in parser_warnings
                ],
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


def advice_action(
    action: str,
    why: str,
    evidence: list[str],
    source: str,
    confidence: float,
    quartus_tool: str,
    validation_stage: str,
) -> dict[str, Any]:
    return {
        "action": action,
        "why": why,
        "evidence": evidence,
        "source": source,
        "confidence": confidence,
        "quartus_tool": quartus_tool,
        "validation_stage": validation_stage,
    }


def build_advice(summary: dict[str, Any]) -> dict[str, Any]:
    health = summary.get("health", {})
    blocked = health.get("status") == "blocked"
    records = []
    if blocked:
        records.append(
            {
                "priority": 0,
                "issue_anchor": {"node": "preflight", "selection_method": "health-gate"},
                "diagnosis": "TIMING_PREFLIGHT_BLOCKED",
                "contributors": [],
                "context": [],
                "next_actions": [
                    advice_action(
                        "Resolve or explicitly waive the reported timing-constraint and clock-transfer findings before using RTL timing advice.",
                        "Timing optimization is not trustworthy while paths may be missing clocks, unconstrained, or timed across unsafe asynchronous transfers.",
                        list(health.get("reasons", [])),
                        "deterministic derived",
                        1.0,
                        "Timing Analyzer: Check Timing, Unconstrained Paths, and Clock Transfers",
                        "final",
                    )
                ],
            }
        )

    for priority, issue in enumerate(summary.get("issues", []), 1):
        anchor = issue_anchor(issue)
        contributors = set(issue.get("contributors", []))
        diagnosis = str(issue.get("primary_diagnosis", "UNKNOWN"))
        confidence = numeric(issue.get("confidence", {}).get("overall")) or 0.0
        evidence = [
            f"WNS {format_number(issue.get('wns_ns'))} ns across {issue.get('paths', 0)} sampled paths",
            f"route {format_number((issue.get('average_route_ratio') or 0) * 100, 1)}%, "
            f"logic levels {issue.get('max_logic_levels', 0)}, fanout {issue.get('max_fanout', 0)}",
        ]
        actions = []
        if not blocked:
            # Quartus recommendations outrank derived and heuristic actions.
            clocks = set(issue.get("clock_domains", []))
            for item in summary.get("quartus_guidance", {}).get("fast_forward", {}).get("records", []):
                optimization = str(item.get("optimization", ""))
                domain = str(item.get("clock_domain", ""))
                if not any(clock and (clock == domain or clock in domain) for clock in clocks):
                    continue
                if not optimization or optimization == "None" or "no further analysis" in optimization.lower():
                    continue
                actions.append(
                    advice_action(
                        optimization,
                        f"Quartus Fast Forward step {item.get('step', 'unknown')} for the same clock domain.",
                        [item.get("source", "Quartus Fast Forward")],
                        "Quartus ground truth",
                        0.95,
                        "Fast Forward Timing Closure Recommendations",
                        "retimed",
                    )
                )
            for item in issue.get("evidence", {}).get("design_assistant", []):
                recommendation = first_field(item.get("fields", {}), ("recommendation",))
                description = first_field(item.get("fields", {}), ("description", "rule"))
                if recommendation:
                    actions.append(
                        advice_action(
                            recommendation,
                            description or "Design Assistant correlates this rule violation to the issue anchor.",
                            [item.get("source", "Quartus Design Assistant")],
                            "Quartus ground truth",
                            numeric(item.get("match_confidence")) or 0.0,
                            "Design Assistant",
                            "synthesis",
                        )
                    )
            for item in issue.get("contextual_evidence", {}).get("retiming_limits", []):
                recommendation = first_field(item.get("fields", {}), ("Recommendation",))
                reason = first_field(item.get("fields", {}), ("Limiting Reason",))
                if recommendation and recommendation.lower() != "none" and "no further analysis" not in reason.lower():
                    actions.append(
                        advice_action(
                            recommendation,
                            reason or "Quartus reports a retiming limit for the issue clock domain.",
                            [item.get("source", "Quartus Retiming Limit Summary")],
                            "Quartus ground truth",
                            0.95,
                            "Fast Forward Timing Closure Recommendations / Retiming Limit Summary",
                            "retimed",
                        )
                    )
            if diagnosis == "ROUTING_LIMITED" and "HIGH_FANOUT" in contributors:
                actions.append(
                    advice_action(
                        "Inspect consumer placement and implement local registered control leaves or placement-aware duplication for the matched high-fanout anchor.",
                        "Routing delay plus node-level high-fanout evidence points to distribution cost; adding a pipeline is not the first action.",
                        evidence,
                        "deterministic derived",
                        confidence,
                        "Chip Planner, Report Register Spread, Non-Global High Fan-Out Signals",
                        "routed",
                    )
                )
            if diagnosis == "ROUTING_LIMITED" and "PHYSICAL_SPREAD" in contributors:
                actions.append(
                    advice_action(
                        "Inspect the producer/consumer footprint and reduce physical spread before changing the logic function.",
                        "Matched register-spread evidence and a routing-dominated path indicate a placement/distribution problem.",
                        evidence,
                        "deterministic derived",
                        confidence,
                        "Chip Planner and Report Register Spread",
                        "routed",
                    )
                )
            if diagnosis == "LOGIC_LIMITED" and "DEEP_LOGIC" in contributors:
                actions.append(
                    advice_action(
                        "Restructure the matched cone with predecode, a balanced tree, or an elastic pipeline boundary while preserving throughput.",
                        "The validated delay split is logic-dominated and the sampled cone has deep logic.",
                        evidence,
                        "deterministic derived",
                        confidence,
                        "Report Logic Depth and Report Timing",
                        "routed",
                    )
                )
            if "RETIMING_RESTRICTED" in contributors and not any(
                action["source"] == "Quartus ground truth" for action in actions
            ):
                actions.append(
                    advice_action(
                        "Review the matched retiming-restriction rows and remove only the restriction that is valid to change.",
                        "A node-level retiming restriction overlaps the sampled issue, but qclose has no official node-specific fix text.",
                        evidence,
                        "heuristic",
                        min(confidence, 0.70),
                        "Report Retiming Restrictions",
                        "retimed",
                    )
                )
            if "MEMORY_ENDPOINT" in issue.get("context", []) and (
                issue.get("evidence", {}).get("pipelining")
                or issue.get("evidence", {}).get("retiming_restrictions")
            ):
                actions.append(
                    advice_action(
                        "Inspect the matched RAM output/control boundary and preserve same-address forwarding while adding only the reported pipeline/retiming boundary.",
                        "A memory endpoint alone is context; this action is emitted because node-level pipelining or retiming evidence also overlaps the issue.",
                        evidence,
                        "deterministic derived",
                        min(confidence, 0.85),
                        "Report Pipelining Information / Report Retiming Restrictions",
                        "retimed",
                    )
                )
        records.append(
            {
                "priority": priority,
                "issue_anchor": anchor,
                "root_cause_candidate": issue.get("root_cause_candidate", False),
                "diagnosis": diagnosis,
                "contributors": sorted(contributors),
                "context": issue.get("context", []),
                "confidence": issue.get("confidence", {}),
                "secondary_anchors": issue.get("secondary_anchors", []),
                "next_actions": actions,
            }
        )

    primary_clock_data = primary_clock(summary) or {}
    wns = numeric(primary_clock_data.get("collected_wns_ns"))
    high_conf_structural = any(
        (numeric(issue.get("confidence", {}).get("overall")) or 0) >= 0.80
        and bool(issue.get("contributors"))
        for issue in summary.get("issues", [])
    )
    dse_eligible = (
        not blocked
        and not high_conf_structural
        and wns is not None
        and -0.20 <= wns < 0
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "health": health,
        "rtl_advice_suppressed": blocked,
        "issues": records,
        "dse_ii": {
            "eligible": dse_eligible,
            "reason": (
                "Timing is close and no high-confidence structural issue is present; use DSE II only after RTL and constraints are stable."
                if dse_eligible
                else "Not recommended now: preflight is blocked, timing is not close, or a higher-confidence structural issue remains."
            ),
            "source": "heuristic",
        },
    }


def render_advice(advice: dict[str, Any]) -> str:
    health = advice.get("health", {})
    lines = [
        "# qclose timing-closure advice",
        "",
        "## Engineering summary",
        "",
        f"- Preflight: **{health.get('status', 'unknown')}**",
        f"- RTL advice suppressed: **{advice.get('rtl_advice_suppressed', False)}**",
        f"- Correlated issues: **{sum(item.get('diagnosis') != 'TIMING_PREFLIGHT_BLOCKED' for item in advice.get('issues', []))}**",
    ]
    for reason in health.get("reasons", [])[:8]:
        lines.append(f"- Health evidence: {reason}")
    lines.extend(["", "## Prioritized actions", ""])
    for record in advice.get("issues", []):
        anchor = record.get("issue_anchor", {})
        lines.append(
            f"### P{record.get('priority')} — `{anchor.get('node', 'unknown')}` — {record.get('diagnosis', 'UNKNOWN')}"
        )
        lines.append("")
        lines.append(
            f"Anchor method: `{anchor.get('selection_method', 'unknown')}`; "
            f"root-cause candidate: `{record.get('root_cause_candidate', False)}`."
        )
        if record.get("context"):
            lines.append(f"Context: {', '.join(record['context'])}.")
        actions = record.get("next_actions", [])
        if not actions:
            lines.append("No RTL action emitted: available evidence does not satisfy a supported rule or preflight is blocked.")
        for index, action in enumerate(actions, 1):
            lines.extend(
                [
                    "",
                    f"{index}. {action['action']}",
                    f"   - Why: {action['why']}",
                    f"   - Source class: `{action['source']}`; confidence: `{format_number(action['confidence'], 2)}`",
                    f"   - Quartus view: {action['quartus_tool']}",
                    f"   - Minimum validation stage: `{action['validation_stage']}`",
                ]
            )
    lines.extend(
        [
            "",
            "## DSE II gate",
            "",
            f"- Eligible: **{advice.get('dse_ii', {}).get('eligible', False)}**",
            f"- {advice.get('dse_ii', {}).get('reason', '')}",
            "",
            "> Source classes: `Quartus ground truth` is copied from an existing Quartus report; "
            "`deterministic derived` is a fixed rule over validated metrics; `heuristic` requires engineering review.",
            "",
        ]
    )
    return "\n".join(lines)


def advise(run_dir: Path) -> dict[str, Any]:
    summary_path = run_dir / "summary.json"
    summary = read_json(summary_path)
    if not isinstance(summary, dict):
        summary = summarize(run_dir)
    result = build_advice(summary)
    write_json(run_dir / "advice.json", result)
    (run_dir / "advice.md").write_text(render_advice(result), encoding="utf-8")
    return result


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


def report_numeric_metrics(summary: dict[str, Any], categories: Iterable[str]) -> dict[tuple[str, str, str], float]:
    result: dict[tuple[str, str, str], float] = {}
    reports = summary.get("reports", {})
    for category in categories:
        for panel in reports.get(category, []):
            columns = [str(item) for item in panel.get("columns", [])]
            for row in panel.get("rows", []):
                if not row:
                    continue
                row_name = str(row[0])
                for index, cell in enumerate(row[1:], 1):
                    value = numeric(cell)
                    if value is None:
                        continue
                    column = columns[index] if index < len(columns) else f"column_{index}"
                    result[(category, row_name, column)] = value
    return result


def structured_numeric_metrics(summary: dict[str, Any]) -> dict[tuple[str, str, str], float]:
    result: dict[tuple[str, str, str], float] = {}
    datasets = summary.get("diagnostics", {}).get("structured", {})
    excluded = ("name", "location", "centroid", "grid", "direction", "reason", "recommendation", "from", "to", "condition")
    for dataset in ("register_spread", "route_nets", "pipelining", "logic_depth"):
        for record in datasets.get(dataset, {}).get("records", []):
            identity = record.get("identity", {})
            record_key = identity.get("normalized") or record.get("node", "")
            if not record_key:
                continue
            for field, cell in record.get("fields", {}).items():
                if any(token in field.lower() for token in excluded):
                    continue
                value = numeric(cell)
                if value is not None:
                    result[(dataset, str(record_key), str(field))] = value
    return result


def bottleneck_records(summary: dict[str, Any]) -> dict[str, dict[str, Any]]:
    table = summary.get("timing", {}).get("bottlenecks", {})
    columns = [str(column) for column in table.get("columns", [])]
    result = {}
    for row in table.get("rows", []):
        fields = row_as_fields(columns, row)
        node = first_field(fields, ("Node",))
        if node:
            result[normalize_node_name(base_node_name(node))] = fields
    return result


def evidence_count(issue: dict[str, Any]) -> int:
    return sum(len(records) for records in issue.get("evidence", {}).values())


def issue_anchor(issue: dict[str, Any]) -> dict[str, Any]:
    """Read schema-v4 anchors while keeping old summaries comparable."""
    return issue.get("issue_anchor") or issue.get("root_cause") or {}


def issue_comparison_key(issue: dict[str, Any]) -> str:
    if issue.get("issue_id"):
        return str(issue["issue_id"])
    root = issue_anchor(issue)
    identity = root.get("identity", {})
    return str(identity.get("base") or identity.get("normalized") or issue.get("hierarchy", ""))


def issue_path_overlap(left: dict[str, Any], right: dict[str, Any]) -> float:
    left_confidence = numeric(left.get("confidence", {}).get("overall")) or 0.0
    right_confidence = numeric(right.get("confidence", {}).get("overall")) or 0.0
    if min(left_confidence, right_confidence) < 0.55:
        return 0.0
    left_paths = set(left.get("path_keys", []))
    right_paths = set(right.get("path_keys", []))
    if not left_paths or not right_paths:
        return 0.0
    return len(left_paths & right_paths) / min(len(left_paths), len(right_paths))


def compare_summaries(old_path: Path, new_path: Path) -> str:
    old_file, old = resolve_summary(old_path)
    new_file, new = resolve_summary(new_path)
    old_clock = primary_clock(old)
    new_clock = primary_clock(new)
    lines = ["# Quartus timing comparison", "", f"- Old: `{old_file}`", f"- New: `{new_file}`", ""]
    old_config = old.get("timing_metadata", {})
    new_config = new.get("timing_metadata", {})
    sampling_keys = ("snapshot", "paths_per_clock", "detailed_paths")
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

    old_aggregate = old.get("timing", {}).get("detailed_path_aggregate", {})
    new_aggregate = new.get("timing", {}).get("detailed_path_aggregate", {})
    if old_aggregate and new_aggregate:
        aggregate_rows = []
        for label, key, scale in (
            ("Average route %", "average_route_ratio", 100.0),
            ("Average route ns", "average_route_delay_ns", 1.0),
            ("Average local IC ns", "average_local_ic_delay_ns", 1.0),
            ("Average fabric IC ns", "average_fabric_ic_delay_ns", 1.0),
            ("Average logic cell ns", "average_logic_cell_delay_ns", 1.0),
            ("Average uTco ns", "average_launch_delay_ns", 1.0),
            ("Max logic levels", "max_logic_levels", 1.0),
            ("Max fanout", "max_fanout", 1.0),
            ("Point-sum failures", "consistency_failures", 1.0),
            ("Quartus-breakdown failures", "quartus_breakdown_failures", 1.0),
            ("Breakdown unavailable", "quartus_breakdown_unavailable", 1.0),
        ):
            old_value = numeric(old_aggregate.get(key))
            new_value = numeric(new_aggregate.get(key))
            old_scaled = old_value * scale if old_value is not None else None
            new_scaled = new_value * scale if new_value is not None else None
            delta = new_scaled - old_scaled if old_scaled is not None and new_scaled is not None else None
            aggregate_rows.append([label, format_number(old_scaled), format_number(new_scaled), format_number(delta)])
        lines.extend(
            [
                "## Detailed path character changes",
                "",
                markdown_table(["Metric", "Old", "New", "Delta"], aggregate_rows),
                "",
                "> These aggregates describe the bounded detailed-path samples. A delta can reflect a path-rank change as well as a physical change.",
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

    if "issues" in old and "issues" in new:
        old_issues = {issue_comparison_key(item): item for item in old.get("issues", [])}
        new_issues = {issue_comparison_key(item): item for item in new.get("issues", [])}
        matched_pairs: list[tuple[dict[str, Any] | None, dict[str, Any] | None, str]] = []
        direct_keys = set(old_issues) & set(new_issues)
        for key in direct_keys:
            old_issue = old_issues[key]
            new_issue = new_issues[key]
            old_wns = numeric(old_issue.get("wns_ns")) if old_issue else None
            new_wns = numeric(new_issue.get("wns_ns")) if new_issue else None
            delta = new_wns - old_wns if old_wns is not None and new_wns is not None else None
            if delta is not None and delta > ISSUE_WNS_CHANGE_NS:
                status = "improved"
            elif delta is not None and delta < -ISSUE_WNS_CHANGE_NS:
                status = "regressed"
            else:
                status = "persisting"
            matched_pairs.append((old_issue, new_issue, status))

        unmatched_old = {key: value for key, value in old_issues.items() if key not in direct_keys}
        unmatched_new = {key: value for key, value in new_issues.items() if key not in direct_keys}
        old_relations = {
            key: [new_key for new_key, new_issue in unmatched_new.items() if issue_path_overlap(issue, new_issue) >= 0.50]
            for key, issue in unmatched_old.items()
        }
        new_relations = {
            key: [old_key for old_key, old_issue in unmatched_old.items() if issue_path_overlap(issue, old_issue) >= 0.50]
            for key, issue in unmatched_new.items()
        }
        consumed_old: set[str] = set()
        consumed_new: set[str] = set()
        for old_key, new_keys in old_relations.items():
            if len(new_keys) >= 2:
                for new_key in new_keys:
                    matched_pairs.append((unmatched_old[old_key], unmatched_new[new_key], "split"))
                    consumed_new.add(new_key)
                consumed_old.add(old_key)
        for new_key, old_keys in new_relations.items():
            if new_key in consumed_new or len(old_keys) < 2:
                continue
            for old_key in old_keys:
                matched_pairs.append((unmatched_old[old_key], unmatched_new[new_key], "merged"))
                consumed_old.add(old_key)
            consumed_new.add(new_key)
        for old_key, new_keys in old_relations.items():
            available = [key for key in new_keys if key not in consumed_new]
            if old_key not in consumed_old and len(available) == 1:
                new_key = available[0]
                matched_pairs.append((unmatched_old[old_key], unmatched_new[new_key], "uncertain"))
                consumed_old.add(old_key)
                consumed_new.add(new_key)
        for key, issue in unmatched_old.items():
            if key not in consumed_old:
                matched_pairs.append((issue, None, "left-sample"))
        for key, issue in unmatched_new.items():
            if key not in consumed_new:
                matched_pairs.append((None, issue, "entered-sample"))

        issue_rows = []
        for old_issue, new_issue, status in matched_pairs:
            old_wns = numeric(old_issue.get("wns_ns")) if old_issue else None
            new_wns = numeric(new_issue.get("wns_ns")) if new_issue else None
            delta = new_wns - old_wns if old_wns is not None and new_wns is not None else None
            root = issue_anchor(new_issue or old_issue or {})
            issue_rows.append(
                [
                    root.get("node") or (new_issue or old_issue or {}).get("hierarchy"),
                    status,
                    format_number(old_wns),
                    format_number(new_wns),
                    format_number(delta),
                    old_issue.get("paths", 0) if old_issue else 0,
                    new_issue.get("paths", 0) if new_issue else 0,
                    format_number(old_issue.get("average_route_ratio") * 100 if old_issue and old_issue.get("average_route_ratio") is not None else None, 1),
                    format_number(new_issue.get("average_route_ratio") * 100 if new_issue and new_issue.get("average_route_ratio") is not None else None, 1),
                    old_issue.get("max_logic_levels", "—") if old_issue else "—",
                    new_issue.get("max_logic_levels", "—") if new_issue else "—",
                    old_issue.get("max_fanout", "—") if old_issue else "—",
                    new_issue.get("max_fanout", "—") if new_issue else "—",
                    old_issue.get("primary_diagnosis", "—") if old_issue else "—",
                    new_issue.get("primary_diagnosis", "—") if new_issue else "—",
                    ", ".join(old_issue.get("contributors", [])) if old_issue else "—",
                    ", ".join(new_issue.get("contributors", [])) if new_issue else "—",
                    format_number(old_issue.get("confidence", {}).get("overall"), 2) if old_issue else "—",
                    format_number(new_issue.get("confidence", {}).get("overall"), 2) if new_issue else "—",
                    format_number(old_issue.get("confidence", {}).get("evidence_coverage"), 2) if old_issue else "—",
                    format_number(new_issue.get("confidence", {}).get("evidence_coverage"), 2) if new_issue else "—",
                ]
            )
        issue_rows.sort(key=lambda row: (row[1] not in {"regressed", "entered-sample", "split", "merged"}, row[3]))
        lines.extend(
            [
                "## Correlated issue changes",
                "",
                markdown_table(
                    [
                        "Issue anchor",
                        "Status",
                        "Old WNS",
                        "New WNS",
                        "Delta",
                        "Old paths",
                        "New paths",
                        "Old route %",
                        "New route %",
                        "Old levels",
                        "New levels",
                        "Old fanout",
                        "New fanout",
                        "Old primary",
                        "New primary",
                        "Old contributors",
                        "New contributors",
                        "Old conf.",
                        "New conf.",
                        "Old evidence",
                        "New evidence",
                    ],
                    issue_rows,
                    50,
                ),
                "",
            ]
        )

        lines.extend(
            [
                "> `entered-sample` and `left-sample` describe the bounded detailed-path sample; "
                "they do not prove that a timing problem appeared or disappeared globally.",
                "",
            ]
        )
    elif int(old.get("schema_version", 1)) != int(new.get("schema_version", 1)):
        lines.extend(
            [
                "> Correlated issue comparison unavailable because the runs use different summary schemas. "
                "Run `summarize` on both collections first.",
                "",
            ]
        )

    categories = ("resources", "routing_usage", "high_fanout", "highest_wire_count", "fast_forward", "fmax")
    old_report_metrics = report_numeric_metrics(old, categories)
    new_report_metrics = report_numeric_metrics(new, categories)
    report_rows = []
    for key in set(old_report_metrics) & set(new_report_metrics):
        old_value = old_report_metrics[key]
        new_value = new_report_metrics[key]
        delta = new_value - old_value
        if abs(delta) < 1e-12:
            continue
        category, row_name, column = key
        report_rows.append([category, row_name, column, format_number(old_value), format_number(new_value), format_number(delta)])
    report_rows.sort(key=lambda row: abs(float(row[-1])) if row[-1] != "—" else 0, reverse=True)
    if report_rows:
        lines.extend(
            [
                "## Compilation report metric changes",
                "",
                markdown_table(["Category", "Metric", "Column", "Old", "New", "Delta"], report_rows, 60),
                "",
            ]
        )

    old_physical = structured_numeric_metrics(old)
    new_physical = structured_numeric_metrics(new)
    physical_rows = []
    for key in set(old_physical) & set(new_physical):
        old_value = old_physical[key]
        new_value = new_physical[key]
        delta = new_value - old_value
        if abs(delta) < 1e-12:
            continue
        dataset, node, field = key
        physical_rows.append([dataset, node, field, format_number(old_value), format_number(new_value), format_number(delta)])
    physical_rows.sort(key=lambda row: abs(float(row[-1])) if row[-1] != "—" else 0, reverse=True)
    if physical_rows:
        lines.extend(
            [
                "## Physical diagnostic metric changes",
                "",
                markdown_table(["Dataset", "Node", "Metric", "Old", "New", "Delta"], physical_rows, 60),
                "",
            ]
        )

    old_bottlenecks = bottleneck_records(old)
    new_bottlenecks = bottleneck_records(new)
    if old_bottlenecks or new_bottlenecks:
        bottleneck_rows = []
        for node in sorted(set(old_bottlenecks) | set(new_bottlenecks)):
            old_record = old_bottlenecks.get(node)
            new_record = new_bottlenecks.get(node)
            status = "persisting" if old_record and new_record else "entered-sample" if new_record else "left-sample"
            old_slack = numeric(old_record.get("Slack")) if old_record else None
            new_slack = numeric(new_record.get("Slack")) if new_record else None
            delta = new_slack - old_slack if old_slack is not None and new_slack is not None else None
            bottleneck_rows.append(
                [
                    node,
                    status,
                    format_number(old_record.get("Rating")) if old_record else "—",
                    format_number(new_record.get("Rating")) if new_record else "—",
                    format_number(old_slack),
                    format_number(new_slack),
                    format_number(delta),
                ]
            )
        lines.extend(
            [
                "## Bottleneck sample changes",
                "",
                markdown_table(["Node", "Status", "Old rating", "New rating", "Old slack", "New slack", "Delta"], bottleneck_rows, 60),
                "",
            ]
        )
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
        "schema_version": SCHEMA_VERSION,
        "collected_at": dt.datetime.now(dt.timezone.utc).astimezone().isoformat(),
        "project_root": str(project_root),
        "source_fingerprint": fingerprint,
        "compilation_state": state,
        "command": " ".join(sys.argv),
        "requested_snapshot": args.snapshot,
        "capabilities": snapshot_capabilities(args.snapshot),
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
            args.snapshot,
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
    if args.snapshot == "final":
        run_logged(
            [str(quartus_sh), "-t", str(report_tcl), args.project, args.revision, str(run_dir)],
            project_root,
            run_dir / "quartus_report_collect.log",
        )
    else:
        write_json(run_dir / "selected_panels.json", [])
        write_json(
            run_dir / "report_metadata.json",
            {
                "status": "not-collected-for-intermediate-stage",
                "snapshot": args.snapshot,
                "reason": "Compilation Report DB panels are final-build context and are not mixed into an intermediate snapshot run.",
            },
        )

    summarize(run_dir)
    advise(run_dir)
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
    collect_parser.add_argument(
        "--snapshot",
        choices=QUARTUS_SNAPSHOTS,
        default="final",
        help="analyze an existing Quartus database snapshot; does not run Fitter or compilation",
    )

    summarize_parser = subparsers.add_parser("summarize", help="regenerate summary files for one collection")
    summarize_parser.add_argument("run_dir", type=Path)

    advise_parser = subparsers.add_parser("advise", help="generate deterministic advice.json and advice.md")
    advise_parser.add_argument("run_dir", type=Path)

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
        elif args.command == "advise":
            advise(args.run_dir.resolve())
            print(args.run_dir.resolve() / "advice.md")
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
