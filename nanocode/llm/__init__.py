"""LLM 适配层：厂商预设 + 工厂函数。

用户只需告诉 nanocode "用哪家、哪个模型"，这里负责找到对应的
协议、地址和 API Key 环境变量。新增一家厂商通常只要在 PRESETS 里加一行。
"""

from __future__ import annotations

import os

from .anthropic import AnthropicProvider
from .base import LLMError, Provider
from .mock import MockProvider
from .openai_compat import OpenAICompatProvider

# 预设：协议、默认地址、读取 Key 的环境变量。
# 模型名经常更新，请以各家官方文档为准，用 --model 或 NANOCODE_MODEL 指定。
PRESETS: dict[str, dict] = {
    "anthropic":   {"protocol": "anthropic", "base_url": "https://api.anthropic.com",                         "key_env": "ANTHROPIC_API_KEY"},
    "openai":      {"protocol": "openai",    "base_url": "https://api.openai.com/v1",                         "key_env": "OPENAI_API_KEY"},
    "deepseek":    {"protocol": "openai",    "base_url": "https://api.deepseek.com/v1",                       "key_env": "DEEPSEEK_API_KEY"},
    "qwen":        {"protocol": "openai",    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "key_env": "DASHSCOPE_API_KEY"},
    "kimi":        {"protocol": "openai",    "base_url": "https://api.moonshot.cn/v1",                        "key_env": "MOONSHOT_API_KEY"},
    "glm":         {"protocol": "openai",    "base_url": "https://open.bigmodel.cn/api/paas/v4",              "key_env": "ZHIPUAI_API_KEY"},
    "siliconflow": {"protocol": "openai",    "base_url": "https://api.siliconflow.cn/v1",                     "key_env": "SILICONFLOW_API_KEY"},
    "openrouter":  {"protocol": "openai",    "base_url": "https://openrouter.ai/api/v1",                      "key_env": "OPENROUTER_API_KEY"},
    "ollama":      {"protocol": "openai",    "base_url": "http://localhost:11434/v1",                         "key_env": ""},
    "mock":        {"protocol": "mock",      "base_url": "",                                                  "key_env": ""},
}


def make_provider(provider: str, model: str = "", base_url: str = "", api_key: str = "",
                  protocol: str = "", **options) -> Provider:
    """根据配置创建 provider。

    provider 可以是预设名（deepseek、qwen……），也可以是 "custom"：
    此时必须给出 protocol（openai / anthropic）和 base_url，适合任何没在预设里的厂商或中转服务。
    """
    preset = PRESETS.get(provider, {})
    protocol = protocol or preset.get("protocol") or "openai"
    base_url = base_url or preset.get("base_url", "")
    if not api_key:
        key_env = preset.get("key_env")
        api_key = (os.environ.get(key_env, "") if key_env else "") or os.environ.get("NANOCODE_API_KEY", "")

    if protocol == "mock":
        return MockProvider(model or "mock-demo", **{k: v for k, v in options.items() if k == "delay"})
    if not model:
        raise LLMError(f"请指定模型名：--model 或环境变量 NANOCODE_MODEL（provider={provider}）")
    if not base_url:
        raise LLMError("自定义 provider 需要 --base-url")
    if not api_key and provider != "ollama":
        hint = preset.get("key_env") or "NANOCODE_API_KEY"
        raise LLMError(f"没有找到 API Key，请设置环境变量 {hint}（或 NANOCODE_API_KEY）")
    common = dict(model=model, api_key=api_key, base_url=base_url,
                  timeout=options.get("timeout", 300.0), extra_headers=options.get("extra_headers"))
    if protocol == "anthropic":
        return AnthropicProvider(**common, prompt_cache=options.get("prompt_cache", True))
    return OpenAICompatProvider(**common, stream_usage=options.get("stream_usage", True))


__all__ = ["PRESETS", "make_provider", "Provider", "LLMError",
           "AnthropicProvider", "OpenAICompatProvider", "MockProvider"]
