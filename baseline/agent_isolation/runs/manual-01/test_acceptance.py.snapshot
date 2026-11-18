"""Offline acceptance of default Agent isolation and custom-tool compatibility."""
import os
from pathlib import Path
import sys
import threading
import unittest

ROOT = Path(os.environ.get("CORECODER_TARGET", Path(__file__).resolve().parents[2])).resolve()
sys.path.insert(0, str(ROOT))
from corecoder.agent import Agent
from corecoder.tools import ALL_TOOLS
from corecoder.tools.agent import AgentTool
from corecoder.tools.base import Tool
from corecoder.tools.todo import TodoWriteTool


class OfflineModel:
    def chat(self, *args, **kwargs):
        raise AssertionError("These tests must never call a model")


class CustomTool(Tool):
    name = "custom"
    description = "custom tool retaining caller-owned resources"
    parameters = {"type": "object", "properties": {}}

    def __init__(self):
        self.lock = threading.Lock()

    def execute(self):
        return "custom-ok"


def todo(agent):
    return next(t for t in agent.tools if t.name == "todo_write")


def worker(agent):
    return next(t for t in agent.tools if t.name == "agent")


def set_task(agent, content):
    return todo(agent).execute([{"content": content, "status": "pending"}])


class AgentIsolationAcceptance(unittest.TestCase):
    def setUp(self):
        # Original code mutates registry singletons; restore them after each
        # test so failures cannot contaminate subsequent tests.
        previous_tools = list(ALL_TOOLS)
        previous_state = [(tool, tool.__dict__.copy()) for tool in previous_tools]

        def restore():
            ALL_TOOLS[:] = previous_tools
            for tool, state in previous_state:
                tool.__dict__.clear()
                tool.__dict__.update(state)

        self.addCleanup(restore)

    def agent(self, **kwargs):
        return Agent(llm=OfflineModel(), **kwargs)

    def test_new_agent_has_empty_todo(self):
        a = self.agent()
        set_task(a, "A-private")
        b = self.agent()
        self.assertEqual(todo(b).render(), "")
        self.assertIn("A-private", todo(a).render())

    def test_updating_b_does_not_replace_a_todo(self):
        a, b = self.agent(), self.agent()
        set_task(a, "A-private")
        set_task(b, "B-private")
        self.assertIn("A-private", todo(a).render())
        self.assertNotIn("B-private", todo(a).render())

    def test_clearing_a_does_not_clear_b(self):
        a, b = self.agent(), self.agent()
        set_task(b, "B-private")
        todo(a).execute([])
        self.assertIn("B-private", todo(b).render())

    def test_prompt_does_not_leak_other_agents_todo(self):
        a, b = self.agent(), self.agent()
        set_task(a, "UNIQUE-A-ONLY-452")
        self.assertNotIn("UNIQUE-A-ONLY-452", b._full_messages()[0]["content"])

    def test_worker_ownership_survives_second_agent(self):
        a, b = self.agent(), self.agent()
        self.assertIs(worker(a)._parent_agent, a)
        self.assertIs(worker(b)._parent_agent, b)

    def test_child_uses_actual_parent_configuration(self):
        permission_a, permission_b = object(), object()
        hooks_a, hooks_b = object(), object()
        a = self.agent(permission=permission_a, hooks=hooks_a, max_context_tokens=24000)
        b = self.agent(permission=permission_b, hooks=hooks_b, max_context_tokens=48000)
        child = worker(a)._new_subagent()
        self.assertIs(child.llm, a.llm)
        self.assertIsNot(child.llm, b.llm)
        self.assertIs(child.permission, permission_a)
        self.assertIs(child.hooks, hooks_a)
        self.assertEqual(child.context.max_tokens, 24000)

    def test_child_todo_is_empty_and_independent(self):
        a = self.agent()
        set_task(a, "parent-only")
        child = worker(a)._new_subagent()
        self.assertEqual(todo(child).render(), "")
        set_task(child, "child-only")
        self.assertIn("parent-only", todo(a).render())
        self.assertNotIn("child-only", todo(a).render())

    def test_child_cannot_recursively_spawn(self):
        a = self.agent()
        child = worker(a)._new_subagent()
        self.assertNotIn("agent", [tool.name for tool in child.tools])

    def test_message_histories_remain_separate(self):
        a, b = self.agent(), self.agent()
        a.messages.append({"role": "user", "content": "private"})
        self.assertEqual(b.messages, [])

    def test_default_tool_schemas_remain_compatible(self):
        a = self.agent()
        self.assertEqual([t.schema() for t in a.tools], [t.schema() for t in ALL_TOOLS])

    def test_explicit_empty_tools_stays_empty(self):
        a = self.agent(tools=[])
        self.assertEqual(a.tools, [])
        self.assertIsNone(a._todo)

    def test_explicit_custom_tools_keep_caller_owned_objects(self):
        custom, custom_todo, custom_worker = CustomTool(), TodoWriteTool(), AgentTool()
        a = self.agent(tools=[custom, custom_todo, custom_worker])
        self.assertIs(a.tools[0], custom)
        self.assertIs(todo(a), custom_todo)
        self.assertIs(worker(a), custom_worker)
        self.assertEqual(custom.execute(), "custom-ok")
        self.assertIs(custom_worker._parent_agent, a)


if __name__ == "__main__":
    unittest.main()
