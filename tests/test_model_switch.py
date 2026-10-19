"""#100：/model 运行时切换模型。"""

import os
import tempfile
import unittest
from unittest import mock

from helpers import Workspace

from nanocode.messages import Usage
from nanocode.session import Options, Session


class ModelSwitchTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.home = tempfile.mkdtemp()
        # 保证 key 缺失测试不受外部环境变量干扰
        env = {k: v for k, v in os.environ.items()
               if k not in ("DEEPSEEK_API_KEY", "NANOCODE_API_KEY", "OPENAI_API_KEY")}
        env["NANOCODE_HOME"] = self.home
        self.env = mock.patch.dict(os.environ, env, clear=True)
        self.env.start()
        self.sessions = []

    def tearDown(self):
        for s in self.sessions:
            s.close()
        self.env.stop()
        self.ws.cleanup()

    def session(self, **opts):
        s = Session(Options(cwd=self.ws.dir, mock_delay=0, provider="mock", **opts),
                    log=lambda m: None)
        self.sessions.append(s)
        return s

    def test_model_shows_current(self):
        s = self.session()
        out, _ = s.handle_command("/model")
        self.assertIn("mock", out)
        self.assertIn(s.provider.model, out)

    def test_model_switch_keeps_provider_settings(self):
        s = self.session()
        old_base, old_key = s.provider.base_url, s.provider.api_key
        out, _ = s.handle_command("/model another-model")
        self.assertIn("已切换到", out)
        self.assertEqual(s.provider.model, "another-model")
        self.assertEqual(s.provider_name, "mock")
        self.assertEqual(s.provider.base_url, old_base)
        self.assertEqual(s.provider.api_key, old_key)
        # Agent 持有的引用也要一起换，否则不生效
        self.assertIs(s.agent.provider, s.provider)

    def test_model_switch_resets_usage(self):
        s = self.session()
        s.agent.usage = Usage(100, 50)
        s.handle_command("/model another-model")
        self.assertEqual(s.agent.usage.input_tokens, 0)
        self.assertEqual(s.agent.usage.output_tokens, 0)

    def test_unknown_provider_keeps_old(self):
        s = self.session()
        old = s.provider
        out, _ = s.handle_command("/model nosuchprovider x")
        self.assertIn("未知的 provider", out)
        self.assertIs(s.provider, old)

    def test_missing_key_keeps_old(self):
        """切到 deepseek 但没配 key：构造失败，原 provider 不能被破坏。"""
        s = self.session()
        old = s.provider
        out, _ = s.handle_command("/model deepseek deepseek-chat")
        self.assertIn("切换失败", out)
        self.assertIs(s.provider, old)
        self.assertEqual(s.provider_name, "mock")

    def test_switch_provider_without_key_needed(self):
        """ollama 预设不要 key，可以直接切过去。"""
        s = self.session()
        out, _ = s.handle_command("/model ollama llama3")
        self.assertIn("已切换到", out)
        self.assertEqual(s.provider_name, "ollama")
        self.assertEqual(s.provider.model, "llama3")
        self.assertIn("11434", s.provider.base_url)

    def test_policy_locked_refuses(self):
        s = self.session()
        s.policy = {"provider": "mock", "model": "locked"}
        old = s.provider
        out, _ = s.handle_command("/model another-model")
        self.assertIn("策略", out)
        self.assertIs(s.provider, old)

    def test_bad_usage(self):
        s = self.session()
        out, _ = s.handle_command("/model a b c")
        self.assertIn("用法", out)

    def test_help_lists_model(self):
        s = self.session()
        out, _ = s.handle_command("/help")
        self.assertIn("/model", out)


if __name__ == "__main__":
    unittest.main()
