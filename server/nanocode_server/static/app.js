// nanocode 平台主界面：会话列表、模型切换、模型与密钥管理。对话本身是嵌进来的 nanocode 界面（iframe）。
"use strict";

const $ = (s, root = document) => root.querySelector(s);
const state = { me: null, models: [], sessions: [], currentId: null, providers: [], creds: [] };

// ============================================================ 工具

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const COLORS = ["#4f46e5", "#0e9384", "#dd2590", "#f79009", "#2e90fa", "#7a5af8"];
const colorFor = (name) => COLORS[[...name].reduce((a, c) => a + c.charCodeAt(0), 0) % COLORS.length];
const STATUS = { running: "运行中", starting: "启动中", stopped: "已停止", failed: "启动失败" };

function ago(ts) {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "刚刚";
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
  return new Date(ts * 1000).toLocaleDateString();
}

function toast(message, kind = "") {
  const t = document.createElement("div");
  t.className = `toast ${kind}`;
  t.textContent = message;
  $("#toasts").append(t);
  setTimeout(() => t.remove(), kind === "error" ? 6000 : 3000);
}

async function api(method, url, body) {
  const r = await fetch(url, {
    method, headers: body === undefined ? {} : { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (r.status === 401) { location.href = "/login"; throw new Error("未登录"); }
  let data;
  try { data = await r.json(); } catch { data = { ok: false, error: `HTTP ${r.status}` }; }
  if (!r.ok && data.ok === undefined) data.ok = false;
  return data;
}

function confirmDialog(title, text, okLabel = "确定", danger = false) {
  const d = $("#confirm");
  $("#confirm-title").textContent = title;
  $("#confirm-text").textContent = text;
  const ok = $("#confirm-ok");
  ok.textContent = okLabel;
  ok.className = danger ? "btn danger" : "btn primary";
  d.showModal();
  return new Promise((resolve) => d.addEventListener("close", () => resolve(d.returnValue === "ok"), { once: true }));
}

function fillModelSelect(select, selectedRef) {
  const groups = {};
  for (const m of state.models) (groups[m.group] ||= []).push(m);
  select.innerHTML = "";
  if (selectedRef && !state.models.some((m) => m.ref === selectedRef)) {
    select.add(new Option("（这个模型已不可用，请换一个）", selectedRef, true, true));
    select.options[0].disabled = true;
  }
  for (const [group, items] of Object.entries(groups)) {
    const og = document.createElement("optgroup");
    og.label = group;
    for (const m of items) og.append(new Option(m.label, m.ref, false, m.ref === selectedRef));
    select.append(og);
  }
}

// ============================================================ 渲染

const current = () => state.sessions.find((s) => s.id === state.currentId) || null;

function renderUser() {
  const me = state.me;
  const who = me.login || me.user;
  $("#user-name").textContent = who;
  const roles = { dev: "开发模式登录", github: "GitHub 登录", proxy: "单点登录" };
  $("#user-role").textContent = (roles[me.auth] || "") + (me.role === "admin" ? " · 管理员" : "");
  const av = $("#avatar");
  av.textContent = who[0].toUpperCase();
  av.style.background = colorFor(who);
  if (me.avatar_url && /^https:\/\//.test(me.avatar_url)) {
    av.textContent = "";
    const img = document.createElement("img");
    img.src = me.avatar_url; img.alt = ""; img.width = 28; img.height = 28;
    img.style.borderRadius = "50%";
    av.append(img);
  }
  $("#hello").textContent = `你好，${me.name || who}`;
  $("#logout").hidden = me.auth === "proxy";
  $("#admin-link").hidden = me.role !== "admin";
  const p = me.platform;
  $("#facts").innerHTML = [
    `<span class="badge ${p.isolated ? "accent" : ""}" title="沙箱启动器">沙箱：${esc(p.launcher)}${p.isolated ? "（隔离）" : "（本机进程，无隔离）"}</span>`,
    `<span class="badge" title="数据库">数据库：${esc(p.database)}</span>`,
    `<span class="badge" title="限流计数">限流：${esc(p.rate_limit)}</span>`,
  ].join("");
}

function renderUsage() {
  const u = state.me.usage, limit = state.me.daily_limit;
  const hasPlatform = state.models.some((m) => m.billable);
  if (hasPlatform && limit > 0) {
    const pct = Math.min(100, (u.billable_tokens / limit) * 100);
    $("#usage").innerHTML = `<div class="usage-label"><span>今日平台额度</span><span>${(u.billable_tokens / 1000).toFixed(1)}k / ${(limit / 1000).toFixed(0)}k</span></div>
      <div class="meter ${pct > 85 ? "hot" : ""}"><i style="width:${pct}%"></i></div>`;
  } else {
    $("#usage").innerHTML = `<div class="usage-label"><span>今日用量</span><span>${u.total_tokens.toLocaleString()} tokens · ${u.requests} 次调用</span></div>`;
  }
}

function renderSessions() {
  const list = $("#sessions");
  if (!state.sessions.length) {
    list.innerHTML = `<div class="sessions-title">会话</div><div class="sessions-empty">还没有会话</div>`;
    return;
  }
  const label = (ref) => (state.models.find((m) => m.ref === ref) || { label: "模型不可用" }).label;
  list.innerHTML = `<div class="sessions-title">会话</div>` + state.sessions.map((s) => `
    <button class="session ${s.id === state.currentId ? "active" : ""}" data-id="${s.id}" title="${esc(s.title)}">
      <span class="dot ${s.status}" title="${STATUS[s.status] || s.status}"></span>
      <span class="session-body">
        <span class="session-title">${esc(s.title)}</span>
        <span class="session-meta">${esc(label(s.model))} · ${ago(s.last_active)}</span>
      </span>
    </button>`).join("");
  list.querySelectorAll(".session").forEach((b) => b.onclick = () => { location.hash = `#/s/${b.dataset.id}`; closeDrawer(); });
}

function renderTopbar() {
  const s = current();
  for (const id of ["title", "status", "model-picker", "reload", "stop", "delete"]) $("#" + id).hidden = !s;
  if (!s) return;
  $("#title").textContent = s.title;
  const st = $("#status");
  st.querySelector(".dot").className = `dot ${s.status}`;
  st.querySelector("span:last-child").textContent = STATUS[s.status] || s.status;
  $("#stop").hidden = s.status !== "running";
  fillModelSelect($("#model"), s.model);
}

function renderWelcome() {
  fillModelSelect($("#quick-model"), state.models[0]?.ref);
  const hasOwn = state.models.some((m) => m.ref.startsWith("c:"));
  $("#quick-model-hint").textContent = hasOwn ? "" : "目前只有演示模型。添加自己的 API Key 后，这里会出现对应的模型。";
}

function renderAll() {
  renderUser(); renderUsage(); renderSessions(); renderTopbar(); renderWelcome();
}

// ============================================================ 数据

async function loadAll() {
  const [me, models, sessions] = await Promise.all([api("GET", "/api/me"), api("GET", "/api/models"), api("GET", "/api/sessions")]);
  state.me = me; state.models = models.models || []; state.sessions = sessions.sessions || [];
  renderAll();
}

async function refreshSessions() {
  const [me, sessions] = await Promise.all([api("GET", "/api/me"), api("GET", "/api/sessions")]);
  state.me = me; state.sessions = sessions.sessions || [];
  renderUsage(); renderSessions(); renderTopbar();
}

// ============================================================ 会话

const frame = $("#frame");

function showOverlay(text, spinner = true) {
  $("#overlay-text").textContent = text; $("#overlay .spinner").hidden = !spinner; $("#overlay").hidden = false;
}
function hideOverlay() { $("#overlay").hidden = true; }

async function openSession(id) {
  state.currentId = id;
  renderSessions(); renderTopbar();
  if (!id || !current()) {
    frame.hidden = true; frame.removeAttribute("src"); $("#welcome").hidden = false;
    return;
  }
  $("#welcome").hidden = true;
  frame.hidden = false;
  // 先确认沙箱能启动（停止的会话会在这里自动恢复），再加载界面；失败时给出明确的提示而不是在 iframe 里显示错误 JSON
  showOverlay(current().status === "running" ? "正在连接…" : "正在启动沙箱…");
  const st = await api("GET", `/s/${id}/api/state`);
  if (st.ok === false) {
    hideOverlay(); toast(st.error || "沙箱启动失败", "error");
    await refreshSessions();
    return;
  }
  const src = `/s/${id}/?embed=1`;
  if (frame.getAttribute("src") !== src) {
    frame.onload = hideOverlay;
    frame.src = src;
  } else hideOverlay();
  refreshSessions();
}

function route() {
  const m = location.hash.match(/^#\/s\/([0-9a-f]{16})$/);
  openSession(m ? m[1] : null);
}

async function createSession(title, model) {
  showOverlay("正在启动沙箱…");
  $("#welcome").hidden = true;
  const r = await api("POST", "/api/sessions", { title, model });
  hideOverlay();
  if (!r.ok) { toast(r.error, "error"); $("#welcome").hidden = !!state.currentId; return; }
  state.sessions.unshift(r.session);
  location.hash = `#/s/${r.session.id}`;
}

$("#new-session").onclick = () => {
  fillModelSelect($("#new-model"), current()?.model || state.models[0]?.ref);
  $("#new-title").value = "";
  $("#new-dialog").showModal();
};
$("#new-dialog").addEventListener("close", () => {
  if ($("#new-dialog").returnValue === "ok") createSession($("#new-title").value.trim(), $("#new-model").value);
});
$("#quick-create").onsubmit = (e) => { e.preventDefault(); createSession($("#quick-title").value.trim(), $("#quick-model").value); };

$("#model").onchange = async () => {
  const s = current(), ref = $("#model").value;
  showOverlay("正在切换模型，沙箱会重启并恢复对话…");
  const r = await api("POST", `/api/sessions/${s.id}/model`, { model: ref });
  if (!r.ok) { hideOverlay(); toast(r.error, "error"); renderTopbar(); return; }
  Object.assign(s, r.session);
  frame.onload = hideOverlay;
  frame.src = `/s/${s.id}/?embed=1`;
  toast("已切换模型");
  refreshSessions();
};

$("#reload").onclick = () => { if (current()) { showOverlay("正在连接…"); frame.onload = hideOverlay; frame.src = frame.src; } };

$("#stop").onclick = async () => {
  const s = current();
  if (!await confirmDialog("停止沙箱", "停止后工作区和对话都会保留，下次打开会自动恢复。", "停止")) return;
  const r = await api("POST", `/api/sessions/${s.id}/stop`, {});
  if (!r.ok) return toast(r.error, "error");
  frame.removeAttribute("src");
  toast("沙箱已停止");
  await refreshSessions();
  $("#welcome").hidden = true;
  showOverlay("沙箱已停止。点击左侧的会话可以重新打开。", false);
};

$("#delete").onclick = async () => {
  const s = current();
  if (!await confirmDialog("删除会话", `“${s.title}”的对话和工作区文件都会被删除，无法恢复。`, "删除", true)) return;
  const r = await api("POST", `/api/sessions/${s.id}/delete`, {});
  if (!r.ok) return toast(r.error, "error");
  state.sessions = state.sessions.filter((x) => x.id !== s.id);
  location.hash = "";
  toast("已删除");
};

$("#title").onclick = () => {
  const s = current(), el = $("#title");
  const input = document.createElement("input");
  input.className = "input"; input.value = s.title; input.maxLength = 100; input.style.maxWidth = "320px";
  el.replaceWith(input); input.focus(); input.select();
  let done = false;
  const finish = async (save) => {
    if (done) return; done = true;
    input.replaceWith(el);
    const title = input.value.trim();
    if (!save || !title || title === s.title) return;
    const r = await api("POST", `/api/sessions/${s.id}/rename`, { title });
    if (!r.ok) return toast(r.error, "error");
    s.title = title; renderTopbar(); renderSessions();
  };
  input.onkeydown = (e) => { if (e.key === "Enter") finish(true); if (e.key === "Escape") finish(false); };
  input.onblur = () => finish(true);
};
$("#title").onkeydown = (e) => { if (e.key === "Enter") $("#title").click(); };

$("#logout").onclick = async () => { await api("POST", "/api/logout", {}); location.href = "/login"; };

// ============================================================ 设置：模型与密钥

async function loadCreds() {
  const r = await api("GET", "/api/credentials");
  state.creds = r.credentials || [];
  const box = $("#creds");
  if (!state.creds.length) {
    box.innerHTML = `<div class="empty-box">还没有添加 Key。添加后，对应的模型会出现在会话的模型列表里。</div>`;
    return;
  }
  box.innerHTML = state.creds.map((c) => `
    <div class="cred">
      <div class="cred-body">
        <div class="cred-name">${esc(c.label)}</div>
        <div class="cred-meta mono">${esc(c.key_hint)} · ${esc(c.base_url)}</div>
        <div class="cred-models">${c.models.map((m) => `<span class="badge">${esc(m)}</span>`).join("")}</div>
      </div>
      <button class="btn ghost icon-btn" data-del="${c.id}" title="删除" aria-label="删除 ${esc(c.label)}"><svg><use href="#i-trash"/></svg></button>
    </div>`).join("");
  box.querySelectorAll("[data-del]").forEach((b) => b.onclick = async () => {
    const c = state.creds.find((x) => x.id === b.dataset.del);
    $("#settings").close();
    const ok = await confirmDialog("删除 Key", `删除“${c.label}”后，使用它的会话需要换一个模型。`, "删除", true);
    $("#settings").showModal();
    if (!ok) return;
    const r = await api("POST", `/api/credentials/${c.id}/delete`, {});
    if (!r.ok) return toast(r.error, "error");
    toast("已删除");
    await loadCreds(); await reloadModels();
  });
}

async function reloadModels() {
  const r = await api("GET", "/api/models");
  state.models = r.models || [];
  renderSessions(); renderTopbar(); renderWelcome(); renderUsage();
}

function credForm() {
  return {
    provider: $("#c-provider").value, label: $("#c-label").value.trim(), api_key: $("#c-key").value.trim(),
    models: $("#c-models").value, base_url: $("#c-base").value.trim(), protocol: $("#c-protocol").value,
  };
}

function syncProvider() {
  const p = state.providers.find((x) => x.id === $("#c-provider").value);
  $("#custom-fields").hidden = p?.id !== "custom";
  $("#c-test-result").textContent = "";
}

async function openSettings() {
  if (!state.providers.length) {
    const r = await api("GET", "/api/providers");
    state.providers = r.providers || [];
    $("#c-provider").innerHTML = state.providers.map((p) => `<option value="${p.id}">${esc(p.label)}</option>`).join("");
    syncProvider();
  }
  await loadCreds();
  $("#settings").showModal();
}

$("#c-provider").onchange = syncProvider;
$("#open-settings").onclick = openSettings;
document.addEventListener("click", (e) => { if (e.target.closest("[data-open-settings]")) { e.preventDefault(); openSettings(); } });
$("#settings-close").onclick = () => $("#settings").close();

$("#c-test").onclick = async () => {
  const out = $("#c-test-result"), btn = $("#c-test");
  out.className = "test-result"; out.textContent = "正在测试…"; btn.disabled = true;
  const r = await api("POST", "/api/credentials/test", credForm());
  btn.disabled = false;
  out.className = `test-result ${r.ok ? "ok" : "err"}`;
  out.textContent = r.ok ? "✓ 连接成功，Key 和模型都可用" : `✗ ${r.error}`;
};

$("#cred-form").onsubmit = async (e) => {
  e.preventDefault();
  const btn = $("#c-save");
  btn.disabled = true;
  const r = await api("POST", "/api/credentials", credForm());
  btn.disabled = false;
  if (!r.ok) { $("#c-test-result").className = "test-result err"; $("#c-test-result").textContent = r.error; return; }
  $("#cred-form").reset(); syncProvider();
  toast(`已添加 ${r.credential.label}`);
  await loadCreds(); await reloadModels();
};

// ============================================================ 窄屏抽屉

function closeDrawer() { $("#app").classList.remove("drawer-open"); }
$("#menu").onclick = () => $("#app").classList.toggle("drawer-open");
$("#app").addEventListener("click", (e) => { if (e.target === $("#app")) closeDrawer(); });

// ============================================================ 启动

// 自适应轮询（#145）：refreshSessions 每次都要查库（/api/me + /api/sessions），
// 可见但长时间无变化的页签不能一直每 10 秒唤醒数据库（托管 Postgres 缩不到零）。
// 无变化时间隔 10s→20s→40s→60s 封顶；有变化、回到前台或用户操作时立刻回到 10s。
// 隐藏页签本来就不轮询（下面的 document.hidden 判断）。
let pollMs = 10000, pollTimer = 0, lastPollSig = "";
function pollSig() { return JSON.stringify([state.me && state.me.usage, state.sessions]); }
function schedulePoll() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    if (!document.hidden) {
      try {
        await refreshSessions();
        const sig = pollSig();
        pollMs = (sig === lastPollSig) ? Math.min(pollMs * 2, 60000) : 10000;
        lastPollSig = sig;
      } catch { /* 网络抖动不断轮询，下次按原间隔重试 */ }
    }
    schedulePoll();
  }, pollMs);
}
document.addEventListener("visibilitychange", () => {
  if (document.hidden) return;
  pollMs = 10000;                                    // 回到前台：立即刷新一次，间隔重置
  refreshSessions().then(() => { lastPollSig = pollSig(); }).catch(() => {});
  schedulePoll();
});

window.addEventListener("hashchange", route);
loadAll().then(() => { lastPollSig = pollSig(); route(); });
schedulePoll();
