# CI 流程演示

该示例展示一条可以审阅的修复链：冻结输入、读取源码、生成候选、复验、拒绝新增回归，以及采纳后回滚。模型响应固定，Agent Loop、文件工具、pytest 和跨进程状态恢复实际执行，不请求模型服务。

## 运行

在完整源码仓库安装开发依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -X utf8 -B -m tests.support.ci_walkthrough
```

输出位于新建的 `.personal/ci-walkthrough/随机ID`，终端返回 `index.html` 和 `report.json` 的路径。原始案例保留，所有修改和测试在工程副本中进行。

## 查看什么

| 自编案例 | 候选公开测试 | 候选独立回归 | 预期结果 |
| --- | --- | --- | --- |
| 标签清洗 | 3 通过 | 3 通过 | 在副本采纳，复验后回滚并核对字节 |
| 分页候选 | 2 通过 | 1 通过、2 失败 | 拒绝生成采纳凭据 |

标签候选补齐首尾空白处理。分页候选故意使用不完整的“整除后加一”，满足公开用例后仍在空列表和整除场景失败，说明公开测试通过不等于可以采纳。

报告页面串起输入、实际工具轨迹、候选差异、公开与独立测试，以及事务状态。JSON、XML 和日志保留本次证据；新进程只读取已完成阶段，不重放诊断或采纳。

## 其他入口

只观察成功、拒绝和次数上限三条消息轨迹：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m tests.support.agent_loop_walkthrough
```

仅使用运行包时，执行 `python run_demo.py` 查看内置文件与 Plan 示例。上述完整 CI 复现依赖源码中的 `tests/fixtures/` 和开发依赖，不随运行包分发。

表中结果是自编案例的预期行为，不是跨任务修复成功率或模型能力提升。检查方法见[测试说明](TESTING.md)，真实模型工作流入口见[使用指南](USAGE.md)。
