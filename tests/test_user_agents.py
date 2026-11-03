"""两条主流程：跨项目复用与优先级，以及选中来源的实际委派。"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

from personal_agent.agent_config import discover_agents, load_agent_definition
from personal_agent.main import run_repl
from personal_agent.tool_registry import TOOL_REGISTRY
from personal_agent.tools import read_file
from personal_agent.usage import TokenPrices


def call(call_id, name, arguments):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


class UserAgentsSmokeTests(unittest.TestCase):
    def test_discovery_reuses_user_definitions_and_prefers_valid_project_definitions(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            project_a, project_b = base / "project-a", base / "project-b"
            user_agents = base / "home/.personal/agents"
            expected = {}

            def write(path, content):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
                expected[path] = path.read_bytes()

            with (
                patch("personal_agent.agent_config.PROJECT_ROOT", project_a),
                patch("personal_agent.agent_config.USER_AGENTS_ROOT", user_agents),
            ):
                self.assertEqual(discover_agents(), ([], []))
                self.assertFalse(project_a.exists())
                self.assertFalse(user_agents.exists())
                project_agents = project_a / ".personal/agents"
                write(project_agents / "shared.md", "---\nallowed-tools: []\n---\nPROJECT_SHARED")
                write(project_agents / "project-only.md", "PROJECT_ONLY")
                write(project_agents / "fallback.md", "---\nname: fallback\n未闭合")
                write(project_agents / "dupe-a.md", "---\nname: duplicate\n---\nA")
                write(project_agents / "dupe-b.md", "---\nname: duplicate\n---\nB")
                for name in ("shared", "fallback", "duplicate", "user-only"):
                    write(user_agents / f"{name}.md", f"---\nallowed-tools: read_file\n---\nUSER_{name}")
                write(user_agents / "nested/hidden.md", "不可递归发现")
                write(user_agents.parent / "outside.md", "不可扩大用户读取范围")

                definitions, warnings = discover_agents()
                selected = {item.name: item for item in definitions}
                self.assertEqual(set(selected), {"shared", "project-only", "fallback", "duplicate", "user-only"})
                self.assertEqual((selected["shared"].source, selected["shared"].allowed_tools), ("project", ()))
                self.assertEqual(selected["shared"].system_prompt, "PROJECT_SHARED")
                self.assertEqual(selected["fallback"].source, "user")
                self.assertEqual(selected["duplicate"].source, "user")
                self.assertEqual(len(warnings), 2)
                self.assertTrue(any("未闭合" in warning for warning in warnings))
                self.assertTrue(any("Agent 名重复" in warning for warning in warnings))
                for invalid in (user_agents / "nested/hidden.md", user_agents.parent / "outside.md"):
                    with self.assertRaises(ValueError):
                        load_agent_definition(invalid)

                # 切换项目，共用同一用户目录；用户角色仍可用且来源不变。
                with patch("personal_agent.agent_config.PROJECT_ROOT", project_b):
                    other, warnings = discover_agents()
                self.assertEqual(warnings, [])
                self.assertEqual({item.name for item in other}, {"shared", "fallback", "duplicate", "user-only"})
                self.assertTrue(all(item.source == "user" for item in other))
                self.assertEqual(next(item for item in other if item.name == "shared").allowed_tools, ("read_file",))

                # 用户根路径损坏时提示，项目有效定义继续可用。
                bad_root = base / "bad-root"
                write(bad_root, "根路径是文件")
                with patch("personal_agent.agent_config.USER_AGENTS_ROOT", bad_root):
                    remaining, warnings = discover_agents()
                self.assertEqual({item.name for item in remaining}, {"shared", "project-only"})
                self.assertTrue(any("必须是目录" in warning for warning in warnings))
                for path, data in expected.items():
                    self.assertEqual(path.read_bytes(), data)

    def test_selected_source_controls_delegation_and_tools_still_use_current_project(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            root, user_agents = base / "project", base / "home/.personal/agents"
            project_agent = root / ".personal/agents/helper.md"
            project_agent.parent.mkdir(parents=True)
            user_agents.mkdir(parents=True)
            project_agent.write_text("---\ndescription: 项目版\nallowed-tools: []\n---\nPRIVATE_PROJECT_BODY", encoding="utf-8")
            user_agent = user_agents / "helper.md"
            user_agent.write_text("---\ndescription: 用户版\nallowed-tools: read_file, grep_search\n---\nPRIVATE_USER_BODY", encoding="utf-8")
            user_before = user_agent.read_bytes()
            (root / "source.txt").write_text("CURRENT_PROJECT_EVIDENCE", encoding="utf-8")
            (user_agents / "source.txt").write_text("USER_DIRECTORY_MUST_NOT_BE_WORKSPACE", encoding="utf-8")
            counts = {"parent": 0, "child": 0}

            def model(messages, definitions, *, on_usage):
                child = "PRIVATE_" in str(messages[0].get("content"))
                role = "child" if child else "parent"
                counts[role] += 1
                source = "project" if counts[role] <= 2 else "user"
                on_usage({"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1})
                if not child:
                    agent = next(item for item in definitions if item["name"] == "agent")
                    self.assertIn('"source": "' + source + '"', agent["description"])
                    self.assertNotIn("PRIVATE_", str(messages) + str(definitions))
                    if counts[role] in (1, 3):
                        return {"role": "assistant", "tool_calls": [call("helper", "agent", {"type": "helper", "prompt": "调查 source.txt"})]}
                    self.assertEqual(messages[-1]["content"], "子任务结论")
                    self.assertNotIn("read_evidence", str(messages))
                    return {"role": "assistant", "content": "父任务完成"}
                self.assertIn(f"角色作用域：{source}", messages[0]["content"])
                self.assertIn("PRIVATE_PROJECT_BODY" if source == "project" else "PRIVATE_USER_BODY", messages[0]["content"])
                self.assertEqual({item["name"] for item in definitions}, set() if source == "project" else {"read_file", "grep_search"})
                if counts[role] in (1, 3):
                    self.assertEqual([item["role"] for item in messages], ["system", "user"])
                    return {"role": "assistant", "tool_calls": [
                        call("read_evidence", "read_file", {"file_path": "source.txt"}),
                        call("search_evidence", "grep_search", {"pattern": "EVIDENCE"}),
                    ]}
                results = {item["tool_call_id"]: item["content"] for item in messages if item["role"] == "tool"}
                if source == "project":
                    self.assertTrue(all("helper 仅允许" in result for result in results.values()))
                else:
                    self.assertIn("CURRENT_PROJECT_EVIDENCE", results["read_evidence"])
                    self.assertNotIn("USER_DIRECTORY_MUST_NOT_BE_WORKSPACE", results["read_evidence"])
                    self.assertIn("显式 deny", results["search_evidence"])
                return {"role": "assistant", "content": "子任务结论"}

            def inputs():
                yield "/agents"
                yield "使用项目版"
                # 项目项格式损坏后，下一轮发现才可改选有效用户定义。
                project_agent.write_text("---\nname: helper\n未闭合", encoding="utf-8")
                yield "/agents"
                yield "使用用户版"
                yield "/usage"
                yield "exit"

            output, errors = io.StringIO(), io.StringIO()
            reader, blocked = Mock(wraps=read_file), Mock()
            with (
                patch("personal_agent.agent_config.PROJECT_ROOT", root),
                patch("personal_agent.agent_config.USER_AGENTS_ROOT", user_agents),
                patch("personal_agent.tools.PROJECT_ROOT", root),
                patch("personal_agent.skills.PROJECT_ROOT", root),
                patch("personal_agent.skills.USER_SKILLS_ROOT", base / "user-skills"),
                patch.dict(TOOL_REGISTRY["read_file"], {"handler": reader}),
                patch.dict(TOOL_REGISTRY["grep_search"], {"handler": blocked}),
                patch("personal_agent.main.get_model_message", side_effect=model),
                patch("personal_agent.main.confirm_in_terminal") as confirm,
                patch("builtins.input", side_effect=inputs()),
                redirect_stdout(output), redirect_stderr(errors),
            ):
                history = run_repl(deny=["grep_search"], prices=TokenPrices(Decimal(1_000_000), Decimal(1_000_000)), max_cost=Decimal(10))
            self.assertEqual(counts, {"parent": 4, "child": 4})
            self.assertEqual(len(history), 8)
            self.assertNotIn("PRIVATE_", str(history))
            self.assertIn("helper [可委派] 项目版 [来源：项目]", output.getvalue())
            self.assertIn("helper [可委派] 用户版 [来源：用户]", output.getvalue())
            self.assertIn("逻辑调用 8 次，完整用量 8 次", output.getvalue())
            self.assertIn("未闭合", errors.getvalue())
            self.assertNotIn("本轮请求失败", errors.getvalue())
            self.assertEqual(user_agent.read_bytes(), user_before)
            reader.assert_called_once()
            blocked.assert_not_called()
            confirm.assert_not_called()
