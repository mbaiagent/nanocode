"""会话生命周期：创建、启动沙箱、切换模型、停止、删除、空闲回收。

一个会话 = 数据库里的一条记录 + 运行时的一个沙箱（可能没在运行）。沙箱随用随启：
访问一个已停止的会话时自动启动，并恢复上次的对话；一段时间不用就回收，工作区保留。
开了对象存储（NCS_OBJECT_STORE=s3）时，停止 / 回收 / 删除后工作区会在后台存一份快照，
本机工作区没了时启动前自动恢复（services/snapshots.py）。
"""

from __future__ import annotations

import json
import secrets
import threading
import time
import urllib.error
import urllib.request

from ..config import Config
from ..models import ModelOption, Session
from ..runtime import Launcher, LaunchError, SandboxSpec
from ..security.tokens import Signer, web_token_digest
from ..storage import Store
from .catalog import MOCK_REF, CatalogError, ModelCatalog

# 用户和项目配置里一律删掉的键：能执行命令的、能放宽权限的、能改请求去向的（nanocode/policy.py 的 forbid）
FORBIDDEN_SETTINGS = ["hooks", "mcpServers", "verify", "permissions.allow", "headers", "rag"]


class SessionError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def build_policy(cfg: Config, option: ModelOption, gateway_url: str) -> dict:
    """一个会话的平台策略：模型去向锁死在网关（或本地的演示模型），危险配置全部删掉。"""
    policy = {"mode": cfg.sandbox_mode, "allowed_modes": list(cfg.sandbox_modes), "forbid": FORBIDDEN_SETTINGS}
    if cfg.sandbox_mcp_servers:
        # forbid 先删掉用户和项目配的 mcpServers，再合并这里的：沙箱里只跑管理员认可的 MCP 服务器
        policy["mcpServers"] = cfg.sandbox_mcp_servers
    if option.ref == MOCK_REF:
        return {**policy, "provider": "mock", "model": option.model}
    base_url = gateway_url + ("/v1" if option.protocol == "openai" else "")    # Anthropic 客户端自己拼 /v1/messages
    return {**policy, "provider": "custom", "protocol": option.protocol, "base_url": base_url, "model": option.model}


