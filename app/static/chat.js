/* Chat screen: streaming answers (SSE), sources, safe markdown. */
"use strict";

(() => {
  const { el, $, api, apiStream, readSSE, ApiError, copyText, storageGet, storageSet } = Core;

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

  Core.registerView("sohbet", { init });
})();
