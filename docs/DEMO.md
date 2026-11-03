# CI 流程复现

此入口属于完整源码仓库的集成检查，需要开发依赖和 `tests/fixtures/`；不在最小运行包中提供。它使用固定模型响应，实际执行 Agent Loop、文件工具、pytest、采纳和回滚，不请求模型服务。

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m tests.support.ci_walkthrough
```

| 自编案例 | 候选公开测试 | 候选独立回归 | 预期行为 |
| --- | --- | --- | --- |
| 标签清洗 | 3 通过 | 3 通过 | 在副本中采纳，再回滚并核对原始字节 |
| 分页候选 | 2 通过 | 1 通过、2 失败 | 检出新增回归，拒绝采纳凭据 |

标签案例修复首尾空白；分页案例故意采用不完整候选，说明公开测试通过后仍可能引入回归。

每次运行创建新的 `.personal/ci-walkthrough/` 子目录，返回报告及 HTML 页面路径，包含本次 XML、日志、差异和事务结果。请以自己的本次产物为准；表中是案例预期，不是跨任务修复成功率。

只看消息与工具调用轨迹：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m tests.support.agent_loop_walkthrough
```

仅使用运行包时，执行 `python run_demo.py` 查看内置文件与 Plan 示例。
