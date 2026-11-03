# 验证记录

更新：2026-11-03。主要本地环境为 Windows、Python 3.12.15、项目独立 .venv。

## 本轮审查与回归

| 检查 | 结果 | 范围 |
| --- | --- | --- |
| 修改前全量 unittest | 208/208 通过 | 初始公开提交的完整 discovery |
| 修改后全量 unittest | 213/213 通过 | 新增 5 项独立回归，包含并发工作区和响应契约 |
| 默认离线验收 | 28/28 组通过 | 新增 core_unit，执行前后源码指纹一致 |
| core_unit | 45 项通过 | 核心循环、工具、权限、Session、Plan、配置和验收器 |
| 历史证据门槛 | 回归通过 | 默认不读取私人文件；显式 --history 仍拒绝缺失/失败证据 |

[审查说明](REVIEW.md)记录问题、复现和改进；[脱敏验证摘要](evidence/global-review.json)记录计数与源码指纹。全量 unittest 和默认分组复用部分测试，不能相加当作独立覆盖率。

28 组的最终总控结果为 passed，live_smoke 未运行，historical_evidence 明确记为 not_requested。一次运行期间的输出文案修改被源码指纹保护检出并判失败；该记录保留在本机，固定源码后重新完成整轮验收。

本轮新增远端模型请求为 0，未升级依赖。原始日志含本机路径，保存在未跟踪的 .personal 目录。

## 已确认的 GitHub 基线

以下两次运行属于初始公开提交 `1c9bd900652eb8e250b875ffcef2de48303f7ee5`：

- [Windows checks：成功](https://github.com/yuhangchen0812/personal-agent/actions/runs/41573857865)。
- [Container acceptance：成功](https://github.com/yuhangchen0812/personal-agent/actions/runs/41573857856)。已读取原始报告，确认容器验收标记与清理结果。

Linux 文件型容器的实际检查包括非 root、只读根目录、断网、卷持久化、新容器恢复不重放工具，以及向活动 CLI 发送外部 SIGTERM 后退出 143、保留完整历史和清理资源。

后续提交的状态应查看对应 [Windows 工作流](https://github.com/yuhangchen0812/personal-agent/actions/workflows/windows-ci.yml)和[容器工作流](https://github.com/yuhangchen0812/personal-agent/actions/workflows/container-acceptance.yml)。旧提交的成功不代替新提交的验证。

## 解释边界

测试主要使用固定模型响应和真实本地组件；自编样本不构成通用修复准确率或模型能力提升证据。Windows Job 不是文件系统或网络沙箱。原生 Anthropic 远端思考签名，以及其他目标系统仍需独立实跑。

初始公开版的完整 CI 演示保留在 [DEMO.md](DEMO.md)，并绑定其原始提交，不改写旧测量来代表本轮结果。
