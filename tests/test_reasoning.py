"""DeepSeek 思考模式：reasoning_content 要存下来，并回传给同一个端点的所有后续请求（#99）。

官方文档：带 tools 的请求必须把所有历史轮次的 reasoning_content 原样回传，否则 400；
不带 tools 时服务端会忽略它。别的厂商不认识这个字段，绝不能发给它们。
"""

import json
import os
import unittest

os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"

from helpers import FakeSSEServer, Workspace, make_agent  # noqa: E402

from nanocode.context import context_tokens, transcript_text  # noqa: E402
from nanocode.llm import AnthropicProvider, OpenAICompatProvider  # noqa: E402
from nanocode.messages import (reasoning_block, text_block, text_of, tool_result_block, tool_use_block,  # noqa: E402
                               user_message)
from nanocode.session import Options, Session  # noqa: E402

TOOLS = [{"name": "read_file", "description": "读文件", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}}]


def sse(*chunks):
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


THINK_AND_CALL = sse(
    {"choices": [{"delta": {"role": "assistant", "reasoning_content": "先读"}}]},
    {"choices": [{"delta": {"reasoning_content": "文件"}}]},
    {"choices": [{"delta": {"content": "我看看"}}]},
    {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "read_file", "arguments": "{\"path\": \"a.py\"}"}}]}}]},
    {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}})


def assistant_turns(body):
    return [m for m in body["messages"] if m["role"] == "assistant"]


class ParseTest(unittest.TestCase):
    def test_reasoning_is_stored_as_a_block_and_not_streamed_as_text(self):
        srv = FakeSSEServer(THINK_AND_CALL)
        try:
            p = OpenAICompatProvider("deepseek-flash", api_key="k", base_url=srv.url + "/v1")
            seen = []
            r = p.complete("SYS", [user_message("读 a.py")], TOOLS, on_text=seen.append)
        finally:
            srv.close()
        self.assertEqual("".join(seen), "我看看")                         # 思考过程不进 on_text
        self.assertEqual(r.content[0], reasoning_block("先读文件", p.source))
        self.assertEqual(r.content[1], text_block("我看看"))
        self.assertEqual(r.content[2]["type"], "tool_use")
        self.assertEqual(text_of({"role": "assistant", "content": r.content}), "我看看")

    def test_no_reasoning_means_no_block(self):
        srv = FakeSSEServer(sse({"choices": [{"delta": {"content": "好"}, "finish_reason": "stop"}]}))
        try:
            r = OpenAICompatProvider("m", api_key="k", base_url=srv.url + "/v1").complete("S", [user_message("hi")], TOOLS)
        finally:
            srv.close()
        self.assertEqual(r.content, [text_block("好")])

    def test_source_is_the_normalized_endpoint_plus_model(self):
        a = OpenAICompatProvider("deepseek-flash", api_key="k", base_url="HTTPS://API.DeepSeek.com/v1/")
        b = OpenAICompatProvider("deepseek-flash", api_key="k", base_url="https://api.deepseek.com/v1")
        c = OpenAICompatProvider("deepseek-flash", api_key="k", base_url="https://openrouter.ai/api/v1")
        gw_ds = OpenAICompatProvider("deepseek-flash", api_key="k", base_url="http://gateway:8081/v1")
        gw_oa = OpenAICompatProvider("gpt-x", api_key="k", base_url="http://gateway:8081/v1")
        self.assertEqual(a.source, b.source)
        self.assertNotEqual(a.source, c.source)
        self.assertNotEqual(gw_ds.source, gw_oa.source)                    # 平台网关：同一地址后面换了厂商


