"""远程沙箱：控制面在一台机器，沙箱在另一台（NCS_LAUNCHER=remote）。

沙箱主机上跑沙箱代理（`python -m nanocode_server sandbox-agent`，见 sandbox_agent.py），
它在本机用 Docker 启动器起停容器，并提供三样东西：
  - 管理接口（/v1/...）：起停、查询、对账，要带共用密钥 NCS_REMOTE_AGENT_SECRET；
  - 转发（/sbx/<会话>/...）：控制面的 /s/<id>/ 请求转到这里，再由代理转给容器，
    凭的是每个沙箱自己的 web_token（和本机 Docker 时一样）；
  - 网关中继：沙箱只能访问沙箱主机网桥上的一个端口，代理把它转到控制面那边的模型网关。

两台机器之间必须走内网隧道（WireGuard 等），代理只监听隧道地址。见 deploy/sandbox-host/。
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

from .base import Launcher, LaunchError, SandboxSpec


class RemoteLauncher(Launcher):
    name = "remote"
    isolated = True                 # 沙箱主机上是 Docker 启动器（非 root、只读根、internal 网络）
    remote = True                   # 沙箱在另一台机器上：网关地址以代理的中继为准，控制面自己直连网关

    def __init__(self, agent_url: str, secret: str, gateway_url: str = "", timeout: float = 15,
                 start_timeout: float = 150):
        self.agent_url = agent_url.rstrip("/")
        self.secret = secret
        self._gateway_url = gateway_url.rstrip("/")
        self.timeout = timeout
        self.start_timeout = start_timeout     # 代理那边要等容器就绪（Docker 启动器默认 60 秒）

    def _call(self, method: str, path: str, body: dict | None = None, timeout: float | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
        req = urllib.request.Request(self.agent_url + path, data=data, method=method,
                                     headers={"authorization": f"Bearer {self.secret}",
                                              "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read() or b"{}").get("error", "")
            except ValueError:
                msg = ""
            raise LaunchError(f"沙箱代理返回 HTTP {e.code}：{msg or e.reason}") from None
        except (urllib.error.URLError, OSError) as e:
            raise LaunchError(f"连不上沙箱代理 {self.agent_url}：{getattr(e, 'reason', e)}") from None
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            raise LaunchError("沙箱代理的响应不是 JSON") from None

    # ------------------------------------------------------------------ Launcher

    def start(self, spec: SandboxSpec) -> str:
        self._call("POST", f"/v1/sandboxes/{spec.session_id}/start", {
            "owner": spec.owner, "web_token": spec.web_token, "api_key": spec.api_key,
            "policy": spec.policy, "resume": spec.resume}, timeout=self.start_timeout)
        # 转发地址带路径前缀：web/proxy.py 和健康检查都在它后面拼 /api/...
        return f"{self.agent_url}/sbx/{spec.session_id}"

    def stop(self, session_id: str) -> None:
        self._call("POST", f"/v1/sandboxes/{session_id}/stop")

    def delete(self, session_id: str) -> None:
        self._call("POST", f"/v1/sandboxes/{session_id}/delete", timeout=60)

    def is_alive(self, session_id: str) -> bool:
        try:
            return bool(self._call("GET", f"/v1/sandboxes/{session_id}").get("alive"))
        except LaunchError:
            return False            # 代理连不上：当作不在了，下次访问会重新启动

    # ------------------------------------------------------------------ 快照：由沙箱代理在沙箱主机上打包 / 恢复
    # 代理只拿到某一个对象的预签名地址，对象存储的密钥留在控制面（见 services/snapshots.py）
    supports_snapshots = True

    def has_workspace(self, session_id: str) -> bool:
        return bool(self._call("GET", f"/v1/sandboxes/{session_id}/workspace").get("exists"))

    def snapshot(self, session_id: str, upload_url: str, max_bytes: int, ignore=()) -> dict:
        return self._snapshot_call("snapshot", session_id, {"url": upload_url, "max_bytes": max_bytes,
                                                            "ignore": list(ignore)})

    def restore(self, session_id: str, download_url: str, max_bytes: int) -> dict:
        return self._snapshot_call("restore", session_id, {"url": download_url, "max_bytes": max_bytes})

    def _snapshot_call(self, op: str, session_id: str, body: dict) -> dict:
        from .snapshot import SnapshotError
        try:
            data = self._call("POST", f"/v1/sandboxes/{session_id}/{op}", body, timeout=900)
        except LaunchError as e:
            # 代理用 422 表示"重试也没用"（太大、归档不合法），其它当作暂时性错误
            raise SnapshotError(str(e), permanent="HTTP 422" in str(e)) from None
        return data.get("stats") or {}

    def reconcile(self, keep: set[str]) -> None:
        try:
            self._call("POST", "/v1/reconcile", {"keep": sorted(keep)}, timeout=60)
        except LaunchError as e:
            # 代理暂时不在不该让控制面起不来；它自己重启时会按最长存活时间清理
            print(f"[nanocode] 沙箱代理对账失败：{e}", file=sys.stderr)

    def shutdown(self) -> None:
        try:
            self._call("POST", "/v1/shutdown", timeout=60)
        except LaunchError:
            pass

    def gateway_url_for_sandbox(self, port: int) -> str:
        """沙箱访问网关的地址：配了 NCS_REMOTE_GATEWAY_URL 就用它，否则问代理（它在沙箱网桥上开了中继）。"""
        if not self._gateway_url:
            try:
                url = str(self._call("GET", "/v1/info").get("gateway_url") or "")
            except LaunchError as e:
                raise LaunchError(f"{e}。可以设置 NCS_REMOTE_GATEWAY_URL 跳过这一步") from None
            if not url.startswith(("http://", "https://")):
                raise LaunchError("沙箱代理没有报告网关中继地址")
            self._gateway_url = url.rstrip("/")
        return self._gateway_url


__all__ = ["RemoteLauncher"]
