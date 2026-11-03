"""两条验收器主流程：双通道失败不伪造通过，以及超时/取消保留部分结果。"""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from codex import verify_acceptance as acceptance


FIXTURE = '''import json,sys,time
from pathlib import Path
name,path,status,code=sys.argv[1:]
Path(path).write_text(json.dumps({"stage":name,"status":status}),encoding="utf-8")
print("fixture worker",flush=True)
if code=="hang": time.sleep(20)
raise SystemExit(int(code))
'''


class AcceptanceTests(unittest.TestCase):
    def test_offline_acceptance_does_not_require_or_read_private_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with patch.object(acceptance, "DEFAULT_STAGES", ("local",)), \
                 patch.object(acceptance, "source_state", return_value={"sha256": "fixture"}), \
                 patch.object(acceptance, "environment_state", return_value={}), \
                 patch.object(acceptance, "historical_evidence", side_effect=AssertionError("private history read")), \
                 patch.object(acceptance, "run_stage", return_value={"stage": "local", "status": "passed"}), redirect_stdout(io.StringIO()):
                report = acceptance.run_acceptance(root)
            self.assertEqual(report["status"], "passed")
            self.assertEqual(report["historical_evidence"]["status"], "not_requested")
            self.assertFalse(report["history_required"])

    def test_explicit_history_still_blocks_on_missing_or_failed_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with patch.object(acceptance, "DEFAULT_STAGES", ("local",)), \
                 patch.object(acceptance, "source_state", return_value={"sha256": "fixture"}), \
                 patch.object(acceptance, "environment_state", return_value={}), \
                 patch.object(acceptance, "historical_evidence", return_value={"status": "failed"}) as history, \
                 patch.object(acceptance, "run_stage", return_value={"stage": "local", "status": "passed"}), redirect_stdout(io.StringIO()):
                report = acceptance.run_acceptance(root, check_history=True)
            history.assert_called_once()
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["m3"]["local_baseline_status"], "passed")
            self.assertTrue(report["history_required"])

    def command(self, root, name, status, code):
        script = root / "worker.py"
        script.write_text(FIXTURE, encoding="utf-8")
        return [sys.executable, "-X", "utf8", "-B", str(script), name, str(root / f"{name}.json"), status, str(code)]

    def test_failed_result_or_failed_exit_cannot_pass_and_other_stage_continues(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            original = acceptance.run_stage
            def stage(name, directory):
                return original(name, directory, command=self.command(root, name, "failed" if name == "first" else "passed", 0))
            with patch.object(acceptance, "DEFAULT_STAGES", ("first", "second")), \
                 patch.object(acceptance, "source_state", return_value={"sha256": "fixture"}), \
                 patch.object(acceptance, "environment_state", return_value={}), \
                 patch.object(acceptance, "historical_evidence", return_value={"status": "passed"}), \
                 patch.object(acceptance, "run_stage", side_effect=stage), redirect_stdout(io.StringIO()):
                report = acceptance.run_acceptance(root)
            self.assertEqual([row["status"] for row in report["stages"]], ["failed", "passed", "not_run"])
            self.assertEqual(report["status"], "failed")
            self.assertEqual(json.loads((root / "summary.json").read_text(encoding="utf-8"))["status"], "failed")
            self.assertIn("fixture worker", (root / "first.log").read_text(encoding="utf-8"))
            bad_exit = original("bad_exit", root, command=self.command(root, "bad_exit", "passed", 7))
            self.assertEqual((bad_exit["status"], bad_exit["returncode"]), ("failed", 7))
            self.assertFalse(report["m3"]["complete"])
            self.assertEqual(acceptance.report_status([], {"status": "passed"}, include_live=False), "failed")

    def test_timeout_is_failed_and_worker_cancellation_stops_later_stages(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            timeout = acceptance.run_stage("slow", root, command=self.command(root, "slow", "passed", "hang"), timeout=0.5)
            self.assertEqual((timeout["status"], timeout["error_type"]), ("failed", "StageTimeout"))
            self.assertIsNotNone(timeout["returncode"])
            original = acceptance.run_stage
            def cancel(name, directory):
                self.assertEqual(name, "first")
                return original(name, directory, command=self.command(root, name, "cancelled", 130))
            with patch.object(acceptance, "DEFAULT_STAGES", ("first", "second")), \
                 patch.object(acceptance, "source_state", return_value={"sha256": "fixture"}), \
                 patch.object(acceptance, "environment_state", return_value={}), \
                 patch.object(acceptance, "historical_evidence", return_value={"status": "passed"}), \
                 patch.object(acceptance, "run_stage", side_effect=cancel), redirect_stdout(io.StringIO()):
                report = acceptance.run_acceptance(root, include_live=True)
            self.assertEqual(report["status"], "cancelled")
            self.assertEqual([row["status"] for row in report["stages"]], ["cancelled", "not_run", "not_run"])
            self.assertTrue((root / "overview.md").exists())
            self.assertFalse((root / "second.json").exists())


if __name__ == "__main__":
    unittest.main()
