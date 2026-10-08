/* Chat screen: RAG answers streamed over SSE, clickable [n] citations, source cards. */
"use strict";

(() => {
  const { el, $, api, apiStream, readSSE, ApiError, copyText, storageGet, storageSet } = Core;
  const STORAGE_KEY = "sozlesme.chat.v2";
  const EXAMPLES = [
    "ABC firmasıyla olan sözleşmede fesih bildirim süresi ne kadar?",
    "Hangi sözleşmelerde cezai şart maddesi var?",
    "Gizlilik yükümlülüğü sözleşme bitiminden sonra ne kadar sürüyor?",
    "Mücbir sebep halleri hangi sözleşmelerde düzenlenmiş?",
  ];
  // Completed turns: {role, content, sources?}. Sources are kept without text.
  let history = [];
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

  function sourceLabel(source) {
    const pages = source.page_start ? `s. ${source.page_start}${source.page_end && source.page_end !== source.page_start ? `–${source.page_end}` : ""}` : "";
    return [source.contract_no || source.contract_id, source.clause_ref, pages].filter(Boolean).join(" · ");
  }

  function citedNumbers(text) {
    return new Set(Array.from(text.matchAll(/\[(\d{1,2})\]/g), (m) => Number(m[1])));
  }

  function addAssistantMessage() {
    const body = el("div", { class: "msg-body" },
      el("span", { class: "typing", "aria-label": "yazıyor" }, el("span"), el("span"), el("span")));
    const sourcesBox = el("div", { class: "msg-sources hidden" });
    const actions = el("div", { class: "msg-actions hidden" });
    const wrapper = el("div", { class: "msg msg-assistant" }, body, sourcesBox, actions);
    $("#chat-log").append(wrapper);
    updateEmptyState();
    scrollToEnd();

    let text = "";
    let sources = [];
    let pending = false;
    const openSource = (n) => {
      const source = sources.find((s) => s.n === n);
      if (source) Core.openSourcePanel(source);
    };
    const paint = () => {
      pending = false;
      body.replaceChildren(Markdown.render(text, { onCitation: openSource }));
      scrollToEnd();
    };
    const renderSources = () => {
      const cited = citedNumbers(text);
      // Show the cited sources; if the answer cites nothing, list all retrieved clauses.
      const shown = cited.size ? sources.filter((s) => cited.has(s.n)) : sources;
      if (!shown.length || text.includes("bu bilgi bulunamadı")) return;
      sourcesBox.replaceChildren(
        el("div", { class: "msg-sources-title" }, cited.size ? "Kaynaklar" : "İlgili maddeler"),
        ...shown.map((s) => el("button", { type: "button", class: "source-card", onclick: () => Core.openSourcePanel(s), title: s.title || "" },
          el("span", { class: "source-n" }, String(s.n)),
          el("span", {}, el("span", { class: "strong" }, sourceLabel(s)),
            s.title ? el("span", { class: "muted" }, ` — ${s.title}`) : null,
            s.is_attachment ? el("span", { class: "muted" }, " 📎") : null),
        )),
      );
      sourcesBox.classList.remove("hidden");
    };
    return {
      setSources(list) {
        sources = list || [];
      },
      get sources() {
        return sources;
      },
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
        renderSources();
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
    $("#chat-scope").disabled = busy;
  }

  function remember(question, answer) {
    // Source texts are not stored (they can be large); the panel reloads them by chunk id.
    const sources = answer.sources.map(({ text, ...rest }) => ({ ...rest, text: undefined }));
    history.push({ role: "user", content: question }, { role: "assistant", content: answer.text, sources });
    storageSet(STORAGE_KEY, { history, scope: $("#chat-scope").value });
  }

  async function ask(question) {
    addUserMessage(question);
    const answer = addAssistantMessage();
    controller = new AbortController();
    setBusy(true);
    let completed = false;
    const payload = {
      message: question,
      history: history.map(({ role, content }) => ({ role, content })),
      contract_id: $("#chat-scope").value || null,
    };
    try {
      const response = await apiStream("api/chat", payload, controller.signal);
      for await (const { event, data } of readSSE(response)) {
        if (event === "sources" && data) answer.setSources(data.sources);
        else if (event === "delta" && data) answer.append(data.text || "");
        else if (event === "error") throw new ApiError(data.code, data.message, 0);
        else if (event === "done") completed = true;
      }
      if (!completed) throw new ApiError("stream_cut", "Bağlantı kesildi; cevap tamamlanamadı.", 0);
      answer.finish();
      remember(question, answer);
    } catch (err) {
      if (err.name === "AbortError") {
        answer.finish("Cevap durduruldu.");
        if (answer.text) remember(question, answer);
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
    storageSet(STORAGE_KEY, { history, scope: $("#chat-scope").value });
    $("#chat-log").querySelectorAll(".msg").forEach((node) => node.remove());
    updateEmptyState();
    $("#chat-input").focus();
  }

  async function loadScopes(selected) {
    const select = $("#chat-scope");
    try {
      // VARSAYIM: The scope list shows up to 500 contracts (enough for the legal team's archive).
      const data = await api("api/contracts?page_size=500");
      select.replaceChildren(
        el("option", { value: "" }, "Tüm sözleşmeler"),
        ...data.items.map((c) => el("option", { value: c.contract_id }, `${c.contract_no || c.contract_id} — ${c.title || ""}`.slice(0, 90))),
      );
      if (selected && data.items.some((c) => c.contract_id === selected)) select.value = selected;
    } catch (err) {
      /* the chat still works across all contracts */
    }
  }

  function restore() {
    const saved = storageGet(STORAGE_KEY, {});
    history = Array.isArray(saved.history) ? saved.history : [];
    for (const turn of history) {
      if (turn.role === "user") {
        addUserMessage(turn.content);
      } else {
        const answer = addAssistantMessage();
        answer.setSources((turn.sources || []).map((s) => ({ ...s, text: undefined })));
        answer.append(turn.content);
        answer.finish();
      }
    }
    updateEmptyState();
    return saved.scope || "";
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
    $("#chat-scope").addEventListener("change", () => storageSet(STORAGE_KEY, { history, scope: $("#chat-scope").value }));
    const scope = restore();
    loadScopes(scope);
  }

  function show(params) {
    // #sohbet/<contract_id> opens the chat scoped to one contract.
    if (params[0]) {
      loadScopes(params[0]).then(() => {
        $("#chat-scope").value = params[0];
        storageSet(STORAGE_KEY, { history, scope: params[0] });
      });
    }
    $("#chat-input").focus();
  }

  Core.registerView("sohbet", { init, show });
})();
