# nanocode Web 面板：多会话并行运行方案（草案，未改代码）

基于 main `e4a9fb1` 阅读 `nanocode/web/server.py`、`static/index.html`、`session.py`、`agent.py`、`permissions.py`、`events.py`、`web/limits.py`、`llm/base.py`，并参考 #5、#106、`docs/12-distributed-deployment.md`。

## 1. 现状：为什么只能跑一个

| 部件 | 现在的做法 | 阻碍并行的点 |
|---|---|---|
| 会话 | `WebApp.session` 只有**一个** `Session`。切换会话（`/api/session` new/load）是在这个对象上换掉 `agent.messages` 和 `session_id`，并清空 `bus.history` | "当前会话"是**全局**的：一个页面切了会话，其他页面也跟着变（这是 #106 的坑）；正在跑的 Agent 用的就是这份 messages，所以 `server.py:806` 忙时不许切 |
| 运行状态 | `_turn_active`、`queue`、`lock` 都挂在 WebApp 上，全局只有一份；`_run` 每次起一个线程跑 `session.send()` | 一把全局"会话锁" |
| 事件流 | 只有一个 `EventBus`（序号 `seq` 全局递增，history 2000 条）；`/api/events` 是 SSE，用一次性票据 + `Last-Event-ID`/`?after=` 续传 | 所有页面只能订阅同一条流 |
| 取消 | `/api/cancel` 清空全局队列，并 set `session.agent.cancel` | 没法只停某一个会话 |
| 权限询问 | `WebApp.ask` 是唯一的 `permissions.ask_fn`，`pending` 是全局 dict；没有浏览器连着超过 30 秒就**按拒绝处理**（`sse_clients == 0`） | 并行后，如果按"这个会话的订阅者"来算，用户切到别的会话 30 秒，后台会话的询问就会被自动拒绝 |
| 权限状态 | `PermissionManager` 一个：`mode`、`session_allow`（"总是允许"）全局共享 | 一个会话点了"总是允许"，其他会话也会被放行 |
| 限流（#25 / limits.py） | 每个 IP 一个令牌桶；`max_queue=3`（全局队列）；`daily_runs=200`（全局）；`NANOCODE_WEB_DAILY_LLM_CALLS` 在 Provider 层全局计数 | 没有"同时跑几个"的上限 |
| 持久化 | `Session.save()` 写 `sessions/<id>.json`（tmp 文件名带 pid，再 `os.replace`）；`_autosave` 在主 Agent 每次 `llm_request` 时保存 | 同一进程里不能有两个对象保存同一个 id（tmp 文件名只带 pid，会互相覆盖） |
| 工作区 | 所有会话共用 `session.cwd`。bash 的 `shell_cwd`、`read_files` 都放在各自 `agent.state` 里，本来就按 Agent 隔离 | 两个会话会同时改同一个目录 |

不阻碍并行的部分：
- 没有 `os.chdir`，也不改 `os.environ`。
- `Provider` 每次调用都是独立的 HTTP 请求，`Usage` 有锁。
- `MCPClient.request` 有锁。
- `HookRunner`、skills、commands 都是只读的。

这些都可以在会话之间**共享**。

现成可以复用的冲突检测：`files.py::_check_fresh` 会在编辑前检查文件 mtime，"读完之后被改过"就拒绝。所以一个会话改了文件，另一个会话想接着改同一个文件时，会被要求重新读。**这能防止旧内容覆盖新内容，但防不住两个会话交替修改同一个文件，也管不了 bash。**

## 2. 总体设计

### 2.1 会话运行时：SessionRuntime + SessionManager（新文件 `nanocode/web/runtime.py`）

- **共享层（每个进程一份）**：`settings`、`policy`、`provider` 工厂、`ToolRegistry`（含 MCP 客户端）、`Hooks`、`skills`、`commands`、`cwd`、`data_dir`。还是在 `cli.py` 里用现有的 `Session(opts)` 构造一次，CLI 不受影响。
- **每会话层**：给 `Session` 加 `Session.spawn(session_id=None)`，复用上面的共享层，新建这些对象：
  - `Agent`（messages、state、usage、cancel、自己的 provider 引用）；
  - `EventBus`（每个会话独立的 seq 和 history）；
  - `PermissionManager` 的副本（`mode` 默认取配置，`session_allow` 独立）；
  - `pending_memory`。
  
  autosave 和 trace 订阅这个会话自己的 bus。审计日志用一个进程级的 `AuditWriter` 加锁，事件里带上 session_id。
