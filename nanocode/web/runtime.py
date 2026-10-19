"""Web 面板的多会话运行时：多个会话可以同时跑 Agent（#106、#5；方案见 docs/web-multisession-plan.md 一期）。

以前 WebApp 只持有一个 Session，"当前会话"是全局的：一个页面切了会话，别的页面也跟着换（#106）；
Agent 在跑时谁也不能切。现在拆成三层：

    SessionManager   会话 id → SessionRuntime；同一个 id 在进程里只有一个运行时（否则两个对象抢着存同一个文件）
    SessionRuntime   一个会话自己的排队、忙碌标志、权限询问、事件总线（原来挂在 WebApp 上的那些）
    RunSlots         全局 / 每客户端的并发上限：满了不拒绝，排队等空位

哪个会话由请求自己指明（请求头 X-Nanocode-Session，事件流用 ?sid=），没指明的走"默认会话"，
旧客户端和控制面转发的行为不变。每个浏览器标签页自己记住当前会话，互不影响。

所有会话共用同一个工作目录：有别的会话在跑时，界面会提示，系统提示里也会提醒 Agent
（改文件前重读、动整个仓库的 git 操作先问用户）。真正的隔离（文件租约、worktree）在后续阶段。
"""

from __future__ import annotations

import datetime
import os
import threading
import time
import uuid

from .limits import DailyCounter, RateLimited

# Agent 忙时仍然可以执行的斜杠命令：只读查询，不会碰正在跑的那一轮对话。
# 其余命令（/clear /compact /resume /mode、以及会转成提示词的自定义命令）会改掉
# Agent 脚下的状态，必须等这一轮结束 —— 见 chat() 里对 #50 的注释。
READ_ONLY_COMMANDS = {"help", "?", "cost", "context", "tools", "todos", "mcp", "skills", "providers", "memory"}

# 忙时还能改的配置项。/mode 不是只读查询，但它只改权限模式这一个配置：正在跑的那一轮
# 已经判过的工具不会回退重判，新模式从下一次工具调用开始生效，不会动 messages / usage
# 这些 Agent 脚下的状态。以前它和 /clear 一起被 429 拒掉，而前端又不显示这个错误，
# 用户以为切换成功了（#78）。
BUSY_ALLOWED = READ_ONLY_COMMANDS | {"mode"}

# 会话状态变化时广播给所有页面的事件类型（前端据此显示"另有 n 个会话在运行"）
_STATUS_TRIGGERS = {"user", "queued", "dequeue", "idle", "permission_request", "permission_answer", "turn_end"}


def valid_session_id(sid: str) -> bool:
    """会话 id 会拼进文件路径，必须挡住路径穿越。"""
    return bool(sid) and len(sid) <= 128 and not any(x in sid for x in ("/", "\\", "..", "\0"))


class RunSlots:
    """同时运行的会话数上限：全局 max_runs 个，每个客户端 per_client 个（0 = 不限）。

    先来先得，但跳过"本客户端已经占满"的等待者，免得一个客户端堵住所有人。
    空位按"一条消息"占用：一轮跑完就释放，排队的下一条重新申请 —— 一个会话排了很多条，
    也不会一直霸着空位让别的会话饿死。等权限确认时仍然占着（让出去的话，批准后可能抢不回来）。
    """

    def __init__(self, max_runs: int, per_client: int):
        self.max_runs = max_runs
        self.per_client = per_client
        self._cond = threading.Condition()
        self._running: dict[str, int] = {}
        self._total = 0
        self._waiters: list[tuple[object, str]] = []

    def _fits(self, client: str) -> bool:
        if self.max_runs and self._total >= self.max_runs:
            return False
        return not (self.per_client and self._running.get(client, 0) >= self.per_client)

    def _grant(self, client: str) -> None:
        self._total += 1
        self._running[client] = self._running.get(client, 0) + 1

    @property
    def running(self) -> int:
        return self._total

    def acquire(self, client: str, cancel: threading.Event, on_wait=None) -> bool:
        """拿到空位返回 True；等待期间被取消返回 False。第一次需要等时调用 on_wait(排第几位)。"""
        with self._cond:
            if not self._waiters and self._fits(client):
                self._grant(client)
                return True
            me = object()
            self._waiters.append((me, client))
            told = False
            try:
                while True:
                    if cancel.is_set():
                        return False
                    first = next((w for w, c in self._waiters if self._fits(c)), None)
                    if first is me:
                        self._grant(client)
                        return True
                    if not told and on_wait:
                        told = True
                        on_wait(next(i for i, (w, _) in enumerate(self._waiters, 1) if w is me))
                    self._cond.wait(0.2)
            finally:
                self._waiters = [(w, c) for w, c in self._waiters if w is not me]
                self._cond.notify_all()

    def release(self, client: str) -> None:
        with self._cond:
            self._total = max(0, self._total - 1)
            n = self._running.get(client, 0) - 1
            if n > 0:
                self._running[client] = n
            else:
                self._running.pop(client, None)
            self._cond.notify_all()


