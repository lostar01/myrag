/* ============================================================
 * RAG 知识库助手 - 管理端（仅管理，无对话）
 * 布局沿用 ChatGPT 风格：浅色侧边栏 + 主内容区
 * 功能：用户管理（创建/角色/启禁用/重置密码/删除）、
 *       文档管理（上传/列表/删除）
 * 依赖：../common.js (window.RAG)
 * ============================================================ */
(function () {
  "use strict";

  const {
    $,
    show,
    hide,
    escapeHtml,
    formatTime,
    api,
    fetchMe,
    isAdminRole,
    setAuthFailureHandler,
    openModal,
    closeModal,
    uploadDocument,
  } = window.RAG;

  const state = {
    user: null,
    users: [],
    docs: [],
  };

  // ---------------- 入口 / 守卫 ----------------
  function goEntry() {
    window.location.replace("../index.html");
  }

  function showGuard() {
    hide($("app-view"));
    show($("guard-view"));
  }

  function enterApp() {
    hide($("guard-view"));
    show($("app-view"));
    renderUser();
    showUsersView();
  }

  function renderUser() {
    const u = state.user || {};
    const name = u.nickname || u.username || "管理员";
    $("user-name").textContent = name;
    $("user-avatar").textContent = name.charAt(0).toUpperCase();
    $("user-role").textContent = isAdminRole(u) ? "管理员" : "普通用户";
  }

  function logout() {
    state.user = null;
    state.users = [];
    state.docs = [];
    window.RAG.clearToken();
    closeModal();
    const sid = $("sidebar");
    if (sid) sid.classList.remove("open");
    goEntry();
  }

  // ---------------- 视图切换 ----------------
  function showUsersView() {
    stopDocsAutoRefresh();
    hide($("docs-view"));
    show($("users-view"));
    $("users-btn").classList.add("active");
    $("docs-btn").classList.remove("active");
    loadUsers();
  }

  function showDocsView() {
    hide($("users-view"));
    show($("docs-view"));
    $("docs-btn").classList.add("active");
    $("users-btn").classList.remove("active");
    loadDocuments();
  }

  // ---------------- 用户管理 ----------------
  async function loadUsers() {
    const tbody = $("users-tbody");
    tbody.innerHTML = `<tr><td colspan="6" class="users-loading">加载中…</td></tr>`;
    try {
      const data = await api("/users");
      state.users = data.users || [];
      const total = data.total != null ? data.total : state.users.length;
      $("users-count").textContent = `共 ${total} 个用户`;
      if (!state.users.length) {
        tbody.innerHTML = `<tr><td colspan="6" class="users-empty">暂无用户</td></tr>`;
        return;
      }
      tbody.innerHTML = "";
      state.users.forEach((u) => tbody.appendChild(renderUserRow(u)));
    } catch (err) {
      tbody.innerHTML = `<tr><td colspan="6" class="users-empty">加载失败：${escapeHtml(
        err.message
      )}</td></tr>`;
    }
  }

  function renderUserRow(u) {
    const tr = document.createElement("tr");
    const isSelf = state.user && state.user.user_id === u.user_id;
    const created = formatTime(u.created_at);
    const roleLabel = u.role === "admin" ? "管理员" : "普通用户";
    const statusLabel = u.status === "active" ? "启用" : "禁用";
    tr.innerHTML = `
      <td class="cell-username">${escapeHtml(u.username)}${
        isSelf ? ' <span class="tag-self">我</span>' : ""
      }</td>
      <td>${escapeHtml(u.nickname || "-")}</td>
      <td><span class="role-badge role-${u.role}">${roleLabel}</span></td>
      <td><span class="status-badge status-${u.status}">${statusLabel}</span></td>
      <td class="cell-time">${escapeHtml(created)}</td>
      <td class="cell-actions">
        <button class="btn-small" data-act="pwd">改密码</button>
        <button class="btn-small" data-act="role" ${isSelf ? "disabled" : ""}>${
      u.role === "admin" ? "设为用户" : "设为管理员"
    }</button>
        <button class="btn-small" data-act="status" ${isSelf ? "disabled" : ""}>${
      u.status === "active" ? "禁用" : "启用"
    }</button>
        <button class="btn-small btn-danger" data-act="del" ${
      isSelf ? "disabled" : ""
    }>删除</button>
      </td>
    `;
    tr.querySelector('[data-act="pwd"]').addEventListener("click", () =>
      openResetPasswordModal(u)
    );
    if (!isSelf) {
      tr.querySelector('[data-act="role"]').addEventListener("click", () => toggleUserRole(u));
      tr.querySelector('[data-act="status"]').addEventListener("click", () =>
        toggleUserStatus(u)
      );
      tr.querySelector('[data-act="del"]').addEventListener("click", () => deleteUser(u));
    }
    return tr;
  }

  async function toggleUserRole(u) {
    const newRole = u.role === "admin" ? "user" : "admin";
    const label = newRole === "admin" ? "管理员" : "普通用户";
    if (!confirm(`确定将用户「${u.username}」设为${label}？`)) return;
    try {
      await api(`/users/${u.user_id}`, {
        method: "PUT",
        body: JSON.stringify({ role: newRole }),
      });
      loadUsers();
    } catch (err) {
      alert("操作失败：" + err.message);
    }
  }

  async function toggleUserStatus(u) {
    const newStatus = u.status === "active" ? "disabled" : "active";
    const label = newStatus === "active" ? "启用" : "禁用";
    if (!confirm(`确定${label}用户「${u.username}」？`)) return;
    try {
      await api(`/users/${u.user_id}`, {
        method: "PUT",
        body: JSON.stringify({ status: newStatus }),
      });
      loadUsers();
    } catch (err) {
      alert("操作失败：" + err.message);
    }
  }

  async function deleteUser(u) {
    if (!confirm(`确定删除用户「${u.username}」？此操作不可恢复。`)) return;
    try {
      await api(`/users/${u.user_id}`, { method: "DELETE" });
      loadUsers();
    } catch (err) {
      alert("删除失败：" + err.message);
    }
  }

  function openCreateUserModal() {
    const body = `
      <div class="form-field">
        <label>用户名</label>
        <input type="text" id="m-username" placeholder="至少3位" />
      </div>
      <div class="form-field">
        <label>昵称（可选）</label>
        <input type="text" id="m-nickname" placeholder="显示名称" />
      </div>
      <div class="form-field">
        <label>初始密码</label>
        <input type="password" id="m-password" placeholder="至少6位" />
      </div>
      <div class="form-field">
        <label>角色</label>
        <select id="m-role">
          <option value="user">普通用户</option>
          <option value="admin">管理员</option>
        </select>
      </div>
      <div class="form-error" id="m-error"></div>
    `;
    openModal("新建用户", body, [
      { text: "取消", className: "btn-small", onClick: closeModal },
      {
        text: "创建",
        className: "btn-small btn-accent",
        onClick: async () => {
          const errEl = $("m-error");
          errEl.textContent = "";
          const username = $("m-username").value.trim();
          const nickname = $("m-nickname").value.trim() || null;
          const password = $("m-password").value;
          const role = $("m-role").value;
          if (username.length < 3) return (errEl.textContent = "用户名至少3位");
          if (password.length < 6) return (errEl.textContent = "密码至少6位");
          try {
            await api("/users", {
              method: "POST",
              body: JSON.stringify({ username, password, nickname, role }),
            });
            closeModal();
            loadUsers();
          } catch (err) {
            errEl.textContent = err.message;
          }
        },
      },
    ]);
  }

  function openResetPasswordModal(u) {
    const body = `
      <div class="form-field">
        <label>为用户「${escapeHtml(u.username)}」设置新密码</label>
        <input type="password" id="m-newpwd" autocomplete="new-password" placeholder="至少6位" />
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
          const password = $("m-newpwd").value;
          if (password.length < 6) return (errEl.textContent = "密码至少6位");
          try {
            await api(`/users/${u.user_id}/password`, {
              method: "PUT",
              body: JSON.stringify({ new_password: password }),
            });
            closeModal();
            alert("密码已更新");
          } catch (err) {
            errEl.textContent = err.message;
          }
        },
      },
    ]);
  }

  // ---------------- 上传记录（全部用户，可删除） ----------------
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
    // 解析中不允许删除（避免后台任务写回脏数据）；is_active 由后端计算（超时卡死的记录可删）
    const active = r.is_active === true;
    tr.innerHTML = `
      <td class="cell-username"${noName ? ` title="${escapeHtml(r.doc_id || "")}"` : ""}>${escapeHtml(
      name
    )}</td>
      <td>${r.page_count != null ? escapeHtml(String(r.page_count)) : "-"}</td>
      <td><span class="doc-status ${escapeHtml(status)}"${tip}>${escapeHtml(
      statusText(status)
    )}</span></td>
      <td class="cell-time">${escapeHtml(formatTime(r.created_at))}</td>
      <td class="cell-actions">
        <button class="btn-small btn-danger" data-act="del" ${active ? "disabled" : ""} ${
      active ? 'title="解析中，完成后可删除"' : ""
    }>删除</button>
      </td>
    `;
    const btn = tr.querySelector('[data-act="del"]');
    if (btn && !active) btn.addEventListener("click", () => deleteUploadRecord(r));
    return tr;
  }

  async function loadDocuments(silent) {
    const tbody = $("docs-tbody");
    if (!tbody) return;
    if (!silent) {
      tbody.innerHTML = `<tr><td colspan="5" class="users-loading">加载中…</td></tr>`;
    }
    try {
      const data = await api("/uploads");
      state.docs = data.records || [];
      const total = data.total != null ? data.total : state.docs.length;
      $("docs-count").textContent = `共 ${total} 条上传记录`;
      if (!state.docs.length) {
        tbody.innerHTML = `<tr><td colspan="5" class="users-empty">暂无上传记录</td></tr>`;
        scheduleDocsAutoRefresh(false);
        return;
      }
      tbody.innerHTML = "";
      state.docs.forEach((r) => tbody.appendChild(renderDocRow(r)));
      scheduleDocsAutoRefresh(state.docs.some((r) => r.is_active === true));
    } catch (err) {
      tbody.innerHTML = `<tr><td colspan="5" class="users-empty">加载失败：${escapeHtml(
        err.message
      )}</td></tr>`;
      scheduleDocsAutoRefresh(false);
    }
  }

  async function deleteUploadRecord(r) {
    const name = r.file_name || r.doc_name || r.doc_id;
    if (
      !confirm(
        `确定删除上传记录「${name}」？将同时清理向量数据与源文件，删除后可重新上传。`
      )
    )
      return;
    try {
      // 记录以 rag_doc 为准，删除按 doc_id
      await api(`/uploads/${encodeURIComponent(r.doc_id)}`, { method: "DELETE" });
      loadDocuments();
    } catch (err) {
      alert("删除失败：" + err.message);
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

  // ---------------- 事件绑定 ----------------
  function bindEvents() {
    $("logout-btn").addEventListener("click", logout);
    $("guard-relogin").addEventListener("click", logout);

    $("users-btn").addEventListener("click", showUsersView);
    $("docs-btn").addEventListener("click", showDocsView);
    $("create-user-btn").addEventListener("click", openCreateUserModal);
    $("refresh-users").addEventListener("click", loadUsers);
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
    $("users-menu-toggle").addEventListener("click", toggleSidebar);
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
    if (!isAdminRole(state.user)) {
      // 已登录但不是管理员：保留 token，仅做访问拦截
      showGuard();
      return;
    }
    enterApp();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
