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

  // 极简 Markdown 渲染（加粗、行内代码、代码块、换行）
  function renderMarkdown(text) {
    let html = escapeHtml(text);
    // 代码块
    html = html.replace(/```([\s\S]*?)```/g, (_, code) =>
      `<pre class="code-block"><code>${code.trim()}</code></pre>`
    );
    // 行内代码
    html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
    // 加粗
    html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    // 换行
    html = html.replace(/\n/g, "<br>");
    return html;
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
