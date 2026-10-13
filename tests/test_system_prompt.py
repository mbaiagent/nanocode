"""system prompt 的 # 环境 段要写明当前运行的模型，Agent 被问到时才不会编答案。对应 issue #21。"""

import unittest

from helpers import Workspace

from nanocode.context import build_system_prompt
from nanocode.session import Options, Session


class ModelIdentityTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()

    def tearDown(self):
        self.ws.cleanup()

    def test_environment_section_names_model_provider_and_protocol(self):
        prompt = build_system_prompt(self.ws.dir, model="gemini-3.1-flash-lite", provider="custom", protocol="openai")
        self.assertIn("- 模型：gemini-3.1-flash-lite（provider=custom，protocol=openai）", prompt)
        env = prompt.split("# 环境", 1)[1]
        self.assertLess(env.index("模型："), env.index("工作目录："))   # 在 # 环境 段里，不是别处

    def test_no_model_means_no_identity_line(self):
        self.assertNotIn("- 模型：", build_system_prompt(self.ws.dir))

    def test_session_passes_its_real_provider_into_the_prompt(self):
        s = Session(Options(provider="mock", cwd=self.ws.dir, mock_delay=0), log=lambda m: None)
        try:
            prompt = s.agent.system_prompt()
            self.assertIn(f"- 模型：{s.provider.model}（provider=mock，protocol=mock）", prompt)
        finally:
            s.close()

    def test_custom_provider_reports_its_own_model(self):
        s = Session(Options(provider="custom", protocol="openai", model="gemini-3.1-flash-lite", api_key="k",
                            base_url="https://example.invalid/v1", cwd=self.ws.dir), log=lambda m: None)
        try:
            self.assertIn("- 模型：gemini-3.1-flash-lite（provider=custom，protocol=openai）", s.agent.system_prompt())
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
