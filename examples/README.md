# 使用模板与示例

- `agents/`：自定义子 Agent 模板。
- `skills/`：Skill 方法模板。
- `mcp-config.json`、`mcp_server_demo.py`：本地 stdio MCP 配置和最小服务，配置入口见[使用指南](../docs/USAGE.md)。
- `tasks/ci-triage/SKILL.md`：CI 导入功能默认使用的方法模板。
- `pytest-ci-demo/`：现有 CI 诊断功能使用的已知示例工程，包含故意失败的业务测试；它是功能示例数据，不是 Personal Agent 自身的回归测试。
- `agent-loop-responses.json`：`python -m personal_agent.agent` 使用的固定响应示例。

应用自身的测试数据和流程复现统一位于 `tests/`。示例不包含凭据或个人运行记录，运行输出不写回本目录。
