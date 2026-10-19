"""模型目录：一个用户能用哪些模型，以及一个会话选的模型最终转发到哪里。

三种来源：
    mock          演示模型（nanocode 的 MockProvider，在沙箱里本地运行，不需要 Key）
    p:<模型>       平台提供的模型，平台付费，受每日额度限制（NCS_PLATFORM_*）
    c:<id>:<模型>  用户自己添加的 API Key（加密保存，只有网关解密使用，不进沙箱）
"""

from __future__ import annotations

import re

from nanocode.llm import PRESETS

from ..config import Config
from ..models import Credential, ModelOption, Upstream
from ..security.secretbox import SecretBox, hint
from ..storage import Store
from .upstream import UpstreamError, check_base_url

MOCK_REF = "mock"
MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,127}$")
PROVIDER_LABELS = {
    "deepseek": "DeepSeek", "openai": "OpenAI", "anthropic": "Anthropic", "qwen": "通义千问", "kimi": "Kimi",
    "glm": "智谱 GLM", "siliconflow": "硅基流动", "openrouter": "OpenRouter", "ollama": "Ollama（本机）",
    "custom": "自定义（OpenAI / Anthropic 兼容）",
}


class CatalogError(ValueError):
    pass


class ModelCatalog:
    def __init__(self, cfg: Config, store: Store, box: SecretBox):
        self.cfg = cfg
        self.store = store
        self.box = box

    # ------------------------------------------------------------------ 可选的厂商（添加 Key 的表单）

    def providers(self) -> list[dict]:
        out = []
        for name, p in PRESETS.items():
            if name == "mock" or (name == "ollama" and not self.cfg.allow_private_upstreams):
                continue
            out.append({"id": name, "label": PROVIDER_LABELS.get(name, name), "protocol": p["protocol"],
                        "base_url": p["base_url"]})
        out.append({"id": "custom", "label": PROVIDER_LABELS["custom"], "protocol": "openai", "base_url": ""})
        return out

    # ------------------------------------------------------------------ 一个用户能用的模型

    def options(self, owner: str) -> list[ModelOption]:
        opts = []
        if self.cfg.enable_mock_model:
            opts.append(ModelOption(MOCK_REF, "mock-demo", "演示模型（不需要 Key）", "演示", "mock"))
        if self.cfg.has_platform_models:
            for m in self.cfg.platform_models:
                opts.append(ModelOption(f"p:{m}", m, m, self.cfg.platform_label, "platform",
                                         self.cfg.platform_protocol, billable=True))
        for c in self.store.list_credentials(owner):
            for m in c.models:
                opts.append(ModelOption(f"c:{c.id}:{m}", m, m, c.label, c.provider, c.protocol))
        return opts

    def option(self, owner: str, ref: str) -> ModelOption:
        for o in self.options(owner):
            if o.ref == ref:
                return o
        raise CatalogError("这个模型不存在，或者你没有权限使用")

    def default_ref(self, owner: str) -> str:
        opts = self.options(owner)
        if not opts:
            raise CatalogError("没有可用的模型：请先在设置里添加一个 API Key")
        mine = [o for o in opts if o.ref.startswith("c:")]
        platform = [o for o in opts if o.ref.startswith("p:")]
        return (mine or platform or opts)[0].ref

    # ------------------------------------------------------------------ 网关：会话选的模型转发到哪里

    def resolve(self, owner: str, ref: str) -> Upstream:
        if ref.startswith("p:") and self.cfg.has_platform_models and ref[2:] in self.cfg.platform_models:
            return Upstream(self.cfg.platform_protocol, self.cfg.platform_base_url, self.cfg.platform_api_key,
                            ref[2:], billable=True)
        if ref.startswith("c:"):
            _, cid, model = ref.split(":", 2)
            c = self.store.get_credential(cid)
            if c and c.owner == owner and model in c.models:
                return Upstream(c.protocol, c.base_url, self.box.decrypt(c.secret, aad=c.owner.encode()), model, billable=False)
        raise CatalogError("会话选的模型已经不可用了（Key 被删除？），请换一个模型")

    # ------------------------------------------------------------------ 用户的 Key

    def add_credential(self, owner: str, provider: str, api_key: str, models: list[str], label: str = "",
                       base_url: str = "", protocol: str = "") -> Credential:
        api_key = (api_key or "").strip()
        if not api_key or len(api_key) > 500 or any(c.isspace() for c in api_key):
            raise CatalogError("API Key 不能为空、不能有空白字符")
        models = [m.strip() for m in models if m and m.strip()]
        if not models or len(models) > 20:
            raise CatalogError("请填写 1 到 20 个模型名")
        bad = [m for m in models if not MODEL_NAME.match(m)]
        if bad:
            raise CatalogError(f"模型名格式不对：{', '.join(bad)}")
        if provider == "custom":
            protocol = protocol or "openai"
            if protocol not in ("openai", "anthropic"):
                raise CatalogError("协议只能是 openai 或 anthropic")
            base_url = (base_url or "").strip().rstrip("/")
        elif provider in PRESETS and provider != "mock":
            if provider == "ollama" and not self.cfg.allow_private_upstreams:
                raise CatalogError("当前部署不允许使用本机模型")
            protocol, base_url = PRESETS[provider]["protocol"], PRESETS[provider]["base_url"]   # 预设的地址不让改
        else:
            raise CatalogError(f"不支持的厂商：{provider}")
        try:
            check_base_url(base_url, self.cfg.allow_private_upstreams)
        except UpstreamError as e:
            raise CatalogError(str(e)) from None
        cred = Credential(id="", owner=owner, provider=provider,
                          label=(label or "").strip()[:40] or PROVIDER_LABELS.get(provider, provider),
                          protocol=protocol, base_url=base_url, models=list(dict.fromkeys(models)),
                          secret=self.box.encrypt(api_key, aad=owner.encode()), key_hint=hint(api_key))
        return self.store.add_credential(cred)

    def draft_upstream(self, provider: str, api_key: str, model: str, base_url: str = "", protocol: str = "") -> Upstream:
        """还没保存的 Key，用来"测试连接"。"""
        if provider in PRESETS and provider not in ("mock", "custom"):
            protocol, base_url = PRESETS[provider]["protocol"], PRESETS[provider]["base_url"]
        protocol = protocol or "openai"            # 和 add_credential 一致：自定义厂商默认 OpenAI 兼容协议
        if protocol not in ("openai", "anthropic") or not base_url or not api_key or not MODEL_NAME.match(model or ""):
            raise CatalogError("请填写完整的厂商、地址、Key 和一个模型名")
        return Upstream(protocol, base_url.rstrip("/"), api_key.strip(), model, billable=False)


__all__ = ["ModelCatalog", "CatalogError", "MOCK_REF", "PROVIDER_LABELS"]
