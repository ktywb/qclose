import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "quartus_timing_analyze.py"
REAL_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "quartus_25_1_agilex7_real"
SPEC = importlib.util.spec_from_file_location("quartus_timing_analyze", MODULE_PATH)
assert SPEC and SPEC.loader
QTA = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(QTA)


class QuartusTimingAnalyzeTest(unittest.TestCase):
    def real_fixture_analysis(self):
        expected = json.loads((REAL_FIXTURE / "expected.json").read_text(encoding="utf-8"))
        paths = QTA.read_jsonl(REAL_FIXTURE / "detailed_paths.jsonl")
        paths.sort(key=lambda item: QTA.numeric(item.get("slack_ns")) or float("inf"))
        neighbors = QTA.neighbor_path_records(REAL_FIXTURE / "neighbor_paths.rpt")
        matches = QTA.match_neighbor_paths(paths, neighbors)
        metrics = []
        for path, neighbor in zip(paths, matches):
            result = QTA.apply_neighbor_validation(QTA.point_metrics(path), neighbor, path)
            metrics.append(
                {
                    "slack_ns": path.get("slack_ns"),
                    "data_delay_ns": path.get("data_delay_ns"),
                    "from": path.get("from"),
                    "to": path.get("to"),
                    "from_clock": path.get("from_clock"),
                    "to_clock": path.get("to_clock"),
                    "corner": path.get("corner"),
                    "logic_levels": path.get("logic_levels"),
                    **result,
                }
            )
        structured = json.loads((REAL_FIXTURE / "structured_evidence.json").read_text(encoding="utf-8"))
        columns, rows = QTA.bottleneck_table(REAL_FIXTURE / "bottlenecks.rpt", {"vpart"})
        structured["bottlenecks"] = QTA.normalized_records(
            "rpt:bottlenecks.rpt", columns, rows, ("Node",), 100
        )
        return expected, paths, metrics, structured

    def test_point_metrics_reproduce_quartus_data_delay_and_slack(self) -> None:
        path = {
            "from": "top|unit|start",
            "to": "top|unit|end",
            "data_delay_ns": 2.0,
            "arrival_time_ns": 5.0,
            "required_time_ns": 4.0,
            "slack_ns": -1.0,
            "points": [
                {"node": "clock", "type": "cell", "incremental_delay_ns": 9.0, "total_delay_ns": 9.0},
                {"node": "top|unit|start", "type": "cell", "incremental_delay_ns": 1.0, "total_delay_ns": 1.0},
                {"node": "top|unit|net0", "type": "ic", "incremental_delay_ns": 0.4, "total_delay_ns": 1.4},
                {"node": "top|unit|net1", "type": "re", "incremental_delay_ns": 0.6, "total_delay_ns": 2.0},
            ],
        }

        metrics = QTA.point_metrics(path)

        self.assertAlmostEqual(metrics["cell_delay_ns"], 1.0)
        self.assertIsNone(metrics["local_ic_delay_ns"])
        self.assertIsNone(metrics["fabric_ic_delay_ns"])
        self.assertAlmostEqual(metrics["route_delay_ns"], 1.0)
        self.assertEqual(metrics["consistency"]["status"], "pass")
        self.assertEqual(metrics["classification_confidence"], "medium")

        validated = QTA.apply_neighbor_validation(
            metrics,
            {
                "source": "rpt:neighbor_paths.rpt",
                "utco_ns": 0.0,
                "cell_delay_ns": 1.0,
                "local_ic_delay_ns": 0.4,
                "fabric_ic_delay_ns": 0.6,
            },
        )
        self.assertEqual(validated["quartus_breakdown_validation"]["status"], "pass")
        self.assertEqual(validated["classification_confidence"], "high")

    def test_node_identity_removes_fit_replica_suffixes(self) -> None:
        identity = QTA.node_identity("top|unit|state[3]~RTM_19_Duplicate_2")

        self.assertEqual(identity["base"], "top|unit|state[3]")
        self.assertEqual(identity["normalized"], "top|unit|state[*]")
        self.assertEqual(identity["hierarchy"], "top|unit")

    def test_real_quartus_fixture_delay_ground_truth(self) -> None:
        expected, paths, metrics, _ = self.real_fixture_analysis()
        metadata = json.loads((REAL_FIXTURE / "timing_metadata.json").read_text(encoding="utf-8"))

        self.assertTrue(metadata["quartus_version"].startswith(expected["quartus_version_prefix"]))
        self.assertEqual(len(paths), expected["path_count"])
        for group in expected["path_groups"]:
            for index in group["indices"]:
                path = paths[index]
                result = metrics[index]
                validation = result["quartus_breakdown_validation"]
                self.assertIn(group["from_contains"], path["from"])
                self.assertIn(group["to_contains"], path["to"])
                self.assertEqual(path["corner"], group["corner"])
                self.assertTrue(path["from_clock"].endswith(group["from_clock_suffix"]))
                self.assertTrue(path["to_clock"].endswith(group["to_clock_suffix"]))
                for field in ("slack_ns", "data_delay_ns", "logic_levels"):
                    if field in group:
                        self.assertAlmostEqual(path[field], group[field])
                self.assertAlmostEqual(result["launch_delay_ns"], group["utco_ns"])
                self.assertAlmostEqual(result["logic_cell_delay_ns"], group["cell_delay_ns"])
                self.assertAlmostEqual(result["local_ic_delay_ns"], group["local_ic_delay_ns"])
                self.assertAlmostEqual(result["fabric_ic_delay_ns"], group["fabric_ic_delay_ns"])
                self.assertAlmostEqual(result["route_ratio"], group["route_ratio"])
                self.assertEqual(result["classification"], group["classification"])
                self.assertEqual(validation["status"], "pass")
                self.assertTrue(all(validation["identity_checks"].values()))
                for error in (
                    "route_sum_error_ns",
                    "logic_cell_error_ns",
                    "launch_utco_error_ns",
                    "data_delay_error_ns",
                    "slack_error_ns",
                ):
                    self.assertLessEqual(validation[error], QTA.DELAY_TOLERANCE_NS)
                self.assertEqual(validation["logic_levels_error"], 0)

    def test_real_quartus_fixture_root_cause_clustering(self) -> None:
        expected, paths, metrics, structured = self.real_fixture_analysis()
        issues = QTA.build_issues(paths, metrics, structured)

        self.assertEqual(len(issues), len(expected["issues"]))
        issues_by_root = {issue["root_cause"]["node"]: issue for issue in issues}
        for expected_issue in expected["issues"]:
            issue = issues_by_root[expected_issue["root_node"]]
            self.assertEqual(issue["root_cause"]["selection_method"], expected_issue["selection_method"])
            self.assertEqual(issue["paths"], expected_issue["sampled_paths"])
            self.assertEqual(issue["primary_diagnosis"], expected_issue["primary_diagnosis"])
            self.assertTrue(set(expected_issue["required_contributors"]) <= set(issue["contributors"]))
            self.assertEqual(issue["confidence"]["timing_consistency"], 1.0)
            self.assertEqual(issue["confidence"]["quartus_breakdown_validation"], 1.0)
            if expected_issue["selection_method"] == "common-exact-node":
                self.assertFalse(
                    {"PHYSICAL_SPREAD", "ROUTING_PRESSURE", "RETIMING_RESTRICTED"}
                    & set(issue["contributors"])
                )

        unavailable_metrics = json.loads(json.dumps(metrics))
        for metric in unavailable_metrics:
            metric["quartus_breakdown_validation"]["status"] = "unavailable"
        unavailable_issues = QTA.build_issues(paths, unavailable_metrics, structured)
        self.assertTrue(all(issue["confidence"]["level"] == "medium" for issue in unavailable_issues))
        self.assertTrue(all(issue["confidence"]["overall"] <= 0.79 for issue in unavailable_issues))

        failed_metrics = json.loads(json.dumps(metrics))
        for metric in failed_metrics:
            metric["quartus_breakdown_validation"]["status"] = "fail"
        failed_issues = QTA.build_issues(paths, failed_metrics, structured)
        self.assertTrue(all(issue["confidence"]["level"] == "low" for issue in failed_issues))
        self.assertTrue(all(issue["confidence"]["overall"] <= 0.35 for issue in failed_issues))

    def test_real_quartus_fixture_conservative_node_normalization(self) -> None:
        expected = json.loads((REAL_FIXTURE / "expected.json").read_text(encoding="utf-8"))
        normalization = expected["normalization"]
        real_samples = json.loads((REAL_FIXTURE / "normalization_samples.json").read_text(encoding="utf-8"))

        self.assertIn(normalization["duplicate_input"], real_samples)
        self.assertEqual(QTA.base_node_name(normalization["duplicate_input"]), normalization["duplicate_base"])
        generated = normalization["generated_nodes_that_must_stay_distinct"]
        self.assertNotEqual(QTA.normalize_node_name(generated[0]), QTA.normalize_node_name(generated[1]))
        bus_bits = normalization["bus_bits_that_may_normalize_together"]
        self.assertEqual(QTA.normalize_node_name(bus_bits[0]), QTA.normalize_node_name(bus_bits[1]))

        contextual_record = {
            "identity": QTA.node_identity("top|unit|unrelated"),
            "source": "fixture",
            "fields": {},
        }
        confidence, method = QTA.node_match_confidence(
            contextual_record, [QTA.node_identity("top|unit|payload[3]")], "top|unit"
        )
        self.assertEqual(method, "same-hierarchy")
        self.assertLess(confidence, 0.60)

    def test_unrecognized_report_is_warning_not_empty_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            (run / "neighbor_paths.rpt").write_text("unrecognized future Quartus format\n", encoding="utf-8")
            summary = QTA.summarize(run)

        self.assertEqual(summary["diagnostics"]["parser_status"]["neighbor_paths"]["status"], "warning")
        self.assertEqual(summary["diagnostics"]["parser_status"]["logic_depth"]["status"], "unavailable")
        self.assertEqual(summary["issues"], [])

    def test_neighbor_report_only_matches_the_same_sampled_path(self) -> None:
        report = """Path #1
; Property                   ; Launch                       ; Path                         ; Latch                        ;
; From Node                  ; start                        ; top|unit|from                ; end                          ;
; To Node                    ; start                        ; top|unit|to                  ; end                          ;
; Launch Clock               ; clk                          ; clk                          ; clk                          ;
; Latch Clock                ; clk                          ; clk                          ; clk                          ;
; Setup Operating Conditions ; Slow vid1b 100C Model        ; Slow vid1 100C Model         ; Slow vid1 100C Model         ;
; Setup Slack                ; -0.900                       ; -1.083                       ; -0.700                       ;
; [c] uTco                   ; 0.000                        ; 1.176                        ; 0.000                        ;
; [a] Cell Delay             ; 0.000                        ; 1.106                        ; 0.000                        ;
; [b] Local IC Delay         ; 0.000                        ; 0.513                        ; 0.000                        ;
; [D] Fabric IC Delay        ; 0.000                        ; 0.710                        ; 0.000                        ;
"""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "neighbor_paths.rpt"
            path.write_text(report, encoding="utf-8")
            records = QTA.neighbor_path_records(path)

        self.assertEqual(records[0]["corner"], "Slow vid1 100C Model")
        same = QTA.match_neighbor_path(
            {
                "from": "top|unit|from",
                "to": "top|unit|to",
                "corner": "Slow vid1 100C Model",
                "slack_ns": -1.083,
            },
            records,
        )
        different = QTA.match_neighbor_path(
            {
                "from": "top|unit|from",
                "to": "top|unit|to",
                "corner": "Slow vid1 100C Model",
                "slack_ns": -1.080,
            },
            records,
        )
        self.assertIsNotNone(same)
        self.assertIsNone(different)
        self.assertIsNone(
            QTA.match_neighbor_path(
                {
                    "from": "top|unit|from",
                    "to": "top|unit|to",
                    "corner": "Slow vid1b 100C Model",
                    "slack_ns": -1.083,
                },
                records,
            )
        )

        duplicate_paths = [
            {
                "from": "top|unit|from",
                "to": "top|unit|to",
                "corner": "Slow vid1 100C Model",
                "slack_ns": -1.083,
            },
            {
                "from": "top|unit|from",
                "to": "top|unit|to",
                "corner": "Slow vid1 100C Model",
                "slack_ns": -1.083,
            },
        ]
        matches = QTA.match_neighbor_paths(duplicate_paths, records)
        self.assertIsNotNone(matches[0])
        self.assertIsNone(matches[1])

    def test_comparison_includes_correlated_and_report_deltas(self) -> None:
        def summary(wns: float, route_ratio: float, resource: int, issue: bool) -> dict:
            issues = []
            if issue:
                issues.append(
                    {
                        "hierarchy": "top|unit",
                        "wns_ns": wns,
                        "diagnoses": ["LOGIC_LIMITED"],
                        "evidence": {"high_fanout": [{"node": "top|unit|enable"}]},
                    }
                )
            return {
                "schema_version": 2,
                "timing_metadata": {"paths_per_clock": 50, "detailed_paths": 20},
                "clocks": [{"name": "clk", "collected_wns_ns": wns, "collected_setup_paths": 50}],
                "timing": {
                    "hierarchy_groups": [{"name": "top|unit", "wns_ns": wns, "paths": 2}],
                    "detailed_path_aggregate": {
                        "average_route_ratio": route_ratio,
                        "average_route_delay_ns": 1.0,
                        "average_local_ic_delay_ns": 0.2,
                        "average_fabric_ic_delay_ns": 0.8,
                        "average_cell_delay_ns": 1.0,
                        "max_logic_levels": 5,
                        "max_fanout": 10,
                        "consistency_failures": 0,
                    },
                    "bottlenecks": {"columns": [], "rows": []},
                },
                "issues": issues,
                "reports": {
                    "resources": [{"columns": ["Resource", "Usage"], "rows": [["ALMs", str(resource)]]}]
                },
                "diagnostics": {"structured": {}},
            }

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old = root / "old.json"
            new = root / "new.json"
            old.write_text(json.dumps(summary(-0.2, 0.6, 100, False)), encoding="utf-8")
            new.write_text(json.dumps(summary(-0.1, 0.4, 90, True)), encoding="utf-8")

            comparison = QTA.compare_summaries(old, new)

        self.assertIn("Detailed path character changes", comparison)
        self.assertIn("Correlated issue changes", comparison)
        self.assertIn("entered-sample", comparison)
        self.assertIn("Compilation report metric changes", comparison)

    def test_root_issue_comparison_statuses(self) -> None:
        def issue(issue_id, node, wns, paths):
            return {
                "issue_id": issue_id,
                "root_cause": {"node": node, "identity": QTA.node_identity(node)},
                "hierarchy": "top|unit",
                "wns_ns": wns,
                "paths": len(paths),
                "path_keys": paths,
                "average_route_ratio": 0.7,
                "primary_diagnosis": "ROUTING_LIMITED",
                "contributors": ["HIGH_FANOUT"],
                "confidence": {"overall": 0.9},
                "evidence": {},
            }

        def summary(issues):
            return {
                "schema_version": 3,
                "timing_metadata": {"paths_per_clock": 50, "detailed_paths": 20},
                "clocks": [{"name": "clk", "collected_wns_ns": -0.2, "collected_setup_paths": 20}],
                "timing": {"hierarchy_groups": [], "detailed_path_aggregate": {}, "bottlenecks": {}},
                "issues": issues,
                "reports": {},
                "diagnostics": {"structured": {}},
            }

        old_issues = [
            issue("stable", "top|unit|stable", -0.2, ["stable-path"]),
            issue("worse", "top|unit|worse", -0.1, ["worse-path"]),
            issue("old-split", "top|unit|old_split", -0.3, ["split-a", "split-b"]),
            issue("old-merge-a", "top|unit|old_merge_a", -0.2, ["merge-a"]),
            issue("old-merge-b", "top|unit|old_merge_b", -0.2, ["merge-b"]),
            issue("old-uncertain", "top|unit|old_uncertain", -0.2, ["uncertain-path"]),
        ]
        new_issues = [
            issue("stable", "top|unit|stable", -0.1, ["stable-path"]),
            issue("worse", "top|unit|worse", -0.2, ["worse-path"]),
            issue("new-split-a", "top|unit|new_split_a", -0.2, ["split-a"]),
            issue("new-split-b", "top|unit|new_split_b", -0.2, ["split-b"]),
            issue("new-merge", "top|unit|new_merge", -0.2, ["merge-a", "merge-b"]),
            issue("new-uncertain", "top|unit|new_uncertain", -0.2, ["uncertain-path"]),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old = root / "old.json"
            new = root / "new.json"
            old.write_text(json.dumps(summary(old_issues)), encoding="utf-8")
            new.write_text(json.dumps(summary(new_issues)), encoding="utf-8")
            comparison = QTA.compare_summaries(old, new)

        for status in ("improved", "regressed", "split", "merged", "uncertain"):
            self.assertIn(status, comparison)


if __name__ == "__main__":
    unittest.main()
