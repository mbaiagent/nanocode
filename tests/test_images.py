"""#110：多模态图片块、适配层透传、@path 提取、不支持视觉时可读错误。"""

import base64
import os
import tempfile
import unittest

from nanocode.images import ImageError, extract_image_refs, load_image_block
from nanocode.llm.anthropic import AnthropicProvider
from nanocode.llm.base import LLMError
from nanocode.llm.mock import MockProvider
from nanocode.llm.openai_compat import OpenAICompatProvider
from nanocode.messages import image_block, text_block, user_message


# 1x1 PNG
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class ImageHelpersTest(unittest.TestCase):
    def test_extract_and_load(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "red.png")
        with open(path, "wb") as f:
            f.write(_PNG)
        text, refs = extract_image_refs(f"这是什么颜色？ @{path}")
        self.assertEqual(text, "这是什么颜色？")
        self.assertEqual(refs, [path])
        block = load_image_block(path)
        self.assertEqual(block["type"], "image")
        self.assertEqual(block["source"]["media_type"], "image/png")
        self.assertTrue(block["source"]["data"])

    def test_reject_huge(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "big.png")
        with open(path, "wb") as f:
            f.write(b"x" * (5 * 1024 * 1024))
        with self.assertRaises(ImageError):
            load_image_block(path)


class AdapterImageTest(unittest.TestCase):
    def test_openai_emits_image_url(self):
        msg = {"role": "user", "content": [
            text_block("颜色？"),
            image_block("image/png", base64.b64encode(_PNG).decode()),
        ]}
        out = OpenAICompatProvider.to_openai_messages("", [msg])
        user = [m for m in out if m["role"] == "user"][-1]
        self.assertIsInstance(user["content"], list)
        types = [p["type"] for p in user["content"]]
        self.assertIn("text", types)
        self.assertIn("image_url", types)
        self.assertTrue(user["content"][types.index("image_url")]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_anthropic_emits_image_source(self):
        msg = {"role": "user", "content": [
            text_block("看图"),
            image_block("image/png", "aaa"),
        ]}
        out = AnthropicProvider.to_anthropic_messages([msg])
        blocks = out[0]["content"]
        img = next(b for b in blocks if b["type"] == "image")
        self.assertEqual(img["source"]["data"], "aaa")

    def test_unsupported_provider_errors_clearly(self):
        mock = MockProvider()
        self.assertFalse(mock.supports_images)
        messages = [user_message("x")]
        messages[0]["content"].append(image_block("image/png", "aaa"))
        # OpenAICompat with supports_images=False
        p = OpenAICompatProvider(model="text-only", api_key="k", base_url="http://127.0.0.1:9")
        p.supports_images = False
        with self.assertRaises(LLMError) as cm:
            p.complete("sys", messages, tools=[])
        self.assertIn("不支持图片", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
