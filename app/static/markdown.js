/* Minimal, safe markdown renderer.
 *
 * Builds DOM nodes with createElement/textContent only - never innerHTML - so
 * model output cannot inject markup. Supported: headings, paragraphs, line
 * breaks, **bold**, *italic*, `code`, ``` code blocks, bullet / numbered lists
 * (flat) and pipe tables. Links are rendered as plain text on purpose.
 */
"use strict";

const Markdown = (() => {
  const INLINE_RE = /(\*\*[^*\n]+\*\*|__[^_\n]+__|`[^`\n]+`|\*[^*\s\n][^*\n]*\*)/g;

  function inline(text) {
    const fragment = document.createDocumentFragment();
    let last = 0;
    for (const match of text.matchAll(INLINE_RE)) {
      if (match.index > last) fragment.append(text.slice(last, match.index));
      const token = match[0];
      let node;
      if (token.startsWith("**") || token.startsWith("__")) {
        node = document.createElement("strong");
        node.textContent = token.slice(2, -2);
      } else if (token.startsWith("`")) {
        node = document.createElement("code");
        node.textContent = token.slice(1, -1);
      } else {
        node = document.createElement("em");
        node.textContent = token.slice(1, -1);
      }
      fragment.append(node);
      last = match.index + token.length;
    }
    if (last < text.length) fragment.append(text.slice(last));
    return fragment;
  }

  const isTableRow = (line) => /^\s*\|.*\|\s*$/.test(line);
  const isTableSeparator = (line) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(line);
  const BULLET_RE = /^\s*[-*•]\s+(.*)$/;
  const NUMBER_RE = /^\s*\d+[.)]\s+(.*)$/;
  const HEADING_RE = /^(#{1,6})\s+(.*)$/;

  function splitRow(line) {
    return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
  }

  function renderTable(rows) {
    const table = document.createElement("table");
    const thead = document.createElement("thead");
    const headRow = document.createElement("tr");
    for (const cell of splitRow(rows[0])) {
      const th = document.createElement("th");
      th.append(inline(cell));
      headRow.append(th);
    }
    thead.append(headRow);
    const tbody = document.createElement("tbody");
    for (const row of rows.slice(2)) {
      const tr = document.createElement("tr");
      for (const cell of splitRow(row)) {
        const td = document.createElement("td");
        td.append(inline(cell));
        tr.append(td);
      }
      tbody.append(tr);
    }
    table.append(thead, tbody);
    const wrapper = document.createElement("div");
    wrapper.className = "md-table";
    wrapper.append(table);
    return wrapper;
  }

  function render(source) {
    const root = document.createDocumentFragment();
    const lines = String(source || "").replace(/\r\n?/g, "\n").split("\n");
    let i = 0;
    let paragraph = [];

    const flushParagraph = () => {
      if (!paragraph.length) return;
      const p = document.createElement("p");
      paragraph.forEach((line, index) => {
        if (index) p.append(document.createElement("br"));
        p.append(inline(line));
      });
      root.append(p);
      paragraph = [];
    };

    while (i < lines.length) {
      const line = lines[i];

      if (/^\s*```/.test(line)) {
        flushParagraph();
        const code = [];
        i += 1;
        while (i < lines.length && !/^\s*```/.test(lines[i])) code.push(lines[i++]);
        i += 1; // closing fence (or end of text while streaming)
        const pre = document.createElement("pre");
        const codeEl = document.createElement("code");
        codeEl.textContent = code.join("\n");
        pre.append(codeEl);
        root.append(pre);
        continue;
      }

      if (!line.trim()) {
        flushParagraph();
        i += 1;
        continue;
      }

      const heading = line.match(HEADING_RE);
      if (heading) {
        flushParagraph();
        // Keep headings small inside chat bubbles: # -> h3 ... ### and deeper -> h5.
        const level = Math.min(heading[1].length + 2, 5);
        const h = document.createElement(`h${level}`);
        h.append(inline(heading[2]));
        root.append(h);
        i += 1;
        continue;
      }

      if (isTableRow(line) && i + 1 < lines.length && isTableSeparator(lines[i + 1])) {
        flushParagraph();
        const rows = [];
        while (i < lines.length && (isTableRow(lines[i]) || (rows.length === 1 && isTableSeparator(lines[i])))) {
          rows.push(lines[i++]);
        }
        root.append(renderTable(rows));
        continue;
      }

      const listMatch = line.match(BULLET_RE) || line.match(NUMBER_RE);
      if (listMatch) {
        flushParagraph();
        const ordered = !BULLET_RE.test(line);
        const list = document.createElement(ordered ? "ol" : "ul");
        const itemRe = ordered ? NUMBER_RE : BULLET_RE;
        while (i < lines.length && itemRe.test(lines[i])) {
          const li = document.createElement("li");
          li.append(inline(lines[i].match(itemRe)[1]));
          list.append(li);
          i += 1;
        }
        root.append(list);
        continue;
      }

      paragraph.push(line);
      i += 1;
    }
    flushParagraph();
    return root;
  }

  return { render, inline };
})();
