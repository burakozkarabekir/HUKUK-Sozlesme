/* Sözleşme Mevzuat Etki Analizi - shared frontend helpers (vanilla JS, no build step).
 *
 * Security rule: model/contract output is NEVER written with innerHTML.
 * Use el()/textContent. All fetch URLs are relative (app runs under /sozlesme/).
 */
"use strict";

const Core = (() => {
  const views = {};
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

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  // ---------- API ----------
  class ApiError extends Error {
    constructor(code, message, status) {
      super(message);
      this.code = code;
      this.status = status;
    }
  }

  const NETWORK_MESSAGE = "Sunucuya ulaşılamadı. Bağlantınızı kontrol edip tekrar deneyin.";

  function handleUnauthenticated(status) {
    // Entra mode: the session expired -> go through the login flow again.
    if (status === 401 && state.me && state.me.auth_mode === "entra") location.href = "auth/login";
  }

  async function errorFrom(response) {
    const data = await response.json().catch(() => null);
    const error = data && data.error;
    handleUnauthenticated(response.status);
    return new ApiError(
      (error && error.code) || "http_error",
      (error && error.message) || `İstek başarısız oldu (HTTP ${response.status}).`,
      response.status,
    );
  }

  /** JSON API call. `path` must be relative (no leading slash). */
  async function api(path, options = {}) {
    if (path.startsWith("/")) path = path.slice(1);
    const opts = { credentials: "same-origin", ...options };
    opts.headers = { Accept: "application/json", "X-Requested-With": "fetch", ...(options.headers || {}) };
    const isBinary = opts.body instanceof Blob || opts.body instanceof ArrayBuffer;
    if (opts.body && !isBinary && typeof opts.body !== "string") {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.body);
    }
    let response;
    try {
      response = await fetch(path, opts);
    } catch (err) {
      if (err.name === "AbortError") throw err;
      throw new ApiError("network_error", NETWORK_MESSAGE, 0);
    }
    if (!response.ok) throw await errorFrom(response);
    const isJson = (response.headers.get("content-type") || "").includes("application/json");
    return isJson ? response.json() : response;
  }

  /** POST returning the raw Response for SSE streaming. */
  async function apiStream(path, body, signal) {
    let response;
    try {
      response = await fetch(path, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", Accept: "text/event-stream", "X-Requested-With": "fetch" },
        body: JSON.stringify(body),
        signal,
      });
    } catch (err) {
      if (err.name === "AbortError") throw err;
      throw new ApiError("network_error", NETWORK_MESSAGE, 0);
    }
    if (!response.ok) throw await errorFrom(response);
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

  function query(params) {
    const search = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value === null || value === undefined || value === "" || value === false) continue;
      search.set(key, value === true ? "true" : String(value));
    }
    const text = search.toString();
    return text ? `?${text}` : "";
  }

  // ---------- Small utilities ----------
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
      /* storage unavailable (private mode etc.) - the app still works */
    }
  }

  function fmtDate(value) {
    if (!value) return "—";
    const match = String(value).match(/^(\d{4})-(\d{2})-(\d{2})/);
    return match ? `${match[3]}.${match[2]}.${match[1]}` : String(value);
  }

  function fmtDateTime(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleString("tr-TR", { dateStyle: "short", timeStyle: "short" });
  }

  const fmtNumber = (value) => (value === null || value === undefined ? "—" : Number(value).toLocaleString("tr-TR"));

  function pdfUrl(contractId, fileId, page) {
    const base = `api/contracts/${encodeURIComponent(contractId)}/files/${encodeURIComponent(fileId)}`;
    return page ? `${base}#page=${page}` : base;
  }

  function pdfLink(contractId, fileId, page, label = "📄 PDF'te aç") {
    return el("a", { class: "btn btn-secondary btn-sm", href: pdfUrl(contractId, fileId, page), target: "_blank", rel: "noopener" }, label);
  }

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

  function emptyRow(colspan, text) {
    return el("tr", {}, el("td", { colspan, class: "muted center" }, text));
  }

  function debounce(fn, wait = 300) {
    let timer = null;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), wait);
    };
  }

  // ---------- Source panel (chat sources, search hits, impact clauses) ----------
  function openSourcePanel(source) {
    const panel = $("#source-panel");
    const body = $("#source-panel-body");
    const parties = (source.parties || []).join(" · ");
    const pages = source.page_start
      ? source.page_end && source.page_end !== source.page_start
        ? `Sayfa ${source.page_start}–${source.page_end}`
        : `Sayfa ${source.page_start}`
      : "";
    body.replaceChildren(
      el("div", { class: "source-meta" },
        el("div", { class: "source-no" }, source.contract_no || source.contract_id),
        source.title ? el("div", { class: "source-title" }, source.title) : null,
        parties ? el("div", { class: "muted small" }, parties) : null,
        el("div", { class: "source-ref" },
          [source.clause_ref, source.clause_title].filter(Boolean).join(" — ") || "Madde bilgisi yok",
          pages ? el("span", { class: "muted" }, ` · ${pages}`) : null),
        source.file_name ? el("div", { class: "muted small" }, (source.is_attachment ? "📎 Ek: " : "📄 ") + source.file_name) : null,
      ),
      el("div", { class: "source-actions" },
        source.file_id ? pdfLink(source.contract_id, source.file_id, source.page_start) : null,
        el("a", { class: "btn btn-secondary btn-sm", href: `#sozlesmeler/${encodeURIComponent(source.contract_id)}` }, "Sözleşmeyi görüntüle"),
      ),
      source.text ? el("pre", { class: "source-text" }, source.text) : el("p", { class: "muted" }, "Madde metni yükleniyor…"),
    );
    panel.classList.add("open");
    panel.setAttribute("aria-hidden", "false");
    if (!source.text && source.chunk_id) {
      api(`api/chunks/${source.chunk_id}`)
        .then((chunk) => openSourcePanel({ ...source, ...chunk, chunk_id: undefined }))
        .catch((err) => showError(err.message));
    }
  }

  function closeSourcePanel() {
    const panel = $("#source-panel");
    panel.classList.remove("open");
    panel.setAttribute("aria-hidden", "true");
  }

  // ---------- Views ----------
  function registerView(name, view) {
    views[name] = view;
  }

  return {
    state, views, el, $, $$, ApiError, api, apiStream, readSSE, query, copyText, storageGet, storageSet,
    fmtDate, fmtDateTime, fmtNumber, pdfUrl, pdfLink, showError, clearError, emptyRow, debounce,
    openSourcePanel, closeSourcePanel, registerView,
  };
})();
