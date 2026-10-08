/* App bootstrap: header, hash router (#view/param), mobile menu. */
"use strict";

(() => {
  const { $, $$, api, state } = Core;
  const VIEWS = ["sohbet", "etki", "arama", "sozlesmeler", "yonetim"];
  const DEFAULT_VIEW = "sohbet";
  const ENV_LABELS = { dev: "GELİŞTİRME", test: "TEST", prod: "" };

  function parseHash() {
    const [name, ...params] = (location.hash || "").replace(/^#/, "").split("/").map(decodeURIComponent);
    if (!VIEWS.includes(name)) return { name: DEFAULT_VIEW, params: [] };
    if (name === "yonetim" && !(state.me && state.me.is_admin)) return { name: DEFAULT_VIEW, params: [] };
    return { name, params };
  }

  function render() {
    const { name, params } = parseHash();
    Core.clearError();
    for (const view of VIEWS) $(`#view-${view}`).classList.toggle("hidden", view !== name);
    $$(".nav-link").forEach((link) => link.classList.toggle("active", link.dataset.view === name));
    const section = $(`#view-${name}`);
    document.title = `${section.dataset.title} · ${(state.me && state.me.app_name) || "Sözleşme Mevzuat Etki Analizi"}`;
    closeMenu();
    const view = Core.views[name];
    if (view && view.show) view.show(params);
  }

  function renderHeader(me) {
    $("#app-title").textContent = me.app_name;
    $("#user-name").textContent = `👤 ${me.display_name}`;
    const badge = $("#env-badge");
    const label = ENV_LABELS[me.env] ?? me.env.toUpperCase();
    badge.textContent = label;
    badge.className = `env-badge env-${me.env}`;
    badge.classList.toggle("hidden", !label);
    $("#nav-admin").classList.toggle("hidden", !me.is_admin);
    $("#logout").classList.toggle("hidden", me.auth_mode !== "entra");
  }

  function closeMenu() {
    $("#sidebar").classList.remove("open");
    $("#menu-toggle").setAttribute("aria-expanded", "false");
  }

  function toggleMenu() {
    const open = $("#sidebar").classList.toggle("open");
    $("#menu-toggle").setAttribute("aria-expanded", String(open));
  }

  async function init() {
    $("#menu-toggle").addEventListener("click", toggleMenu);
    $("#source-panel-close").addEventListener("click", Core.closeSourcePanel);
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") Core.closeSourcePanel();
    });
    window.addEventListener("hashchange", render);
    try {
      state.me = await api("api/me");
      renderHeader(state.me);
    } catch (err) {
      Core.showError(err.message);
    }
    for (const view of Object.values(Core.views)) {
      try {
        if (view.init) view.init();
      } catch (err) {
        console.error(err); // eslint-disable-line no-console
      }
    }
    render();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
