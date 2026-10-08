/* Legislation impact analysis: 1) text/PDF + filters 2) editable keywords 3) progress 4) results. */
"use strict";

(() => {
  const { el, $, $$, api, fmtDateTime, fmtNumber, pdfLink, emptyRow } = Core;
  const LEVELS = {
    ETKILENIR: { label: "Etkilenir", badge: "badge-red" },
    MUHTEMELEN_ETKILENIR: { label: "Muhtemelen Etkilenir", badge: "badge-orange" },
    ETKILENMEZ: { label: "Etkilenmez", badge: "badge-gray" },
    DEGERLENDIRILEMEDI: { label: "Değerlendirilemedi", badge: "badge-yellow" },
  };
  const LEVEL_ORDER = ["ETKILENIR", "MUHTEMELEN_ETKILENIR", "ETKILENMEZ", "DEGERLENDIRILEMEDI"];
  const STATUS = { queued: "Sırada", running: "Çalışıyor", completed: "Tamamlandı", failed: "Hata", cancelled: "İptal edildi" };

  const state = { text: "", inputKind: "text", extraction: null, keywords: [], job: null, pollTimer: null, pdf: null };

  // ---------- steps ----------
  function showStep(step) {
    for (const n of [1, 2, 3, 4]) $(`#im-step${n}`).classList.toggle("hidden", n !== step);
    $$("#im-steps li").forEach((li) => {
      const n = Number(li.dataset.step);
      li.classList.toggle("active", n === step);
      li.classList.toggle("done", n < step);
    });
  }

  function reset() {
    stopPolling();
    Object.assign(state, { text: "", inputKind: "text", extraction: null, keywords: [], job: null, pdf: null });
    $("#im-text").value = "";
    $("#im-title").value = "";
    $("#im-file").value = "";
    $("#im-file-name").textContent = "";
    updateChars();
    showStep(1);
    if (location.hash !== "#etki") history.replaceState(null, "", "#etki");
  }

  function updateChars() {
    const n = $("#im-text").value.length;
    $("#im-chars").textContent = n ? `${fmtNumber(n)} karakter` : "";
  }

  function filters() {
    return {
      contract_type: $("#im-type").value || null,
      party: $("#im-party").value.trim() || null,
      date_from: $("#im-from").value || null,
      date_to: $("#im-to").value || null,
      active_only: $("#im-active").checked,
    };
  }

  function busy(button, text) {
    const original = button.dataset.label || button.textContent;
    button.dataset.label = original;
    button.disabled = Boolean(text);
    button.textContent = text || original;
  }

  // ---------- step 1 -> 2: extract concepts ----------
  async function extract() {
    const button = $("#im-extract");
    const text = $("#im-text").value.trim();
    if (!state.pdf && !text) {
      Core.showError("Lütfen mevzuat değişikliği metnini yapıştırın veya bir PDF seçin.");
      return;
    }
    Core.clearError();
    busy(button, "Kavramlar çıkarılıyor…");
    try {
      const usePdf = state.pdf && !text;
      const result = usePdf
        ? await api("api/impact/extract", { method: "POST", body: state.pdf, headers: { "Content-Type": "application/pdf" } })
        : await api("api/impact/extract", { method: "POST", body: { text } });
      state.text = result.text;
      state.inputKind = result.input_kind;
      state.extraction = result.extraction;
      state.keywords = [...result.extraction.keywords];
      if (usePdf) {
        $("#im-text").value = result.text;
        updateChars();
      }
      renderConcepts();
      showStep(2);
    } catch (err) {
      Core.showError(err.message);
    } finally {
      busy(button, null);
    }
  }

  function chipList(items) {
    return items.length ? items.map((t) => el("span", { class: "chip chip-static" }, t)) : [el("span", { class: "muted" }, "—")];
  }

  function renderConcepts() {
    const ex = state.extraction;
    $("#im-summary").textContent = ex.summary || "—";
    $("#im-topics").replaceChildren(...chipList(ex.topics || []));
    $("#im-types").replaceChildren(...chipList(ex.contract_types || []));
    $("#im-effective").textContent = ex.effective_date || "Metinde belirtilmemiş";
    renderChips();
  }

  function renderChips() {
    $("#im-chips").replaceChildren(
      ...state.keywords.map((keyword, index) =>
        el("span", { class: "chip" }, keyword,
          el("button", { type: "button", class: "chip-x", "aria-label": `${keyword} kaldır`, onclick: () => { state.keywords.splice(index, 1); renderChips(); } }, "×"))),
    );
    $("#im-run").disabled = state.keywords.length === 0;
    $("#im-kw-count").textContent = `(${state.keywords.length})`;
  }

  function addChip() {
    const input = $("#im-chip-input");
    for (const part of input.value.split(",")) {
      const keyword = part.trim();
      if (keyword && !state.keywords.some((k) => k.toLocaleLowerCase("tr") === keyword.toLocaleLowerCase("tr"))) state.keywords.push(keyword);
    }
    input.value = "";
    renderChips();
    input.focus();
  }

  // ---------- step 2 -> 3: run ----------
  async function run() {
    const button = $("#im-run");
    busy(button, "Başlatılıyor…");
    try {
      const result = await api("api/impact/run", {
        method: "POST",
        body: {
          title: $("#im-title").value.trim() || null,
          text: state.text,
          keywords: state.keywords,
          extraction: state.extraction,
          filters: filters(),
          input_kind: state.inputKind,
        },
      });
      location.hash = `#etki/${result.job_id}`;
    } catch (err) {
      Core.showError(err.message);
    } finally {
      busy(button, null);
    }
  }

  // ---------- step 3: progress ----------
  function stopPolling() {
    clearTimeout(state.pollTimer);
    state.pollTimer = null;
  }

  async function loadJob(jobId) {
    stopPolling();
    try {
      const job = await api(`api/impact/${encodeURIComponent(jobId)}`);
      if (!location.hash.startsWith(`#etki/${jobId}`)) return; // user navigated away
      state.job = job;
      if (job.status === "queued" || job.status === "running") {
        renderProgress(job);
        showStep(3);
        state.pollTimer = setTimeout(() => loadJob(jobId), 1500);
      } else {
        renderResults(job);
        showStep(4);
        loadHistory();
      }
    } catch (err) {
      Core.showError(err.message);
      showStep(1);
    }
  }

  function renderProgress(job) {
    $("#im-job-title").textContent = job.title || "Analiz";
    const pct = job.total ? Math.round((job.processed / job.total) * 100) : 0;
    $("#im-bar").style.width = `${job.status === "queued" ? 0 : Math.max(pct, 3)}%`;
    $("#im-progress-text").textContent =
      job.status === "queued"
        ? "Sırada bekliyor…"
        : job.total
          ? `${fmtNumber(job.processed)} / ${fmtNumber(job.total)} sözleşme değerlendirildi (%${pct})`
          : "Aday maddeler aranıyor…";
    $("#im-cancel").disabled = job.cancel_requested;
    $("#im-cancel").textContent = job.cancel_requested ? "İptal ediliyor…" : "İptal Et";
  }

  async function cancel() {
    if (!state.job) return;
    try {
      await api(`api/impact/${state.job.id}/cancel`, { method: "POST" });
      loadJob(state.job.id);
    } catch (err) {
      Core.showError(err.message);
    }
  }

  // ---------- step 4: results ----------
  function levelBadge(level) {
    const info = LEVELS[level] || { label: level, badge: "badge-gray" };
    return el("span", { class: `badge ${info.badge}` }, info.label);
  }

  function renderResults(job) {
    $("#im-res-title").textContent = job.title || "Analiz";
    $("#im-res-meta").textContent = `${fmtDateTime(job.created_at)} · ${job.created_by || "—"} · ${fmtNumber(job.processed)} / ${fmtNumber(job.total)} sözleşme değerlendirildi`;
    $("#im-export").href = `api/impact/${encodeURIComponent(job.id)}/export.xlsx`;
    const notice = $("#im-res-notice");
    notice.className = "hidden";
    if (job.status === "failed") {
      notice.className = "alert alert-error";
      notice.textContent = `Analiz tamamlanamadı: ${job.error_message || "bilinmeyen hata"}`;
    } else if (job.status === "cancelled") {
      notice.className = "alert alert-warn";
      notice.textContent = "Analiz iptal edildi. Aşağıda iptalden önce değerlendirilen sözleşmeler var.";
    } else if (!job.total) {
      notice.className = "alert alert-warn";
      notice.textContent = "Anahtar kelimelerle eşleşen sözleşme maddesi bulunamadı. Kelimeleri genişletip yeniden deneyin.";
    }
    $("#im-counts").replaceChildren(
      ...LEVEL_ORDER.filter((l) => job.counts[l]).map((l) => el("span", { class: "count-pill" }, levelBadge(l), ` ${job.counts[l]}`)),
    );
    $("#im-res-summary").textContent = (job.extraction && job.extraction.summary) || "";
    $("#im-res-keywords").replaceChildren(...chipList(job.keywords || []));
    renderTable();
  }

  function visibleResults() {
    const job = state.job;
    if (!job || !job.results) return [];
    const levels = new Set($$("#im-level-filter input:checked").map((i) => i.value));
    const text = $("#im-res-q").value.trim().toLocaleLowerCase("tr");
    const onlyUnverified = $("#im-unverified").checked;
    let rows = job.results.filter((r) => levels.has(r.impact_level));
    if (text) {
      rows = rows.filter((r) =>
        [r.contract_no, r.title, (r.parties || []).join(" "), r.summary, ...(r.affected_clauses || []).map((c) => `${c.clause_ref} ${c.reason}`)]
          .join(" ").toLocaleLowerCase("tr").includes(text));
    }
    if (onlyUnverified) rows = rows.filter((r) => r.unverified_quotes);
    const sort = $("#im-sort").value;
    const byLevel = (a, b) => LEVEL_ORDER.indexOf(a.impact_level) - LEVEL_ORDER.indexOf(b.impact_level);
    const byConf = (a, b) => (b.confidence || 0) - (a.confidence || 0);
    const byNo = (a, b) => String(a.contract_no || "").localeCompare(String(b.contract_no || ""), "tr");
    const sorters = { level: (a, b) => byLevel(a, b) || byConf(a, b), confidence: (a, b) => byConf(a, b) || byLevel(a, b), contract: byNo };
    return rows.sort(sorters[sort] || sorters.level);
  }

  function renderTable() {
    const rows = visibleResults();
    const tbody = $("#im-rows");
    if (!rows.length) {
      tbody.replaceChildren(emptyRow(7, "Filtrelere uyan sonuç yok."));
      return;
    }
    tbody.replaceChildren(...rows.flatMap(resultRows));
  }

  function resultRows(result) {
    const clauses = result.affected_clauses || [];
    const detail = el("tr", { class: "detail-row hidden" }, el("td", { colspan: 7 }, resultDetail(result)));
    const toggle = el("button", { type: "button", class: "btn-link expander", "aria-expanded": "false", title: "Ayrıntılar" }, "▸");
    const flip = () => {
      const open = detail.classList.toggle("hidden") === false;
      toggle.textContent = open ? "▾" : "▸";
      toggle.setAttribute("aria-expanded", String(open));
    };
    toggle.addEventListener("click", (event) => {
      event.stopPropagation();
      flip();
    });
    const warn = result.unverified_quotes ? el("span", { class: "warn", title: "Doğrulanamayan alıntı var" }, " ⚠️") : null;
    const row = el("tr", { class: "clickable", onclick: flip },
      el("td", {}, toggle),
      el("td", {}, levelBadge(result.impact_level), warn),
      el("td", { class: "nowrap strong" }, result.contract_no || result.contract_id),
      el("td", {}, (result.parties || []).join(", ") || result.title || "—"),
      el("td", {}, clauses.map((c) => c.clause_ref).filter(Boolean).join(", ") || "—"),
      el("td", {}, result.error ? `⚠️ ${result.error}` : (clauses[0] && clauses[0].reason) || result.summary || "—"),
      el("td", { class: "nowrap" }, result.confidence !== undefined && result.confidence !== null ? `%${Math.round(result.confidence * 100)}` : "—"),
    );
    return [row, detail];
  }

  function resultDetail(result) {
    const clauses = result.affected_clauses || [];
    return el("div", { class: "result-detail" },
      el("div", { class: "kv" },
        el("dt", {}, "Sözleşme"), el("dd", {}, el("a", { href: `#sozlesmeler/${encodeURIComponent(result.contract_id)}` }, `${result.contract_no || result.contract_id} — ${result.title || ""}`)),
        el("dt", {}, "Özet"), el("dd", {}, result.summary || "—"),
        el("dt", {}, "Önerilen aksiyon"), el("dd", {}, result.recommended_action || "—"),
        el("dt", {}, "Değerlendirilen madde"), el("dd", {}, fmtNumber(result.candidate_count)),
      ),
      ...clauses.map((c) => el("div", { class: `affected ${c.quote_verified ? "" : "unverified"}` },
        el("div", { class: "affected-head" },
          el("span", { class: "strong" }, c.clause_ref || "Madde"),
          c.clause_title ? ` — ${c.clause_title}` : "",
          c.page ? el("span", { class: "muted" }, ` · s. ${c.page}`) : null,
          c.is_attachment && c.file_name ? el("span", { class: "muted" }, ` · 📎 ${c.file_name}`) : null,
        ),
        el("blockquote", {}, c.quote),
        c.quote_verified ? null : el("p", { class: "warn small" }, "⚠️ Bu alıntı sözleşme metninde birebir bulunamadı; model hatası olabilir. Lütfen PDF'ten kontrol edin."),
        el("p", {}, el("span", { class: "strong" }, "Gerekçe: "), c.reason),
        el("div", { class: "source-actions" },
          c.chunk_id ? el("button", { type: "button", class: "btn btn-secondary btn-sm", onclick: () => Core.openSourcePanel({ chunk_id: c.chunk_id, contract_id: result.contract_id, contract_no: result.contract_no, title: result.title, parties: result.parties, clause_ref: c.clause_ref, page_start: c.page, file_id: c.file_id }) }, "Maddeyi göster") : null,
          c.file_id ? pdfLink(result.contract_id, c.file_id, c.page) : null,
        ),
      )),
    );
  }

  // ---------- history ----------
  async function loadHistory() {
    const tbody = $("#im-history");
    try {
      const data = await api("api/impact/history?limit=30");
      tbody.replaceChildren(
        ...(data.items.length
          ? data.items.map((job) => el("tr", { class: "clickable", onclick: () => (location.hash = `#etki/${job.id}`) },
              el("td", { class: "nowrap" }, fmtDateTime(job.created_at)),
              el("td", {}, job.title || "—"),
              el("td", {}, job.created_by || "—"),
              el("td", {}, STATUS[job.status] || job.status),
              el("td", {}, `${job.counts.ETKILENIR || 0} / ${job.counts.MUHTEMELEN_ETKILENIR || 0}`),
              el("td", {}, `${fmtNumber(job.processed)} / ${fmtNumber(job.total)}`)))
          : [emptyRow(6, "Henüz analiz yapılmadı.")]),
      );
    } catch (err) {
      tbody.replaceChildren(emptyRow(6, err.message));
    }
  }

  function fillTypes() {
    const select = $("#im-type");
    if (select.options.length > 1) return;
    api("api/contracts/filters")
      .then((options) => select.append(...options.contract_types.map((t) => el("option", { value: t }, t))))
      .catch(() => {});
  }

  // ---------- wiring ----------
  function init() {
    $("#im-text").addEventListener("input", updateChars);
    $("#im-file").addEventListener("change", () => {
      const file = $("#im-file").files[0];
      state.pdf = null;
      $("#im-file-name").textContent = "";
      if (!file) return;
      if (file.type && file.type !== "application/pdf") {
        Core.showError("Sadece PDF dosyası yüklenebilir.");
        $("#im-file").value = "";
        return;
      }
      state.pdf = file;
      $("#im-file-name").textContent = `${file.name} (${fmtNumber(Math.round(file.size / 1024))} KB) — metin kutusu boşsa PDF kullanılır`;
    });
    $("#im-extract").addEventListener("click", extract);
    $("#im-chip-add").addEventListener("click", addChip);
    $("#im-chip-input").addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        addChip();
      }
    });
    $("#im-back").addEventListener("click", () => showStep(1));
    $("#im-run").addEventListener("click", run);
    $("#im-cancel").addEventListener("click", cancel);
    $("#im-new").addEventListener("click", reset);
    for (const id of ["#im-res-q"]) $(id).addEventListener("input", renderTable);
    for (const id of ["#im-sort", "#im-unverified"]) $(id).addEventListener("change", renderTable);
    $$("#im-level-filter input").forEach((input) => input.addEventListener("change", renderTable));
  }

  function show(params) {
    fillTypes();
    loadHistory();
    if (params[0]) {
      loadJob(params[0]);
    } else {
      stopPolling();
      if (state.job || !$("#im-step3").classList.contains("hidden") || !$("#im-step4").classList.contains("hidden")) {
        state.job = null;
        showStep(state.extraction ? 2 : 1);
      }
    }
  }

  Core.registerView("etki", { init, show });
})();
