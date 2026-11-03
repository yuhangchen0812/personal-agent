"""展示五种模式的权限决定，不调用工具，不请求模型。"""

from personal_agent.permissions import PERMISSION_MODES, check_permission


def main() -> None:
    tools = ("read_file", "write_file", "edit_file", "run_shell")
    print("权限规则演示：仅判断，不执行工具。")
    print(f"{'mode':20}" + "".join(f"{tool:14}" for tool in tools))
    for mode in PERMISSION_MODES:
        actions = [check_permission(tool, mode)["action"] for tool in tools]
        print(f"{mode:20}" + "".join(f"{action:14}" for action in actions))

    print("\n规则优先级：")
    cases = [
        ("default + allow write_file", "default", ["write_file"], None),
        ("plan + allow write_file", "plan", ["write_file"], None),
        ("bypassPermissions + deny write_file", "bypassPermissions", None, ["write_file"]),
    ]
    for label, mode, allow, deny in cases:
        decision = check_permission("write_file", mode, allow=allow, deny=deny)
        print(f"{label} → {decision['action']}：{decision['message']}")


if __name__ == "__main__":
    main()
