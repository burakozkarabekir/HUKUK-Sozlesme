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