- `SessionRuntime` 包住一个 spawn 出来的 Session，把现在 WebApp 上的这些状态都挪进来：`lock`、`queue`、`_turn_active`、`pending`（权限询问）、`busy`、`subscribers`。现有 `chat()`、`_run_queue()`、`ask()` 的逻辑基本原样搬进去，只是 `self.session` 换成 `self.rt.session`。
- `SessionManager`：
  - `get_or_load(sid)`：**同一个 sid 在进程里只有一个运行时**，这样就不会有两个对象同时保存同一个 id。
  - `create()`；
  - `list()`：磁盘上的会话列表，加上内存里每个会话的状态（running、queued、waiting_permission）；
  - LRU 淘汰：内存里最多保留 `NANOCODE_WEB_MAX_LIVE_SESSIONS` 个会话（默认 8）。只淘汰空闲且没有订阅者的，淘汰前先 `save()`；**正在跑或在排队的永远不淘汰**。
- 启动时恢复的最近会话作为"默认会话"。请求不带 sid 时用它，所以旧客户端、控制面转发和现有测试都不受影响。

### 2.2 接口：一律带会话 id

- 加请求头 `X-Nanocode-Session: <sid>`（EventSource 发不出请求头，所以 SSE 用 `?sid=`）。sid 沿用现有的合法性校验（不能含 `/`、`\`、`..`）。
- `/api/chat`、`/api/state`、`/api/messages`、`/api/context`、`/api/cancel`、`/api/permission`、`/api/events` 都按 sid 找运行时。
- `/api/session`：
  - `new` 返回新的 sid，`load` 只确认会话存在并返回 sid，**都不改任何全局状态**，也就**不再需要"忙时不许切会话"**；
  - "当前会话"改由前端维护：放在 URL `?s=sid` 和 sessionStorage 里，每个标签页各自独立。
- `/api/sessions` 的每一项加上 `running`、`queued`、`awaiting_permission`、`last_activity` 字段。
- `/api/permission` 校验 id 是否属于这个 sid，不属于就返回 404。
- `/api/cancel` 只停这个 sid，另加 `{"all": true}` 用来全部停止。
- 斜杠命令都只作用于当前 sid：`/clear`、`/compact`、`/resume`、`/mode`、`/model`。`/resume` 在 Web 里等于"在本标签打开最近的会话"。
- `# 记忆` 和 `/memory accept` 会写共享的 `NANOCODE.md`，加一把进程锁。

### 2.3 并发上限，以及和现有限流的关系

- 新增调度器 `RunSlots`，用一个 FIFO 的信号量控制：
  - `NANOCODE_WEB_MAX_RUNS`：全局同时运行的会话数，默认 **3**，设为 0 表示不限；
  - `NANOCODE_WEB_MAX_RUNS_PER_CLIENT`：每个客户端同时运行的会话数，默认 **2**。"客户端"沿用 `_client_key()`，即 IP 或 XFF；三期改成按登录用户名。`docs/12` 里也建议每个用户 2 到 3 个。
- 满了**不拒绝，而是排队**：这条消息进入这个会话自己的队列，状态显示"等待空位（第 n 位）"，发 `queued` 事件并带上 `reason: "slots"`。
- **每轮结束就释放空位**，下一轮重新申请。这样一个会话排了好几条，也不会一直占着空位饿死其他会话。等待权限确认时**仍然占着空位**。不这样的话，空位一让出去，用户批准后会话可能抢不回空位，就卡住了。这是有意为之，靠 permission_timeout 兜底。
- 和现有限制的关系：

| 限制 | 改后 |
|---|---|
| 每个 IP 的令牌桶 | 不变，仍按客户端计、跨会话共享（两个标签页一起发，共用 burst=5） |
| `max_queue` | 语义改成**每个会话**最多排 3 条；另加 `NANOCODE_WEB_MAX_QUEUE_TOTAL`（默认 10），防止开很多会话把排队总量刷上去 |
| `daily_runs` | 不变，仍是全局计数。每轮真正开始（含出队）时扣一次，并行不会绕过它 |
| `DAILY_LLM_CALLS` | 不变，Provider 层的进程级计数本来就有锁 |
| `/compact` 忙时仍拒绝 | 只按本会话判断是否忙 |

- 风险提示：并行三个会话，模型费用的速度就是三倍。启动日志 `limits.describe()` 里要把这两个新上限打出来。

### 2.4 事件流

- 每个会话一个 bus，**seq 各自递增**。`Last-Event-ID` 和 `?after=` 的语义不变，现有的"序号比我大就从头补发"逻辑（#86）照搬。
- 每条 SSE 连接**只订阅一个会话的流**。另外有一个进程级的轻量状态总线，只发 `session_status{sid, running, queued, awaiting_permission}`，每条连接都会推送它，前端靠它给其他会话更新徽标。状态事件**不带 id 行**，不影响续传；刚连上时先补发一份全量快照。
- 线程开销：标签页数 × 1 条 SSE，外加运行中的会话数 × (1 个 run 线程 + 只读工具线程池)。本地单人用够了。#5 第 3 点提到的连接数上限和请求超时（照 `server/.../framework.py` 的做法），建议在一期顺手加上，大约 30 行。

