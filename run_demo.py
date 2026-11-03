"""通过绝对路径启动交付工具，保留调用者当前目录。"""

from personal_agent.delivery import main


if __name__ == "__main__":
    import sys
    main(sys.argv[1:] or ["demo"])
