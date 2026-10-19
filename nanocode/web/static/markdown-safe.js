/* Markdown 渲染后的链接/图片清洗（#89）。
 *
 * marked 不负责过滤 URL 协议：`[x](javascript:alert(1))` 会原样生成
 * `<a href="javascript:alert(1)">`。面板里的 token 能调用 /api/chat 和
 * /api/permission，所以一条恶意链接 = 一次潜在的远程命令执行。
 *
 * 这里只放行 http(s) / mailto 和相对路径；其它协议（javascript:、data:、
 * vbscript:、file: …）直接摘掉 href/src。safeUrl 是纯函数，node 下可直接单测。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else { root.safeUrl = api.safeUrl; root.sanitizeHtml = api.sanitizeHtml; }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  const HAS_SCHEME = /^[a-z][a-z0-9+.-]*:/i;
  const SAFE_SCHEME = /^(https?:|mailto:)/i;

  function safeUrl(url) {
    const value = String(url == null ? "" : url).trim();
    if (!value) return false;
    // 浏览器解析 URL 时会忽略控制字符/空白：`java\nscript:` 仍是 javascript:。
    // 判断协议前先按同样规则归一化，避免用换行绕过。
    const probe = value.replace(/[\u0000-\u0020\u007f]+/g, "");
    return SAFE_SCHEME.test(probe) || !HAS_SCHEME.test(probe);
  }

  function sanitizeHtml(html) {
    const tpl = document.createElement("template");
    tpl.innerHTML = html;
    tpl.content.querySelectorAll("a").forEach((a) => {
      const href = a.getAttribute("href");
      if (href == null) return;
      if (!safeUrl(href)) { a.removeAttribute("href"); return; }
      if (SAFE_SCHEME.test(href.trim())) {
        a.setAttribute("target", "_blank");
        a.setAttribute("rel", "noopener noreferrer");
      }
    });
    tpl.content.querySelectorAll("img").forEach((img) => {
      const src = img.getAttribute("src");
      if (src == null) return;
      if (!safeUrl(src)) { img.removeAttribute("src"); return; }
      img.setAttribute("loading", "lazy");
      img.setAttribute("referrerpolicy", "no-referrer");
    });
    return tpl.innerHTML;
  }

  return { safeUrl: safeUrl, sanitizeHtml: sanitizeHtml };
});
