"""不联网的失败流程检查；实际新 venv 安装须显式运行 release-check。"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from personal_agent.release_check import check_release_evidence, clean_environment, run_release_check


class FailedProcess:
    real = False
    fail_at = "create-venv"

    def __init__(self, directory, environment):
        self.directory, self.environment, self.calls = directory, environment, []

    def run(self, stage, command, *, cwd, expected=0, timeout=60):
        self.calls.append(stage)
        assert not self.environment.get("DASHSCOPE_API_KEY") and "PYTHONPATH" not in self.environment
        (self.directory / "fixture-calls.json").write_text(json.dumps(self.calls), encoding="utf-8")
        if stage == self.fail_at:
            raise RuntimeError("injected failure: " + stage)
        environment = self.directory / "project/.venv"
        if stage == "create-venv":
            environment.mkdir()
            (environment / "pyvenv.cfg").write_text("include-system-site-packages = false\n", encoding="utf-8")
        info = {"prefix": str(environment), "base_prefix": sys.base_prefix, "executable": str(environment / "python-fixture"),
                "user_site_enabled": False, "libraries_present": {name: False for name in ("openai", "anthropic", "dotenv")}}
        return subprocess.CompletedProcess(command, 0, json.dumps(info), "")


def verify_workflow():
    with tempfile.TemporaryDirectory(prefix="release-failures-中文 空格-") as temporary:
        root = Path(temporary).resolve()
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "fixture-secret", "ANTHROPIC_API_KEY": "fixture-native", "PYTHONPATH": "untrusted-fixture", "PIP_INDEX_URL": "https://example.invalid/simple", "PERSONAL_AGENT_MODEL": "fixture"}):
            cleaned = clean_environment()
            assert not any(key in cleaned for key in ("DASHSCOPE_API_KEY", "ANTHROPIC_API_KEY", "PYTHONPATH", "PIP_INDEX_URL", "PERSONAL_AGENT_MODEL"))
            assert cleaned["PIP_CONFIG_FILE"] == os.devnull
            assert os.environ["DASHSCOPE_API_KEY"] == "fixture-secret"  # 未改写父环境。
            for failure in ("create-venv", "install", "pip-check"):
                cls = type("FailAtStage", (FailedProcess,), {"fail_at": failure})
                directory = root / failure
                report = run_release_check(directory, _runner_factory=cls)
                assert report["status"] == "failed" and not report["clean_install_verified"]
                assert report["active_stage"] == failure and report["new_remote_model_calls"] == 0
                calls = json.loads((directory / "fixture-calls.json").read_text(encoding="utf-8"))
                assert calls[-1] == failure and "doctor" not in calls and "offline-smoke" not in calls
                before = (directory / "report.json").read_bytes()
                try:
                    run_release_check(directory, _runner_factory=cls)
                except ValueError:
                    pass
                else:
                    raise AssertionError("existing release overwritten")
                assert (directory / "report.json").read_bytes() == before
                try:
                    check_release_evidence(directory / "report.json")
                except ValueError:
                    pass
                else:
                    raise AssertionError("failed installation promoted")
        process = subprocess.run([sys.executable, "-O", "-m", "personal_agent.delivery", "release-check", "--output-dir", str(root / "optimized")],
                                 cwd=ROOT, capture_output=True, timeout=20)
        assert process.returncode == 2 and not (root / "optimized").exists()
    return {"status": "passed", "validation": "injected child-process failures; actual package creation and retained reports; no pip download or SDK install",
            "failed_venv_install_and_dependency_checks_stop_without_model_or_promotion": True,
            "child_environment_isolated_parent_preserved_and_existing_output_rejected": True,
            "new_remote_model_calls": 0, "clean_install_verified": False}


if __name__ == "__main__":
    print(json.dumps(verify_workflow(), ensure_ascii=False))
