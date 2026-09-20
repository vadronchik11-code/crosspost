/* Эмодзи рисуем картинками VK – теми же файлами, что показывает vk.com.
   Основной путь: https://vk.com/emoji/e/<utf8-hex>.png (+ _2x для ретины), FE0F из кода выбрасывается.
   Флаги и клавишные цифры там нет – они на старом пути https://vk.com/images/emoji/<utf16-hex>_2x.png.
   Если картинки нет нигде – показываем системный символ. */
const VKEmoji = (() => {
  let RE;
  try { RE = new RegExp("\\p{RGI_Emoji}|\\p{Extended_Pictographic}\\uFE0F?", "gv"); }
  catch {
    RE = /(?:\p{Regional_Indicator}{2}|[0-9#*]️?⃣|\p{Extended_Pictographic}(?:️|\p{Emoji_Modifier})?(?:‍\p{Extended_Pictographic}(?:️|\p{Emoji_Modifier})?)*)/gu;
  }
  const PRESENTATION = /^\p{Emoji_Presentation}/u;
  const enc = new TextEncoder();

  // символы вроде © ® ™ ‼ без FE0F VK оставляет текстом – мы тоже
  function isEmoji(s) {
    return PRESENTATION.test(s) || s.includes("️") || s.includes("‍") || s.includes("⃣") || /\p{Emoji_Modifier}/u.test(s);
  }
  function utf8hex(s) { return [...enc.encode(s.replace(/️/g, ""))].map(b => b.toString(16).padStart(2, "0")).join(""); }
  function utf16hex(s) { const t = s.replace(/️/g, ""); let out = ""; for (let i = 0; i < t.length; i++) out += t.charCodeAt(i).toString(16).toUpperCase().padStart(4, "0"); return out; }
  function urls(e) {
    const h = utf8hex(e);
    return { src: `https://vk.com/emoji/e/${h}.png`, src2x: `https://vk.com/emoji/e/${h}_2x.png`, old: `https://vk.com/images/emoji/${utf16hex(e)}_2x.png` };
  }
  const escA = (s) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  function imgHtml(e) {
    const u = urls(e);
    return `<img class="emoji" alt="${escA(e)}" draggable="false" src="${u.src}" srcset="${u.src2x} 2x" data-old="${u.old}" onerror="VKEmoji.fail(this)">`;
  }
  function imgNode(e) {
    const img = document.createElement("img");
    const u = urls(e);
    img.className = "emoji"; img.alt = e; img.draggable = false;
    img.src = u.src; img.srcset = `${u.src2x} 2x`; img.dataset.old = u.old;
    img.onerror = () => fail(img);
    return img;
  }
  // нет картинки на новом пути -> пробуем старый -> текст
  function fail(img) {
    if (img.dataset.old && img.src !== img.dataset.old) { img.removeAttribute("srcset"); img.src = img.dataset.old; img.dataset.old = ""; return; }
    img.replaceWith(document.createTextNode(img.alt || ""));
  }

  function test(s) { RE.lastIndex = 0; let m; while ((m = RE.exec(s))) { if (isEmoji(m[0])) return true; } return false; }

  // в строке HTML (текст уже экранирован) заменяем эмодзи на <img>
  function html(s) { return s.replace(RE, (m) => (isEmoji(m) ? imgHtml(m) : m)); }

  /* Текст -> фрагмент DOM (текстовые узлы + <img>). Если передан caret (смещение в исходном тексте),
     вернёт узел/смещение, куда поставить курсор после замены. */
  function fragment(text, caret = null) {
    const frag = document.createDocumentFragment();
    let pos = 0, caretNode = null, caretOffset = 0, m;
    RE.lastIndex = 0;
    const pushText = (t) => {
      if (!t) return;
      const tn = document.createTextNode(t);
      if (caret !== null && caret >= pos && caret <= pos + t.length) { caretNode = tn; caretOffset = caret - pos; }
      frag.appendChild(tn); pos += t.length;
    };
    let last = 0;
    while ((m = RE.exec(text))) {
      if (!isEmoji(m[0])) continue;
      pushText(text.slice(last, m.index));
      const img = imgNode(m[0]);
      frag.appendChild(img);
      if (caret !== null && caret > pos && caret <= pos + m[0].length) { caretNode = img; caretOffset = -1; }
      pos += m[0].length; last = m.index + m[0].length;
    }
    pushText(text.slice(last));
    return { frag, caretNode, caretOffset };
  }

  return { test, html, fragment, fail, RE };
})();
