"""Skill 缓存的真实磁盘读取计数与 CLI 维护流程；不请求外部模型。"""

import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.support.verify_delivery import isolated
from tests.support.verify_repl_commands import invoke
from personal_agent import skills, skill_search
from personal_agent.session import Session
from personal_agent.skill_cache import SkillCache, current_skill_cache, skill_cache_scope
from personal_agent.skill_lifecycle import SkillLifecycleStore


@contextmanager
def count_reads(root):
    """只统计真实 SKILL.md 的原字节读取，其他会话/报告读取不计入。"""
    original = Path.read_bytes
    reads = []
    def read(path, *args, **kwargs):
        if path.name == "SKILL.md" and path.is_relative_to(root):
            reads.append(path.relative_to(root).as_posix())
        return original(path, *args, **kwargs)
    with patch.object(Path, "read_bytes", read):
        yield reads


def document(path, name, description, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n{body}\n", encoding="utf-8")


def results(query):
    definitions, warnings = skills.discover_skills()
    hits = skill_search.retrieve_relevant_skills(query, definitions)
    return definitions, warnings, [{"name": hit.skill.name, "source": hit.skill.source, "score": hit.score,
        "raw_score": hit.raw_score, "name_bonus": hit.name_bonus, "tokens": list(hit.matched_tokens)} for hit in hits]


def verify_reuse_and_external_changes():
    with tempfile.TemporaryDirectory(prefix="personal-skill-cache-") as temporary:
        root = Path(temporary).resolve()
        with isolated(root):
            review = skills.create_project_skill("review-files", "inspect exception handling", "PROJECT_SHADOWED inspect exceptions")
            render = skills.create_project_skill("render-files", "render files", "RENDER_OLD render files")
            user = root / "isolated-user-skills/review/SKILL.md"
            document(user, "review-files", "inspect exception handling", "USER_METHOD inspect exceptions")
            queries = ("inspect exception", "render files", "volcanicunicorn")
            build = skill_search._build_skill_index
            assert current_skill_cache() is None
            with count_reads(root) as baseline_reads, patch("personal_agent.skill_search._build_skill_index", wraps=build) as builds:
                baseline = [results(query)[2] for query in queries]
                baseline_measurement = {"skill_file_reads": len(baseline_reads), "index_builds": builds.call_count}
            cache = SkillCache()
            with skill_cache_scope(cache), count_reads(root) as cached_reads, patch("personal_agent.skill_search._build_skill_index", wraps=build) as builds:
                cached = [results(query)[2] for query in queries]
                cached_measurement = {"skill_file_reads": len(cached_reads), "index_builds": builds.call_count}
                assert cached == baseline and baseline[0][0]["source"] == "user"
                assert baseline_measurement == {"skill_file_reads": 9, "index_builds": 3}
                assert cached_measurement == {"skill_file_reads": 3, "index_builds": 1}
                initial_info = cache.info()

                before = len(cached_reads)
                document(render, "render-files", "changedtoken render", "EXTERNAL_NEW_METHOD changedtoken")
                _, warnings, hits = results("changedtoken")
                assert not warnings and hits[0]["name"] == "render-files" and len(cached_reads) - before == 1
                changed_reads = len(cached_reads) - before
                render.write_text("---\nname: render-files\n---\n", encoding="utf-8")
                definitions, warnings, hits = results("changedtoken")
                assert warnings and not hits and all(item.name != "render-files" for item in definitions)
                document(render, "render-files", "changedtoken render", "REPAIRED_METHOD changedtoken")
                assert results("changedtoken")[2][0]["name"] == "render-files"

                # 即使子目录先存在、根目录时间不再改变，下一次扫描也能发现其中新增的文件。
                extra = root / ".personal/skills/previously-empty/SKILL.md"
                extra.parent.mkdir()
                results("newexternal")
                document(extra, "new-external", "newexternal", "newexternal method")
                assert results("newexternal")[2][0]["name"] == "new-external"
                extra.unlink()
                assert not results("newexternal")[2]
                user.unlink()
                definitions, warnings, hits = results("PROJECT_SHADOWED")
                assert not warnings and next(item for item in definitions if item.name == "review-files").source == "project"
                assert hits and hits[0]["source"] == "project"
                final_info = cache.info()
            assert current_skill_cache() is None and review.is_file()
            return {"status": "passed", "validation": "actual Path.read_bytes and index-build counts; real file edits/additions/deletions, no model requests",
                    "comparison": {"files": 3, "queries": list(queries), "without_cache": baseline_measurement, "with_cache": cached_measurement,
                                   "scores_and_order_identical": True, "initial_cache_info": initial_info},
                    "external_change": {"only_changed_file_reread": changed_reads, "corrupt_file_dropped_then_repaired": True,
                                        "new_file_in_existing_directory_detected": True, "deletion_and_user_priority_updated": True},
                    "final_cache_info": final_info, "scope_restored": True,
                    "measurement_boundary": "three local files and three authored queries; no timing benchmark or model-quality claim"}


def verify_cli_and_boundaries():
    with tempfile.TemporaryDirectory(prefix="personal-skill-cache-cli-") as temporary:
        root = Path(temporary).resolve()
        with isolated(root), patch("personal_agent.main.get_model_settings", return_value={"protocol": "openai", "model": "fixed-cache", "base_url": "https://example.invalid"}):
            skills.create_project_skill("review-files", "review files", "REVIEW_METHOD")
            lifecycle = SkillLifecycleStore()
            snapshots, calls = {}, []
            def take(name):
                snapshots[name] = current_skill_cache().info()
            def model(messages, definitions=None, *, on_usage, **options):
                calls.append(messages)
                assert "NEW_METHOD" in messages[-1]["content"]
                on_usage({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
                return {"role": "assistant", "content": "CACHE_TASK_DONE"}
            def inputs():
                take("startup")
                yield "/skills cache"
                yield "/skills search review-files"
                take("first_query")
                yield "/skills search review-files"
                take("second_query")
                assert snapshots["first_query"]["definition_loads"] == snapshots["second_query"]["definition_loads"] == 1
                assert snapshots["second_query"]["index_builds"] == 1 and snapshots["second_query"]["index_hits"] == 1
                yield "/skill-create extra-flow | extra flow | | ORIGINAL_METHOD"
                take("created")
                assert snapshots["created"]["cached_files"] == 0 and not snapshots["created"]["index_ready"]
                yield "/skills search extra-flow"
                yield "/skill-evolve extra-flow NEW_METHOD"
                take("evolved")
                assert snapshots["evolved"]["cached_files"] == 0
                yield "/skills search NEW_METHOD"
                yield "/skill-archive extra-flow cache-demo"
                take("archived")
                assert not any(item.name == "extra-flow" for item in skills.discover_skills()[0])
                archive = lifecycle.list_archives()[0]
                yield f"/skill-restore {archive['id']}"
                take("restored")
                assert snapshots["restored"]["cached_files"] == 0
                yield "/skills search NEW_METHOD"
                yield "/extra-flow example"
                yield "/skills refresh"
                take("refreshed")
                assert snapshots["refreshed"]["cached_files"] == 2 and not snapshots["refreshed"]["index_ready"]
                yield "/skills search NEW_METHOD"
                yield "/skills cache"
                take("final")
                yield "exit"
            with patch("personal_agent.main.get_model_message", side_effect=model), patch("builtins.input", side_effect=inputs()):
                code, output, errors = invoke(["--session", "cache-demo", "--with-tools", "--dont-ask"])
            assert code == 0 and not errors, errors
            assert len(calls) == 1 and len(Session("cache-demo").load()) == 2
            assert "Skill 缓存（本次运行）" in output and "Skill 缓存已清除" in output
            assert current_skill_cache() is None
            # 下一次聊天创建新缓存；查看状态不扫描目录、也不发请求。
            with patch("personal_agent.main.get_model_message", side_effect=AssertionError("cache status must not call model")), \
                 patch("builtins.input", side_effect=["/skills cache", "exit"]):
                code, next_output, errors = invoke([])
            assert code == 0 and not errors and "解析尝试 0 次" in next_output

            # 模拟状态元数据未变：发现可能命中旧定义，真正调用仍重读；refresh 强制更新发现。
            target = root / ".personal/skills/refreshable/SKILL.md"
            document(target, "refreshable", "freshness", "BODY_OLD")
            cache = SkillCache()
            original_stat = Path.stat
            signature = target.stat()
            def stat(path, *args, **kwargs):
                return signature if path == target else original_stat(path, *args, **kwargs)
            with skill_cache_scope(cache), patch.object(Path, "stat", stat):
                definitions = skills.discover_skills()[0]
                assert next(item for item in definitions if item.name == "refreshable").prompt_template.strip() == "BODY_OLD"
                document(target, "refreshable", "freshness", "BODY_NEW")
                stale = skills.discover_skills()[0]
                assert next(item for item in stale if item.name == "refreshable").prompt_template.strip() == "BODY_OLD"
                assert "BODY_NEW" in skills.prepare_named_skill("/refreshable args", stale)
                assert "BODY_NEW" in skills.load_inline_skill("refreshable")
                cache.clear()
                assert "BODY_NEW" in next(item for item in skills.discover_skills()[0] if item.name == "refreshable").prompt_template

            # 热缓存不能绕过重新执行的路径校验；这是模拟 resolve，不创建 Windows 链接或改 ACL。
            real_resolve = Path.resolve
            def resolve(path, *args, **kwargs):
                if path == target:
                    return root.parent / "outside-skill/SKILL.md"
                return real_resolve(path, *args, **kwargs)
            with skill_cache_scope():
                skills.discover_skills()
                with patch.object(Path, "resolve", resolve):
                    definitions, warnings = skills.discover_skills()
                assert warnings and not any(item.name == "refreshable" for item in definitions)

            # 同一个缓存对象切换根目录，及嵌套作用域退出，都不能返回上一工程的定义。
            other = root / "other-project"
            other.mkdir()
            document(other / ".personal/skills/other/SKILL.md", "other", "other", "OTHER_METHOD")
            with skill_cache_scope(cache):
                with isolated(other):
                    assert [item.name for item in skills.discover_skills()[0]] == ["other"]
                    assert skill_search.retrieve_relevant_skills("other", skills.discover_skills()[0])[0].skill.name == "other"
                with skill_cache_scope() as nested:
                    assert nested is not cache and nested.info()["definition_loads"] == 0
                assert current_skill_cache() is cache
            return {"status": "passed", "validation": "actual CLI maintenance and one fixed model callback; metadata and resolve boundaries simulated explicitly",
                    "snapshots": snapshots, "model_callbacks": len(calls), "maintenance_visible": ["create", "evolve", "archive", "restore", "refresh"],
                    "fresh_execution_with_unchanged_metadata": True, "path_validation_on_warm_cache": True,
                    "root_and_nested_scope_isolation": True, "new_chat_starts_empty": True}