class SessionRuntime:
    """一个会话的运行状态。原来 WebApp 上的 chat / 排队 / 权限询问逻辑原样搬到这里，只是从全局变成按会话。"""

    def __init__(self, app, manager: "SessionManager", session):
        self.app = app
        self.manager = manager
        self.session = session
        self.lock = threading.Lock()
        self.queue: list[str] = []      # 忙时排队的消息，按先进先出执行
        # 「这个会话有一轮工作在进行」（含排队中、等空位的消息），由本类独占维护。
        # 不能用 agent.busy 代替：Agent.run() 在 finally 里会把它清零，而那时 _run
        # 还没开始处理队列，中间的窗口会让新请求误判成「不忙」而并发起第二个 Agent（#29）。
        self._turn_active = False
        self.pending: dict[str, dict] = {}   # 权限询问 id → 等待槽
        self.client = "local"           # 这一轮是哪个客户端发起的（每客户端并发上限按它算）
        self.waiting_slot = False
        self.subscribers = 0            # 正连着这个会话事件流的页面数
        self.last_used = time.time()
        session.permissions.ask_fn = self.ask
        session.system_note = lambda: manager.parallel_note(self)
        session.bus.subscribe(self._on_event)

    @property
    def sid(self) -> str:
        return self.session.session_id

    @property
    def busy(self) -> bool:
        """这个会话是否还有工作没做完（含排队中的消息）。

        以 _turn_active 为准：Agent.run() 一返回就把 agent.busy 清掉了，而从那一刻到
        _run() 处理完队列之间还有一段路，只看 agent.busy 会误判成空闲并放第二个 Agent 进来。
        agent.busy 也一起看，是为了兼容"直接把 agent.busy 置 True 表示忙"的调用方。
        """
        return self._turn_active or self.session.agent.busy

    def status(self) -> dict:
        return {"id": self.sid, "running": self.busy, "queued": len(self.queue),
                "waiting_slot": self.waiting_slot, "awaiting_permission": bool(self.pending)}

    def _on_event(self, e: dict) -> None:
        if e["type"] in _STATUS_TRIGGERS:
            self.manager.notify()

    # ---------------------------------------------------------------- 权限询问
    def ask(self, name: str, input: dict, target: str) -> str:
        """权限询问：发事件给浏览器，然后阻塞等待 /api/permission 的回答。

        按拒绝处理的三种情况，Agent 都不会永远卡住（等待期间这个会话一直算忙，还占着一个运行空位）：
          - 用户点了中断
          - 超过 permission_timeout（默认 10 分钟，环境变量 NANOCODE_PERMISSION_TIMEOUT 可改）
          - 一个浏览器都没连着（页面全关了）超过 disconnect_grace 秒。刷新页面会很快重连，不受影响。
            这里看的是**所有**页面，不是"正看着这个会话的页面"：用户切去看别的会话时，
            后台会话的询问要接着等，不能 30 秒后被自动拒掉。
        """
        app = self.app
        rid = uuid.uuid4().hex[:8]
        slot = {"event": threading.Event(), "answer": "no"}
        self.pending[rid] = slot
        self.session.bus.emit("permission_request", id=rid, name=name, input=input, target=target)
        deadline = time.time() + app.permission_timeout
        no_client_since = None
        while not slot["event"].wait(0.2):
            now = time.time()
            if self.session.agent.cancel.is_set() or now > deadline:
                break
            if app.sse_clients == 0:
                no_client_since = no_client_since or now
                if now - no_client_since > app.disconnect_grace:
                    break
            else:
                no_client_since = None
        self.pending.pop(rid, None)
        self.session.bus.emit("permission_answer", id=rid, decision=slot["answer"])
        return slot["answer"]

    # ---------------------------------------------------------------- 发消息
    def chat(self, message: str, client: str = "local") -> dict:
        """处理一条消息。超限时抛 RateLimited，由 Handler 转成 429。

        只有真正要跑模型的消息才扣额度：# 记忆和不产生 prompt 的 /命令 不扣。
        """
        s = self.session
        app = self.app
        limits = app.limits
        self.last_used = time.time()
        with self.lock:
            message = message.strip()
            if message.startswith("#"):
                with self.manager.memory_lock:      # NANOCODE.md 是所有会话共用的文件
                    return {"ok": True, "output": s.add_memory(message.lstrip("#"))}
            prompt = message
            output = ""
            if message.startswith("/"):
                # 先取命令名、先判忙，再执行。反过来（先 handle_command 后判忙）会让
                # /clear、/resume、/compact 在 Agent 正在跑的时候改掉它脚下的状态：
                # /clear 清空正在使用的 messages，/resume 换掉整个对话，/compact 在锁里
                # 发模型调用还把取消按钮一起堵死（#50）。
                # 纯查询类命令和 /mode 不受影响，忙时照样能用（#78）。
                name = message[1:].strip().partition(" ")[0]
                if self.busy and name not in BUSY_ALLOWED:
                    raise RateLimited("busy", f"Agent 正在工作，等它做完或先中断再用 /{name}", 5)
                if name == "resume":
                    # Web 面板里"恢复最近会话"就是在本标签打开它，交给会话列表做；在这里原地换掉
                    # 对话的话，目标会话可能正在别的标签里跑，进程里就会出现两份同一个会话。
                    output = "Web 面板里请用顶部的会话列表切换会话（每个标签页各自记住自己的当前会话）。"
                    s.bus.emit("command", input=message, output=output)
                    return {"ok": True, "output": output}
                old_sid = s.session_id
                with self.manager.memory_lock:      # /memory accept 也会写 NANOCODE.md
                    output, prompt = s.handle_command(message)
                if s.session_id != old_sid:         # /clear 换了新的会话 id
                    self.manager.rekey(self, old_sid)
                s.bus.emit("command", input=message, output=output)
                if not prompt:
                    # /compact 虽然不产生 prompt，但它自己会调一次模型做总结，
                    # 必须和普通消息一样过限流与每日预算，否则是个免费刷模型的入口（#50）。
                    if name != "compact":
                        return {"ok": True, "output": output}
            wait = limits.bucket.take(client)
            if wait:
                w = max(1, int(wait + 0.999))
                raise RateLimited("rate_limited", f"发送太频繁，请 {w} 秒后再试", w)
            if self.busy:
                # 不设上限的话，连点 100 次就会排进 100 轮完整的 Agent 循环（issue #7）。
                # 每个会话最多排 max_queue 条；所有会话加起来最多 max_queue_total 条，
                # 否则开很多会话就能把排队总量刷上去。
                if limits.max_queue and len(self.queue) >= limits.max_queue:
                    raise RateLimited("queue_full",
                                      f"排队已满（{limits.max_queue} 条），等当前任务做完或先中断", 5)
                if limits.max_queue_total and self.manager.total_queued() >= limits.max_queue_total:
                    raise RateLimited("queue_full",
                                      f"所有会话的排队总数已满（{limits.max_queue_total} 条），等一会儿再发", 5)
                # 忙时不再拒绝：收下排队，当前任务做完自动执行。立即发 user 事件，
                # 让前端马上显示这条消息（带排队标记），实际执行时 agent.run 会再发一次，
                # 前端按文本去重。
                self.queue.append(prompt)
                s.bus.emit("user", agent="main", text=message)
                s.bus.emit("queued", position=len(self.queue))
                return {"ok": True, "queued": True, "position": len(self.queue)}
            if not limits.daily_runs.take():
                raise RateLimited("daily_budget", "今天的调用预算已用完，明天再来（或调大 NANOCODE_WEB_DAILY_RUNS）",
                                  DailyCounter.seconds_to_midnight())
            if prompt is None:
                # /compact 已经把事做完了（它自己调过一次模型做总结），这里只补记一次
                # 每日预算，然后结束 —— 不能把 None 当 prompt 起一轮 Agent：
                # 那会往对话里塞一条 text=None 的消息（真实接口直接 400），
                # 还会多发一次模型请求、多扣一次预算（#73）。
                return {"ok": True, "output": output}
            # 两个标志一起置位：_turn_active 是本层的权威判定，agent.busy 让界面立刻反应过来
            self._turn_active = True
            s.agent.busy = True
            self.client = client
            # 上一轮按过的"停止"不能让这一轮在等空位时直接退出（Agent.run 开头也会清，但等空位在它前面）
            s.agent.cancel.clear()
        threading.Thread(target=self._run, args=(prompt,), daemon=True).start()
        return {"ok": True}

    def cancel(self) -> None:
        """"停止"连排队的一起停，否则队列里的会接着烧额度。"""
        with self.lock:
            dropped = len(self.queue)
            self.queue.clear()
        if dropped:
            self.session.bus.emit("dequeue", remaining=0)
        self.session.agent.cancel.set()

    def _end_turn(self) -> None:
        """这一轮工作彻底结束（队列已空，或排队消息被丢弃）：清掉两个标志。

        调用方必须持有 self.lock。先清 _turn_active 能让紧接着的 chat() 立刻看到空闲；
        agent.busy 在 Agent.run() 结束时已被自己清零，这里再清一次是为了覆盖
        "还没进入 session.send() 就被取消"这类没走到 Agent.run 的路径。
        """
        self._turn_active = False
        self.session.agent.busy = False

    def _run(self, prompt: str) -> None:
        try:
            self._run_queue(prompt)
        finally:
            # 兜底：_turn_active 相当于一把会话锁，任何退出路径都必须放开。
            # 正常收尾时 _end_turn() 已经清掉，这里不会重复发 idle；
            # 只有被中断（BaseException）、连 _run_queue 都没走完的路径才会落到这里。
            with self.lock:
                if self._turn_active:
                    self._end_turn()
                    self.session.bus.emit("idle")

    def _acquire_slot(self) -> bool:
        def on_wait(position: int) -> None:
            self.waiting_slot = True
            self.session.bus.emit("queued", position=position, reason="slots",
                                  message=f"同时运行的会话已达上限，等待空位（第 {position} 位）")
        ok = self.manager.slots.acquire(self.client, self.session.agent.cancel, on_wait)
        if self.waiting_slot:
            self.waiting_slot = False
            self.session.bus.emit("dequeue", remaining=len(self.queue), reason="slots")
        return ok

    def _run_queue(self, prompt: str) -> None:
        while True:
            if not self._acquire_slot():
                # 等空位时被中断：这一条和排队的都不跑了
                with self.lock:
                    self.queue.clear()
                    self._end_turn()
                    self.session.bus.emit("idle")
                return
            try:
                self.session.send(prompt)
            except Exception as e:  # noqa: BLE001
                from .server import friendly_error
                self.session.bus.emit("error", agent="main", message=friendly_error(e),
                                      retryable=True, prompt=prompt)
            finally:
                self.manager.slots.release(self.client)
            with self.lock:
                if not self.queue:
                    self._end_turn()
                    self.session.bus.emit("idle")
                    return
                if not self.app.limits.daily_runs.take():   # 排队的消息也要扣每日预算，否则能绕过去
                    dropped = len(self.queue)
                    self.queue.clear()
                    self._end_turn()
                    self.session.bus.emit("error", agent="main", retryable=False,
                                          message=f"今天的调用预算已用完，已丢弃排队中的 {dropped} 条消息")
                    self.session.bus.emit("idle")
                    return
                prompt = self.queue.pop(0)
                self.session.bus.emit("dequeue", remaining=len(self.queue))
            # _turn_active 保持 True，直接进入下一轮


