"""思考 token 也算在 max_tokens 里：预算被思考用光、没有正文时，加大预算重试一次（#102）。"""

import json
import os
import unittest
from unittest import mock

os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"

from helpers import FakeSSEServer, Workspace  # noqa: E402

from nanocode.agent import Agent  # noqa: E402
from nanocode.events import EventBus  # noqa: E402
from nanocode.hooks import HookRunner  # noqa: E402
from nanocode.llm import OpenAICompatProvider  # noqa: E402
from nanocode.messages import Usage, text_of  # noqa: E402
from nanocode.permissions import PermissionManager  # noqa: E402
from nanocode.tools import ToolRegistry, default_tools  # noqa: E402


def sse(*chunks):
    return "".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks) + "data: [DONE]\n\n"


def burned(n):
    """只有思考、被 max_tokens 截断。"""
    return sse({"choices": [{"delta": {"reasoning_content": "想" * 20}}]},
               {"choices": [{"delta": {}, "finish_reason": "length"}]},
               {"choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": n,
                                         "completion_tokens_details": {"reasoning_tokens": n}}})


ANSWER = sse({"choices": [{"delta": {"reasoning_content": "想好了"}}]},
             {"choices": [{"delta": {"content": "答案是 42"}, "finish_reason": "stop"}]},
             {"choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": 30,
                                       "completion_tokens_details": {"reasoning_tokens": 20}}})


class _Case(unittest.TestCase):
    def run_agent(self, streams, max_tokens=100):
        srv = FakeSSEServer(streams)
        self.addCleanup(srv.close)
        ws = Workspace()
        self.addCleanup(ws.cleanup)
        bus = EventBus()
        events = []
        bus.subscribe(events.append)
        provider = OpenAICompatProvider("deepseek-v4-pro", api_key="k", base_url=srv.url + "/v1")
        agent = Agent(provider, ToolRegistry(default_tools()), PermissionManager(ws.dir, mode="bypass"),
                      HookRunner(None, ws.dir), bus, ws.dir, system_prompt=lambda: "SYS", max_tokens=max_tokens)
        final = agent.run("问个难题")
        return final, agent, srv, events


class RetryTest(_Case):
    def test_reasoning_only_truncation_is_retried_with_double_budget(self):
        final, agent, srv, events = self.run_agent([burned(100), ANSWER])
        self.assertEqual(final, "答案是 42")
        self.assertEqual([r["body"]["max_tokens"] for r in srv.requests], [100, 200])
        warnings = [e["message"] for e in events if e["type"] == "warning"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("200", warnings[0])
        # 失败的那次不进历史：只有一条 assistant 消息
        self.assertEqual(sum(1 for m in agent.messages if m["role"] == "assistant"), 1)
        # 两次的用量都计入
        self.assertEqual(agent.usage.output_tokens, 130)
        self.assertEqual(agent.usage.reasoning_tokens, 120)

    def test_second_truncation_surfaces_a_clear_message(self):
        final, agent, srv, _ = self.run_agent([burned(100), burned(200)])
        self.assertEqual(len(srv.requests), 2)                      # 只重试一次
        self.assertIn("全部用在了思考上", final)
        self.assertIn("max_tokens（200）", final)
        self.assertIn("NANOCODE_MAX_TOKENS_CEILING", final)
        self.assertNotIn("(空回复)", final)
        self.assertNotIn("被截断", final)                           # 不重复提示
        self.assertEqual(text_of(agent.messages[-1]), final)

    def test_ceiling_caps_the_retry_budget(self):
        with mock.patch.dict(os.environ, {"NANOCODE_MAX_TOKENS_CEILING": "150"}):
            _, _, srv, _ = self.run_agent([burned(100), ANSWER])
        self.assertEqual([r["body"]["max_tokens"] for r in srv.requests], [100, 150])

    def test_no_retry_when_ceiling_is_not_above_the_budget(self):
        with mock.patch.dict(os.environ, {"NANOCODE_MAX_TOKENS_CEILING": "100"}):
            final, agent, srv, _ = self.run_agent([burned(100), ANSWER])
        self.assertEqual(len(srv.requests), 1)
        self.assertIn("全部用在了思考上", final)
        self.assertEqual(agent.usage.output_tokens, 100)            # 不重复计数

    def test_truncation_with_text_is_not_retried(self):
        partial = sse({"choices": [{"delta": {"content": "写了一半"}, "finish_reason": "length"}]})
        final, _, srv, _ = self.run_agent([partial, ANSWER])
        self.assertEqual(len(srv.requests), 1)
        self.assertIn("写了一半", final)
        self.assertIn("被截断", final)

    def test_non_thinking_provider_empty_truncation_is_not_retried(self):
        """不思考的厂商（没有 reasoning、没有 reasoning_tokens）：行为和以前一样，不多发请求、不加大 max_tokens。"""
        empty = sse({"choices": [{"delta": {}, "finish_reason": "length"}]},
                    {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 100}})
        final, _, srv, _ = self.run_agent([empty, ANSWER])
        self.assertEqual(len(srv.requests), 1)
        self.assertIn("(空回复)", final)


class UsageTest(unittest.TestCase):
    def test_reasoning_tokens_are_parsed(self):
        srv = FakeSSEServer(ANSWER)
        try:
            r = OpenAICompatProvider("m", api_key="k", base_url=srv.url + "/v1").complete("S", [], [])
        finally:
            srv.close()
        self.assertEqual((r.usage.output_tokens, r.usage.reasoning_tokens), (30, 20))
        self.assertEqual(r.usage.to_dict()["reasoning_tokens"], 20)

    def test_other_providers_usage_dict_is_unchanged(self):
        srv = FakeSSEServer(sse({"choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}]},
                                {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 1,
                                                          "completion_tokens_details": None}}))
        try:
            r = OpenAICompatProvider("m", api_key="k", base_url=srv.url + "/v1").complete("S", [], [])
        finally:
            srv.close()
        self.assertEqual(r.usage.to_dict(), {"input_tokens": 3, "output_tokens": 1, "estimated": False})

    def test_round_trip(self):
        u = Usage(1, 2, False, 3)
        self.assertEqual(Usage.from_dict(u.to_dict()), u)
        self.assertEqual(Usage.from_dict({"input_tokens": 1, "output_tokens": 2}).reasoning_tokens, 0)


if __name__ == "__main__":
    unittest.main()
