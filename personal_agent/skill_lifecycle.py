"""项目内 Skill 使用事件、显式整目录归档与无覆盖恢复。"""

import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from . import skills


EVENTS = ("retrieved", "inline_loaded", "fork_started", "fork_returned")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class SkillLifecycleStore:
    def __init__(self):
        self.root = skills.PROJECT_ROOT.resolve()

    def _path(self, relative: str | Path) -> Path:
        path = self.root / relative
        if not path.is_relative_to(self.root / ".personal") or path.resolve() != path:
            raise ValueError("技能生命周期记录须留在当前项目原始 .personal 路径，不能使用链接。")
        return path

    def _write(self, path: Path, record: dict, *, new: bool = True) -> None:
        path = self._path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        self._path(path)
        if new:
            created = False
            try:
                with path.open("xb") as target:
                    created = True
                    target.write(payload)
            except BaseException:
                if created:
                    path.unlink(missing_ok=True)
                raise
        else:
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=".lifecycle-", suffix=".tmp", delete=False) as target:
                    temporary = Path(target.name)
                    target.write(payload)
                os.replace(temporary, path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

    def record(self, skill: skills.SkillDefinition, event: str, entrypoint: str) -> dict:
        """每个事件单独文件；不保存参数/任务正文，不把加载标成成功。"""
        if event not in EVENTS:
            raise ValueError("未知技能使用事件。")
        path, source = skills._skill_path(skill.file_path)
        location = str(path) if source == "user" else path.relative_to(self.root).as_posix()
        record = {"version": 1, "id": "use_" + uuid4().hex, "project_root": str(self.root),
                  "created_at": _now(), "name": skill.name, "source": source, "skill_path": location,
                  "context": skill.context, "method_sha256": _digest(skill.prompt_template.encode("utf-8")),
                  "source_sha256": skill.source_sha256,
                  "event": event, "entrypoint": entrypoint}
        self._write(self._path(f".personal/skill-usage/events/{record['id']}.json"), record)
        return record

    def statistics(self, name: str | None = None) -> list[dict]:
        """按作用域和路径聚合；同名用户/项目记录分开，重启后仍可读取。"""
        selected, _ = skills.discover_skills()
        current = {(skill.source, str(skill.file_path) if skill.source == "user" else skill.file_path.relative_to(self.root).as_posix()): skill for skill in selected}
        grouped = {}

        def item(key, skill_name):
            return grouped.setdefault(key, {"name": skill_name, "source": key[0], "skill_path": key[1],
                                           "selected_now": key in current, "counts": dict.fromkeys(EVENTS, 0),
                                           "last_event_at": None, "method_versions": []})

        for key, skill in current.items():
            item(key, skill.name)
        directory = self._path(".personal/skill-usage/events")
        records = []
        for path in directory.glob("*.json"):
            record = json.loads(self._path(path).read_text(encoding="utf-8"))
            if (not isinstance(record, dict) or record.get("version") != 1 or record.get("id") != path.stem
                    or not re.fullmatch(r"use_[a-f0-9]{32}", path.stem) or record.get("project_root") != str(self.root)
                    or record.get("event") not in EVENTS or record.get("source") not in ("project", "user")
                    or any(not isinstance(record.get(key), str) or not record[key] for key in ("created_at", "name", "skill_path", "method_sha256"))):
                raise ValueError("技能使用记录格式或项目归属不匹配。")
            records.append(record)
        for record in sorted(records, key=lambda value: (value["created_at"], value["id"])):
            row = item((record["source"], record["skill_path"]), record["name"])
            row["name"] = current[(row["source"], row["skill_path"])].name if row["selected_now"] else record["name"]
            row["counts"][record["event"]] += 1
            row["last_event_at"] = record["created_at"]
            if record["method_sha256"] not in row["method_versions"]:
                row["method_versions"].append(record["method_sha256"])
        return sorted((row for row in grouped.values() if name is None or row["name"] == name), key=lambda row: (row["name"], row["source"], row["skill_path"]))

    def _archive_path(self, archive_id: str) -> Path:
        if not isinstance(archive_id, str) or not re.fullmatch(r"arc_[a-f0-9]{32}", archive_id):
            raise ValueError("归档 ID 无效，请使用列表中的完整 arc_ID。")
        return self._path(f".personal/skill-archives/{archive_id}")

    def _original(self, record: dict) -> Path:
        relative = Path(record["skill_path"])
        if (len(relative.parts) != 4 or relative.parts[:2] != (".personal", "skills")
                or relative.parts[2] in {".", ".."} or relative.name != "SKILL.md"):
            raise ValueError("归档中的原技能路径无效。")
        return self._path(relative)

    def archive_record(self, archive_id: str) -> dict:
        record = json.loads(self._path(self._archive_path(archive_id) / "record.json").read_text(encoding="utf-8"))
        if (not isinstance(record, dict) or record.get("version") != 1 or record.get("id") != archive_id
                or record.get("project_root") != str(self.root) or record.get("status") not in ("archived", "restored")
                or not isinstance(record.get("reason"), str)
                or any(not isinstance(record.get(key), str) or not record[key] for key in ("skill_path", "name", "sha256", "created_at"))
                or not re.fullmatch(r"[a-f0-9]{64}", record["sha256"])):
            raise ValueError("技能归档格式、ID 或项目归属不匹配。")
        self._original(record)
        return record

    def list_archives(self) -> list[dict]:
        directory = self._path(".personal/skill-archives")
        return [self.archive_record(path.name) for path in sorted(directory.glob("arc_*")) if path.is_dir()]

    def archive(self, name: str, reason: str = "") -> dict:
        """先保存来源记录，再移动整个目录；只接受当前有效的项目目标。"""
        skill = skills.get_project_skill(name)
        original = self._path(skill.file_path)
        raw = original.read_bytes()
        record = {"version": 1, "id": "arc_" + uuid4().hex, "project_root": str(self.root),
                  "created_at": _now(), "name": skill.name, "skill_path": original.relative_to(self.root).as_posix(),
                  "sha256": _digest(raw), "status": "archived", "reason": reason}
        directory = self._archive_path(record["id"])
        payload = self._path(directory / "skill")
        # 两端都在本项目的明确根目录内；只作 rename，不跨盘搬运或递归删除。
        directory.mkdir(parents=True)
        try:
            self._write(directory / "record.json", record)
            if self._path(original).read_bytes() != raw:
                raise ValueError("技能在归档前发生变化，未移动。")
            self._path(payload)
            original.parent.rename(payload)
            skills.reset_skill_cache()
        except BaseException:
            if not payload.exists():
                (directory / "record.json").unlink(missing_ok=True)
                directory.rmdir()
            raise
        return record

    def restore(self, archive_id: str) -> dict:
        """无覆盖恢复到原目录；移动后回执失败可用同 ID 重试补齐。"""
        record = self.archive_record(archive_id)
        if record["status"] == "restored":
            return record
        directory = self._archive_path(archive_id)
        payload = self._path(directory / "skill")
        original = self._original(record)
        if payload.exists():
            if not payload.is_dir() or _digest(self._path(payload / "SKILL.md").read_bytes()) != record["sha256"]:
                raise ValueError("归档 Skill 文件指纹不匹配，未恢复。")
            if original.parent.exists():
                raise ValueError("原技能目录已存在，恢复不会覆盖。")
            for scope in (skills.USER_SKILLS_ROOT.absolute(), self.root / ".personal/skills"):
                active, _ = skills._scan_skill_directory(scope)
                if any(skill.name == record["name"] for skill in active):
                    raise ValueError("当前已有同名用户或项目技能，未恢复。")
            original.parent.parent.mkdir(parents=True, exist_ok=True)
            self._path(original)
            self._path(payload).rename(original.parent)
        elif not original.is_file() or _digest(original.read_bytes()) != record["sha256"]:
            raise ValueError("归档内容缺失且原位置不匹配，无法恢复。")
        skills.reset_skill_cache()
        record.update(status="restored", restored_at=_now())
        try:
            self._write(directory / "record.json", record, new=False)
        except (ValueError, OSError):
            raise RuntimeError(f"技能已恢复到 {original}，但回执未保存；重试 /skill-restore {archive_id} 补齐，勿移动或编辑文件。") from None
        return record


def record_skill_event(skill: skills.SkillDefinition, event: str, entrypoint: str) -> None:
    """运行时遥测尽力写入；记录失败提示到 stderr，不撤销已执行任务。"""
    try:
        record = SkillLifecycleStore().record(skill, event, entrypoint)
        from .skill_learning import observe_skill_event
        observe_skill_event(record)
    except (ValueError, OSError) as error:
        print(f"技能使用记录未保存（{type(error).__name__}）；计数可能不完整。", file=sys.stderr)


def format_skill_statistics(rows: list[dict]) -> str:
    lines = ["Skill 使用统计（历史事件次数，不代表任务成功率）："]
    for row in rows:
        lines.append(f"- {row['name']} [{row['source']}]；当前选中：{row['selected_now']}；{row['skill_path']}")
        lines.append("  " + "；".join(f"{event}={count}" for event, count in row["counts"].items()) + f"；最近事件：{row['last_event_at'] or '无'}；方法版本数：{len(row['method_versions'])}")
    if not rows:
        lines.append("暂无匹配的技能或历史记录。")
    return "\n".join(lines)
