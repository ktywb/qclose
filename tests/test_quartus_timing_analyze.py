import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "quartus_timing_analyze.py"
SPEC = importlib.util.spec_from_file_location("quartus_timing_analyze", MODULE_PATH)
assert SPEC and SPEC.loader
QTA = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(QTA)


class QuartusTimingAnalyzeTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
