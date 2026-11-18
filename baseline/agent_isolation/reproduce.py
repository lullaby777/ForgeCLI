"""Reproduce state leakage without running any model or shell tool."""
from test_acceptance import AgentIsolationAcceptance, todo, worker, set_task


def main():
    fixture = AgentIsolationAcceptance()
    fixture.setUp()
    try:
        a = fixture.agent(permission=object(), hooks=object())
        set_task(a, "A-private")
        b = fixture.agent(permission=object(), hooks=object())
        print("新建 B 后，B 的清单（预期为空）:", repr(todo(b).render()))
        print("A 的 worker 是否仍归 A（预期 True）:", worker(a)._parent_agent is a)
        set_task(b, "B-private")
        print("B 更新后，A 的清单（预期仍为 A-private）:", repr(todo(a).render()))
        child = worker(a)._new_subagent()
        print("A 创建的子 Agent 是否使用 A 的模型（预期 True）:", child.llm is a.llm)
        print("A 创建的子 Agent 是否使用 A 的权限（预期 True）:", child.permission is a.permission)
    finally:
        fixture.doCleanups()


if __name__ == "__main__":
    main()
