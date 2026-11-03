"""固定 Plan 流程，实际调用终端审批和文件工具，不请求模型。"""

from personal_agent.permissions import confirm_in_terminal
from personal_agent.plan import PlanSession
from personal_agent.tool_registry import execute_tool


def main() -> None:
    plan = PlanSession("acceptEdits")
    target = "playground/计划演示/结果.txt"

    def execute(name: str, arguments: dict) -> str:
        result = execute_tool(name, arguments, plan=plan, confirm=confirm_in_terminal)
        print(result)
        return result

    print("离线 Plan 演示：仅使用固定工具参数；计划获批后恢复 acceptEdits。")
    execute("enter_plan_mode", {})
    execute("write_file", {"file_path": plan.plan_file, "content": f"1. 创建或覆盖 {target}，写入固定演示文本。\n2. 读取文件核对结果。\n"})
    print("\n未批准时，任务写入和 Shell 都应拒绝：")
    execute("write_file", {"file_path": target, "content": "未批准，不能写入。\n"})
    execute("run_shell", {"command": "Write-Output '这条命令不应执行'"})
    print("\n第一次建议输入 n，观察返回规划状态：")
    execute("exit_plan_mode", {})
    if plan.state != "executing":
        execute("edit_file", {"file_path": plan.plan_file, "old_string": "固定演示文本", "new_string": "已批准后执行成功"})
        print("\n第二次建议输入 y，批准修订后的实际计划：")
        execute("exit_plan_mode", {})
    if plan.state == "executing":
        execute("write_file", {"file_path": target, "content": "已批准后执行成功。\n"})
        execute("read_file", {"file_path": target})
    else:
        print("计划仍未获批准，未执行任务。")
    print(plan.describe())


if __name__ == "__main__":
    main()
