"""Provider 测试：用假的 SSE 服务器验证"统一格式 ⇄ 厂商格式"的双向翻译。"""

import json
import os
import unittest

os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"

from helpers import FakeSSEServer  # noqa: E402

from nanocode.llm import AnthropicProvider, LLMError, MockProvider, OpenAICompatProvider, make_provider  # noqa: E402
from nanocode.llm.base import iter_sse  # noqa: E402
from nanocode.messages import tool_result_block, tool_use_block, user_message  # noqa: E402

HISTORY = [
    user_message("读一下 a.py"),
    {"role": "assistant", "content": [{"type": "text", "text": "好的"}, tool_use_block("c1", "read_file", {"path": "a.py"})]},
    {"role": "user", "content": [tool_result_block("c1", "1\tprint(1)")]},
]
TOOLS = [{"name": "read_file", "description": "读文件", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}}]


def sse(*events, done=False):
    out = ""
    for ev in events:
        if isinstance(ev, tuple):
            out += f"event: {ev[0]}\ndata: {json.dumps(ev[1])}\n\n"
        else:
            out += f"data: {json.dumps(ev)}\n\n"
    return out + ("data: [DONE]\n\n" if done else "")


class TestSSE(unittest.TestCase):
    def test_parse(self):
        lines = [b"event: a\n", b"data: {\"x\": 1}\n", b"\n", b": ping\n", b"data: [DONE]\n", b"\n"]
        self.assertEqual(list(iter_sse(lines)), [("a", '{"x": 1}'), ("message", "[DONE]")])


class TestOpenAI(unittest.TestCase):
    def test_translate_request(self):
        msgs = OpenAICompatProvider.to_openai_messages("SYS", HISTORY)
        self.assertEqual([m["role"] for m in msgs], ["system", "user", "assistant", "tool"])
        self.assertEqual(msgs[2]["tool_calls"][0]["function"]["arguments"], '{"path": "a.py"}')
        self.assertEqual(msgs[3]["tool_call_id"], "c1")

    def test_stream_with_tool_calls(self):
        stream = sse(
            {"choices": [{"delta": {"content": "我来"}}]},
            {"choices": [{"delta": {"content": "看看"}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "read_file", "arguments": "{\"pa"}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "th\": \"b.py\"}"}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 20}},
            done=True)
        srv = FakeSSEServer(stream)
        try:
            p = OpenAICompatProvider("m", api_key="k", base_url=srv.url + "/v1")
            seen = []
            r = p.complete("SYS", HISTORY, TOOLS, on_text=seen.append)
        finally:
            srv.close()
        self.assertEqual("".join(seen), "我来看看")
        self.assertEqual(r.stop_reason, "tool_use")
        self.assertEqual(r.content[1], {"type": "tool_use", "id": "call_1", "name": "read_file", "input": {"path": "b.py"}})
        self.assertEqual((r.usage.input_tokens, r.usage.output_tokens), (100, 20))
        req = srv.requests[0]
        self.assertEqual(req["path"], "/v1/chat/completions")
        headers = {k.lower(): v for k, v in req["headers"].items()}
        self.assertEqual(headers["authorization"], "Bearer k")
        self.assertEqual(req["body"]["tools"][0]["function"]["name"], "read_file")

    def test_http_error(self):
        srv = FakeSSEServer('{"error": "bad key"}', status=401)
        try:
            p = OpenAICompatProvider("m", api_key="k", base_url=srv.url)
            with self.assertRaises(LLMError) as cm:
                p.complete("", HISTORY, [])
        finally:
            srv.close()
        self.assertEqual(cm.exception.status, 401)


class TestAnthropic(unittest.TestCase):
    def test_stream_with_tool_use(self):
        stream = sse(
            ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 50, "cache_read_input_tokens": 30}}}),
            ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "先读"}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("content_block_start", {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {}}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{\"path\":"}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": " \"c.py\"}"}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 1}),
            ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 12}}),
            ("message_stop", {"type": "message_stop"}))
        srv = FakeSSEServer(stream)
        try:
            p = AnthropicProvider("claude-x", api_key="k", base_url=srv.url)
            r = p.complete("SYS", HISTORY, TOOLS)
        finally:
            srv.close()
        self.assertEqual(r.content[0], {"type": "text", "text": "先读"})
        self.assertEqual(r.content[1]["input"], {"path": "c.py"})
        self.assertEqual(r.stop_reason, "tool_use")
        self.assertEqual((r.usage.input_tokens, r.usage.output_tokens), (80, 12))
        body = srv.requests[0]["body"]
        self.assertEqual(srv.requests[0]["path"], "/v1/messages")
        self.assertEqual(body["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(body["messages"][2]["content"][0]["tool_use_id"], "c1")

    def run_stream(self, *events):
        srv = FakeSSEServer(sse(*events))
        try:
            return AnthropicProvider("claude-x", api_key="k", base_url=srv.url).complete("SYS", HISTORY, TOOLS)
        finally:
            srv.close()

    def test_explicit_null_usage_fields_do_not_crash(self):
        """OpenRouter 等兼容端点会显式返回 "cache_read_input_tokens": null（#18）。dict.get 的默认值只对"键缺失"生效。"""
        r = self.run_stream(
            ("message_start", {"type": "message_start", "message": {"usage": {
                "input_tokens": 7, "cache_read_input_tokens": None, "cache_creation_input_tokens": None, "output_tokens": None}}}),
            ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": None}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "好"}}),
            ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": None}}),
            ("message_stop", {"type": "message_stop"}))
        self.assertEqual(r.content[0], {"type": "text", "text": "好"})
        self.assertEqual((r.usage.input_tokens, r.usage.output_tokens), (7, 0))     # 不能是 None：后面要累加

    def test_whole_usage_object_null(self):
        r = self.run_stream(
            ("message_start", {"type": "message_start", "message": {"usage": None}}),
            ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": None}),
            ("message_stop", {"type": "message_stop"}))
        self.assertEqual((r.usage.input_tokens, r.usage.output_tokens), (0, 0))


class TestFactory(unittest.TestCase):
    def test_presets(self):
        self.assertIsInstance(make_provider("mock"), MockProvider)
        p = make_provider("deepseek", model="deepseek-chat", api_key="x")
        self.assertIsInstance(p, OpenAICompatProvider)
        self.assertIn("deepseek", p.base_url)
        p = make_provider("custom", protocol="anthropic", base_url="https://relay.example.com", model="m", api_key="x")
        self.assertIsInstance(p, AnthropicProvider)
        with self.assertRaises(LLMError):
            make_provider("openai", api_key="x")  # 没有模型名


if __name__ == "__main__":
    unittest.main()
