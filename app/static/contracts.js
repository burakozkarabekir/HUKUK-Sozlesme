/* Contracts screen: filtered list (#sozlesmeler) and detail (#sozlesmeler/<id>). */
"use strict";

(() => {
  const { el, $, api, query, fmtDate, fmtNumber, pdfLink, emptyRow, debounce } = Core;
  const PAGE_SIZE = 25;
  let page = 1;
  let filtersLoaded = false;

  function currentFilters() {
    return {
      q: $("#ct-q").value.trim(),
      contract_type: $("#ct-type").value,
      status: $("#ct-status").value,
      party: $("#ct-party").value.trim(),
      active_only: $("#ct-active").checked,
    };
  }

  async function loadFilterOptions() {
    if (filtersLoaded) return;
    try {
      const options = await api("api/contracts/filters");
      fillSelect($("#ct-type"), options.contract_types, "Tüm tipler");
      fillSelect($("#ct-status"), options.statuses, "Tüm durumlar");
      Core.state.filterOptions = options;
      filtersLoaded = true;
    } catch (err) {
      Core.showError(err.message);
    }
  }

  function fillSelect(select, values, allLabel) {
    const current = select.value;
    select.replaceChildren(el("option", { value: "" }, allLabel), ...values.map((v) => el("option", { value: v }, v)));
    select.value = current;
  }

  function statusBadge(contract) {
    return el("span", { class: `badge ${contract.is_active ? "badge-green" : "badge-gray"}` }, contract.status || (contract.is_active ? "Yürürlükte" : "—"));
  }

  async function loadList() {
    const tbody = $("#ct-rows");
    tbody.replaceChildren(emptyRow(7, "Yükleniyor…"));
    try {
      const data = await api(`api/contracts${query({ ...currentFilters(), page, page_size: PAGE_SIZE })}`);
      $("#ct-count").textContent = `${fmtNumber(data.total)} sözleşme`;
      if (!data.items.length) {
        tbody.replaceChildren(emptyRow(7, data.total ? "Bu sayfada kayıt yok." : "Sözleşme bulunamadı. Yönetim ekranından senkronizasyon başlatabilirsiniz."));
      } else {
        tbody.replaceChildren(...data.items.map(row));
      }
      const pages = Math.max(1, Math.ceil(data.total / PAGE_SIZE));
      $("#ct-page").textContent = `Sayfa ${page} / ${pages}`;
      $("#ct-prev").disabled = page <= 1;
      $("#ct-next").disabled = page >= pages;
    } catch (err) {
      tbody.replaceChildren(emptyRow(7, err.message));
    }
  }

  function row(contract) {
    const open = () => (location.hash = `#sozlesmeler/${encodeURIComponent(contract.contract_id)}`);
    return el("tr", { class: "clickable", tabindex: "0", onclick: open, onkeydown: (e) => e.key === "Enter" && open() },
      el("td", { class: "nowrap strong" }, contract.contract_no || "—"),
      el("td", {}, contract.title || "—"),
      el("td", {}, (contract.parties || []).join(", ") || "—"),
      el("td", {}, contract.contract_type || "—"),
      el("td", { class: "nowrap" }, `${fmtDate(contract.start_date)} – ${fmtDate(contract.end_date)}`),
      el("td", {}, statusBadge(contract)),
      el("td", { class: "nowrap" }, contract.ocr_pending ? el("span", { class: "badge badge-orange", title: "Taranmış PDF; OCR gerekli" }, "OCR bekliyor") : "✓"),
    );
  }

  async function loadDetail(contractId) {
    const box = $("#ct-detail");
    box.replaceChildren(el("p", { class: "muted" }, "Yükleniyor…"));
    try {
      const c = await api(`api/contracts/${encodeURIComponent(contractId)}`);
      box.replaceChildren(
        el("div", { class: "detail-header" },
          el("a", { href: "#sozlesmeler", class: "btn btn-secondary btn-sm" }, "← Listeye dön"),
          el("h3", {}, `${c.contract_no || c.contract_id} — ${c.title || ""}`),
          statusBadge(c),
          el("a", { href: `#sohbet/${encodeURIComponent(c.contract_id)}`, class: "btn btn-primary btn-sm" }, "💬 Bu sözleşmede sor"),
        ),
        el("dl", { class: "kv card" },
          el("dt", {}, "Taraflar"), el("dd", {}, (c.parties || []).join(", ") || "—"),
          el("dt", {}, "Tip"), el("dd", {}, c.contract_type || "—"),
          el("dt", {}, "Başlangıç / Bitiş"), el("dd", {}, `${fmtDate(c.start_date)} – ${fmtDate(c.end_date)}`),
          el("dt", {}, "Kaynak"), el("dd", {}, `${c.source || "—"} (ID: ${c.contract_id})`),
          ...Object.entries(c.extra || {}).slice(0, 12).flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, typeof v === "object" ? JSON.stringify(v) : String(v))]),
        ),
        el("h4", {}, "Dosyalar"),
        el("div", { class: "table-wrap" }, el("table", { class: "table" },
          el("thead", {}, el("tr", {}, ...["Dosya", "Tür", "Sayfa", "Metin", ""].map((h) => el("th", {}, h)))),
          el("tbody", {}, ...c.files.map((f) => el("tr", {},
            el("td", {}, f.file_name),
            el("td", {}, f.is_attachment ? "📎 Ek" : "Ana sözleşme"),
            el("td", {}, fmtNumber(f.page_count)),
            el("td", {}, ocrLabel(f)),
            el("td", {}, pdfLink(c.contract_id, f.file_id, null)),
          ))),
        )),
        el("h4", {}, `Maddeler (${c.clauses.length})`),
        c.clauses.length ? el("div", { class: "clauses" }, ...c.clauses.map((k) => clauseItem(c, k))) : el("p", { class: "muted" }, "Bu sözleşmeden metin çıkarılamadı."),
      );
    } catch (err) {
      box.replaceChildren(el("a", { href: "#sozlesmeler", class: "btn btn-secondary btn-sm" }, "← Listeye dön"), el("p", { class: "alert alert-error" }, err.message));
    }
  }

  function ocrLabel(file) {
    if (file.ocr_status === "done") return "OCR ile okundu";
    if (file.ocr_status === "pending") return el("span", { class: "badge badge-orange" }, "Taranmış – OCR bekliyor");
    if (file.ocr_status === "failed") return el("span", { class: "badge badge-red" }, "OCR başarısız");
    return `Metin (${file.extract_method || "pdf"})`;
  }

  function clauseItem(contract, clause) {
    const pages = clause.page_end && clause.page_end !== clause.page_start ? `s. ${clause.page_start}–${clause.page_end}` : `s. ${clause.page_start}`;
    const file = contract.files.find((f) => f.file_id === clause.file_id);
    return el("details", { class: "clause" },
      el("summary", {},
        el("span", { class: "strong" }, clause.clause_ref || "Bölüm"),
        clause.clause_title ? ` — ${clause.clause_title}` : "",
        el("span", { class: "muted small" }, ` · ${pages}${clause.is_attachment ? " · 📎 " + (file ? file.file_name : "Ek") : ""}`),
      ),
      el("pre", { class: "source-text" }, clause.text),
      el("div", { class: "source-actions" }, pdfLink(contract.contract_id, clause.file_id, clause.page_start)),
    );
  }

  function show(params) {
    loadFilterOptions();
    const detail = params[0];
    $("#ct-list").classList.toggle("hidden", Boolean(detail));
    $("#ct-detail").classList.toggle("hidden", !detail);
    if (detail) loadDetail(detail);
    else loadList();
  }

  function init() {
    const reload = () => {
      page = 1;
      loadList();
    };
    $("#ct-q").addEventListener("input", debounce(reload));
    $("#ct-party").addEventListener("input", debounce(reload));
    for (const id of ["#ct-type", "#ct-status", "#ct-active"]) $(id).addEventListener("change", reload);
    $("#ct-prev").addEventListener("click", () => { page = Math.max(1, page - 1); loadList(); });
    $("#ct-next").addEventListener("click", () => { page += 1; loadList(); });
  }

  Core.registerView("sozlesmeler", { init, show });
})();
