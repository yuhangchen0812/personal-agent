"""最小交付入口：无需密钥的环境检查、离线演示和可校验运行包。"""

import argparse
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parent.parent


def doctor(*, target="runtime"):
    if target not in {"runtime", "container"}:
        raise ValueError("环境目标仅支持 runtime/container。")
    packages = []
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+-]+)", line.strip())
        if match is None:
            raise ValueError("交付检查只接受明确锁定的运行依赖。")
        name, expected = match.groups()
        try:
            installed = version(name)
        except PackageNotFoundError:
            installed = None
        packages.append({"name": name, "expected": expected, "installed": installed, "matches": installed == expected})
    runtime = sys.version_info >= (3, 12) and all(item["matches"] for item in packages)
    clients = {name: shutil.which(name) is not None for name in ("docker", "podman")}
    container = {"clients": clients, "engine_status": "not_probed", "engine": None}
    if target == "container":
        engine = next((name for name, present in clients.items() if present), None)
        container.update(engine=engine, engine_status="client_missing")
        if engine:
            try:
                result = subprocess.run([engine, "info"], stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
                container["engine_status"] = "reachable" if result.returncode == 0 else "unreachable"
            except (OSError, subprocess.TimeoutExpired):
                container["engine_status"] = "unreachable"
    ready = runtime and (target == "runtime" or container["engine_status"] == "reachable")
    return {"version": 1, "kind": "delivery_environment", "target": target, "status": "ready" if ready else "not_ready",
        "python": platform.python_version(), "platform": platform.system(), "project_root": str(ROOT),
        "runtime_ready": runtime, "packages": packages, "container": container,
        "credential_presence": {name: bool(os.environ.get(name)) for name in ("DASHSCOPE_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL")},
        "new_remote_model_calls": 0, "container_build_or_run_verified": False,
        "boundary": "runtime dependencies and optional read-only engine probe; no model request, image build or container execution"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    inspect = sub.add_parser("doctor", help="只检查运行依赖；container 额外只读探测 Engine")
    inspect.add_argument("--target", choices=("runtime", "container"), default="runtime")
    demo = sub.add_parser("demo", help="固定模型的实际文件/Plan 演示，无需密钥")
    demo.add_argument("--output-dir", type=Path)
    check = sub.add_parser("check-demo", help="只读重验已生成的演示，不重放工具")
    check.add_argument("--report", type=Path, required=True)
    pack = sub.add_parser("pack", help="导出源码、锁定依赖清单及示例，不包含环境/运行数据")
    pack.add_argument("--output-dir", type=Path, required=True)
    package = sub.add_parser("check-package", help="只读核对 ZIP 清单、指纹及允许路径")
    package.add_argument("--manifest", type=Path, required=True)
    container = sub.add_parser("container-check", help="实际构建并验收 Linux 镜像/卷/停止信号；需要 Docker Engine")
    container.add_argument("--output-dir", type=Path, required=True)
    release = sub.add_parser("release-check", help="新运行包/venv 的全新安装与离线验收；显式从 PyPI 下载依赖")
    release.add_argument("--output-dir", type=Path, required=True)
    release_evidence = sub.add_parser("check-release", help="只读重验已有安装与产物，不安装或重放")
    release_evidence.add_argument("--report", type=Path, required=True)
    options = vars(parser.parse_args(argv))
    action = options.pop("action")
    try:
        if action == "doctor":
            value = doctor(**options)
        elif action == "container-check":
            from .container_check import run_container_acceptance
            value = run_container_acceptance(**options)
        elif action in {"release-check", "check-release"}:
            from .release_check import run_release_check, check_release_evidence
            value = run_release_check(**options) if action == "release-check" else check_release_evidence(**options)
        elif action in {"demo", "check-demo"}:
            from .delivery_demo import check_demo, run_demo
            value = run_demo(**options) if action == "demo" else check_demo(**options)
        else:
            from .delivery_bundle import check_package, create_package
            value = create_package(**options) if action == "pack" else check_package(**options)
    except KeyboardInterrupt:
        print(json.dumps({"status": "cancelled", "action": action}, ensure_ascii=False))
        raise SystemExit(130) from None
    except (ValueError, OSError, ImportError) as error:
        print(json.dumps({"status": "rejected", "action": action, "error_type": type(error).__name__, "message": str(error)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2) from None
    print(json.dumps(value, ensure_ascii=False, indent=2))
    raise SystemExit(0 if value["status"] in {"ready", "passed", "packaged"} else 130 if value["status"] == "cancelled" else 1)


if __name__ == "__main__":
    main()
