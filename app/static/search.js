/* Keyword search screen: AND/OR, "exact phrase", filters, highlighted snippets. */
"use strict";

(() => {
  const { el, $, api, query, fmtNumber, pdfLink } = Core;
  const PAGE_SIZE = 20;
  let page = 1;
  let lastQuery = null;

  function params() {
    return {
      q: $("#sr-q").value.trim(),
      mode: $("input[name='sr-mode']:checked").value,
      contract_type: $("#sr-type").value,
      party: $("#sr-party").value.trim(),
      date_from: $("#sr-from").value,
      date_to: $("#sr-to").value,
      active_only: $("#sr-active").checked,
    };
  }

  function snippet(segments) {
    return el("p", { class: "snippet" }, ...segments.map((s) => (s.hit ? el("mark", {}, s.text) : s.text)));
  }

  function resultCard(item) {
    const pages = item.page_end && item.page_end !== item.page_start ? `s. ${item.page_start}–${item.page_end}` : `s. ${item.page_start}`;
    return el("article", { class: "result" },
      el("div", { class: "result-head" },
        el("a", { class: "strong", href: `#sozlesmeler/${encodeURIComponent(item.contract_id)}` }, item.contract_no || item.contract_id),
        el("span", {}, ` — ${item.title || ""}`),
        el("span", { class: "spacer" }),
        el("span", { class: "muted small" }, (item.parties || []).join(", ")),
      ),
      el("div", { class: "result-ref" },
        el("span", { class: "strong" }, item.clause_ref || "Bölüm"),
        item.clause_title ? ` — ${item.clause_title}` : "",
        el("span", { class: "muted" }, ` · ${pages}${item.is_attachment ? ` · 📎 ${item.file_name || "Ek"}` : ""}`),
      ),
      snippet(item.snippet),
      el("div", { class: "source-actions" },
        el("button", { type: "button", class: "btn btn-secondary btn-sm", onclick: () => Core.openSourcePanel({ ...item, text: null }) }, "Maddeyi göster"),
        pdfLink(item.contract_id, item.file_id, item.page_start),
      ),
    );
  }

  async function run(append) {
    const p = params();
    if (!p.q) {
      $("#sr-q").focus();
      return;
    }
    if (!append) page = 1;
    lastQuery = p;
    const results = $("#sr-results");
    const more = $("#sr-more");
    if (!append) results.replaceChildren(el("p", { class: "muted" }, "Aranıyor…"));
    more.classList.add("hidden");
    try {
      const data = await api(`api/search${query({ ...p, page, page_size: PAGE_SIZE })}`);
      if (!append) results.replaceChildren();
      $("#sr-summary").textContent = data.total
        ? `${fmtNumber(data.total)} madde bulundu (${p.mode === "and" ? "tüm kelimeler" : "kelimelerden biri"}).`
        : "";
      if (!data.total) {
        results.replaceChildren(el("p", { class: "muted" }, "Sonuç bulunamadı. Kelimeleri azaltmayı veya 'Herhangi biri' seçeneğini deneyin."));
        return;
      }
      results.append(...data.items.map(resultCard));
      more.classList.toggle("hidden", page * PAGE_SIZE >= data.total);
    } catch (err) {
      results.replaceChildren(el("p", { class: "alert alert-error" }, err.message));
    }
  }

  function fillTypes() {
    const select = $("#sr-type");
    if (select.options.length > 1) return;
    api("api/contracts/filters")
      .then((options) => select.append(...options.contract_types.map((t) => el("option", { value: t }, t))))
      .catch(() => {});
  }

  function init() {
    $("#sr-form").addEventListener("submit", (event) => {
      event.preventDefault();
      run(false);
    });
    $("#sr-more").addEventListener("click", () => {
      page += 1;
      run(true);
    });
    for (const id of ["#sr-type", "#sr-active", "#sr-from", "#sr-to"]) {
      $(id).addEventListener("change", () => lastQuery && run(false));
    }
    for (const radio of document.querySelectorAll("input[name='sr-mode']")) {
      radio.addEventListener("change", () => lastQuery && run(false));
    }
  }

  function show() {
    fillTypes();
    $("#sr-q").focus();
  }

  Core.registerView("arama", { init, show });
})();