class SessionManager:
    """会话 id → 运行时。懒加载，内存里最多常驻 max_live 个，空闲且没人看的按最久未用淘汰。"""

    def __init__(self, app, base_session, max_live: int = 8):
        self.app = app
        self.base_session = base_session      # 启动时构造的那个 Session：共享部件（MCP 等）归它管
        self.max_live = max_live
        self.slots = RunSlots(app.limits.max_runs, app.limits.max_runs_per_client)
        self.memory_lock = threading.Lock()
        self._lock = threading.RLock()
        self._listeners: list = []
        base = SessionRuntime(app, self, base_session)
        self._live: dict[str, SessionRuntime] = {base.sid: base}
        self.base = base
        self.default = base                  # 不带会话 id 的请求用它（旧客户端、控制面转发）

    # ---------------------------------------------------------------- 查找 / 创建
    def get(self, sid: str | None) -> SessionRuntime:
        """按 id 找运行时；不在内存就从磁盘载入。id 非法抛 ValueError，会话不存在抛 LookupError。"""
        if not sid:
            return self.default
        if not valid_session_id(sid):
            raise ValueError("会话 id 非法")
        with self._lock:
            rt = self._live.get(sid)
            if rt is None:
                try:
                    child = self.base_session.spawn(sid)
                except (OSError, ValueError) as e:
                    raise LookupError(f"会话 {sid} 不存在或无法读取") from e
                rt = SessionRuntime(self.app, self, child)
                self._live[sid] = rt
                self._evict()
            rt.last_used = time.time()
            return rt

    def create(self) -> SessionRuntime:
        with self._lock:
            sid = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            d = os.path.join(self.base_session.data_dir, "sessions")
            while sid in self._live or os.path.exists(os.path.join(d, f"{sid}.json")):
                sid += "_1"
            child = self.base_session.spawn()
            child.session_id = sid
            rt = SessionRuntime(self.app, self, child)
            self._live[sid] = rt
            self._evict()
            return rt

    def rekey(self, rt: SessionRuntime, old_sid: str) -> None:
        """会话换了 id（/clear 开始新对话）后更新索引。"""
        with self._lock:
            if self._live.get(old_sid) is rt:
                del self._live[old_sid]
            self._live[rt.sid] = rt
        self.notify()

    def live(self) -> list[SessionRuntime]:
        with self._lock:
            return list(self._live.values())

    def total_queued(self) -> int:
        return sum(len(rt.queue) for rt in self.live())

    def _evict(self) -> None:
        """超过常驻上限时，按最久未用淘汰：只淘汰空闲、没人连着、不是默认/启动会话的。淘汰前先存盘。"""
        if not self.max_live or len(self._live) <= self.max_live:
            return
        idle = sorted((rt for rt in self._live.values()
                       if rt is not self.base and rt is not self.default and not rt.busy
                       and not rt.pending and rt.subscribers == 0),
                      key=lambda rt: rt.last_used)
        for rt in idle[:len(self._live) - self.max_live]:
            del self._live[rt.sid]
            if rt.session.agent.messages:
                rt.session.save()
            rt.session.close()

    # ---------------------------------------------------------------- 状态
    def list_sessions(self) -> list[dict]:
        """磁盘上的会话列表 + 内存里各会话的运行状态；刚建还没存过盘的新会话排在最前面。"""
        live = {rt.sid: rt for rt in self.live()}
        out = self.base_session.list_sessions()
        on_disk = {s["id"] for s in out}
        fresh = [{"id": sid, "messages": len(rt.session.agent.messages), "mtime": rt.last_used,
                  "title": "（新会话）"} for sid, rt in live.items() if sid not in on_disk]
        out = sorted(fresh, key=lambda s: -s["mtime"]) + out
        for s in out:
            rt = live.get(s["id"])
            st = rt.status() if rt else {}
            s["running"] = bool(st.get("running"))
            s["queued"] = st.get("queued", 0)
            s["awaiting_permission"] = bool(st.get("awaiting_permission"))
        return out

    def snapshot(self) -> dict:
        """广播给所有页面的会话状态：只列有事的会话（在跑、排队、等确认）。没有 seq，不进事件历史。"""
        busy = [rt.status() for rt in self.live() if rt.busy or rt.queue or rt.pending]
        return {"type": "sessions_status", "sessions": busy, "running_slots": self.slots.running,
                "max_runs": self.slots.max_runs}

    def subscribe(self, fn) -> callable:
        with self._lock:
            self._listeners.append(fn)
        return lambda: self._listeners.remove(fn) if fn in self._listeners else None

    def notify(self) -> None:
        if not self._listeners:
            return
        snap = self.snapshot()
        for fn in list(self._listeners):
            try:
                fn(snap)
            except Exception:  # noqa: BLE001 —— 页面出错不能影响 Agent
                pass

    def parallel_note(self, rt: SessionRuntime) -> str:
        """有别的会话在同一目录并行工作时，附在系统提示末尾提醒 Agent。没有就返回空串（不影响提示缓存）。"""
        others = [r for r in self.live() if r is not rt and r.busy]
        if not others:
            return ""
        return ("# 并行会话提醒\n"
                f"另有 {len(others)} 个会话正在同一工作目录里并行工作，它们可能同时修改文件。"
                "修改文件前先重新读取最新内容；git commit / checkout / reset / stash / rebase、删除文件、"
                "大范围格式化这类影响整个仓库的操作，先征得用户同意。")

    def close(self) -> None:
        for rt in self.live():
            if rt is not self.base:
                rt.session.close()
