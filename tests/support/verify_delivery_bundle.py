"""实际打包、解压副本与外部目录启动；固定演示不调用远端模型。"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from personal_agent.delivery_bundle import check_package, create_package
from personal_agent.delivery_demo import check_demo, run_demo


def reject(call):
    try:
        call()
    except ValueError:
        return
    raise AssertionError("invalid delivery artifact accepted")


def verify_workflow():
    with tempfile.TemporaryDirectory(prefix="delivery-bundle-中文 空格-") as temporary:
        root = Path(temporary).resolve()
        caller, unpacked = root / "外部 调用目录", root / "解压 项目"
        caller.mkdir(); unpacked.mkdir()
        package = create_package(root / "运行包")
        checked = check_package(package["manifest"])
        assert checked["status"] == "passed"
        with zipfile.ZipFile(package["archive"]) as source:
            names = source.namelist()
            assert not any(part in {".personal", ".venv", ".git", "Reference"} for name in names for part in Path(name).parts)
            assert not any(Path(name).parts[0] in {"tests", "scripts", ".github", ".personal"} for name in names)
            assert ".env" not in names and "playground/写入演示/问候.txt" not in names
            source.extractall(unpacked)  # 先校验本次自产 ZIP 的允许路径和指纹，再解压。
        environment = dict(os.environ)
        for name in ("DASHSCOPE_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"):
            environment.pop(name, None)

        def cli(*args, expected=0):
            process = subprocess.run([sys.executable, "-X", "utf8", "-B", str(unpacked / "run_demo.py"), *args], cwd=caller,
                env=environment, capture_output=True, timeout=60)
            assert process.returncode == expected, process.stderr.decode("utf-8", errors="replace")
            return json.loads(process.stdout)

        doctor = cli("doctor")
        assert doctor["status"] == "ready" and doctor["project_root"] == str(unpacked)
        assert not any(doctor["credential_presence"].values()) and not doctor["container_build_or_run_verified"]
        no_site = subprocess.run([sys.executable, "-S", "-X", "utf8", "-B", str(unpacked / "run_demo.py"), "doctor"], cwd=caller,
            env=environment, capture_output=True, timeout=60)
        assert no_site.returncode == 1 and json.loads(no_site.stdout)["status"] == "not_ready"
        demo = cli("demo", "--output-dir", "离线 结果")
        assert demo["status"] == "passed" and all(demo["checks"].values())
        assert Path(demo["report"]).parent == caller / "离线 结果"
        paths = list((caller / "离线 结果").rglob("*"))
        before = {str(path):path.read_bytes() for path in paths if path.is_file()}
        assert cli("check-demo", "--report", demo["report"])["status"] == "passed"
        assert {str(path):path.read_bytes() for path in paths if path.is_file()} == before
        help_result = subprocess.run([sys.executable, "-X", "utf8", "-B", str(unpacked / "run_agent.py"), "--help"], cwd=caller,
            env=environment, capture_output=True, timeout=60)
        assert help_result.returncode == 0 and b"--prompt" in help_result.stdout
        # 副本中放入私有运行文件及非占位配置，再从副本重新打包，确认均未导出。
        marker = "PACK_TEST_PRIVATE_MARKER"
        for relative in (".env", ".env.example", ".personal/private.json", ".venv/private.py", "playground/private.txt"):
            path = unpacked / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(marker, encoding="utf-8")
        repacked = cli("pack", "--output-dir", "二次运行包")
        with zipfile.ZipFile(repacked["archive"]) as source:
            assert all(marker.encode() not in source.read(name) for name in source.namelist())
        assert cli("check-package", "--manifest", repacked["manifest"])["status"] == "passed"
        # 检查不重放，内容/展示变化拒绝，原字节恢复后仍可核验。
        trace = json.loads((caller / "离线 结果/trace.json").read_text(encoding="utf-8"))
        plan_path = caller / "离线 结果/workspace" / trace[2]["arguments"]["file_path"]
        for target in (caller / "离线 结果/workspace/playground/summary.json", caller / "离线 结果/index.html", plan_path):
            raw = target.read_bytes(); target.write_bytes(raw + b"\n")
            reject(lambda: check_demo(demo["report"])); target.write_bytes(raw)
        archive = Path(package["archive"])
        raw = archive.read_bytes(); archive.write_bytes(raw + b"changed")
        reject(lambda: check_package(package["manifest"])); archive.write_bytes(raw)
        manifest = Path(package["manifest"])
        original = manifest.read_bytes()
        value = json.loads(original); value["entries"]["../outside.py"] = {"sha256":"0"*64,"bytes":0}
        manifest.write_text(json.dumps(value), encoding="utf-8")
        reject(lambda: check_package(manifest)); manifest.write_bytes(original)
        reject(lambda: run_demo(caller / "离线 结果"))
        reject(lambda: create_package(root / "运行包"))
        assert check_package(manifest)["status"] == "passed" and check_demo(demo["report"])["status"] == "passed"
        if os.name == "nt":
            process = subprocess.run(f'cmd.exe /d /s /c ""{ROOT / "demo.cmd"}" doctor"', cwd=caller, capture_output=True, timeout=60)
            assert process.returncode == 0, process.stderr.decode("utf-8", errors="replace")
            assert json.loads(process.stdout)["status"] == "ready"
        return {"status":"passed", "validation":"actual ZIP, extracted source imports, fresh CLI processes, Agent Loop and file tools; scripted model",
            "package_and_repackage_exclude_private_files_and_generate_blank_credentials":True,
            "external_unicode_cwd_extracted_doctor_demo_and_main_help":True,
            "actual_plan_denial_fixed_approval_write_readback":True,
            "read_only_rechecks_no_replay_and_tamper_rejection":True,
            "windows_cmd_launcher_checked":os.name == "nt", "new_remote_model_calls":0,
            "boundary":"existing Python environment/dependencies reused; no fresh dependency installation, Linux or container run"}
