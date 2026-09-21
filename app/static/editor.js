/* Rich-text редактор для VK и Telegram.
   contenteditable + сериализация в подмножество HTML (то же, что понимает Telegram Bot API).
   TG: контекстное меню по правому клику на выделении. VK: всплывающая панель Ж К Ч 🔗 над выделением. */
const RTE = (() => {
  const TAGS = { B: "b", STRONG: "b", I: "i", EM: "i", U: "u", S: "s", STRIKE: "s", DEL: "s", CODE: "code", PRE: "pre", BLOCKQUOTE: "blockquote", "TG-SPOILER": "tg-spoiler" };
  const ALLOWED = {
    tg: new Set(["b", "i", "u", "s", "code", "pre", "blockquote", "tg-spoiler", "a"]),
    vk: new Set(["b", "i", "u", "a"]),
    common: new Set(["b", "i", "u", "s", "a"]),   // общий текст: то, что понимают обе площадки
    plain: new Set(),
  };
  const CMDS = {
    tg: ["bold", "italic", "underline", "strike", "quote", "code", "spoiler", "link", "clear"],
    vk: ["bold", "italic", "underline", "link", "clear"],
    common: ["bold", "italic", "underline", "strike", "link", "clear"],
    plain: [],
  };
  const MENU = [
    ["Жирный", "Ctrl+B", "bold"], ["Курсив", "Ctrl+I", "italic"], ["Подчёркнутый", "Ctrl+U", "underline"],
    ["Зачёркнутый", "Ctrl+Shift+X", "strike"], ["Цитата", "Ctrl+Shift+.", "quote"], ["Моноширинный", "Ctrl+Shift+M", "code"],
    ["Скрытый", "Ctrl+Shift+P", "spoiler"], null, ["Добавить ссылку", "Ctrl+K", "link"], null, ["Без форматирования", "Ctrl+Shift+N", "clear"],
  ];
  const KEYS = { KeyB: [false, "bold"], KeyI: [false, "italic"], KeyU: [false, "underline"], KeyX: [true, "strike"], KeyM: [true, "code"], KeyP: [true, "spoiler"], Period: [true, "quote"], KeyK: [false, "link"], KeyN: [true, "clear"] };
  const escT = (s) => s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  const escA = (s) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const editors = new Map(); // el -> platform

  // ---------------- HTML из буфера обмена -> наш HTML (ссылки в словах, жирный и т.п., переносы строк)
  function normalizeHref(href) {
    try {
      const u = new URL(href, "https://vk.com");
      if (/vk\.(com|ru)$/.test(u.hostname) && u.pathname === "/away.php" && u.searchParams.get("to")) return u.searchParams.get("to");
      return u.href;
    } catch { return href; }
  }
  const BLOCK = new Set(["DIV", "P", "LI", "TR", "H1", "H2", "H3", "H4", "BLOCKQUOTE", "SECTION", "ARTICLE"]);
  function clipboardToOurHtml(html, allowed) {
    const doc = new DOMParser().parseFromString(html, "text/html");
    doc.querySelectorAll("script,style,meta,title").forEach((n) => n.remove());
    const walk = (node) => {
      let out = "";
      for (const ch of node.childNodes) {
        if (ch.nodeType === 3) { out += escT(ch.textContent.replace(/\u00a0/g, " ")); continue; }
        if (ch.nodeType !== 1) continue;
        const tag = ch.tagName;
        if (tag === "BR") { out += "\n"; continue; }
        if (tag === "IMG") { out += escT(ch.getAttribute("alt") || ""); continue; }
        const inner = walk(ch);
        const name = tag === "A" ? "a" : TAGS[tag];
        if (name === "a" && allowed.has("a") && ch.getAttribute("href")) out += `<a href="${escA(normalizeHref(ch.getAttribute("href")))}">${inner}</a>`;
        else if (name && allowed.has(name)) out += `<${name}>${inner}</${name}>`;
        else if (BLOCK.has(tag)) { if (out && !out.endsWith("\n")) out += "\n"; out += inner; if (!inner.endsWith("\n")) out += "\n"; }
        else out += inner;
      }
      return out;
    };
    return walk(doc.body).replace(/\n{3,}/g, "\n\n").replace(/^\n+|\n+$/g, "");
  }

  // ---------------- DOM -> HTML-строка
  function serialize(node, allowed) {
    let out = "";
    for (const ch of node.childNodes) {
      if (ch.nodeType === 3) { out += escT(ch.textContent); continue; }
      if (ch.nodeType !== 1) continue;
      const tag = ch.tagName;
      if (tag === "BR") { out += "\n"; continue; }
      if (tag === "IMG") { if (ch.classList.contains("emoji")) out += escT(ch.getAttribute("alt") || ""); continue; }
      const inner = serialize(ch, allowed);
      if (tag === "A" && allowed.has("a")) out += `<a href="${escA(ch.getAttribute("href") || "")}">${inner}</a>`;
      else if (TAGS[tag] && allowed.has(TAGS[tag])) out += `<${TAGS[tag]}>${inner}</${TAGS[tag]}>`;
      else if (tag === "DIV" || tag === "P") {
        if (out && !out.endsWith("\n")) out += "\n";
        out += inner;
        if (!inner.endsWith("\n")) out += "\n";
      } else out += inner;
    }
    return out;
  }
  function get(el) {
    return serialize(el, ALLOWED[editors.get(el) || "tg"]).replace(/\n+$/, "");
  }

  // ---------------- HTML-строка -> DOM (санитайз + \n -> <br>)
  function toDom(html, allowed) {
    const doc = new DOMParser().parseFromString(`<div>${html || ""}</div>`, "text/html");
    const frag = document.createDocumentFragment();
    const walk = (src, dst) => {
      for (const ch of src.childNodes) {
        if (ch.nodeType === 3) {
          ch.textContent.split("\n").forEach((t, i) => {
            if (i) dst.appendChild(document.createElement("br"));
            if (t) dst.appendChild(VKEmoji.fragment(t).frag);
          });
        } else if (ch.nodeType === 1) {
          if (ch.tagName === "IMG") { if (ch.classList.contains("emoji")) dst.appendChild(VKEmoji.fragment(ch.getAttribute("alt") || "").frag); continue; }
          const tag = ch.tagName, name = tag === "A" ? "a" : TAGS[tag];
          if (name && allowed.has(name)) {
            const el = document.createElement(name);
            if (name === "a") el.setAttribute("href", ch.getAttribute("href") || "");
            walk(ch, el); dst.appendChild(el);
          } else if (tag === "BR") dst.appendChild(document.createElement("br"));
          else walk(ch, dst);
        }
      }
    };
    walk(doc.body.firstChild, frag);
    return frag;
  }
  function set(el, html) { el.replaceChildren(toDom(html, ALLOWED[editors.get(el) || "tg"])); }
  function setPlain(el, text) { set(el, escT(text || "")); }
  function getPlain(el) { return get(el).replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&"); }
  function plainLength(html) {
    return (html || "").replace(/<[^>]+>/g, "").replace(/&(amp|lt|gt|quot);/g, "x").length;
  }

  // Эмодзи, набранные с клавиатуры, превращаем в картинки VK, не теряя курсор
  function emojifyTyped(root) {
    if (!VKEmoji.test(root.textContent)) return;
    const sel = getSelection();
    const anchor = sel.rangeCount && sel.isCollapsed && root.contains(sel.anchorNode) ? sel.anchorNode : null;
    const anchorOffset = sel.anchorOffset;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    let caret = null;
    for (const tn of nodes) {
      if (!VKEmoji.test(tn.textContent)) continue;
      const { frag, caretNode, caretOffset } = VKEmoji.fragment(tn.textContent, tn === anchor ? anchorOffset : null);
      if (tn === anchor && caretNode) caret = { node: caretNode, offset: caretOffset };
      tn.replaceWith(frag);
    }
    if (caret) {
      const r = document.createRange();
      if (caret.offset < 0) r.setStartAfter(caret.node); else r.setStart(caret.node, caret.offset);
      r.collapse(true); sel.removeAllRanges(); sel.addRange(r);
    }
  }

  // ---------------- команды
  function unwrapEl(el) { const p = el.parentNode; while (el.firstChild) p.insertBefore(el.firstChild, el); p.removeChild(el); }
  function anchorEl() { const s = getSelection(); const n = s.anchorNode; return n ? (n.nodeType === 1 ? n : n.parentElement) : null; }
  function wrap(root, tag) {
    const sel = getSelection(); if (!sel.rangeCount || sel.isCollapsed) return;
    const existing = anchorEl()?.closest(tag);
    if (existing && root.contains(existing)) { unwrapEl(existing); return; }
    const r = sel.getRangeAt(0);
    const w = document.createElement(tag); w.appendChild(r.extractContents()); r.insertNode(w);
    sel.removeAllRanges(); const nr = document.createRange(); nr.selectNodeContents(w); sel.addRange(nr);
  }
  function clearFormat(root) {
    document.execCommand("removeFormat"); document.execCommand("unlink");
    const sel = getSelection(); if (!sel.rangeCount) return;
    const r = sel.getRangeAt(0);
    root.querySelectorAll("code,pre,blockquote,tg-spoiler,b,strong,i,em,u,s,strike,a").forEach((n) => { if (r.intersectsNode(n)) unwrapEl(n); });
  }
  function exec(el, cmd) {
    if (!CMDS[editors.get(el)].includes(cmd)) return;
    el.focus();
    switch (cmd) {
      case "bold": document.execCommand("bold"); break;
      case "italic": document.execCommand("italic"); break;
      case "underline": document.execCommand("underline"); break;
      case "strike": document.execCommand("strikeThrough"); break;
      case "code": wrap(el, "code"); break;
      case "spoiler": wrap(el, "tg-spoiler"); break;
      case "quote": wrap(el, "blockquote"); break;
      case "link": {
        const cur = anchorEl()?.closest("a");
        const url = prompt("Ссылка:", cur ? cur.getAttribute("href") : "https://");
        if (url === null) return;
        if (!url.trim() || url.trim() === "https://") document.execCommand("unlink");
        else document.execCommand("createLink", false, url.trim());
        break;
      }
      case "clear": clearFormat(el); break;
    }
    el.dispatchEvent(new Event("input", { bubbles: true }));
  }
  function active(cmd) {
    try {
      if (cmd === "bold" || cmd === "italic" || cmd === "underline") return document.queryCommandState(cmd);
      if (cmd === "link") return !!anchorEl()?.closest("a");
    } catch { /* ignore */ }
    return false;
  }

  // ---------------- контекстное меню (Telegram)
  const menu = document.createElement("div"); menu.className = "ctx-menu hidden"; document.body.appendChild(menu);
  let menuFor = null;
  function showMenu(x, y, el) {
    menuFor = el;
    menu.innerHTML = MENU.map((m) => m ? `<button data-cmd="${m[2]}"><span>${m[0]}</span><kbd>${m[1]}</kbd></button>` : `<hr>`).join("");
    menu.classList.remove("hidden");
    const r = menu.getBoundingClientRect();
    menu.style.left = Math.min(x, innerWidth - r.width - 8) + "px";
    menu.style.top = Math.min(y, innerHeight - r.height - 8) + "px";
  }
  function hideMenu() { menu.classList.add("hidden"); menuFor = null; }
  menu.addEventListener("mousedown", (e) => e.preventDefault());
  menu.addEventListener("click", (e) => { const b = e.target.closest("[data-cmd]"); if (b && menuFor) exec(menuFor, b.dataset.cmd); hideMenu(); });
  document.addEventListener("mousedown", (e) => { if (!menu.contains(e.target)) hideMenu(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { hideMenu(); hideBubble(); } });
  window.addEventListener("scroll", hideMenu, true);

  // ---------------- всплывающая панель (VK)
  // На телефоне правого клика нет – для Telegram тоже показываем панель, с кнопкой «⋯» на полное меню.
  const TOUCH = matchMedia("(hover: none)").matches;
  const LINK_SVG = `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M10 13a5 5 0 0 0 7.1 0l3-3a5 5 0 0 0-7.1-7.1l-1.7 1.7"/><path d="M14 11a5 5 0 0 0-7.1 0l-3 3a5 5 0 0 0 7.1 7.1l1.7-1.7"/></svg>`;
  const BUBBLE = {
    vk: `<button data-cmd="bold" title="Жирный (Ctrl+B)"><b>Ж</b></button><button data-cmd="italic" title="Курсив (Ctrl+I)"><i>К</i></button><button data-cmd="underline" title="Подчёркнутый (Ctrl+U)"><u>Ч</u></button><button data-cmd="link" title="Ссылка (Ctrl+K)">${LINK_SVG}</button>`,
    common: `<button data-cmd="bold" title="Жирный (Ctrl+B)"><b>Ж</b></button><button data-cmd="italic" title="Курсив (Ctrl+I)"><i>К</i></button><button data-cmd="underline" title="Подчёркнутый (Ctrl+U)"><u>Ч</u></button><button data-cmd="strike" title="Зачёркнутый (Ctrl+Shift+X)"><s>З</s></button><button data-cmd="link" title="Ссылка (Ctrl+K)">${LINK_SVG}</button>`,
    tg: `<button data-cmd="bold"><b>Ж</b></button><button data-cmd="italic"><i>К</i></button><button data-cmd="underline"><u>Ч</u></button><button data-cmd="strike"><s>З</s></button><button data-cmd="link">${LINK_SVG}</button><button data-cmd="more">⋯</button>`,
  };
  const bubble = document.createElement("div"); bubble.className = "fmt-bubble hidden";
  document.body.appendChild(bubble);
  let bubbleFor = null, bubbleKind = null;
  bubble.addEventListener("mousedown", (e) => e.preventDefault());
  bubble.addEventListener("click", (e) => {
    const b = e.target.closest("[data-cmd]"); if (!b || !bubbleFor) return;
    if (b.dataset.cmd === "more") { const r = bubble.getBoundingClientRect(); showMenu(r.left, r.bottom + 6, bubbleFor); return; }
    exec(bubbleFor, b.dataset.cmd); updateBubble();
  });
  function hideBubble() { bubble.classList.add("hidden"); bubbleFor = null; }
  function updateBubble() {
    const sel = getSelection();
    const el = document.activeElement;
    const kind = editors.get(el);
    const show = kind === "vk" || kind === "common" || (kind === "tg" && TOUCH);
    if (!sel.rangeCount || sel.isCollapsed || !show || !el.contains(sel.anchorNode)) { hideBubble(); return; }
    const rect = sel.getRangeAt(0).getBoundingClientRect();
    if (!rect.width && !rect.height) { hideBubble(); return; }
    bubbleFor = el;
    if (bubbleKind !== kind) { bubble.innerHTML = BUBBLE[kind]; bubbleKind = kind; }
    bubble.classList.remove("hidden");
    bubble.querySelectorAll("[data-cmd]").forEach((b) => b.classList.toggle("on", active(b.dataset.cmd)));
    const w = bubble.offsetWidth, h = bubble.offsetHeight;
    let left = rect.left + rect.width / 2 - w / 2, top = rect.top - h - 8;
    // на телефоне системное меню выделения (Копировать/Вставить/BIU) висит над текстом – уходим под выделение
    if (TOUCH) top = rect.bottom + 14;
    if (top < 8) top = rect.bottom + 14;
    if (top + h > innerHeight - 8) top = Math.max(8, rect.top - h - 8);
    left = Math.max(8, Math.min(left, innerWidth - w - 8));
    bubble.style.left = left + "px"; bubble.style.top = top + "px";
  }
  document.addEventListener("selectionchange", () => requestAnimationFrame(updateBubble));
  window.addEventListener("scroll", () => requestAnimationFrame(updateBubble), true);
  window.addEventListener("resize", hideBubble);

  // ---------------- инициализация поля
  function init(el, platform) {
    editors.set(el, platform);
    el.contentEditable = "true";
    el.spellcheck = true;
    el.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); document.execCommand("insertLineBreak"); return; }
      if (!(e.ctrlKey || e.metaKey)) return;
      const k = KEYS[e.code];
      if (!k || k[0] !== e.shiftKey || e.altKey) return;
      if (!CMDS[platform].includes(k[1])) return;
      e.preventDefault(); exec(el, k[1]);
    });
    el.addEventListener("paste", (e) => {
      e.preventDefault();
      const rich = e.clipboardData.getData("text/html");
      const text = e.clipboardData.getData("text/plain") || "";
      let our = rich && ALLOWED[platform].size ? clipboardToOurHtml(rich, ALLOWED[platform]) : "";
      if (!our.trim()) our = escT(text);
      const html = VKEmoji.html(our).replace(/\r?\n/g, "<br>");
      document.execCommand("insertHTML", false, html);
    });
    el.addEventListener("input", (e) => { if (!e.isTrusted) return; emojifyTyped(el); });
    if (platform === "tg") {
      el.addEventListener("contextmenu", (e) => {
        const sel = getSelection();
        if (sel.isCollapsed || !el.contains(sel.anchorNode)) return;
        e.preventDefault(); showMenu(e.clientX, e.clientY, el);
      });
    }
    el.addEventListener("blur", () => setTimeout(() => { if (document.activeElement !== el) hideBubble(); }, 0));
  }

  return { init, get, getPlain, set, setPlain, plainLength, exec, ALLOWED };
})();
