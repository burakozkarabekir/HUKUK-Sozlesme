/* Admin screen: sync status + trigger, OCR queue, token usage, dependency health. */
"use strict";

(() => {
  const { el, $, api, fmtDateTime, fmtNumber, emptyRow } = Core;
  const SYNC_STATUS = { running: "⏳ Çalışıyor", success: "✅ Başarılı", partial: "⚠️ Kısmen başarılı", failed: "❌ Başarısız" };
  let pollTimer = null;

  function kv(pairs) {
    return el("dl", { class: "kv" }, ...pairs.flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, v)]));
  }

  function renderStatus(s) {
    const c = s.counts;
    $("#adm-counts").replaceChildren(
      ...[["Sözleşme", c.contracts], ["Dosya", c.files], ["Ek", c.attachments], ["Madde", c.chunks], ["Embedding", c.embeddings]].map(
        ([label, value]) => el("div", { class: "stat" }, el("div", { class: "stat-value" }, fmtNumber(value)), el("div", { class: "stat-label" }, label)),
      ),
    );
    const cfg = s.config;
    $("#adm-config").replaceChildren(kv([
      ["Ortam", cfg.env], ["Kimlik doğrulama", cfg.auth_mode], ["Sözleşme kaynağı", cfg.contract_source],
      ["Sohbet modeli", cfg.chat_model], ["Vektör arama", cfg.vector_search ? `Açık (${cfg.embedding_model})` : "Kapalı (sadece FTS5)"],
      ["OCR", s.ocr_enabled ? "Açık" : "Kapalı"],
    ]));

    $("#adm-sync-btn").disabled = s.sync_running;
    $("#adm-sync-state").textContent = s.sync_running ? "⏳ Senkronizasyon çalışıyor…" : "";
    $("#adm-runs").replaceChildren(
      ...(s.sync_runs.length ? s.sync_runs.map(runRow) : [emptyRow(7, "Henüz senkronizasyon yapılmadı.")]),
    );

    $("#adm-ocr-count").textContent = `(${s.ocr_pending.length})`;
    $("#adm-ocr").replaceChildren(
      ...(s.ocr_pending.length
        ? s.ocr_pending.map((f) => el("tr", {},
            el("td", {}, el("a", { href: `#sozlesmeler/${encodeURIComponent(f.contract_id)}` }, f.contract_no || f.contract_id)),
            el("td", {}, f.title || "—"), el("td", {}, f.file_name), el("td", {}, fmtNumber(f.page_count)),
            el("td", {}, f.ocr_status === "failed" ? "OCR başarısız" : "OCR bekliyor")))
        : [emptyRow(5, "OCR bekleyen dosya yok.")]),
    );

    const today = s.token_usage_today;
    $("#adm-tokens-today").textContent = today ? `${fmtNumber(today.total_tokens)} token (${fmtNumber(today.calls)} çağrı)` : "0 token";
    $("#adm-tokens").replaceChildren(
      ...(s.token_usage.length
        ? s.token_usage.map((u) => el("tr", {}, el("td", {}, u.day), el("td", {}, fmtNumber(u.calls)), el("td", {}, fmtNumber(u.input_tokens)), el("td", {}, fmtNumber(u.output_tokens)), el("td", { class: "strong" }, fmtNumber(u.total_tokens))))
        : [emptyRow(5, "Kayıt yok.")]),
    );

    clearTimeout(pollTimer);
    if (s.sync_running || (s.sync_runs[0] && s.sync_runs[0].status === "running")) pollTimer = setTimeout(load, 2000);
  }

  function runRow(run) {
    const errors = run.errors || [];
    return el("tr", {},
      el("td", {}, `#${run.id}`),
      el("td", {}, fmtDateTime(run.started_at)),
      el("td", {}, SYNC_STATUS[run.status] || run.status),
      el("td", {}, `${run.trigger}${run.triggered_by ? ` (${run.triggered_by})` : ""}`),
      el("td", {}, `${run.new_count} / ${run.updated_count} / ${run.unchanged_count}`),
      el("td", {}, errors.length
        ? el("details", {}, el("summary", {}, `${run.error_count} hata`),
            el("ul", { class: "small" }, ...errors.slice(0, 50).map((e) => el("li", {}, `${e.contract_id || "-"}: ${e.message}`))))
        : "—"),
      el("td", {}, run.finished_at ? fmtDateTime(run.finished_at) : "—"),
    );
  }

  async function load() {
    try {
      renderStatus(await api("api/admin/status"));
    } catch (err) {
      Core.showError(err.message);
    }
  }

  async function startSync() {
    const button = $("#adm-sync-btn");
    button.disabled = true;
    try {
      const result = await api("api/admin/sync", { method: "POST", body: { full: $("#adm-sync-full").checked } });
      $("#adm-sync-state").textContent = result.message;
      setTimeout(load, 500);
    } catch (err) {
      $("#adm-sync-state").textContent = "";
      Core.showError(err.message);
      button.disabled = false;
    }
  }

  async function checkDeps() {
    const box = $("#adm-deps");
    box.replaceChildren(el("p", { class: "muted" }, "Kontrol ediliyor… (LLM ve Turkuaz çağrıları birkaç saniye sürebilir)"));
    try {
      const deps = await api("health/deps");
      box.replaceChildren(kv(Object.entries(deps.checks).map(([name, check]) => [
        name, `${check.ok ? "✅" : check.ok === null ? "➖" : "❌"} ${check.message}${check.latency_ms ? ` (${check.latency_ms} ms)` : ""}`,
      ])));
    } catch (err) {
      box.replaceChildren(el("p", { class: "alert alert-error" }, err.message));
    }
  }

  function init() {
    $("#adm-sync-btn").addEventListener("click", startSync);
    $("#adm-refresh").addEventListener("click", load);
    $("#adm-deps-btn").addEventListener("click", checkDeps);
  }

  function show() {
    load();
  }

  Core.registerView("yonetim", { init, show });
})();
