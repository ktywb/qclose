import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "path_skeleton.py"


class PathSkeletonTest(unittest.TestCase):
    def test_summary_rows_match_detailed_paths_and_report_missing_points(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            (run_dir / "summary.md").write_text("# Quartus timing analysis\n")
            first = {
                "from": "vpart|engine|scatterTranslator|activeSegmentValid",
                "to": "vpart|engine|dataWriteQ|ram_ext|ram_block0~reg0",
                "from_clock": "clk",
                "to_clock": "clk",
                "corner": "Slow",
                "slack_ns": -0.316,
                "data_delay_ns": 2.691,
                "logic_levels": 6,
            }
            second = {**first, "to": "vpart|engine|dataWriteQ|ram_ext|ram_block1~reg0"}
            (run_dir / "summary.json").write_text(
                json.dumps({"timing": {"worst_paths": [first, second]}})
            )
            detailed = {
                **first,
                "points": [
                    {"index": 0, "type": "cell", "node": "clock"},
                    {"index": 1, "type": "cell", "node": first["from"]},
                    {"index": 2, "type": "utco", "node": first["from"] + "|q"},
                    {"index": 3, "type": "re", "node": "H0"},
                    {"index": 4, "type": "cell", "node": "vpart|engine|scatterTranslator|hitLogicalEnd[0]~13|combout"},
                    {"index": 5, "type": "cell", "node": "vpart|engine|scatterTranslator|add_0~1|cout"},
                    {"index": 6, "type": "cell", "node": "vpart|engine|scatterTranslator|add_0~2|sumout"},
                    {"index": 7, "type": "cell", "node": "vpart|engine|scatterTranslator|LessThan_4~1|combout"},
                    {"index": 8, "type": "ic", "node": "vpart|engine|dataWriteQ|ram_ext|ram_block0|portbaddr[5]"},
                    {"index": 9, "type": "cell", "node": first["to"]},
                ],
            }
            (run_dir / "detailed_paths.jsonl").write_text(json.dumps(detailed) + "\n")

            result = subprocess.run(
                [sys.executable, str(SCRIPT), str(run_dir / "summary.md")],
                capture_output=True, text=True, check=True,
            )
            self.assertIn("#1 slack=-0.316", result.stdout)
            self.assertIn("scatterTranslator.hitLogicalEnd[0]", result.stdout)
            self.assertEqual(result.stdout.count("scatterTranslator.add_0"), 1)
            self.assertIn("dataWriteQ.RAM.portbaddr[5]", result.stdout)
            self.assertNotIn("[0] clock", result.stdout)
            self.assertIn("#2 slack=-0.316", result.stdout)
            self.assertIn("No detailed points for this row", result.stdout)

            rank_result = subprocess.run(
                [sys.executable, str(SCRIPT), str(run_dir), "--rank", "2"],
                capture_output=True, text=True, check=True,
            )
            self.assertNotIn("#1 slack=", rank_result.stdout)
            self.assertIn("#2 slack=", rank_result.stdout)

            details = subprocess.run(
                [sys.executable, str(SCRIPT), str(run_dir), "--rank", "1", "--details"],
                capture_output=True, text=True, check=True,
            ).stdout
            self.assertIn("[5] cell vpart|engine|scatterTranslator|add_0~1|cout", details)
            self.assertIn("[8] ic vpart|engine|dataWriteQ|ram_ext|ram_block0|portbaddr[5]", details)
            self.assertNotIn("[3] re H0", details)

            routing = subprocess.run(
                [sys.executable, str(SCRIPT), str(run_dir), "--rank", "1", "--routing"],
                capture_output=True, text=True, check=True,
            ).stdout
            self.assertIn("[3] re H0", routing)


if __name__ == "__main__":
    unittest.main()
