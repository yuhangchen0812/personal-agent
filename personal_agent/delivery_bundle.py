"""按允许清单生成最小运行包，ZIP 和文件清单可独立只读重验。"""

import json
from pathlib import Path, PurePosixPath
import zipfile

from .file_runtime import plain_path, write_json_atomic
from .source_snapshot import digest


ROOT = Path(__file__).resolve().parent.parent
ROOT_FILES = {"run_agent.py", "run_task.py", "run_demo.py", "run.cmd", "task.cmd", "demo.cmd", "requirements.txt", ".env.example", "Dockerfile", ".dockerignore"}
TEXT_SUFFIXES = {".py", ".md", ".json", ".jsonl", ".ini", ".toml", ".cfg", ".txt"}
ENV_TEMPLATE = "# Supply credentials locally after extraction. Never commit this file with real keys.\nDASHSCOPE_API_KEY=\nPERSONAL_AGENT_PROTOCOL=openai\nANTHROPIC_API_KEY=\nANTHROPIC_MODEL=\n"
README = """# Personal Agent 最小运行包

本包包含应用源码、示例、锁定的运行依赖清单及启动器；不包含 Python、已安装依赖、密钥或历史运行记录。
需要 Python 3.12+。在解压目录的 PowerShell 中安装一次依赖：

```powershell
py -3.12 -m venv .venv
.\\.venv\\Scripts\\python.exe -m pip install -r requirements.txt
.\\demo.cmd doctor
.\\demo.cmd
```

demo 使用固定模型，实际执行文件工具、Plan 与预先限定的演示审批，不请求远端模型。
Linux/macOS 安装依赖后使用 `python run_demo.py doctor` 和 `python run_demo.py`。
自定义输出：`python run_demo.py demo --output-dir 新目录`；完成后打开返回的 index.html。
只读重验：`python run_demo.py check-demo --report 演示目录/report.json`，不重放模型或工具。

真实 Agent 启动：`python run_agent.py --help`；Windows 为 `.\\run.cmd --help`。
真实任务需自行通过环境变量或本地 .env 提供模型配置，模板为 .env.example；首次请求前核对所选模型。
本包不包含 pytest 开发依赖、verify.cmd 或 codex 历史验收脚本。CI 示例重放如需 pytest，应另装对应开发依赖。
容器配置仅为待目标环境验证的文件；doctor container 检查不代表已经构建/运行镜像。
"""


def _allowed(name):
    path = PurePosixPath(name)
    if (not isinstance(name, str) or path.as_posix() != name or path.is_absolute() or "\\" in name or ":" in name
            or any(part in {".", ".."} or part.casefold() in {".git", ".venv", ".personal", "reference", "__pycache__"}
                or part.casefold().startswith(".env") and name != ".env.example" for part in path.parts)):
        return False
    return name in ROOT_FILES | {"README.md", "playground/README.md"} or (len(path.parts) == 2 and path.parts[0] == "personal_agent" and path.suffix == ".py") or (
        len(path.parts) > 1 and path.parts[0] == "examples" and path.suffix in TEXT_SUFFIXES)


def create_package(output_dir):
    directory = plain_path(output_dir)
    if directory.exists():
        raise ValueError("运行包输出须为新目录。")
    paths = [*(ROOT / name for name in sorted(ROOT_FILES - {".env.example"})), *sorted((ROOT / "personal_agent").glob("*.py")),
        *sorted(path for path in (ROOT / "examples").rglob("*") if path.is_file() and _allowed(path.relative_to(ROOT).as_posix())), ROOT / "playground/README.md"]
    files = {path.relative_to(ROOT).as_posix(): plain_path(path).read_bytes() for path in paths}
    files["README.md"] = README.encode("utf-8")
    files[".env.example"] = ENV_TEMPLATE.encode("utf-8")
    if len(files) > 1000 or sum(map(len, files.values())) > 64 * 1024 * 1024 or any(not _allowed(name) for name in files):
        raise ValueError("运行包超过允许清单或大小上限。")
    directory.mkdir(parents=True, exist_ok=False)
    archive = directory / "personal-agent.zip"
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as target:
        for name, raw in sorted(files.items()):
            target.writestr(name, raw)
    for path in paths:
        if plain_path(path).read_bytes() != files[path.relative_to(ROOT).as_posix()]:
            raise ValueError("打包期间源码输入变化，请保留本次产物并使用新目录重做。")
    manifest = {"version": 1, "kind": "personal_agent_runtime_bundle", "archive": archive.name, "archive_sha256": digest(archive.read_bytes()),
        "entries": {name: {"sha256": digest(raw), "bytes": len(raw)} for name, raw in sorted(files.items())},
        "python_minimum": "3.12", "dependencies_bundled": False, "generated_files": ["README.md", ".env.example"], "new_remote_model_calls": 0,
        "boundary": "allowlisted source and dependency requirements; not a container image or a bundled interpreter"}
    write_json_atomic(directory / "manifest.json", manifest)
    check_package(directory / "manifest.json")
    return {"status": "packaged", "archive": str(archive), "manifest": str(directory / "manifest.json"), "files": len(files),
        "dependencies_bundled": False, "new_remote_model_calls": 0}


def check_package(manifest):
    path = plain_path(manifest)
    value = json.loads(path.read_text(encoding="utf-8"))
    if (value.get("version") != 1 or value.get("kind") != "personal_agent_runtime_bundle" or value.get("archive") != "personal-agent.zip"
            or not isinstance(value.get("entries"), dict) or not 1 <= len(value["entries"]) <= 1000
            or not ROOT_FILES | {"README.md", "personal_agent/delivery.py", "personal_agent/delivery_demo.py"} <= set(value["entries"])
            or any(not _allowed(name) for name in value["entries"])):
        raise ValueError("运行包清单或允许路径无效。")
    archive = plain_path(path.parent / value["archive"])
    if archive.stat().st_size > 64 * 1024 * 1024 or digest(archive.read_bytes()) != value["archive_sha256"]:
        raise ValueError("运行包 ZIP 指纹变化或超过大小上限。")
    try:
        with zipfile.ZipFile(archive) as source:
            entries = source.infolist()
            if (len(entries) != len(value["entries"]) or {entry.filename for entry in entries} != set(value["entries"])
                    or sum(entry.file_size for entry in entries) > 64 * 1024 * 1024):
                raise ValueError("运行包 ZIP 的文件清单不一致。")
            for entry in entries:
                raw = source.read(entry)
                if {"sha256": digest(raw), "bytes": len(raw)} != value["entries"][entry.filename]:
                    raise ValueError("运行包内文件字节变化。")
    except zipfile.BadZipFile:
        raise ValueError("运行包不是完整 ZIP。") from None
    return {"status": "passed", "manifest": str(path), "files": len(value["entries"]), "new_remote_model_calls": 0,
        "validation": "read-only ZIP inventory and byte hashes; no extraction or execution"}