### 2.5 权限询问

- `ask()` 搬进 `SessionRuntime`，事件发到本会话的 bus，`pending` 也按会话存放。
- "没有浏览器连着就按拒绝处理"改成看**全局**有没有 SSE 连接，而不是看这个会话有没有订阅者。用户切走去看别的会话时，后台的询问会继续等待，最多 10 分钟（`permission_timeout`）。
- 前端在后台会话的徽标上显示"🔔 待确认"，点一下切过去就会看到确认框。切过去时 history 会补发 `permission_request`，前端按 `permission_answer` 去重，只弹还没回答的。
- `/mode` 和"总是允许"都只对本会话生效（`PermissionManager` 副本）。平台的 `allowed_modes` 仍然从共享的 policy 读取。

### 2.6 前端（`static/index.html`）

- 当前 sid 存在 `?s=` 和 sessionStorage 里，所有 `post` 和 `getJSON` 自动带上请求头。
- 切换会话**不再 `location.reload()`**，而是：关掉旧的 EventSource → 清空聊天区 → `/api/messages?sid` → 用该会话的 `lastSeq` 重新连 SSE。一期可以先保留 reload（带上 `?s=`），实现最简单。
- 会话下拉框改成列表或带徽标：`●运行中` / `⏳排队/等空位` / `🔔待确认`，靠 `session_status` 实时刷新。
- 停止按钮只停当前会话。另加"全部停止"，有会话在跑时才显示。
- `busy`、`queueCount` 等状态改成跟着当前 sid 走，切换时从 `/api/state` 同步一次。
- 删掉 `session_switched` 的处理（不再有全局切换）。

### 2.7 同一工作区的冲突：风险和三种做法

所有会话的 cwd 都一样。真正的风险有：
- 两个会话交替修改同一个文件，mtime 检查会让模型反复重读，或者改出逻辑冲突；
- bash 是全局副作用：同时 `git commit` / `checkout` / `stash`，会遇到 `.git/index.lock` 冲突、改错分支；同时跑测试或构建会抢端口和产物目录；
- 一个会话的 `verify` 或终审 diff 会把另一个会话的改动算进来（`review.py` 是对文件做快照）。

| 做法 | 做什么 | 代价 | 建议 |
|---|---|---|---|
| A 提醒 | 有其他会话在跑时：前端顶部显示"另有 n 个会话在同一目录工作"；给 Agent 的 system 提示加一句"另有会话在并行修改此目录，git 提交、切分支、reset 这类全局操作前先询问用户" | 很小 | **一期就做** |
| B 跨会话文件租约 | 进程级 `FileLeases`：`write_file`、`edit_file` 写某个路径前登记"会话 X 正在改"。另一个会话再写同一路径时返回 ToolError（"该文件正被会话 X 修改，换个文件或等它结束"），会话这一轮结束时释放；终审和 verify 的 diff 里标出其他会话改的文件。bash 管不了，只能靠 A | 中等，大约 120 行 | **二期** |
| C 每会话一个 git worktree（可选） | 新建会话时可以勾选"独立工作区"：`git worktree add .nanocode/worktrees/<sid>`，这个会话的 cwd、权限根和 data_dir 都指向它；结束时由用户决定合回去还是丢掉 | 大：Session 要支持每会话独立的 cwd，非 git 目录不能用，合并冲突要交给用户处理 | **四期，按需做** |

### 2.8 和 #106（按登录态隔离）的关系：分开做

- 一期把"当前会话"放到前端以后，**#106 里那个真实的坑（一个页面新建会话把另一个页面的上下文换掉）就已经解决了**。而且是按标签页隔离，比 #106 提的"按 cookie 隔离"更细，它的"二期 page_id"也一并省掉了。
- #106 剩下的是**多人归属**：A 用户看不到、也操作不了 B 用户的会话。这放到三期：
  - `create_session` 记下登录用户名；
  - 会话文件里存 `owner`；
  - `/api/sessions` 按 owner 过滤，其他接口访问不属于自己的 sid 一律返回 404；
  - 每客户端并发上限改成按用户名计。
- 要说清楚的是：同一个进程里的多个用户**仍然共用同一个工作目录和同一个操作系统用户**，bash 能看到彼此的文件。这不算真正的隔离。真正的多人隔离要用 `server/` 平台的"每个会话一个沙箱"（docs/12 的方案 A）。所以三期只适合"几个互相信任的人共用一个自托管面板"的场景。

## 3. 分期计划