class SessionManager:
    def __init__(self, cfg: Config, store: Store, launcher: Launcher, catalog: ModelCatalog, log=print,
                 snapshots=None):
        self.cfg = cfg
        self.snapshots = snapshots                  # SnapshotManager；None = 没开对象存储
        self.store = store
        self.launcher = launcher
        self.catalog = catalog
        self.log = log
        self.gateway_signer = Signer(cfg.secret_key, "gateway")
        # 沙箱访问网关的地址：明确配置了就用配置的（网关是单独部署的服务）；否则问启动器（Docker 要用网桥地址）
        # 远程沙箱（NCS_LAUNCHER=remote）例外：沙箱在另一台机器上，只能经沙箱代理的中继访问网关，
        # 中继地址以启动器给的为准
        if getattr(launcher, "remote", False):
            self.gateway_url = launcher.gateway_url_for_sandbox(cfg.gateway_port).rstrip("/")
        else:
            self.gateway_url = (cfg.gateway_url or launcher.gateway_url_for_sandbox(cfg.gateway_port)
                                or cfg.sandbox_gateway_url).rstrip("/")
        self._admission = threading.Lock()          # 容量检查和占位必须是原子的
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    @property
    def control_gateway_url(self) -> str:
        """控制面自己访问网关的地址（测试 Key 用）。远程沙箱时沙箱用的中继地址在沙箱主机的网桥上，
        控制面到不了，要直连网关。"""
        if self.cfg.gateway_url:
            return self.cfg.gateway_url.rstrip("/")
        if getattr(self.launcher, "remote", False):
            return self.cfg.sandbox_gateway_url
        return self.gateway_url

    def _lock(self, sid: str) -> threading.Lock:
        # RLock 而不是 Lock：switch_model 要持锁走完「查 busy → stop → 改 DB → 重启」
        # 整条序列，而 stop / ensure_running 自己也会拿同一把锁（#61）。
        with self._locks_guard:
            return self._locks.setdefault(sid, threading.RLock())

    # ------------------------------------------------------------------ 查询

    def list(self, owner: str) -> list[Session]:
        return self.store.list_sessions(owner)

    def owned(self, owner: str, sid: str) -> Session:
        s = self.store.get_session(sid) if sid.isalnum() and len(sid) == 16 else None
        if s is None or s.owner != owner:
            raise SessionError(404, "会话不存在")     # 别人的会话也说不存在，不暴露它是否存在
        return s

    # ------------------------------------------------------------------ 生命周期

    def _check_capacity(self, owner: str, exclude: str = "") -> None:
        active = [s for s in self.store.active_sessions() if s.id != exclude]
        if sum(s.owner == owner for s in active) >= self.cfg.max_sessions_per_user:
            raise SessionError(429, f"你同时运行的会话已达上限（{self.cfg.max_sessions_per_user} 个），先停掉一个再试")
        if len(active) >= self.cfg.max_sandboxes:
            raise SessionError(503, "平台容量已满，请稍后再试")

    def _wait_started(self, sid: str, timeout: float = 120) -> Session:
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.5)
            s = self.store.get_session(sid)
            if s is None:
                raise SessionError(404, "会话不存在")
            if s.status == "running" and s.endpoint:
                return s
            if s.status in ("failed", "stopped"):
                raise SessionError(502, f"沙箱启动失败：{s.error or '请刷新重试'}")
        raise SessionError(503, "沙箱还在启动，请稍后刷新")

    def create(self, owner: str, title: str, model_ref: str = "") -> Session:
        try:
            ref = model_ref or self.catalog.default_ref(owner)
            self.catalog.option(owner, ref)
        except CatalogError as e:
            raise SessionError(400, str(e)) from None
        with self._admission:
            self._check_capacity(owner)
        s = self.store.create_session(owner, (title or "").strip() or "新会话", ref)
        return self.ensure_running(s.id)

    def ensure_running(self, sid: str) -> Session:
        """沙箱在运行就直接返回（信任数据库里的状态，不在每个请求上都问启动器）；否则启动它。

        进程内的锁挡住同一个副本里的并发请求；数据库里的 claim_start 挡住多个副本同时启动同一个会话。
        """
        with self._lock(sid):
            s = self.store.get_session(sid)
            if s is None:
                raise SessionError(404, "会话不存在")
            if s.status == "running" and s.endpoint:
                return s
            try:
                option = self.catalog.option(s.owner, s.model_ref)
            except CatalogError as e:
                raise SessionError(409, f"{e}。请在页面上为这个会话换一个模型") from None
            with self._admission:
                self._check_capacity(s.owner, exclude=sid)
                web_token = secrets.token_urlsafe(32)
                if not self.store.claim_start(sid, web_token):
                    return self._wait_started(sid)        # 别的副本正在启动它
            api_key = "" if option.ref == MOCK_REF else self.gateway_signer.sign(
                # 载荷里带本次启动的 web_token 摘要（代次）：stop / 重启会换 web_token，
                # 网关比对当前值即可拒绝旧令牌——沙箱停了、或者泄漏的令牌，都不能
                # 再消耗平台额度（#53）。
                {"u": s.owner, "s": sid, "w": web_token_digest(web_token)}, self.cfg.token_ttl)
            spec = SandboxSpec(sid, s.owner, web_token, api_key, build_policy(self.cfg, option, self.gateway_url),
                               resume=bool(s.started))
            try:
                if self.snapshots is not None and s.started:
                    # 拿到启动租约之后再恢复：多个副本不会同时往同一个工作区里解压
                    self.snapshots.settle(sid)
                    self.snapshots.restore_if_missing(s.owner, sid)
                endpoint = self.launcher.start(spec)
            except LaunchError as e:
                if self.store.fail_start(sid, web_token, str(e)[:500]):
                    raise SessionError(502, f"沙箱启动失败：{e}") from None
                # 启动这期间会话被删除/停止/被别人重抢了：状态以数据库为准，不覆盖（#59）
                raise SessionError(409, "启动期间会话被删除或停止了，请刷新后重试") from None
            now = time.time()
            if not self.store.finish_start(sid, web_token, endpoint, now):
                # 写完 running 之前状态就变了（删除、停止、租约被重抢）：不能把 deleted
                # 复活成 running（#59）。刚拉起的沙箱也没人管了，要收拾掉。
                self.launcher.stop(sid)
                raise SessionError(409, "启动期间会话被删除或停止了，请刷新后重试")
            s = self.store.get_session(sid)
            if s is None or s.status != "running":
                # 极端竞态：finish_start 成功后的同一瞬间又被删了。沙箱一样要收掉，
                # 不能留一个数据库里已经没有会话的容器/Pod（#59）。
                self.launcher.stop(sid)
                raise SessionError(409, "启动期间会话被删除或停止了，请刷新后重试")
            return s

    def stop(self, sid: str, snapshot: bool = True) -> None:
        """停沙箱（保留工作区）。开了对象存储时随后在后台存快照——不等它，失败也不影响停止。"""
        with self._lock(sid):
            s = self.store.get_session(sid)
            was_active = bool(s and s.started and s.active)     # 本来就停着的再点停止：不重复存
            self.launcher.stop(sid)
            self.store.stop_session(sid)
        if snapshot and self.snapshots is not None and was_active:
            self.snapshots.schedule(s.owner, sid)

    def delete(self, sid: str) -> None:
        with self._lock(sid):
            s = self.store.get_session(sid)
            if self.snapshots is not None and self.cfg.snapshot_on_delete and s is not None and s.started:
                # 先停容器、标记删除，快照（成功或最终失败）之后再删本地工作区
                self.launcher.stop(sid)
                self.store.update_session(sid, status="deleted", endpoint="", web_token="")
                self.snapshots.schedule(s.owner, sid, then=lambda: self.launcher.delete(sid))
                return
            self.launcher.delete(sid)
            self.store.update_session(sid, status="deleted", endpoint="", web_token="")

    def snapshot_now(self, owner: str, sid: str) -> dict:
        """手动快照（会话主人）。沙箱在运行也可以：打包在容器外面做，不跟随链接。"""
        if self.snapshots is None:
            raise SessionError(404, "没有开启工作区快照（NCS_OBJECT_STORE=off）")
        s = self.owned(owner, sid)
        if not s.started:
            raise SessionError(409, "这个会话还没有启动过，没有工作区")
        from ..runtime.snapshot import SnapshotError
        from ..storage.objectstore import ObjectStoreError
        try:
            if not self.launcher.has_workspace(sid):
                raise SessionError(409, "本机没有这个会话的工作区（打开会话会先从快照恢复）")
            return self.snapshots.snapshot_now(s.owner, sid)
        except (SnapshotError, ObjectStoreError, LaunchError) as e:
            raise SessionError(502, f"快照失败：{e}") from None

    def list_snapshots(self, owner: str, sid: str) -> list[dict]:
        if self.snapshots is None:
            return []
        s = self.owned(owner, sid)
        from ..storage.objectstore import ObjectStoreError
        try:
            return [{k: v for k, v in x.items() if k != "key"} for x in self.snapshots.list(s.owner, sid)]
        except ObjectStoreError as e:
            raise SessionError(502, f"列不出快照：{e}") from None

    def mark_dead(self, sid: str) -> None:
        """转发失败：沙箱已经不在了。标记为已停止，下次访问会重新启动。"""
        self.store.mark_dead(sid)

    def touch(self, sid: str) -> None:
        self.store.update_session(sid, last_active=time.time())

    def switch_model(self, owner: str, sid: str, ref: str) -> Session:
        """换模型：沙箱里的模型配置是启动时锁定的，所以换完重启沙箱（恢复对话），Agent 也知道自己换了模型。

        整条序列必须在 per-session 锁里做完。以前「查 busy → stop → 改 DB → 重启」
        四步之间不持锁，两个后果：
          1. 检查通过后新的 /api/chat 立刻进来 → stop 把沙箱杀了，当前回合连工具执行
             一起丢掉，用户看到的是聊到一半会话没了；
          2. 另一个请求在 stop 之后、ensure_running 之前插进来，会拿**旧** model_ref
             重启沙箱；随后 switch 看到 running 直接返回，于是「数据库说新模型、
             沙箱跑的还是旧模型」，而网关只认数据库，两边就打架了（#61）。
        """
        s = self.owned(owner, sid)
        try:
            self.catalog.option(owner, ref)
        except CatalogError as e:
            raise SessionError(400, str(e)) from None
        with self._lock(sid):
            # 锁内再查一次：拿锁之前那段时间里，Agent 可能刚好开始跑
            s = self.store.get_session(sid) or s
            if s.status == "running":
                state = self.sandbox_state(s)
                if state and (state.get("busy") or state.get("queue")):
                    raise SessionError(409, "Agent 正在工作，等它做完（或先中断）再换模型")
            was_running = s.status == "running"
            self.stop(sid, snapshot=False)          # 马上要重启，不值得存快照（也免得启动等它）
            self.store.update_session(sid, model_ref=ref)
            # 重启也用刚写进去的 ref：锁保证了这中间没人能拿旧配置先把沙箱拉起来
            return self.ensure_running(sid) if was_running else self.store.get_session(sid)

    # ------------------------------------------------------------------ 维护

    def sandbox_state(self, s: Session) -> dict | None:
        req = urllib.request.Request(s.endpoint + "/api/state", headers={"x-nanocode-token": s.web_token, "Host": "localhost"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.loads(r.read())
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def reconcile(self) -> None:
        """控制面重启后：数据库里标着运行中、实际已经不在的沙箱，改成已停止；
        再让启动器清掉数据库里已经没有对应会话的沙箱（无主容器 / 无主进程，#60）。"""
        for s in self.store.active_sessions():
            if not self.launcher.is_alive(s.id):
                self.mark_dead(s.id)
        self.launcher.reconcile({s.id for s in self.store.active_sessions()})

    def reap_once(self, now: float | None = None) -> list[str]:
        """回收空闲的沙箱：超过 idle_timeout 没有请求、Agent 不在工作。只连着事件流的页面不算活动。
        沙箱存活时间接近网关令牌有效期时也回收，下次访问带着新令牌重新启动。"""
        now = time.time() if now is None else now
        reaped = []
        for s in self.store.active_sessions():
            if s.status != "running":
                continue
            idle = now - s.last_active > self.cfg.idle_timeout
            expiring = s.started and now > s.started + self.cfg.token_ttl - 300
            if not (idle or expiring):
                continue
            state = self.sandbox_state(s)
            if state and (state.get("busy") or state.get("queue")) and not expiring:
                self.touch(s.id)
                continue
            self.stop(s.id)
            reaped.append(s.id)
        return reaped

    def reap_forever(self, stop: threading.Event, interval: float = 60) -> None:
        while not stop.wait(interval):
            try:
                self.reap_once()
            except Exception as e:  # noqa: BLE001 —— 回收线程不能因为一次异常就停掉
                self.log(f"回收沙箱出错：{type(e).__name__}: {e}")


__all__ = ["SessionManager", "SessionError", "build_policy", "FORBIDDEN_SETTINGS"]
