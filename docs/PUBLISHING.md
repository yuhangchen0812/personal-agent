# 上传到 GitHub

本目录为独立的公开仓库快照，保留一套新的本地提交历史。请将本目录作为上传源，不要把开发环境的 .env、.personal、虚拟环境或历史报告加入提交。

## 建议仓库信息

- 仓库名：personal-agent
- Description：Python CLI agent with permissions, resumable sessions, skills, MCP, and reproducible CI repair workflows.
- Topics：python、ai-agent、cli、tool-calling、mcp、llm、pytest

## 推送

在 GitHub 创建空仓库，然后在本目录执行，将示例 URL 替换为自己的地址：

```powershell
git remote add origin https://github.com/YOUR_USERNAME/personal-agent.git
git push -u origin main
```

如使用 ZIP，先解压；ZIP 不包含 .git，需要自行 git init、git add 和 git commit。上传前运行 git status，检查只有计划公开的文件。

仓库已提供指向 main 分支实际工作流状态的 CI badge。每次推送后查看对应提交的 Actions 结果。仓库当前未指定开源许可证；可按作者选择补充许可证文件。

## 项目展示要点

用一条完整任务解释权限边界、状态恢复、证据和回归检查，展示真实差异、日志和回滚结果。README 的自编案例数量不是通用成功率；无需把模型文字或已观察样本包装成能力提升。
