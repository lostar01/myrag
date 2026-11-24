/* ============================================================
 * RAG 知识库助手 - 入口登录页
 * 流程：登录（或注册）→ 默认进入用户端
 * 管理员进入用户端后，可在侧边栏「🛠 管理端」切换到管理端
 * 已登录（token 有效）时直接进入用户端
 * 依赖：common.js (window.RAG)
 * ============================================================ */
(function () {
  "use strict";

  const {
    $,
    show,
    hide,
    api,
    getToken,
    setToken,
    clearToken,
    fetchMe,
  } = window.RAG;

  let allowRegister = false;

  function goUser() {
    window.location.replace("user/index.html");
  }

  // ---------------- 表单 tab ----------------
  function switchAuthTab(tab) {
    document.querySelectorAll(".auth-tab").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.tab === tab);
    });
    if (tab === "login") {
      show($("login-form"));
      hide($("register-form"));
    } else {
      hide($("login-form"));
      show($("register-form"));
    }
  }

  async function loadAuthConfig() {
    try {
      const cfg = await api("/auth/config");
      allowRegister = !!cfg.allow_register;
    } catch {
      allowRegister = false;
    }

    const tabs = $("auth-tabs");
    if (allowRegister) {
      show($("register-tab"));
      show(tabs);
      hide($("auth-footnote"));
    } else {
      hide($("register-tab"));
      if (tabs) hide(tabs);
      show($("auth-footnote"));
      switchAuthTab("login");
    }
  }

  // ---------------- 登录 / 注册 ----------------
  async function handleLogin(e) {
    e.preventDefault();
    const errEl = $("login-error");
    errEl.textContent = "";

    const username = $("login-username").value.trim();
    const password = $("login-password").value;
    const submit = $("login-submit");

    submit.disabled = true;
    try {
      const data = await api("/users/login", {
        method: "POST",
        body: JSON.stringify({ username, password }),
      });
      setToken(data.token);
      goUser();
    } catch (err) {
      errEl.textContent = err.message;
    } finally {
      submit.disabled = false;
    }
  }

  async function handleRegister(e) {
    e.preventDefault();
    const errEl = $("register-error");
    errEl.textContent = "";

    const username = $("reg-username").value.trim();
    const nickname = $("reg-nickname").value.trim() || null;
    const password = $("reg-password").value;

    try {
      await api("/users/register", {
        method: "POST",
        body: JSON.stringify({ username, password, nickname }),
      });
      // 注册成功后自动登录并进入用户端
      const data = await api("/users/login", {
        method: "POST",
        body: JSON.stringify({ username, password }),
      });
      setToken(data.token);
      goUser();
    } catch (err) {
      errEl.textContent = err.message;
    }
  }

  // ---------------- 事件绑定 ----------------
  function bindEvents() {
    document.querySelectorAll(".auth-tab").forEach((btn) => {
      btn.addEventListener("click", () => switchAuthTab(btn.dataset.tab));
    });
    $("login-form").addEventListener("submit", handleLogin);
    $("register-form").addEventListener("submit", handleRegister);
  }

  // ---------------- 启动 ----------------
  async function init() {
    bindEvents();
    await loadAuthConfig();

    // 已登录（token 有效）→ 直接进用户端，不再展示登录表单
    if (!getToken()) return;
    try {
      await fetchMe();
      goUser();
    } catch {
      // token 失效：清掉并停留在登录页
      clearToken();
    }
  }

  document.addEventListener("DOMContentLoaded", init);
})();
