/* ============================================================
 * RAG 知识库助手 - 公共脚本
 * 用户端 (web/user) 与管理端 (web/admin) 共用
 * 无构建、无 ES module，页面直接 <script src="..."> 引入
 * ============================================================ */
(function (global) {
  "use strict";

  // 后端地址：默认本机；如前后端分离部署可改为 http://localhost:8000
  const API_BASE = "http://127.0.0.1:8000";
  const TOKEN_KEY = "rag_token";

  // ---------------- 基础工具 ----------------
  const $ = (id) => document.getElementById(id);

  function show(el) {
    el && el.classList.remove("hidden");
  }

  function hide(el) {
    el && el.classList.add("hidden");
  }

  const HTML_ESCAPES = {
    "&": "&" + "amp;",
    "<": "&" + "lt;",
    ">": "&" + "gt;",
    '"': "&" + "quot;",
    "'": "&" + "#39;",
  };

  function escapeHtml(str) {
    return String(str).replace(/[&<>"']/g, (ch) => HTML_ESCAPES[ch]);
  }

  // ---------------- Markdown 渲染 ----------------
  // 无依赖的极简 Markdown -> HTML（块级）：标题 / 列表（嵌套）/ 代码块 / 引用 /
  // 分隔线 / 表格 / 段落；行内：加粗、斜体、删除线、行内码、链接。
  // 先 escapeHtml，再按行解析，不会执行任何用户输入的 HTML。

  // 行内元素（输入必须已 escapeHtml）
  function renderInline(text) {
    let s = text;
    s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
    s = s.replace(/\*\*\*([^*]+)\*\*\*/g, "<strong><em>$1</em></strong>");
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/~~([^~]+)~~/g, "<del>$1</del>");
    s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
    s = s.replace(
      /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>'
    );
    return s;
  }

  // 列表：按缩进层级嵌套（2 空格/层，tab 视作 2 空格）
  function renderList(items) {
    let html = "";
    const stack = []; // { ordered, indent, liOpen }
    const closeItem = () => {
      const top = stack[stack.length - 1];
      if (top && top.liOpen) {
        html += "</li>";
        top.liOpen = false;
      }
    };
    items.forEach((it) => {
      while (stack.length && it.indent < stack[stack.length - 1].indent) {
        closeItem();
        const top = stack.pop();
        html += top.ordered ? "</ol>" : "</ul>";
      }
      const top = stack[stack.length - 1];
      if (!top || it.indent > top.indent) {
        stack.push({ ordered: it.ordered, indent: it.indent, liOpen: false });
        html += it.ordered
          ? `<ol${it.num && it.num !== 1 ? ` start="${it.num}"` : ""}>`
          : "<ul>";
      } else if (top.ordered !== it.ordered) {
        closeItem();
        stack.pop();
        html += top.ordered ? "</ol>" : "</ul>";
        stack.push({ ordered: it.ordered, indent: it.indent, liOpen: false });
        html += it.ordered ? "<ol>" : "<ul>";
      } else {
        closeItem();
      }
      html += `<li>${renderInline(escapeHtml(it.text))}`;
      stack[stack.length - 1].liOpen = true;
    });
    while (stack.length) {
      closeItem();
      const top = stack.pop();
      html += top.ordered ? "</ol>" : "</ul>";
    }
    return html;
  }

  const RE_HEADING = /^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
  const RE_HR = /^ {0,3}([-*_])[ \t]*(\1[ \t]*){2,}$/;
  const RE_QUOTE = /^ {0,3}>\s?(.*)$/;
  const RE_ITEM = /^(\s*)([-*+]|\d{1,3}[.)])\s+(.*)$/;
  const RE_TABLE_ROW = /^\s*\|.*\|\s*$/;
  const RE_TABLE_SEP = /^\s*\|?[\s:|-]+\|?\s*$/;

  function splitTableRow(line) {
    return line
      .trim()
      .replace(/^\|/, "")
      .replace(/\|$/, "")
      .split("|")
      .map((c) => c.trim());
  }

  function renderMarkdown(text) {
    if (text == null) return "";
    const lines = String(text).replace(/\r\n?/g, "\n").split("\n");
    const out = [];
    let para = [];

    const flushPara = () => {
      if (!para.length) return;
      out.push(`<p>${para.map((l) => renderInline(escapeHtml(l))).join("<br>")}</p>`);
      para = [];
    };

    let i = 0;
    while (i < lines.length) {
      const line = lines[i];

      // 空行：段落结束
      if (!line.trim()) {
        flushPara();
        i += 1;
        continue;
      }

      // 代码块 ```lang
      if (/^\s*```/.test(line)) {
        flushPara();
        const lang = line.trim().replace(/^```/, "").trim();
        const code = [];
        i += 1;
        while (i < lines.length && !/^\s*```/.test(lines[i])) {
          code.push(lines[i]);
          i += 1;
        }
        i += 1; // 跳过结束 ```
        const cls = lang ? ` class="language-${escapeHtml(lang)}"` : "";
        out.push(`<pre class="code-block"><code${cls}>${escapeHtml(code.join("\n"))}</code></pre>`);
        continue;
      }

      // 分隔线
      if (RE_HR.test(line)) {
        flushPara();
        out.push("<hr>");
        i += 1;
        continue;
      }

      // 标题
      const heading = line.match(RE_HEADING);
      if (heading) {
        flushPara();
        const level = Math.min(heading[1].length, 6);
        out.push(`<h${level}>${renderInline(escapeHtml(heading[2]))}</h${level}>`);
        i += 1;
        continue;
      }

      // 引用块（连续多行合并）
      if (RE_QUOTE.test(line)) {
        flushPara();
        const quoted = [];
        while (i < lines.length && RE_QUOTE.test(lines[i])) {
          quoted.push(lines[i].match(RE_QUOTE)[1]);
          i += 1;
        }
        out.push(
          `<blockquote>${quoted.map((l) => renderInline(escapeHtml(l))).join("<br>")}</blockquote>`
        );
        continue;
      }

      // 表格（| a | b | + | --- | --- |）
      if (RE_TABLE_ROW.test(line) && i + 1 < lines.length && RE_TABLE_SEP.test(lines[i + 1])) {
        flushPara();
        const head = splitTableRow(line);
        i += 2;
        const body = [];
        while (i < lines.length && RE_TABLE_ROW.test(lines[i])) {
          body.push(splitTableRow(lines[i]));
          i += 1;
        }
        const th = head.map((c) => `<th>${renderInline(escapeHtml(c))}</th>`).join("");
        const trs = body
          .map(
            (cells) =>
              `<tr>${cells.map((c) => `<td>${renderInline(escapeHtml(c))}</td>`).join("")}</tr>`
          )
          .join("");
        out.push(
          `<div class="table-wrap"><table><thead><tr>${th}</tr></thead><tbody>${trs}</tbody></table></div>`
        );
        continue;
      }

      // 列表（连续列表项，可嵌套；列表项之间允许空行，与 CommonMark 一致）
      if (RE_ITEM.test(line)) {
        flushPara();
        const items = [];
        while (i < lines.length) {
          if (RE_ITEM.test(lines[i])) {
            const m = lines[i].match(RE_ITEM);
            const indentStr = m[1].replace(/\t/g, "  ");
            items.push({
              indent: Math.floor(indentStr.length / 2),
              ordered: /\d/.test(m[2]),
              num: parseInt(m[2], 10) || 1,
              text: lines[i].replace(RE_ITEM, "$3"),
            });
            i += 1;
            continue;
          }
          // 空行后仍是列表项 → 跳过空行继续（loose list）
          if (!lines[i].trim()) {
            let j = i;
            while (j < lines.length && !lines[j].trim()) j += 1;
            if (j < lines.length && RE_ITEM.test(lines[j])) {
              i = j;
              continue;
            }
          }
          break;
        }
        out.push(renderList(items));
        continue;
      }

      // 普通段落行
      para.push(line.trim());
      i += 1;
    }
    flushPara();
    return out.join("");
  }

  function formatTime(value) {
    if (!value) return "-";
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return "-";
    return d.toLocaleString();
  }

  // ---------------- Token ----------------
  function getToken() {
    return localStorage.getItem(TOKEN_KEY) || "";
  }

  function setToken(token) {
    if (token) localStorage.setItem(TOKEN_KEY, token);
  }

  function clearToken() {
    localStorage.removeItem(TOKEN_KEY);
  }

  // ---------------- 请求封装 ----------------
  // 401 时的处理由页面注入（各自做 UI 收尾）
  let authFailureHandler = null;

  function setAuthFailureHandler(fn) {
    authFailureHandler = typeof fn === "function" ? fn : null;
  }

  async function api(path, options = {}) {
    const headers = options.headers || {};
    const token = getToken();
    if (token) headers["Authorization"] = `Bearer ${token}`;
    if (options.body && !(options.body instanceof FormData)) {
      headers["Content-Type"] = "application/json";
    }

    let resp;
    try {
      resp = await fetch(API_BASE + path, { ...options, headers });
    } catch (err) {
      // 网络层失败（后端未启动 / 断网 / 跨域被拦）
      throw new Error("无法连接服务器，请检查网络或后端服务是否已启动");
    }

    const data = await resp.json().catch(() => ({}));

    if (resp.status === 401) {
      // 登录接口的 401 是“用户名或密码错误”，不是登录态过期
      const isLogin = path === "/users/login";
      if (isLogin) {
        throw new Error(data.detail || data.msg || "用户名或密码错误");
      }
      // 其余接口的 401 = token 失效：交给页面处理（回到登录页）
      if (token && authFailureHandler) authFailureHandler();
      throw new Error(data.detail || "登录已过期，请重新登录");
    }

    if (!resp.ok) {
      throw new Error(data.detail || data.msg || `请求失败 (${resp.status})`);
    }
    return data;
  }

  function fetchMe() {
    return api("/users/me");
  }

  function isAdminRole(user) {
    return !!user && user.role === "admin";
  }

  // ---------------- 弹窗 ----------------
  let modalBound = false;

  function bindModalEvents() {
    if (modalBound) return;
    modalBound = true;
    const overlay = $("modal-overlay");
    const closeBtn = $("modal-close");
    if (closeBtn) closeBtn.addEventListener("click", closeModal);
    if (overlay) {
      overlay.addEventListener("click", (e) => {
        if (e.target === overlay) closeModal();
      });
    }
  }

  function openModal(title, bodyHtml, footerButtons) {
    bindModalEvents();
    const titleEl = $("modal-title");
    const bodyEl = $("modal-body");
    const footer = $("modal-footer");
    if (!titleEl || !bodyEl || !footer) return;
    titleEl.textContent = title;
    bodyEl.innerHTML = bodyHtml;
    footer.innerHTML = "";
    (footerButtons || []).forEach((btn) => {
      const b = document.createElement("button");
      b.className = btn.className || "btn-small";
      b.textContent = btn.text;
      b.addEventListener("click", btn.onClick);
      footer.appendChild(b);
    });
    show($("modal-overlay"));
  }

  function closeModal() {
    hide($("modal-overlay"));
    const bodyEl = $("modal-body");
    const footer = $("modal-footer");
    if (bodyEl) bodyEl.innerHTML = "";
    if (footer) footer.innerHTML = "";
  }

  // ---------------- 输入框自适应 ----------------
  function autoResize(el) {
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 200) + "px";
  }

  // ---------------- 文档上传 + 任务轮询 ----------------
  // onUpdate({ phase, fileName, status, progress, error, taskId })
  // phase: uploading | queued | polling | success | failed | error
  // 上传失败（网络/校验）会 reject；轮询失败通过 phase="error" 通知后 resolve
  async function uploadDocument(file, onUpdate) {
    const notify = typeof onUpdate === "function" ? onUpdate : () => {};
    if (!file) throw new Error("未选择文件");

    notify({ phase: "uploading", fileName: file.name });
    const form = new FormData();
    form.append("file", file);
    const data = await api("/upload", { method: "POST", body: form });
    notify({ phase: "queued", fileName: file.name, taskId: data.task_id });

    return pollTask(data.task_id, file.name, notify);
  }

  function pollTask(taskId, fileName, notify, intervalMs = 2000) {
    return new Promise((resolve) => {
      const tick = async () => {
        let t;
        try {
          t = await api(`/task/${taskId}`);
        } catch (err) {
          notify({ phase: "error", fileName, error: err.message });
          resolve({ status: "error", error_msg: err.message });
          return;
        }

        const status = (t && t.status) || "unknown";
        notify({
          phase: "polling",
          fileName,
          status,
          progress: t ? t.progress : null,
          error: t ? t.error_msg : null,
        });

        if (status === "success" || status === "done" || status === "completed") {
          notify({ phase: "success", fileName, status, progress: 100 });
          resolve(t);
          return;
        }
        if (status === "failed" || status === "error") {
          notify({
            phase: "failed",
            fileName,
            status,
            error: (t && t.error_msg) || "解析失败",
          });
          resolve(t);
          return;
        }
        setTimeout(tick, intervalMs);
      };
      tick();
    });
  }

  // ---------------- 导出 ----------------
  global.RAG = {
    API_BASE,
    TOKEN_KEY,
    $,
    show,
    hide,
    escapeHtml,
    renderMarkdown,
    formatTime,
    getToken,
    setToken,
    clearToken,
    api,
    fetchMe,
    isAdminRole,
    setAuthFailureHandler,
    openModal,
    closeModal,
    autoResize,
    uploadDocument,
  };
})(window);