class PassBackTest(unittest.TestCase):
    def test_next_request_carries_reasoning_of_every_prior_assistant_turn(self):
        srv = FakeSSEServer(THINK_AND_CALL)
        try:
            p = OpenAICompatProvider("deepseek-flash", api_key="k", base_url=srv.url + "/v1")
            history = [user_message("读 a.py")]
            for _ in range(2):                                             # 两次"思考 + 调工具"
                r = p.complete("SYS", history, TOOLS)
                history.append({"role": "assistant", "content": r.content})
                history.append({"role": "user", "content": [tool_result_block("call_1", "x = 1")]})
            # 中途插一条没有思考内容的 assistant 消息（例如上下文压缩后补的确认），再开新一轮提问
            history += [{"role": "assistant", "content": [text_block("好的")]}, user_message("再读 b.py")]
            p.complete("SYS", history, TOOLS)
        finally:
            srv.close()
        self.assertNotIn("reasoning_content", json.dumps(srv.requests[0]["body"]))   # 第一轮还没有历史
        second = assistant_turns(srv.requests[1]["body"])
        self.assertEqual([m["reasoning_content"] for m in second], ["先读文件"])
        third = assistant_turns(srv.requests[2]["body"])
        self.assertEqual([m["reasoning_content"] for m in third], ["先读文件", "先读文件", ""])
        self.assertEqual(third[0]["tool_calls"][0]["id"], "call_1")
        self.assertEqual(third[0]["content"], "我看看")

    def test_other_endpoints_never_see_reasoning(self):
        """切换厂商后，DeepSeek 留下的思考内容不能发给 OpenAI / 千问 / OpenRouter 等，它们不认识这个字段。"""
        deepseek = OpenAICompatProvider("deepseek-flash", api_key="k", base_url="https://api.deepseek.com/v1")
        history = [user_message("读 a.py"),
                   {"role": "assistant", "content": [reasoning_block("先读文件", deepseek.source), text_block("我看看"),
                                                     tool_use_block("call_1", "read_file", {"path": "a.py"})]},
                   {"role": "user", "content": [tool_result_block("call_1", "x = 1")]}]
        srv = FakeSSEServer(sse({"choices": [{"delta": {"content": "好"}, "finish_reason": "stop"}]}))
        try:
            OpenAICompatProvider("gpt-x", api_key="k", base_url=srv.url + "/v1").complete("SYS", history, TOOLS)
        finally:
            srv.close()
        self.assertNotIn("reasoning_content", json.dumps(srv.requests[0]["body"]))
        self.assertNotIn("先读文件", json.dumps(srv.requests[0]["body"], ensure_ascii=False))
        # 不传 reasoning_source（旧调用方式）也和以前完全一样
        self.assertNotIn("reasoning_content", json.dumps(OpenAICompatProvider.to_openai_messages("S", history)))
        # 同一个网关地址、换了模型（多用户平台里换成别家）：也不带
        gw = OpenAICompatProvider("gpt-x", api_key="k", base_url="https://api.deepseek.com/v1")
        self.assertNotIn("reasoning_content", json.dumps(OpenAICompatProvider.to_openai_messages("S", history, gw.source)))
        # 换回 DeepSeek：又能带上
        wire = OpenAICompatProvider.to_openai_messages("S", history, deepseek.source)
        self.assertEqual(wire[2]["reasoning_content"], "先读文件")

    def test_anthropic_never_receives_reasoning(self):
        block = reasoning_block("想", "openai:https://api.deepseek.com/v1#deepseek-flash")
        out = AnthropicProvider.to_anthropic_messages([
            {"role": "assistant", "content": [block, text_block("答"), tool_use_block("c1", "read_file", {})]},
            {"role": "assistant", "content": [block]}])
        self.assertEqual([b["type"] for b in out[0]["content"]], ["text", "tool_use"])
        self.assertEqual(out[1]["content"], [{"type": "text", "text": "(空)"}])
        self.assertNotIn("reasoning", json.dumps(out))


class AgentAndSessionTest(unittest.TestCase):
    SRC = "openai:https://api.deepseek.com/v1#deepseek-flash"

    def test_agent_keeps_reasoning_and_session_round_trip_preserves_it(self):
        ws = Workspace()
        ws.write("a.py", "x = 1\n")
        agent, _, _ = make_agent(ws, [[reasoning_block("先读", self.SRC), tool_use_block("t1", "read_file", {"path": "a.py"})],
                                      [reasoning_block("读完了", self.SRC), text_block("好了")]])
        final = agent.run("读 a.py")
        self.assertEqual(final, "好了")                                    # 最终回复不含思考过程
        s = Session(Options(provider="mock", cwd=ws.dir, mock_delay=0), log=lambda m: None)
        try:
            s.agent.messages = agent.messages
            s.save()
            sid = s.session_id
        finally:
            s.close()
        s2 = Session(Options(provider="mock", cwd=ws.dir, mock_delay=0), log=lambda m: None)   # 从磁盘读回来
        try:
            s2.resume(sid)
            restored = s2.agent.messages
        finally:
            s2.close()
        self.assertIsNot(restored, agent.messages)
        wire = OpenAICompatProvider.to_openai_messages("S", restored, self.SRC)
        self.assertEqual([m["reasoning_content"] for m in wire if m["role"] == "assistant"], ["先读", "读完了"])

    def test_reasoning_only_reply_is_still_an_empty_reply(self):
        ws = Workspace()
        agent, _, _ = make_agent(ws, [[reasoning_block("想了想", self.SRC)]])
        agent.run("hi")
        self.assertEqual(text_of(agent.messages[-1]), "(空回复)")

    def test_compaction_summary_ignores_reasoning_but_token_estimate_counts_it(self):
        plain = [user_message("hi"), {"role": "assistant", "content": [text_block("答")]}]
        thought = [user_message("hi"), {"role": "assistant", "content": [reasoning_block("思" * 500, self.SRC), text_block("答")]}]
        self.assertEqual(transcript_text(thought), transcript_text(plain))
        self.assertGreater(context_tokens("S", thought, TOOLS), context_tokens("S", plain, TOOLS) + 400)


if __name__ == "__main__":
    unittest.main()
