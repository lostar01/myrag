/* ============================================================
 * RAG 知识库助手 - 用户端
 * 功能：登录/注册、对话（会话 + SSE 流式问答）、文件上传、修改密码
 * 依赖：../common.js (window.RAG)
 * ============================================================ */
(function () {
  "use strict";

  const {
    $,
    show,
    hide,
    escapeHtml,
    renderMarkdown,
    formatTime,
    api,
    fetchMe,
    isAdminRole,
    setAuthFailureHandler,
    openModal,
    closeModal,
    autoResize,
    uploadDocument,
  } = window.RAG;

  // ---------------- 全局状态 ----------------
  const state = {
    user: null,
    currentChatId: null,
    sessions: [],
    streaming: false,
  };

  // ---------------- 入口跳转 ----------------
  function goEntry() {
    window.location.replace("../index.html");
  }

  function logout() {
    state.user = null;
    state.currentChatId = null;
    state.sessions = [];
    state.streaming = false;
    window.RAG.clearToken();
    closeUserMenu();
    closeModal();
    goEntry();
  }

  async function enterApp() {
    show($("app-view"));
    renderUser();
    // 管理员额外显示「管理端」入口
    if (isAdminRole(state.user)) {
      show($("admin-link"));
    } else {
      hide($("admin-link"));
    }
    await loadSessions();
    showChatView();
  }

  function renderUser() {
    const u = state.user || {};
    const name = u.nickname || u.username || "用户";
    $("user-name").textContent = name;
    $("user-avatar").textContent = name.charAt(0).toUpperCase();
    $("user-role").textContent = isAdminRole(u) ? "管理员" : "普通用户";
  }

  // ---------------- 会话管理 ----------------
  async function loadSessions() {
    try {
      const data = await api("/sessions");
      state.sessions = data.sessions || [];
      renderSessions();
    } catch (err) {
      console.error("加载会话失败", err);
    }
  }

  function renderSessions() {
    const list = $("session-list");
    list.innerHTML = "";
    if (!state.sessions.length) {
      list.innerHTML = `<div class="session-empty">暂无对话</div>`;
      return;
    }
    state.sessions.forEach((s) => {
      const item = document.createElement("div");
      item.className = "session-item" + (s.chat_id === state.currentChatId ? " active" : "");
      item.dataset.id = s.chat_id;
      item.innerHTML = `
        <span class="session-title">${escapeHtml(s.title || "新对话")}</span>
        <button class="session-del" title="删除">✕</button>
      `;
      item.querySelector(".session-title").addEventListener("click", () => openSession(s.chat_id));
      item.querySelector(".session-del").addEventListener("click", (ev) => {
        ev.stopPropagation();
        deleteSession(s.chat_id);
      });
      list.appendChild(item);
    });
  }

  async function createSession() {
    try {
      const data = await api("/sessions", {
        method: "POST",
        body: JSON.stringify({ title: "新对话" }),
      });
      state.currentChatId = data.session.chat_id;
      await loadSessions();
      clearMessages();
      showChatView();
      $("chat-title").textContent = "新对话";
      $("input").focus();
    } catch (err) {
      alert("创建会话失败：" + err.message);
    }
  }

  async function openSession(chatId) {
    state.currentChatId = chatId;
    renderSessions();
    showChatView();
    clearMessages();
    const session = state.sessions.find((s) => s.chat_id === chatId);
    $("chat-title").textContent = session ? session.title || "对话" : "对话";
    try {
      const data = await api(`/sessions/${chatId}/messages`);
      (data.messages || []).forEach((m) => {
        appendMessage(m.role, m.content);
      });
      scrollToBottom();
    } catch (err) {
      console.error("加载消息失败", err);
    }
  }

  async function deleteSession(chatId) {
    if (!confirm("确定删除该对话？")) return;
    try {
      await api(`/sessions/${chatId}`, { method: "DELETE" });
      if (state.currentChatId === chatId) {
        state.currentChatId = null;
        clearMessages();
        $("chat-title").textContent = "新对话";
      }
      await loadSessions();
    } catch (err) {
      alert("删除失败：" + err.message);
    }
  }

  // ---------------- 聊天 ----------------
  function clearMessages() {
    $("messages").innerHTML = "";
  }

  function scrollToBottom() {
    const box = $("messages");
    box.scrollTop = box.scrollHeight;
  }

  function appendMessage(role, content) {
    const box = $("messages");
    const wrap = document.createElement("div");
    // 注意：class 必须是 `msg user` / `msg assistant`（两个独立 class），
    // CSS 里用的是 `.msg.user` / `.msg.assistant` 这种复合选择器；
    // 写成 `msg-user` 会导致气泡 / 对齐 / 头像配色全部不生效。
    wrap.className = `msg ${role}`;
    wrap.dataset.role = role;

    // 头像
    const avatar = document.createElement("div");
    avatar.className = "msg-avatar";
    if (role === "assistant") {
      avatar.textContent = "📚";
    } else {
      const name = (state.user && (state.user.nickname || state.user.username)) || "U";
      avatar.textContent = name.charAt(0).toUpperCase();
    }

    // 正文（样式对齐 style.css 的 .msg-body / .msg-content）
    const body = document.createElement("div");
    body.className = "msg-body";
    const bubble = document.createElement("div");
    bubble.className = "msg-content";
    if (role === "assistant") {
      bubble.innerHTML = renderMarkdown(content || "");
    } else {
      bubble.textContent = content || "";
    }
    body.appendChild(bubble);
    wrap.appendChild(avatar);
    wrap.appendChild(body);
    box.appendChild(wrap);
    scrollToBottom();
    return { wrap, bubble };
  }

  // 更新会话标题（后端在新会话第一问后自动命名，通过 SSE 的 title 事件下发）
  function applySessionTitle(chatId, title) {
    if (!chatId || !title) return;
    const session = state.sessions.find((s) => s.chat_id === chatId);
    if (session) session.title = title;
    if (state.currentChatId === chatId) {
      $("chat-title").textContent = title;
    }
    renderSessions();
  }

  async function sendMessage() {
    if (state.streaming) return;
    const input = $("input");
    const query = input.value.trim();
    if (!query) return;

    // 没有会话则先创建
    if (!state.currentChatId) {
      await createSession();
      if (!state.currentChatId) return;
    }

    input.value = "";
    autoResize(input);
    appendMessage("user", query);

    // 助手占位气泡
    const { bubble } = appendMessage("assistant", "");
    bubble.innerHTML = `<span class="typing">思考中…</span>`;

    state.streaming = true;
    $("send-btn").disabled = true;

    let answer = "";
    let sourcesHtml = "";

    try {
      const url = `${window.RAG.API_BASE}/chat/stream?chat_id=${encodeURIComponent(
        state.currentChatId
      )}&query=${encodeURIComponent(query)}`;
      const token = window.RAG.getToken();
      const resp = await fetch(url, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (resp.status === 401) {
        logout();
        throw new Error("登录已过期，请重新登录");
      }
      if (!resp.ok || !resp.body) {
        throw new Error(`流式请求失败 (${resp.status})`);
      }

      const reader = resp.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        // 按 SSE 事件分隔（\n\n）切分
        let idx;
        while ((idx = buffer.indexOf("\n\n")) !== -1) {
          const rawEvent = buffer.slice(0, idx);
          buffer = buffer.slice(idx + 2);
          const line = rawEvent.split("\n").find((l) => l.startsWith("data:"));
          if (!line) continue;
          const jsonStr = line.slice(5).trim();
          if (!jsonStr) continue;
          let evt;
          try {
            evt = JSON.parse(jsonStr);
          } catch {
            continue;
          }
          handleStreamEvent(evt, bubble, (a) => (answer = a), (s) => (sourcesHtml = s));
        }
      }

      // 收尾渲染
      if (answer) {
        bubble.innerHTML = renderMarkdown(answer) + sourcesHtml;
      } else if (!sourcesHtml) {
        bubble.innerHTML = `<span class="msg-empty">（无回答）</span>`;
      }
    } catch (err) {
      bubble.innerHTML = `<span class="msg-error">出错了：${escapeHtml(err.message)}</span>`;
    } finally {
      state.streaming = false;
      $("send-btn").disabled = false;
      scrollToBottom();
      // 首条消息后刷新会话标题
      if (
        state.sessions.find(
          (s) => s.chat_id === state.currentChatId && s.title === "新对话"
        )
      ) {
        loadSessions();
      }
    }
  }

  function renderSources(sources) {
    if (!sources || !sources.length) return "";
    const items = sources
      .map((s, i) => {
        // 后端 sources 字段：section_path / title / page_no / doc_id
        const name = s.section_path || s.title || s.doc_name || s.doc_id || `来源 ${i + 1}`;
        const page = s.page_no != null ? ` · 第 ${s.page_no} 页` : "";
        const score =
          s.rerank_score != null
            ? ` (${Number(s.rerank_score).toFixed(3)})`
            : s.score != null
            ? ` (${Number(s.score).toFixed(3)})`
            : "";
        return `<div class="source-item"><span class="source-idx">${i + 1}</span>${escapeHtml(
          name
        )}${escapeHtml(page)}${score}</div>`;
      })
      .join("");
    return `<div class="sources"><div class="sources-title">📎 参考来源</div>${items}</div>`;
  }

  function handleStreamEvent(evt, bubble, setAnswer, setSources) {
    if (evt.type === "sources") {
      setSources(renderSources(evt.sources || []));
    } else if (evt.type === "delta") {
      // 增量内容：累积并实时渲染
      const cur = bubble.dataset.acc || "";
      const next = cur + (evt.content || "");
      bubble.dataset.acc = next;
      setAnswer(next);
      bubble.innerHTML = renderMarkdown(next);
      scrollToBottom();
    } else if (evt.type === "done") {
      const final = evt.answer || bubble.dataset.acc || "";
      setAnswer(final);
      bubble.dataset.acc = final;
      bubble.innerHTML = renderMarkdown(final);
      // 回答已结束：立即恢复输入，不等后续的 title 事件（标题生成可能慢）
      state.streaming = false;
      $("send-btn").disabled = false;
    } else if (evt.type === "title") {
      // 新会话第一问后自动命名的标题
      applySessionTitle(evt.chat_id, evt.title);
    } else if (evt.type === "error") {
      bubble.innerHTML = `<span class="msg-error">出错了：${escapeHtml(
        evt.message || "未知错误"
      )}</span>`;
    }
  }

  // ---------------- 视图切换 ----------------
  function showChatView() {
    stopDocsAutoRefresh();
    show($("chat-view"));
    hide($("docs-view"));
  }

  function showDocsView() {
    hide($("chat-view"));
    show($("docs-view"));
    loadDocuments();
  }

  // ---------------- 上传记录（上传 + 我的上传记录） ----------------
  // 状态文案：pending / running 来自 rag_task，success / failed 为最终态
  const STATUS_TEXT = {
    pending: "等待解析",
    running: "解析中",
    processing: "解析中",
    success: "成功",
    failed: "失败",
  };

  function statusText(status) {
    return STATUS_TEXT[status] || status || "未知";
  }

  // 有记录处于等待/解析中时，每 3s 静默刷新一次（离开视图停止）
  let docsAutoTimer = null;

  function stopDocsAutoRefresh() {
    if (docsAutoTimer) {
      clearTimeout(docsAutoTimer);
      docsAutoTimer = null;
    }
  }

  function scheduleDocsAutoRefresh(hasActive) {
    stopDocsAutoRefresh();
    if (!hasActive) return;
    const view = $("docs-view");
    if (view && view.classList.contains("hidden")) return;
    docsAutoTimer = setTimeout(() => loadDocuments(true), 3000);
  }

  function renderDocRow(r) {
    const tr = document.createElement("tr");
    const status = r.status || "unknown";
    // 文件名：绝不能用 doc_id / task_id 兜底（uuid 十六进制看起来像 md5，会让人误读）
    const name = r.file_name || r.doc_name || "（未知文件名）";
    const noName = !(r.file_name || r.doc_name);
    // 失败原因：鼠标悬停在状态徽标上显示（后端已截断为 500 字）
    const tip = status === "failed" && r.error_msg ? ` title="${escapeHtml(r.error_msg)}"` : "";
    tr.innerHTML = `
      <td class="cell-username"${noName ? ` title="${escapeHtml(r.doc_id || "")}"` : ""}>${escapeHtml(
      name
    )}</td>
      <td>${r.page_count != null ? escapeHtml(String(r.page_count)) : "-"}</td>
      <td><span class="doc-status ${escapeHtml(status)}"${tip}>${escapeHtml(
      statusText(status)
    )}</span></td>
      <td class="cell-time">${escapeHtml(formatTime(r.created_at))}</td>
    `;
    return tr;
  }

  async function loadDocuments(silent) {
    const tbody = $("docs-tbody");
    if (!tbody) return;
    if (!silent) {
      tbody.innerHTML = `<tr><td colspan="4" class="users-loading">加载中…</td></tr>`;
    }
    try {
      const data = await api("/uploads");
      const records = data.records || [];
      if (!records.length) {
        tbody.innerHTML = `<tr><td colspan="4" class="users-empty">暂无上传记录，请上传文件</td></tr>`;
        scheduleDocsAutoRefresh(false);
        return;
      }
      tbody.innerHTML = "";
      records.forEach((r) => tbody.appendChild(renderDocRow(r)));
      scheduleDocsAutoRefresh(records.some((r) => r.is_active === true));
    } catch (err) {
      tbody.innerHTML = `<tr><td colspan="4" class="users-empty">加载失败：${escapeHtml(
        err.message
      )}</td></tr>`;
      scheduleDocsAutoRefresh(false);
    }
  }

  function renderUploadProgress(info) {
    const prog = $("upload-progress");
    show(prog);
    const name = escapeHtml(info.fileName || "");
    if (info.phase === "uploading") {
      prog.innerHTML = `<div class="prog-text">正在上传 ${name}…</div>`;
    } else if (info.phase === "queued") {
      prog.innerHTML = `<div class="prog-text">已提交解析任务：${name}</div>`;
    } else if (info.phase === "polling") {
      const progress = info.progress != null ? ` (${info.progress}%)` : "";
      prog.innerHTML = `<div class="prog-text">${name}：${escapeHtml(
        info.status || "处理中"
      )}${progress}</div>`;
    } else if (info.phase === "success") {
      prog.innerHTML = `<div class="prog-text">✅ ${name} 解析完成</div>`;
      loadDocuments();
    } else if (info.phase === "failed") {
      prog.innerHTML = `<div class="prog-text msg-error">❌ ${name} 解析失败：${escapeHtml(
        info.error || ""
      )}</div>`;
      loadDocuments();
    } else if (info.phase === "error") {
      prog.innerHTML = `<div class="prog-text msg-error">查询任务失败：${escapeHtml(
        info.error || ""
      )}</div>`;
    }
  }

  async function handleFile(file) {
    if (!file) return;
    try {
      await uploadDocument(file, renderUploadProgress);
    } catch (err) {
      show($("upload-progress"));
      $("upload-progress").innerHTML = `<div class="prog-text msg-error">上传失败：${escapeHtml(
        err.message
      )}</div>`;
    }
  }

  // ---------------- 修改密码 ----------------
  function openChangePasswordModal() {
    const body = `
      <div class="form-field">
        <label>旧密码</label>
        <input type="password" id="m-oldpwd" autocomplete="current-password" placeholder="请输入当前密码" />
      </div>
      <div class="form-field">
        <label>新密码</label>
        <input type="password" id="m-newpwd" autocomplete="new-password" placeholder="至少6位" />
      </div>
      <div class="form-field">
        <label>确认新密码</label>
        <input type="password" id="m-confirmpwd" autocomplete="new-password" placeholder="再次输入新密码" />
      </div>
      <div class="form-error" id="m-error"></div>
    `;
    openModal("修改密码", body, [
      { text: "取消", className: "btn-small", onClick: closeModal },
      {
        text: "保存",
        className: "btn-small btn-accent",
        onClick: async () => {
          const errEl = $("m-error");
          errEl.textContent = "";
          const oldPwd = $("m-oldpwd").value;
          const newPwd = $("m-newpwd").value;
          const confirmPwd = $("m-confirmpwd").value;

          if (!oldPwd) return (errEl.textContent = "请输入旧密码");
          if (newPwd.length < 6) return (errEl.textContent = "新密码至少6位");
          if (newPwd !== confirmPwd) return (errEl.textContent = "两次输入的新密码不一致");
          if (newPwd === oldPwd) return (errEl.textContent = "新密码不能与旧密码相同");

          try {
            await api("/users/me/password", {
              method: "PUT",
              body: JSON.stringify({ old_password: oldPwd, new_password: newPwd }),
            });
            closeModal();
            alert("密码修改成功，请使用新密码重新登录");
            logout();
          } catch (err) {
            errEl.textContent = err.message;
          }
        },
      },
    ]);
  }

  // ---------------- 我的记忆 ----------------
  // 记忆是完全私有且可完全关掉的：看到什么、删什么、一键清空，全部在这个弹窗里。
  const MEMORY_KIND_LABEL = { profile: "画像", preference: "偏好" };

  function memoryRow(m) {
    const meta = [];
    meta.push(`权重 ${Number(m.weight || 0).toFixed(2)}`);
    meta.push(`命中 ${m.hits || 0} 次`);
    meta.push(m.source_chat_id ? "自动学习" : "手动添加");
    if (m.expires_at) meta.push(`有效期至 ${formatTime(m.expires_at)}`);
    return `
      <div class="memory-item">
        <div class="memory-content">${escapeHtml(m.content)}</div>
        <div class="memory-meta">
          <span class="memory-kind">${MEMORY_KIND_LABEL[m.kind] || "偏好"}</span>
          <span>${escapeHtml(meta.join(" · "))}</span>
          <button class="btn-small btn-danger memory-del" data-del="${escapeHtml(m.memory_id)}">删除</button>
        </div>
      </div>`;
  }

  async function openMemoriesModal() {
    let data;
    try {
      data = await api("/memories?limit=200");
    } catch (err) {
      alert("读取记忆失败：" + err.message);
      return;
    }

    const items = data.items || [];
    const archived = data.archived_total || 0;
    let hint = data.memory_enabled
      ? "系统会在每次回答后自动学习你稳定的背景与偏好（回答风格、关注领域等），提问时带进上下文。觉得不对就直接删。"
      : "记忆功能当前未开启（config.yaml 的 memory.enable = false），下面只是历史遗留条目，可以逐条删除或全部清空。";
    // 这条很重要：条目被自动去重/过期归档时，列表会突然变 0 条，不解释就是“我的记忆丢了”
    if (archived > 0 && items.length === 0) {
      hint += `（另有 ${archived} 条旧记录已被归档：自动去重、条目封顶或过期，也可能你之前删过；它们不会再被使用，也不在下面列出。）`;
    }

    const body = `
      <div class="memory-hint">${hint}</div>
      <div class="memory-list">
        ${items.length ? items.map(memoryRow).join("") : '<div class="memory-empty">还没有记忆条目</div>'}
      </div>
      <div class="memory-form">
        <div class="form-field memory-form-input">
          <label for="m-memory-content">手动补一条（最多 200 字）</label>
          <input id="m-memory-content" maxlength="200" placeholder="例：回答要简短，结论写在前面" />
        </div>
        <div class="form-field memory-form-kind">
          <label for="m-memory-kind">类型</label>
          <select id="m-memory-kind">
            <option value="preference">偏好</option>
            <option value="profile">画像</option>
          </select>
        </div>
        <button class="btn-small btn-accent" id="m-memory-add" type="button">添加</button>
      </div>
      <div class="form-error" id="m-memory-error"></div>
    `;

    openModal("我的记忆", body, [
      { text: "关闭", className: "btn-small", onClick: closeModal },
      {
        text: "清空全部记忆",
        className: "btn-small btn-danger",
        onClick: async () => {
          if (!confirm("确定清空我的全部记忆？此操作不可撤销。")) return;
          try {
            const res = await api("/memories", { method: "DELETE" });
            alert(res.msg || "已清空");
            closeModal();
          } catch (err) {
            alert("清空失败：" + err.message);
          }
        },
      },
    ]);

    // openModal 只给 footer 按钮绑事件，列表行内/表单里的事件要自己接
    const addBtn = $("m-memory-add");
    const input = $("m-memory-content");
    const submitAdd = async () => {
      const errEl = $("m-memory-error");
      errEl.textContent = "";
      const content = input.value.trim();
      if (!content) return (errEl.textContent = "请输入记忆内容");
      addBtn.disabled = true;
      try {
        await api("/memories", {
          method: "POST",
          body: JSON.stringify({ kind: $("m-memory-kind").value, content }),
        });
        await openMemoriesModal();  // 重拉一次列表（同时能看到自动去重的结果）
      } catch (err) {
        errEl.textContent = err.message;
        addBtn.disabled = false;
      }
    };
    addBtn.addEventListener("click", submitAdd);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") submitAdd();
    });

    document.querySelectorAll("[data-del]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const errEl = $("m-memory-error");
        errEl.textContent = "";
        try {
          await api(`/memories/${btn.getAttribute("data-del")}`, { method: "DELETE" });
          const row = btn.closest(".memory-item");
          if (row) row.remove();
        } catch (err) {
          errEl.textContent = err.message;
        }
      });
    });
  }

  // ---------------- 头像下拉菜单 ----------------
  function closeUserMenu() {
    const pop = $("user-menu-pop");
    const btn = $("user-menu-btn");
    if (!pop || !btn) return;
    pop.classList.add("hidden");
    btn.setAttribute("aria-expanded", "false");
  }

  function toggleUserMenu() {
    const pop = $("user-menu-pop");
    const btn = $("user-menu-btn");
    if (!pop || !btn) return;
    const willOpen = pop.classList.contains("hidden");
    pop.classList.toggle("hidden", !willOpen);
    btn.setAttribute("aria-expanded", willOpen ? "true" : "false");
  }

  // ---------------- 事件绑定 ----------------
  function bindEvents() {
    // 侧边栏
    $("new-chat-btn").addEventListener("click", createSession);
    $("logout-btn").addEventListener("click", logout);
    $("docs-btn").addEventListener("click", showDocsView);
    $("change-pwd-btn").addEventListener("click", () => {
      closeUserMenu();
      openChangePasswordModal();
    });
    $("memories-btn").addEventListener("click", () => {
      closeUserMenu();
      openMemoriesModal();
    });
    $("back-chat-btn").addEventListener("click", showChatView);

    // 头像下拉菜单：点头像展开，点外部 / Esc 关闭
    $("user-menu-btn").addEventListener("click", (e) => {
      e.stopPropagation();
      toggleUserMenu();
    });
    document.addEventListener("click", (e) => {
      if (!(e.target instanceof Element)) return;
      if (!e.target.closest(".user-menu")) closeUserMenu();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeUserMenu();
    });

    // 聊天输入
    const input = $("input");
    input.addEventListener("input", () => autoResize(input));
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
      }
    });
    $("send-btn").addEventListener("click", sendMessage);

    // 文件
    $("refresh-docs").addEventListener("click", () => loadDocuments());
    const zone = $("upload-zone");
    const fileInput = $("file-input");
    zone.addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", () => {
      if (fileInput.files[0]) handleFile(fileInput.files[0]);
      fileInput.value = "";
    });
    zone.addEventListener("dragover", (e) => {
      e.preventDefault();
      zone.classList.add("dragover");
    });
    zone.addEventListener("dragleave", () => zone.classList.remove("dragover"));
    zone.addEventListener("drop", (e) => {
      e.preventDefault();
      zone.classList.remove("dragover");
      if (e.dataTransfer.files[0]) handleFile(e.dataTransfer.files[0]);
    });

    // 移动端菜单
    const toggleSidebar = () => $("sidebar").classList.toggle("open");
    $("menu-toggle").addEventListener("click", toggleSidebar);
    $("docs-menu-toggle").addEventListener("click", toggleSidebar);
  }

  // ---------------- 启动 ----------------
  async function init() {
    setAuthFailureHandler(goEntry);
    bindEvents();

    if (!window.RAG.getToken()) {
      goEntry();
      return;
    }
    try {
      state.user = await fetchMe();
    } catch {
      goEntry();
      return;
    }
    await enterApp();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
