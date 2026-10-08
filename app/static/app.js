/* Sözleşme Mevzuat Etki Analizi - frontend (vanilla JS, no build step).
 *
 * Security rule: model/contract output is NEVER written with innerHTML.
 * Use el()/textContent. All fetch URLs are relative (app runs under /sozlesme/).
 */
"use strict";

const App = (() => {
  const VIEWS = ["sohbet", "etki", "arama", "sozlesmeler", "yonetim"];
  const DEFAULT_VIEW = "sohbet";
  const state = { me: null };

  // ---------- DOM helpers ----------
  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "dataset") Object.assign(node.dataset, value);
      else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? "" : String(value));
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function $(selector) {
    return document.querySelector(selector);
  }

  // ---------- API ----------
  class ApiError extends Error {
    constructor(code, message, status) {
      super(message);
      this.code = code;
      this.status = status;
    }
  }

  const NETWORK_MESSAGE = "Sunucuya ulaşılamadı. Bağlantınızı kontrol edip tekrar deneyin.";

  async function api(path, options = {}) {
    // Paths must be relative (no leading slash) so they resolve under /sozlesme/.
    if (path.startsWith("/")) path = path.slice(1);
    const opts = { credentials: "same-origin", ...options };
    opts.headers = { Accept: "application/json", ...(options.headers || {}) };
    if (opts.body && !(opts.body instanceof FormData) && typeof opts.body !== "string") {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.body);
    }
    let response;
    try {
      response = await fetch(path, opts);
    } catch (err) {
      throw new ApiError("network_error", NETWORK_MESSAGE, 0);
    }
    const isJson = (response.headers.get("content-type") || "").includes("application/json");
    const data = isJson ? await response.json().catch(() => null) : null;
    if (!response.ok) {
      const error = data && data.error;
      throw new ApiError(
        (error && error.code) || "http_error",
        (error && error.message) || `İstek başarısız oldu (HTTP ${response.status}).`,
        response.status,
      );
    }
    return data;
  }

  /** POST that returns the raw Response for streaming (SSE). Throws ApiError on HTTP errors. */
  async function apiStream(path, body, signal) {
    let response;
    try {
      response = await fetch(path, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        body: JSON.stringify(body),
        signal,
      });
    } catch (err) {
      if (err.name === "AbortError") throw err;
      throw new ApiError("network_error", NETWORK_MESSAGE, 0);
    }
    if (!response.ok) {
      const data = await response.json().catch(() => null);
      const error = data && data.error;
      throw new ApiError(
        (error && error.code) || "http_error",
        (error && error.message) || `İstek başarısız oldu (HTTP ${response.status}).`,
        response.status,
      );
    }
    return response;
  }

  /** Parse a text/event-stream body into {event, data} objects. */
  async function* readSSE(response) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let index;
      while ((index = buffer.indexOf("\n\n")) >= 0) {
        const raw = buffer.slice(0, index);
        buffer = buffer.slice(index + 2);
        let event = "message";
        const dataLines = [];
        for (const line of raw.split("\n")) {
          if (line.startsWith("event:")) event = line.slice(6).trim();
          else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
        }
        let data = null;
        try {
          data = dataLines.length ? JSON.parse(dataLines.join("\n")) : null;
        } catch (err) {
          data = null;
        }
        yield { event, data };
      }
    }
  }

  async function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return;
    }
    // Plain-HTTP fallback (Clipboard API needs a secure context).
    const area = el("textarea", { class: "offscreen", readonly: true });
    area.value = text;
    document.body.append(area);
    area.select();
    document.execCommand("copy");
    area.remove();
  }

  function storageGet(key, fallback) {
    try {
      const raw = sessionStorage.getItem(key);
      return raw ? JSON.parse(raw) : fallback;
    } catch (err) {
      return fallback;
    }
  }

  function storageSet(key, value) {
    try {
      sessionStorage.setItem(key, JSON.stringify(value));
    } catch (err) {
      /* storage unavailable (private mode etc.) - chat still works */
    }
  }

  // ---------- Chat ----------
  const Chat = (() => {
    const STORAGE_KEY = "sozlesme.chat.v1";
    const EXAMPLES = [
      "ABC firmasıyla olan sözleşmede fesih bildirim süresi ne kadar?",
      "Hangi sözleşmelerde cezai şart maddesi var?",
      "Gizlilik yükümlülüğü sözleşme bitiminden sonra ne kadar sürüyor?",
      "Mücbir sebep maddesi genel olarak neyi düzenler?",
    ];
    let history = []; // [{role, content}] - completed turns only
    let controller = null;

    function scrollToEnd() {
      const log = $("#chat-log");
      log.scrollTop = log.scrollHeight;
    }

    function updateEmptyState() {
      $("#chat-empty").classList.toggle("hidden", $("#chat-log").querySelectorAll(".msg").length > 0);
    }

    function addUserMessage(text) {
      $("#chat-log").append(el("div", { class: "msg msg-user" }, el("div", { class: "msg-body" }, text)));
      updateEmptyState();
      scrollToEnd();
    }

    function addAssistantMessage() {
      const body = el("div", { class: "msg-body" },
        el("span", { class: "typing", "aria-label": "yazıyor" }, el("span"), el("span"), el("span")));
      const actions = el("div", { class: "msg-actions hidden" });
      const wrapper = el("div", { class: "msg msg-assistant" }, body, actions);
      $("#chat-log").append(wrapper);
      updateEmptyState();
      scrollToEnd();

      let text = "";
      let pending = false;
      const paint = () => {
        pending = false;
        body.replaceChildren(Markdown.render(text));
        scrollToEnd();
      };
      return {
        append(delta) {
          text += delta;
          if (!pending) {
            pending = true;
            requestAnimationFrame(paint);
          }
        },
        get text() {
          return text;
        },
        finish(note) {
          paint();
          if (!text) body.replaceChildren(el("span", { class: "muted" }, "(boş cevap)"));
          if (note) wrapper.append(el("div", { class: "msg-note" }, note));
          if (text) {
            const copyBtn = el("button", { type: "button", class: "btn-link", title: "Cevabı kopyala" }, "📋 Kopyala");
            copyBtn.addEventListener("click", async () => {
              try {
                await copyText(text);
                copyBtn.textContent = "✓ Kopyalandı";
              } catch (err) {
                copyBtn.textContent = "Kopyalanamadı";
              }
              setTimeout(() => (copyBtn.textContent = "📋 Kopyala"), 1500);
            });
            actions.replaceChildren(copyBtn);
            actions.classList.remove("hidden");
          }
        },
        fail(message) {
          wrapper.classList.add("msg-error");
          if (text) {
            paint();
            wrapper.append(el("div", { class: "msg-note" }, "⚠️ " + message));
          } else {
            body.replaceChildren("⚠️ " + message);
          }
        },
      };
    }

    function setBusy(busy) {
      const send = $("#chat-send");
      send.textContent = busy ? "Durdur" : "Gönder";
      send.classList.toggle("btn-danger", busy);
      send.classList.toggle("btn-primary", !busy);
      $("#chat-new").disabled = busy;
    }

    async function ask(question) {
      addUserMessage(question);
      const answer = addAssistantMessage();
      controller = new AbortController();
      setBusy(true);
      let completed = false;
      try {
        const response = await apiStream("api/chat", { message: question, history }, controller.signal);
        for await (const { event, data } of readSSE(response)) {
          if (event === "delta" && data) answer.append(data.text || "");
          else if (event === "error") throw new ApiError(data.code, data.message, 0);
          else if (event === "done") completed = true;
        }
        if (!completed) throw new ApiError("stream_cut", "Bağlantı kesildi; cevap tamamlanamadı.", 0);
        answer.finish();
        history.push({ role: "user", content: question }, { role: "assistant", content: answer.text });
        storageSet(STORAGE_KEY, history);
      } catch (err) {
        if (err.name === "AbortError") {
          answer.finish("Cevap durduruldu.");
          if (answer.text) {
            history.push({ role: "user", content: question }, { role: "assistant", content: answer.text });
            storageSet(STORAGE_KEY, history);
          }
        } else {
          answer.fail(err.message || "Beklenmeyen bir hata oluştu.");
        }
      } finally {
        controller = null;
        setBusy(false);
        $("#chat-input").focus();
      }
    }

    function submit() {
      if (controller) {
        controller.abort();
        return;
      }
      const input = $("#chat-input");
      const question = input.value.trim();
      if (!question) return;
      input.value = "";
      ask(question);
    }

    function reset() {
      if (controller) return;
      history = [];
      storageSet(STORAGE_KEY, history);
      $("#chat-log").querySelectorAll(".msg").forEach((node) => node.remove());
      updateEmptyState();
      $("#chat-input").focus();
    }

    function restore() {
      history = storageGet(STORAGE_KEY, []);
      if (!Array.isArray(history)) history = [];
      for (const turn of history) {
        if (turn.role === "user") {
          addUserMessage(turn.content);
        } else {
          const answer = addAssistantMessage();
          answer.append(turn.content);
          answer.finish();
        }
      }
      updateEmptyState();
    }

    function init() {
      const examples = $("#chat-examples");
      for (const text of EXAMPLES) {
        examples.append(el("button", { type: "button", class: "example", onclick: () => ask(text) }, text));
      }
      $("#chat-form").addEventListener("submit", (event) => {
        event.preventDefault();
        submit();
      });
      $("#chat-input").addEventListener("keydown", (event) => {
        if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
          event.preventDefault();
          submit();
        }
      });
      $("#chat-new").addEventListener("click", reset);
      restore();
    }

    return { init };
  })();

  function showError(message) {
    const box = $("#global-error");
    box.textContent = message;
    box.classList.remove("hidden");
  }

  function clearError() {
    const box = $("#global-error");
    box.textContent = "";
    box.classList.add("hidden");
  }

  // ---------- Router ----------
  function currentView() {
    const hash = (location.hash || "").replace("#", "");
    if (!VIEWS.includes(hash)) return DEFAULT_VIEW;
    if (hash === "yonetim" && !(state.me && state.me.is_admin)) return DEFAULT_VIEW;
    return hash;
  }

  function render() {
    const view = currentView();
    clearError();
    for (const name of VIEWS) {
      $(`#view-${name}`).classList.toggle("hidden", name !== view);
    }
    document.querySelectorAll(".nav-link").forEach((link) => {
      link.classList.toggle("active", link.dataset.view === view);
    });
    const section = $(`#view-${view}`);
    document.title = `${section.dataset.title} · ${(state.me && state.me.app_name) || "Sözleşme Mevzuat Etki Analizi"}`;
    closeMenu();
    if (view === "yonetim") loadHealth();
  }

  // ---------- Header ----------
  const ENV_LABELS = { dev: "GELİŞTİRME", test: "TEST", prod: "" };

  function renderHeader(me) {
    $("#app-title").textContent = me.app_name;
    $("#user-name").textContent = `👤 ${me.display_name}`;
    const badge = $("#env-badge");
    const label = ENV_LABELS[me.env] || me.env.toUpperCase();
    badge.textContent = label;
    badge.className = `env-badge env-${me.env}`;
    badge.classList.toggle("hidden", !label);
    $("#nav-admin").classList.toggle("hidden", !me.is_admin);
  }

  // ---------- Admin: health ----------
  async function loadHealth() {
    const box = $("#health-info");
    box.replaceChildren(el("dt", {}, "Durum"), el("dd", {}, "Yükleniyor…"));
    try {
      const health = await api("health");
      box.replaceChildren(
        el("dt", {}, "Durum"), el("dd", {}, health.status === "ok" ? "✅ Çalışıyor" : "⚠️ Sorunlu"),
        el("dt", {}, "Veritabanı"), el("dd", {}, health.db === "ok" ? "✅ Erişilebilir" : "❌ Hata"),
        el("dt", {}, "Ortam"), el("dd", {}, health.env),
        el("dt", {}, "Sürüm"), el("dd", {}, health.version),
      );
    } catch (err) {
      box.replaceChildren(el("dt", {}, "Durum"), el("dd", {}, "❌ " + err.message));
    }
  }

  // ---------- Mobile menu ----------
  function closeMenu() {
    $("#sidebar").classList.remove("open");
    $("#menu-toggle").setAttribute("aria-expanded", "false");
  }

  function toggleMenu() {
    const open = $("#sidebar").classList.toggle("open");
    $("#menu-toggle").setAttribute("aria-expanded", String(open));
  }

  // ---------- Init ----------
  async function init() {
    $("#menu-toggle").addEventListener("click", toggleMenu);
    window.addEventListener("hashchange", render);
    Chat.init();
    try {
      state.me = await api("api/me");
      renderHeader(state.me);
    } catch (err) {
      showError(err.message);
    }
    render();
  }

  return { init, api, el, ApiError };
})();

document.addEventListener("DOMContentLoaded", App.init);
