"""保留调用方目录的任务包启动入口。"""

from personal_agent.task_runner import main


if __name__ == "__main__":
    import sys
    if sys.argv[1:2] == ["learn"]:
        from personal_agent.task_learning import main as learn_main
        learn_main(sys.argv[2:])
    elif sys.argv[1:2] == ["ci"]:
        from personal_agent.ci_runner import main as ci_main
        ci_main(sys.argv[2:])
    elif sys.argv[1:2] == ["junit"]:
        from personal_agent.junit_import import main as junit_main
        junit_main(sys.argv[2:])
    elif sys.argv[1:2] == ["diagnose"]:
        from personal_agent.ci_diagnosis import main as diagnose_main
        diagnose_main(sys.argv[2:])
    elif sys.argv[1:2] == ["repair"]:
        from personal_agent.ci_repair import main as repair_main
        repair_main(sys.argv[2:])
    elif sys.argv[1:2] == ["repair-eval"]:
        from personal_agent.repair_eval import main as repair_eval_main
        repair_eval_main(sys.argv[2:])
    elif sys.argv[1:2] == ["followup"]:
        from personal_agent.diagnosis_followup import main as followup_main
        followup_main(sys.argv[2:])
    elif sys.argv[1:2] == ["history"]:
        from personal_agent.diagnosis_history import main as history_main
        history_main(sys.argv[2:])
    elif sys.argv[1:2] == ["workflow"]:
        from personal_agent.repair_workflow import main as workflow_main
        workflow_main(sys.argv[2:])
    else:
        main()