| 期 | 内容 | 主要文件 | 规模（粗估） | 测试 |
|---|---|---|---|---|
| **一期：后端多会话并行 + 前端最小适配** | `runtime.py`（SessionRuntime/Manager/RunSlots）；`Session.spawn()`；server.py 所有接口按 sid 路由；每会话的队列、取消、权限、bus；去掉忙时不许切会话；并发上限和总排队上限；权限断线判断改成看全局；做法 A 的提醒；前端带 sid、切换用 `?s=` 重载；可选：SSE 连接数上限 | 新增 `web/runtime.py` ~280 行；`server.py` +180/−120；`session.py` +70/−20；`limits.py` +40；`index.html` +60/−25 | ~650 行改动 | 新增 ~25 个：两个会话真正并行（mock 加延迟）、全局上限和每客户端上限、满了排队后按顺序执行、每轮释放空位不饿死、只停一个会话、权限 id 跨会话返回 404、后台会话的询问切走不会被自动拒、/mode 和"总是允许"不串到别的会话、同 sid 只有一个运行时、淘汰不动在跑的会话、不带 sid 兼容旧行为。现有 `test_web_busy` / `ratelimit` / `history` 改成在单个会话里断言（行为不变），预计要调 10 来个 |
| **二期：体验 + 文件租约** | 前端：会话列表徽标、`session_status` 状态流、切换不重载、后台会话待确认提醒、全部停止；后端：做法 B 的 `FileLeases`、终审和 verify 里标出其他会话的改动 | `index.html` +180/−60；`server.py`/`runtime.py` +60；`tools/files.py`、`tools/base.py` +80；`review.py` +20 | ~400 行 | ~12 个：租约冲突时报错、一轮结束后释放、状态快照和推送、切换后续传不重复、前端静态检查（参照 test_web_ux） |
| **三期（可选）：按登录用户归属（#106）** | 登录会话记下用户名；会话文件存 owner；列表过滤，越权访问返回 404；并发上限按用户名计；迁移：老会话没有 owner 的归给第一个登录的用户，或者保持公共可见（需要定） | `server.py` +100；`runtime.py` +50；`session.py` +20 | ~200 行 | ~10 个 |
| **四期（可选）：每会话一个 worktree（做法 C）** | 新建会话时可选独立工作区；Session 支持每会话独立的 cwd、权限根和 data_dir；结束时提供合并或丢弃的指引 | `session.py`/`runtime.py` +150；`index.html` +40 | ~250 行 | ~8 个（需要 git，Windows 上要跳过或适配） |

一期做完就能用：可以并行、切换不受阻，#106 的串页面问题也一并解决。二期补上体验和安全网，可以和一期合成一次发布。三期、四期看需求再做。

## 4. 风险

1. **费用和配额**：并行会成倍加快消耗。默认上限要保守（3/2），`daily_runs` 和 `DAILY_LLM_CALLS` 保持全局。
2. **工作区冲突**：bash 和 git 管不住，只能提醒加文件租约。真正的隔离要靠 worktree（四期）。
3. **共享的 MCP 服务器**：有状态的 MCP（比如浏览器、数据库会话）被两个会话同时调用时可能互相干扰。可以考虑一期在 MCP 配置里加 `per_session: true`，让这类服务器每个会话单独起一个进程，或者先在文档里写明。
4. **内存**：每个会话保留 2000 条事件历史和完整 messages，靠 LRU 淘汰和最多 8 个常驻会话控制。RAG 索引现在放在 `agent.state` 里，每个会话会各建一份，可以改成按 cwd 共享缓存（`DocIndex` 本来就有 RLock）。
5. **改动面大**：server.py 现有的 9 个测试文件、约 150 个 Web 测试都依赖 `app.session` / `app.queue`。缓解办法：在 WebApp 上保留 `session`、`queue`、`busy` 这几个兼容属性，都指向默认会话，迁移测试时就不用一次改完。
6. **#93 正在改**：Windows CI 那条线也会动测试和 server 相关文件。一期要等 #93 合进去以后再开分支，避免冲突。

## 5. 需要你定的问题

1. **并发上限默认值**：全局同时跑 3 个、每个客户端 2 个，满了就排队等空位。可以吗？还是先默认 2 个？
2. **工作区冲突**：一期做提醒、二期做文件租约，够不够？要不要把四期的每会话 worktree 提前，甚至作为默认？
3. **#106 / 三期要不要做**：面板定位是"本地单人用，多标签页并行"（做到二期为止，#106 用一期的按标签页隔离来关掉），还是"几个人共用一个自托管面板"（再做三期）？真正的多人隔离建议走 `server/` 平台。
4. **`/model` 和权限模式**：按会话各自独立（我的默认方案），还是保持全局，改一处所有会话都跟着变？
