/* AI Teaches Me · 渲染内核（《技术文档》§4.8 / §13.3；《项目文档》§8.3 内容块协议）
 *
 * 三条设计要点：
 * 1. 全部离线：只使用 app/web/vendor/ 下的本地库，任何远程地址一律拒绝（§1.5 红线 4）；
 * 2. 逐块降级：任一元素失败只影响该块，正文永远可读（§4.10 失败矩阵）；
 * 3. 换主题只改 CSS 变量，不重建 DOM（§4.8，保留滚动位置）。
 *
 * 教学区 / 追问抽屉 / 小灶三处共用这一份实现，只有容器宽度不同。
 */
(function () {
  "use strict";

  var MATH_TOKEN_PREFIX = "@@ATM_MATH_";
  var CALLOUT_ICONS = { hook: "⚡", tip: "💡", warn: "⚠️", question: "❓", fun: "🎈" };
  var CALLOUT_TITLES = { hook: "钩子", tip: "提示", warn: "注意", question: "想一想", fun: "有趣" };

  var LAZY_THRESHOLD = 60;
  var observer = null;

  var state = {
    root: null,
    meta: null,
    blocks: 0,
    tokens: 0,
    initialized: false,
    fallbackNotice: false,
    vendor: { markdownIt: false, katex: false, hljs: false },
    md: null,
    // 人设徽章（讲义顶部「谁在讲」）与划词标注（引用到追问）
    persona: null,
    annotations: [],
    // JS → Python 的事件队列：由 Python 侧的定时器 drainEvents() 取走
    events: []
  };

  function escapeHtml(text) {
    return String(text === null || text === undefined ? "" : text).replace(
      /[&<>"']/g,
      function (ch) {
        return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
      }
    );
  }

  function el(tag, className, html) {
    var node = document.createElement(tag);
    if (className) { node.className = className; }
    if (html !== null && html !== undefined) { node.innerHTML = html; }
    return node;
  }

  /* --- 本地依赖探测与降级 -------------------------------------------- */
  function detectVendor() {
    state.vendor = {
      markdownIt: typeof window.markdownit === "function",
      katex: typeof window.katex !== "undefined" && typeof window.katex.renderToString === "function",
      hljs: typeof window.hljs !== "undefined"
    };
    state.md = state.vendor.markdownIt
      ? window.markdownit({
          html: false,
          linkify: false,
          breaks: false,
          highlight: function (code, lang) {
            var language = String(lang || "").trim();
            var body = renderCodeHtml(code, language);
            var className = "hljs" + (language ? " language-" + language : "");
            return '<pre class="atm-code"><code class="' + escapeHtml(className) + '">' + body + "</code></pre>";
          }
        })
      : null;
    return state.vendor;
  }

  function vendorWarningElement() {
    var missing = [];
    if (!state.vendor.markdownIt) { missing.push("markdown-it"); }
    if (!state.vendor.katex) { missing.push("KaTeX"); }
    if (!state.vendor.hljs) { missing.push("highlight.js"); }
    if (!missing.length) { return null; }
    return el(
      "div",
      "atm-vendor-warning",
      "本地渲染依赖缺失：" + escapeHtml(missing.join(" / ")) +
        "（当前为降级渲染；把库放入 app/web/vendor/ 后即完整渲染）"
    );
  }

  /* --- Markdown（vendor 缺失时的最小兜底） --------------------------- */
  function inlineFallback(value) {
    return escapeHtml(value)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/\*([^*]+)\*/g, "<em>$1</em>");
  }

  function fallbackMarkdown(text) {
    var lines = String(text === null || text === undefined ? "" : text).split(/\r?\n/);
    var html = [];
    var listOpen = false;
    function closeList() { if (listOpen) { html.push("</ul>"); listOpen = false; } }
    lines.forEach(function (line) {
      var trimmed = line.trim();
      if (!trimmed) { closeList(); return; }
      var heading = trimmed.match(/^(#{1,6})\s+(.*)$/);
      if (heading) {
        closeList();
        var level = heading[1].length;
        html.push("<h" + level + ">" + inlineFallback(heading[2]) + "</h" + level + ">");
        return;
      }
      if (/^([-*]|\d+\.)\s+/.test(trimmed)) {
        if (!listOpen) { html.push("<ul>"); listOpen = true; }
        html.push("<li>" + inlineFallback(trimmed.replace(/^([-*]|\d+\.)\s+/, "")) + "</li>");
        return;
      }
      closeList();
      html.push("<p>" + inlineFallback(trimmed) + "</p>");
    });
    closeList();
    return html.join("\n");
  }

  function renderMarkdown(text) {
    if (state.md) {
      try { return state.md.render(String(text === null || text === undefined ? "" : text)); }
      catch (error) { /* 落到兜底实现 */ }
    }
    return fallbackMarkdown(text);
  }

  /* --- LaTeX：throwOnError=false 语义，失败降级为等宽原文（§11.3） --- */
  function renderMath(tex, displayMode) {
    if (state.vendor.katex) {
      try {
        return window.katex.renderToString(tex, {
          displayMode: !!displayMode,
          throwOnError: false,
          strict: "ignore"
        });
      } catch (error) { /* 降级 */ }
    }
    var raw = (displayMode ? "$$" : "$") + tex + (displayMode ? "$$" : "$");
    return '<code class="atm-math-fallback">' + escapeHtml(raw) + "</code>";
  }

  function renderRich(markdownText) {
    var source = String(markdownText === null || markdownText === undefined ? "" : markdownText);
    var placeholders = [];
    var withTokens = source.replace(
      /\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\$([^$\n]+?)\$|\\\(([\s\S]+?)\\\)/g,
      function (match, d1, d2, d3, d4) {
        var tex = d1 || d2 || d3 || d4 || "";
        var html = renderMath(tex, Boolean(d1 || d2));
        var token = MATH_TOKEN_PREFIX + placeholders.length + "@@";
        placeholders.push({ token: token, html: html });
        return token;
      }
    );
    var html = renderMarkdown(withTokens);
    placeholders.forEach(function (item) {
      html = html.split(item.token).join(item.html);
    });
    return html;
  }

  function renderCodeHtml(code, lang) {
    var source = String(code === null || code === undefined ? "" : code);
    if (state.vendor.hljs && lang) {
      try {
        if (!window.hljs.getLanguage || window.hljs.getLanguage(lang)) {
          return window.hljs.highlight(source, { language: lang }).value;
        }
      } catch (error) { /* 降级为纯文本 */ }
    }
    return escapeHtml(source);
  }

  /* --- 安全：本地路径与 SVG 清洗（Python 侧还会再清洗一次，§13.3/§13.4） --- */
  function safeLocalUrl(url) {
    if (!url) { return null; }
    var value = String(url).trim();
    if (/^(https?:)?\/\//i.test(value)) { return null; }
    if (/^(javascript|vbscript|data:text\/html):/i.test(value)) { return null; }
    return value;
  }

  function stripDangerousMarkup(svgText) {
    return String(svgText === null || svgText === undefined ? "" : svgText)
      .replace(/<\s*script[\s\S]*?<\s*\/\s*script\s*>/gi, "")
      .replace(/<\s*foreignObject[\s\S]*?<\s*\/\s*foreignObject\s*>/gi, "")
      .replace(/\son[a-z]+\s*=\s*"[^"]*"/gi, "")
      .replace(/\son[a-z]+\s*=\s*'[^']*'/gi, "");
  }

  /* --- 单个内容块 ---------------------------------------------------- */
  /* --- 虚拟化与懒挂载（README：>300 块时只挂可视区附近；重块滚动到才渲染） --- */
  function ensureObserver() {
    if (observer || typeof IntersectionObserver === "undefined") { return observer; }
    observer = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry) {
          if (!entry.isIntersecting) { return; }
          var node = entry.target;
          if (typeof node.atmMount === "function") {
            var mount = node.atmMount;
            delete node.atmMount;
            mount(node);
          }
          observer.unobserve(node);
        });
      },
      { rootMargin: "600px 0px" }
    );
    return observer;
  }

  function deferMount(node, mountFn) {
    node.atmMount = mountFn;
    var obs = ensureObserver();
    if (obs) { obs.observe(node); } else { mountFn(node); }
    return node;
  }

  function heavyBlockShouldDefer() {
    return state.blocks >= LAZY_THRESHOLD;
  }

  /* --- 图片灯箱（点击放大，只对本地图片） --- */
  function openLightbox(src, alt) {
    var overlay = document.createElement("div");
    overlay.className = "atm-lightbox";
    var image = document.createElement("img");
    image.setAttribute("src", src);
    image.setAttribute("alt", alt || "");
    overlay.appendChild(image);
    overlay.addEventListener("click", function () { overlay.remove(); });
    document.body.appendChild(overlay);
  }

  /* --- 图示灯箱（双击放大：SVG 克隆到浮层，按屏幕尽量放大，可滚动看整张图） --- */
  function openDiagramLightbox(svgMarkup, title) {
    var overlay = document.createElement("div");
    overlay.className = "atm-lightbox atm-lightbox-diagram";
    var host = document.createElement("div");
    host.className = "atm-lightbox-host";
    host.innerHTML = stripDangerousMarkup(svgMarkup || "");
    var svg = host.querySelector("svg");
    if (!svg) { return; }
    svg.removeAttribute("width");
    svg.removeAttribute("height");
    svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
    svg.style.width = "min(94vw, 1600px)";
    svg.style.height = "auto";
    svg.style.maxHeight = "84vh";
    svg.style.background = "#FFFFFF";
    svg.style.borderRadius = "12px";
    svg.style.padding = "14px";
    if (title) { host.appendChild(el("div", "atm-lightbox-caption", escapeHtml(String(title)))); }
    var hint = el("div", "atm-lightbox-caption", "点击任意处或按 Esc 关闭");
    host.appendChild(hint);
    overlay.appendChild(host);
    overlay.addEventListener("click", function () { overlay.remove(); });
    document.addEventListener("keydown", function onKey(event) {
      if (event.key === "Escape") { overlay.remove(); document.removeEventListener("keydown", onKey); }
    });
    document.body.appendChild(overlay);
  }
  function renderMeta(block) {
    state.meta = {
      lesson_id: block.lesson_id || null,
      depth: block.depth || null,
      version: block.version || null
    };
    var parts = [];
    if (block.lesson_id) { parts.push(String(block.lesson_id)); }
    if (block.depth) { parts.push("深度：" + String(block.depth)); }
    if (block.version) { parts.push("v" + String(block.version)); }
    return el("div", "atm-meta", escapeHtml(parts.join(" · ")));
  }

  function renderCallout(block) {
    var style = CALLOUT_TITLES[block.style] ? block.style : "tip";
    var node = el("section", "atm-block atm-callout");
    node.setAttribute("data-style", style);
    var title = el("div", "atm-callout-title", escapeHtml(CALLOUT_ICONS[style] + " " + CALLOUT_TITLES[style]));
    node.appendChild(title);
    node.appendChild(el("div", "atm-callout-body", renderRich(block.md || "")));
    return node;
  }

  function renderAnalogy(block) {
    var node = el("section", "atm-block atm-card");
    var head = escapeHtml(block.title || "类比");
    var route = "";
    if (block.from || block.to) { route = "（" + escapeHtml(block.from || "?") + " → " + escapeHtml(block.to || "?") + "）"; }
    node.appendChild(el("div", "atm-callout-title", "🔁 " + head + route));
    node.appendChild(el("div", null, renderRich(block.md || "")));
    return node;
  }

  function renderTable(block) {
    var node = el("section", "atm-block");
    var headers = Array.isArray(block.headers) ? block.headers : [];
    var rows = Array.isArray(block.rows) ? block.rows : [];
    var html = ['<table class="atm-table">'];
    if (headers.length) {
      html.push("<thead><tr>");
      headers.forEach(function (cell) { html.push("<th>" + escapeHtml(cell) + "</th>"); });
      html.push("</tr></thead>");
    }
    html.push("<tbody>");
    rows.forEach(function (row) {
      html.push("<tr>");
      (Array.isArray(row) ? row : [row]).forEach(function (cell) {
        html.push("<td>" + escapeHtml(cell) + "</td>");
      });
      html.push("</tr>");
    });
    html.push("</tbody></table>");
    node.innerHTML = html.join("");
    return node;
  }

  function renderCode(block) {
    var node = el("section", "atm-block");
    if (block.lang) { node.appendChild(el("div", "atm-lang", escapeHtml(block.lang))); }
    var pre = el("pre", "atm-code");
    var code = el("code", "hljs");
    code.innerHTML = renderCodeHtml(block.code, block.lang);
    pre.appendChild(code);
    node.appendChild(pre);
    if (block.runnable) {
      node.appendChild(el("div", "atm-placeholder", "（可本地试跑：M5 提供受控子进程入口）"));
    }
    return node;
  }

  function renderQuiz(block) {
    var node = el("section", "atm-block atm-card");
    node.appendChild(el("div", "atm-callout-title", "✍️ 小测验"));
    node.appendChild(el("div", null, renderRich(block.question || "")));
    var options = Array.isArray(block.options) ? block.options : [];
    options.forEach(function (option, index) {
      var label = el("label", "atm-quiz-option");
      label.innerHTML =
        '<input type="radio" disabled /> ' +
        escapeHtml(String.fromCharCode(65 + index) + ". " + option);
      node.appendChild(label);
    });
    var details = el("details", null);
    var answerIndex = typeof block.answer === "number" ? block.answer : -1;
    var answerText = answerIndex >= 0 && options[answerIndex] !== undefined ? options[answerIndex] : "—";
    details.innerHTML =
      "<summary>查看答案与解析</summary><div>" +
      escapeHtml("答案：" + answerText) + "<br />" + renderRich(block.explain || "") +
      "</div>";
    node.appendChild(details);
    return node;
  }

  function renderSummary(block) {
    var node = el("section", "atm-block atm-card atm-summary");
    node.appendChild(el("div", "atm-callout-title", "🧾 本节小结"));
    var points = Array.isArray(block.points) ? block.points : [];
    var list = "<ul>";
    points.forEach(function (point) { list += "<li>" + escapeHtml(point) + "</li>"; });
    list += "</ul>";
    node.appendChild(el("div", null, list));
    return node;
  }

  function renderNextPreview(block) {
    var node = el("section", "atm-block atm-card");
    node.appendChild(el("div", "atm-callout-title", "➡️ 下节预告"));
    node.appendChild(el("div", null, renderRich(block.md || "")));
    return node;
  }

  function renderRef(block) {
    var node = el("section", "atm-block atm-ref");
    var label = [];
    if (block.material_id) { label.push("资料 " + String(block.material_id)); }
    if (block.page !== undefined && block.page !== null) { label.push("第 " + String(block.page) + " 页"); }
    if (block.url) { label.push(String(block.url)); }
    var inner = label.length ? "<strong>" + escapeHtml(label.join(" · ")) + "</strong><br />" : "";
    inner += escapeHtml(block.quote || block.excerpt || "");
    node.innerHTML = inner;
    return node;
  }

  function renderImage(block) {
    var node = el("section", "atm-block");
    var source = safeLocalUrl(block.path || block.src);
    if (!source) {
      node.appendChild(el("div", "atm-error", "图片来源被拒绝：只允许本地受控路径（§13.3）。"));
      return node;
    }
    node.setAttribute("data-asset", source);
    node.setAttribute("data-caption", String(block.caption || ""));
    var img = document.createElement("img");
    img.setAttribute("src", source);
    img.setAttribute("loading", "lazy");
    img.className = "atm-image";
    img.alt = block.caption || "资料图片";
    img.addEventListener("click", function () { openLightbox(source, block.caption || ""); });
    node.appendChild(img);
    if (block.caption) { node.appendChild(el("div", "atm-placeholder", escapeHtml(block.caption))); }
    return node;
  }

  function renderSvg(block) {
    var node = el("section", "atm-block atm-svg");
    if (block.title) { node.setAttribute("aria-label", String(block.title)); }
    if (block.caption) { node.appendChild(el("div", "atm-placeholder", escapeHtml(block.caption))); }
    var mount = function (target) {
      target.classList.remove("atm-deferred");
      var host = document.createElement("div");
      host.innerHTML = stripDangerousMarkup(block.svg || "");
      target.appendChild(host);
      var svgNode = host.querySelector("svg");
      if (svgNode) {
        target.style.cursor = "zoom-in";
        target.title = "双击看整张图";
        target.addEventListener("dblclick", function () {
          openDiagramLightbox(svgNode.outerHTML, block.title || block.caption || "");
        });
      }
    };
    if (block.svg && heavyBlockShouldDefer()) {
      node.classList.add("atm-deferred");
      return deferMount(node, mount);
    }
    mount(node);
    return node;
  }

  function renderDiagram(block) {
    var kind = block.kind || "svg";
    if (kind === "svg") { return renderSvg({ svg: block.content, title: block.title }); }
    var node = el("section", "atm-block");
    node.appendChild(el("div", "atm-lang", escapeHtml(String(kind))));
    node.appendChild(el("div", "atm-placeholder", "（本地离线渲染暂不支持 " + escapeHtml(String(kind)) + "，先展示源码）"));
    var pre = el("pre", "atm-code");
    var code = el("code");
    code.innerHTML = escapeHtml(block.content || "");
    pre.appendChild(code);
    node.appendChild(pre);
    return node;
  }

  function renderAnimation(block) {
    var node = el("section", "atm-block atm-animation");
    if (block.title) { node.appendChild(el("div", "atm-lang", escapeHtml(block.title))); }
    var mount = function (target) {
      target.classList.remove("atm-deferred");
      var frame = document.createElement("iframe");
      frame.setAttribute("sandbox", "allow-scripts");
      frame.setAttribute("referrerpolicy", "no-referrer");
      frame.setAttribute("srcdoc", String(block.html || ""));
      frame.style.height = String(block.height || 320) + "px";
      target.appendChild(frame);
    };
    if (block.html && heavyBlockShouldDefer()) {
      node.classList.add("atm-deferred");
      node.appendChild(el("div", "atm-placeholder", "（动画会在滚动到可视区时挂载）"));
      return deferMount(node, mount);
    }
    mount(node);
    if (block.fallback_md) {
      var fallback = el("div", "atm-placeholder", escapeHtml(block.fallback_md));
      fallback.style.display = "none";
      node.appendChild(fallback);
    }
    return node;
  }

  function renderError(block) {
    var node = el("section", "atm-block atm-error");
    node.innerHTML = escapeHtml(block.message || "生成过程中出现问题");
    if (block.recoverable) {
      node.appendChild(el("div", "atm-placeholder", "该块可重试；其余内容不受影响。"));
    }
    return node;
  }

  function renderDone(block) {
    state.tokens = typeof block.tokens === "number" ? block.tokens : 0;
    var node = el("div", "atm-done");
    node.style.display = "none";
    node.setAttribute("data-tokens", String(state.tokens));
    return node;
  }

  function renderUnknown(block) {
    var details = el("details", "atm-block atm-raw");
    details.innerHTML =
      "<summary>未知内容块（已折叠，原始 JSON）</summary><pre>" +
      escapeHtml(JSON.stringify(block, null, 2)) + "</pre>";
    return details;
  }

  function buildBlockElement(type, block) {
    switch (type) {
      case "meta": return renderMeta(block);
      case "text": return el("section", "atm-block atm-text", renderRich(block.md || ""));
      case "callout": return renderCallout(block);
      case "analogy": return renderAnalogy(block);
      case "table": return renderTable(block);
      case "code": return renderCode(block);
      case "quiz": return renderQuiz(block);
      case "summary": return renderSummary(block);
      case "next_preview": return renderNextPreview(block);
      case "ref": return renderRef(block);
      case "image": return renderImage(block);
      case "svg": return renderSvg(block);
      case "diagram": return renderDiagram(block);
      case "animation": return renderAnimation(block);
      case "error": return renderError(block);
      case "done": return renderDone(block);
      default: return renderUnknown(block);
    }
  }

  function blockElement(block) {
    var type = block && block.type ? String(block.type) : "error";
    var node = buildBlockElement(type, block);
    // 记录块类型：划词标注时随事件一起回传，方便定位「这句出自哪种块」
    if (node && node.setAttribute && type !== "done") {
      node.setAttribute("data-block-type", type);
      if (CITABLE_TYPES[type]) { attachCiteButton(node, block || {}); }
    }
    return node;
  }

  /* --- 对外 API（也是 QWebChannel 桥的接收侧，§4.8） ------------------ */
  function appendBlock(block) {
    if (!state.root) { return null; }
    var node = blockElement(block || {});
    state.root.appendChild(node);
    if (!block || block.type !== "done") { state.blocks += 1; }
    return node;
  }

  function appendBlocks(blocks) {
    (Array.isArray(blocks) ? blocks : []).forEach(appendBlock);
    return state.blocks;
  }

  function reset() {
    if (state.root) { state.root.innerHTML = ""; }
    state.meta = null;
    state.blocks = 0;
    state.tokens = 0;
    state.fallbackNotice = false;
    if (state.root) {
      var warning = vendorWarningElement();
      if (warning) { state.root.appendChild(warning); }
      renderPersonaBadge();
    }
    return state.blocks;
  }

  function renderBlocks(blocks) {
    reset();
    var count = appendBlocks(blocks);
    applyAnnotations();
    return count;
  }

  function renderFallbackMarkdown(text, note) {
    if (!state.root) { return false; }
    if (!state.fallbackNotice) {
      state.root.appendChild(
        el("div", "atm-error", note || "输出协议错乱，已按整段 Markdown 渲染（已生成的块仍保留在下方）。")
      );
      state.fallbackNotice = true;
    }
    state.root.appendChild(el("section", "atm-block atm-text", renderRich(text || "")));
    return true;
  }

  function toggleCodeTheme(mode) {
    var light = document.getElementById("atm-hljs-light");
    var dark = document.getElementById("atm-hljs-dark");
    var useDark = String(mode || "").toLowerCase() === "dark";
    if (light) { light.disabled = useDark; }
    if (dark) { dark.disabled = !useDark; }
  }

  function setTheme(cssVars) {
    var rootStyle = document.documentElement.style;
    if (typeof cssVars === "string") {
      cssVars.split(";").forEach(function (pair) {
        var index = pair.indexOf(":");
        if (index > 0) {
          var name = pair.slice(0, index).trim();
          var value = pair.slice(index + 1).trim();
          if (name) { rootStyle.setProperty(name, value); }
        }
      });
    } else if (cssVars && typeof cssVars === "object") {
      Object.keys(cssVars).forEach(function (name) {
        rootStyle.setProperty(name, String(cssVars[name]));
      });
      if (cssVars["--atm-theme"]) { document.documentElement.setAttribute("data-theme", String(cssVars["--atm-theme"])); }
      if (cssVars["--atm-mode"]) { document.documentElement.setAttribute("data-mode", String(cssVars["--atm-mode"])); }
      toggleCodeTheme(cssVars["--atm-mode"]);
    }
    return getState();
  }

  function getState() {
    return {
      blocks: state.blocks,
      tokens: state.tokens,
      meta: state.meta,
      vendor: state.vendor,
      theme: document.documentElement.getAttribute("data-theme"),
      mode: document.documentElement.getAttribute("data-mode"),
      persona: state.persona ? state.persona.name : null,
      annotations: state.annotations.length,
      textLength: state.root ? state.root.textContent.length : 0  // textContent：不受 content-visibility 影响
    };
  }

  function init(options) {
    var config = options || {};
    if (!state.root) {
      state.root = config.root || document.getElementById("atm-root");
    }
    if (!state.initialized) {
      detectVendor();
      state.initialized = true;
      var warning = vendorWarningElement();
      if (warning && state.root) { state.root.appendChild(warning); }
      // 划词工具条：只在讲义（本页）里启用；滚动 / Esc 时收起
      document.addEventListener("mouseup", onDocumentMouseUp);
      document.addEventListener("click", onDocumentClick);
      document.addEventListener("mouseover", onDocumentMouseOver);
      document.addEventListener("mouseout", onDocumentMouseOut);
      document.addEventListener("scroll", function () { hideAnnotBar(); hideAnnotationBubble(); }, true);
      document.addEventListener("keydown", function (event) {
        if (event.key === "Escape") { hideAnnotBar(); hideAnnotationBubble(); }
      });
      window.addEventListener("resize", function () { hideAnnotBar(); hideAnnotationBubble(); });
    }
    if (config.persona) { setPersona(config.persona); }
    if (config.annotations) { setAnnotations(config.annotations); }
    if (config.blocks) { appendBlocks(config.blocks); }
    if (config.theme) { setTheme(config.theme); }
    return getState();
  }

  /* --- 逐字实时预览（M21）：正在生成的那一行，边到达边显示 --- */
  var liveNode = null;

  function streamStart() {
    streamEnd();
    if (!state.root) { return false; }
    liveNode = el("section", "atm-block atm-text atm-live");
    state.root.appendChild(liveNode);
    return true;
  }

  function streamUpdate(markdown) {
    if (!liveNode) { streamStart(); }
    if (!liveNode) { return false; }
    liveNode.innerHTML = renderRich(markdown || "");
    if (state.root && state.root.scrollHeight - state.root.scrollTop - state.root.clientHeight < 260) {
      state.root.scrollTop = state.root.scrollHeight;
    }
    return true;
  }

  function streamEnd() {
    if (liveNode) { liveNode.remove(); }
    liveNode = null;
    return true;
  }

  /* --- 人设徽章（人设必须在讲义里「看得见」，§4.14 / 新增 FR） ---------- */
  function renderPersonaBadge() {
    if (!state.root) { return null; }
    var old = state.root.querySelector(".atm-persona");
    if (old) { old.remove(); }
    var persona = state.persona;
    if (!persona || !persona.name) { return null; }
    var node = el("div", "atm-persona");
    node.setAttribute("data-persona", String(persona.id || ""));
    node.appendChild(el("span", "atm-persona-avatar", escapeHtml(String(persona.avatar || "🎓"))));
    var body = el("span", "atm-persona-body");
    body.appendChild(el("span", "atm-persona-name", escapeHtml(String(persona.name))));
    var tone = Array.isArray(persona.tone) ? persona.tone.filter(Boolean).join(" · ") : String(persona.tone || "");
    var suffix = tone ? "正在讲解 · 语气：" + tone : "正在讲解本节";
    body.appendChild(el("span", "atm-persona-note", escapeHtml(suffix)));
    node.appendChild(body);
    var hint = el("span", "atm-persona-hint", "选中任意文字可标注或追问");
    node.appendChild(hint);
    state.root.insertBefore(node, state.root.firstChild);
    return node;
  }

  function setPersona(persona) {
    state.persona = persona && persona.name ? persona : null;
    renderPersonaBadge();
    return state.persona;
  }

  function getPersona() {
    return state.persona;
  }

  /* --- 划词标注（选中 → 标注 / 追问） --------------------------------- */
  var MAX_SELECTION = 600;
  var annotBar = null;
  // 这几类块「有什么可以被引用」很清楚，直接给一个悬停按钮（图片 / 图示）
  var CITABLE_TYPES = { image: 1, svg: 1, diagram: 1 };

  function citeLabelFor(block, node) {
    var type = String((block && block.type) || "");
    var caption = String((block && (block.caption || block.title)) || "").trim();
    if (type === "image") {
      var asset = String((block && (block.path || block.src)) || (node ? node.getAttribute("data-asset") : "") || "");
      return "图片" + (caption ? "：" + caption : "") + (asset ? "（文件：" + asset + "）" : "");
    }
    return "图示" + (caption ? "：" + caption : "");
  }

  function attachCiteButton(node, block) {
    if (!node) { return node; }
    node.setAttribute("data-citable", "1");
    var button = el("button", "atm-cite-button", "💬 引用到追问");
    button.type = "button";
    button.title = "把这张图引用到右侧追问";
    button.addEventListener("mousedown", function (event) { event.preventDefault(); });
    button.addEventListener("click", function (event) {
      event.preventDefault();
      event.stopPropagation();
      pushEvent({
        action: "cite_block",
        quote: citeLabelFor(block, node),
        block_type: String((block && block.type) || ""),
        block_index: blockIndexOf(node),
        asset: String((block && (block.path || block.src)) || node.getAttribute("data-asset") || "")
      });
    });
    node.appendChild(button);
    return node;
  }

  function pushEvent(payload) {
    if (!payload || !payload.action) { return false; }
    state.events.push(payload);
    return true;
  }

  function drainEvents() {
    // 踩过的坑：QtWebEngine 只把基本类型回传 Python，返回 JS 数组会得到空字符串，
    // 事件会被静默丢掉（表现为「点了标注 / 追问没反应」）。所以这里统一返回 JSON 字符串。
    if (!state.events.length) { return "[]"; }
    var pending = state.events;
    state.events = [];
    return JSON.stringify(pending);
  }

  function clearSelection() {
    var selection = window.getSelection ? window.getSelection() : null;
    if (selection && selection.removeAllRanges) { selection.removeAllRanges(); }
  }

  function hideAnnotBar() {
    if (annotBar) { annotBar.remove(); annotBar = null; }
  }

  function blockIndexOf(node) {
    if (!node || !state.root) { return null; }
    var block = node.closest ? node.closest(".atm-block") : null;
    if (!block) { return null; }
    var all = state.root.querySelectorAll(".atm-block");
    for (var index = 0; index < all.length; index += 1) {
      if (all[index] === block) { return index; }
    }
    return null;
  }

  function currentSelectionInfo() {
    var selection = window.getSelection ? window.getSelection() : null;
    if (!selection || selection.isCollapsed || !selection.rangeCount) { return null; }
    var text = String(selection.toString() || "").replace(/\s+/g, " ").trim();
    if (!text) { return null; }
    var range = selection.getRangeAt(0);
    var start = range.startContainer;
    var node = start && start.nodeType === 1 ? start : (start ? start.parentElement : null);
    if (!node || !state.root || !state.root.contains(node)) { return null; }
    var block = node.closest ? node.closest(".atm-block") : null;
    if (!block) { return null; }
    var rect = typeof range.getBoundingClientRect === "function" ? range.getBoundingClientRect() : null;
    return {
      quote: text.slice(0, MAX_SELECTION),
      node: node,
      block: block,
      blockIndex: blockIndexOf(node),
      blockType: String(block.getAttribute("data-block-type") || ""),
      rect: rect
    };
  }

  function actionButton(label, titleText, onClick) {
    var button = document.createElement("button");
    button.type = "button";
    button.className = "atm-annotbar-button";
    button.textContent = label;
    button.title = titleText;
    button.addEventListener("mousedown", function (event) { event.preventDefault(); });
    button.addEventListener("click", function (event) {
      event.preventDefault();
      event.stopPropagation();
      onClick();
    });
    return button;
  }

  function showAnnotBar(info) {
    hideAnnotBar();
    var bar = el("div", "atm-annotbar");
    bar.appendChild(
      actionButton("✏️ 标注", "把这段原文记为本节标注", function () {
        pushEvent({
          action: "annotate",
          quote: info.quote,
          block_index: info.blockIndex,
          block_type: info.blockType
        });
        hideAnnotBar();
        clearSelection();
      })
    );
    bar.appendChild(
      actionButton("💬 追问", "直接把这段原文引用到右侧追问", function () {
        pushEvent({ action: "ask_quote", quote: info.quote });
        hideAnnotBar();
        clearSelection();
      })
    );
    document.body.appendChild(bar);
    // 用文档坐标（absolute 定位）：滚动 / 打印导出都不会跑位，屏幕上就在选区旁边
    var scrollX = window.pageXOffset || 0;
    var scrollY = window.pageYOffset || 0;
    var rect = info.rect;
    var left = rect ? rect.left + scrollX + rect.width / 2 - bar.offsetWidth / 2 : 24;
    var top = rect ? rect.top + scrollY - bar.offsetHeight - 10 : scrollY + 24;
    var maxLeft = Math.max(8, document.documentElement.clientWidth - bar.offsetWidth - 8 + scrollX);
    var maxTop = Math.max(8 + scrollY, document.documentElement.clientHeight - bar.offsetHeight - 8 + scrollY);
    bar.style.left = Math.max(8, Math.min(left, maxLeft)) + "px";
    bar.style.top = Math.max(8 + scrollY, Math.min(top, maxTop)) + "px";
    annotBar = bar;
    return bar;
  }

  function onDocumentMouseUp() {
    window.setTimeout(function () {
      var info = currentSelectionInfo();
      if (!info) { hideAnnotBar(); return; }
      showAnnotBar(info);
    }, 10);
  }

  /* --- 点已标注的划线 → 让主线程打开「写评论 / 引用到追问」 ----------------- */
  function onDocumentClick(event) {
    var target = event.target;
    var mark = target && target.closest ? target.closest("mark.atm-annotation") : null;
    if (!mark || !state.root || !state.root.contains(mark)) { return; }
    pushEvent({
      action: "open_annotation",
      annotation_id: String(mark.getAttribute("data-annotation-id") || "")
    });
  }

  /* --- 鼠标停在划线上 → 浮出气泡：评论 / 引用到追问 ----------------------- */
  var annotBubble = null;
  var annotBubbleTimer = null;

  function annotationById(annotationId) {
    var wanted = String(annotationId || "");
    for (var index = 0; index < state.annotations.length; index += 1) {
      if (String(state.annotations[index].id || "") === wanted) { return state.annotations[index]; }
    }
    return null;
  }

  function hideAnnotationBubble() {
    if (annotBubbleTimer) { window.clearTimeout(annotBubbleTimer); annotBubbleTimer = null; }
    if (annotBubble) { annotBubble.remove(); annotBubble = null; }
  }

  function showAnnotationBubble(mark) {
    hideAnnotationBubble();
    var annotationId = String(mark.getAttribute("data-annotation-id") || "");
    var annotation = annotationById(annotationId) || { quote: String(mark.textContent || ""), note: "" };
    var bubble = el("div", "atm-annotbubble");
    var note = String(annotation.note || "").trim();
    bubble.appendChild(el("div", "atm-annotbubble-note", escapeHtml(note || "（这条划线还没有评论）")));

    var row = el("div", "atm-annotbubble-row");
    var commentButton = actionButton("💬 评论", "给这条划线写 / 改评论", function () {
        pushEvent({ action: "comment_annotation", annotation_id: annotationId });
        hideAnnotationBubble();
      });
    commentButton.classList.add("atm-annotbubble-button");
    row.appendChild(commentButton);
    var citeButton = actionButton("🔗 引用到追问", "把这句话（连评论一起）引用到右侧追问", function () {
        pushEvent({ action: "cite_annotation", annotation_id: annotationId });
        hideAnnotationBubble();
      });
    citeButton.classList.add("atm-annotbubble-button");
    row.appendChild(citeButton);
    bubble.appendChild(row);
    bubble.addEventListener("mouseenter", function () {
      if (annotBubbleTimer) { window.clearTimeout(annotBubbleTimer); annotBubbleTimer = null; }
    });
    bubble.addEventListener("mouseleave", hideAnnotationBubble);
    document.body.appendChild(bubble);

    var scrollX = window.pageXOffset || 0;
    var scrollY = window.pageYOffset || 0;
    var rect = mark.getBoundingClientRect();
    // 兜底：万一块还在 content-visibility 的「跳过」状态，rect 会不可信 → 至少把气泡夹在可视区里
    var usable = rect && rect.width > 0 && rect.height <= 220;
    var maxLeft = Math.max(8, document.documentElement.clientWidth - bubble.offsetWidth - 8 + scrollX);
    var maxTop = Math.max(8 + scrollY, document.documentElement.clientHeight - bubble.offsetHeight - 8 + scrollY);
    var wantedLeft = usable ? rect.left + rect.width / 2 - bubble.offsetWidth / 2 : scrollX + 24;
    var wantedTop = usable ? rect.bottom + scrollY + 8 : scrollY + 24;
    bubble.style.left = Math.max(8, Math.min(wantedLeft, maxLeft)) + "px";
    bubble.style.top = Math.max(8 + scrollY, Math.min(wantedTop, maxTop)) + "px";
    annotBubble = bubble;
    return bubble;
  }

  function onDocumentMouseOver(event) {
    var target = event.target;
    var mark = target && target.closest ? target.closest("mark.atm-annotation") : null;
    if (!mark || !state.root || !state.root.contains(mark)) { return; }
    if (annotBubble && event.relatedTarget && annotBubble.contains(event.relatedTarget)) { return; }
    showAnnotationBubble(mark);
  }

  function onDocumentMouseOut(event) {
    var target = event.target;
    var mark = target && target.closest ? target.closest("mark.atm-annotation") : null;
    if (!mark) { return; }
    var to = event.relatedTarget;
    if (to && (mark.contains(to) || (annotBubble && annotBubble.contains(to)))) { return; }
    annotBubbleTimer = window.setTimeout(hideAnnotationBubble, 240);
  }

  /* --- 标注高亮：把已标注的原文片段在讲义里标出来 --------------------- */
  function clearAnnotationMarks() {
    if (!state.root) { return 0; }
    var marks = state.root.querySelectorAll("mark.atm-annotation");
    var count = marks.length;
    Array.prototype.forEach.call(marks, function (mark) {
      var parent = mark.parentNode;
      if (!parent) { return; }
      while (mark.firstChild) { parent.insertBefore(mark.firstChild, mark); }
      parent.removeChild(mark);
      if (parent.normalize) { parent.normalize(); }
    });
    return count;
  }

  // 只取「看得见」的文字：跳过已有标注、KaTeX 的无障碍 MathML（里面是 LaTeX 原文，
  // 屏幕上看不到，会污染匹配）、以及脚本/样式。真实的坑：不跳过这些，
  // 带公式或 Markdown 的句子就会「划不上线」。
  var TEXT_SKIP_SELECTOR = "mark.atm-annotation, .katex-mathml, annotation, script, style";

  function visibleTextNodes(rootEl) {
    if (!rootEl) { return []; }
    var nodes = [];
    var walker = document.createTreeWalker(rootEl, NodeFilter.SHOW_TEXT, {
      acceptNode: function (node) {
        if (!node.nodeValue || !node.nodeValue.length) { return NodeFilter.FILTER_REJECT; }
        var parent = node.parentElement;
        if (parent && parent.closest(TEXT_SKIP_SELECTOR)) { return NodeFilter.FILTER_REJECT; }
        return NodeFilter.FILTER_ACCEPT;
      }
    });
    var current = walker.nextNode();
    while (current) {
      nodes.push(current);
      current = walker.nextNode();
    }
    return nodes;
  }

  // 把可见文字拼成「空白统一成一个空格」的版本，并保留 → 原始字符下标的映射。
  // 带 Markdown 的段落里，模型写的换行 / 缩进在 DOM 里是 \n 与连续空格，
  // 而选中的文字是折叠过空白的，直接用 indexOf 会找不到（这就是「划不上线」的真凶）。
  function normalizeTextMap(nodes) {
    var chars = [];
    nodes.forEach(function (node, nodeIndex) {
      var value = node.nodeValue || "";
      for (var offset = 0; offset < value.length; offset += 1) {
        chars.push({ nodeIndex: nodeIndex, offset: offset, ch: value.charAt(offset) });
      }
    });
    var text = "";
    var map = [];
    var pendingSpace = -1;
    for (var index = 0; index < chars.length; index += 1) {
      if (/\s/.test(chars[index].ch)) {
        if (pendingSpace < 0 && text.length > 0) { pendingSpace = index; }
        continue;
      }
      if (pendingSpace >= 0) { text += " "; map.push(pendingSpace); pendingSpace = -1; }
      text += chars[index].ch;
      map.push(index);
    }
    return { text: text, map: map, chars: chars };
  }

  function wrapCharRange(prepared, nodes, startChar, endChar, annotation) {
    var pieces = [];
    var current = null;
    for (var index = startChar; index <= endChar; index += 1) {
      var info = prepared.chars[index];
      if (!info) { continue; }
      if (current && current.nodeIndex === info.nodeIndex && current.to === info.offset) {
        current.to = info.offset + 1;
      } else {
        current = { nodeIndex: info.nodeIndex, from: info.offset, to: info.offset + 1 };
        pieces.push(current);
      }
    }
    if (!pieces.length) { return false; }
    // 一句话可能被拆成多段（加粗 / 行内代码 / 公式各占一个节点）：💬 只在最后一段显示，
    // 否则每个词后面都挂一个气泡，看着像乱码。
    var lastIndex = pieces.length - 1;
    pieces.forEach(function (piece, index) { piece.isLast = index === lastIndex; });
    // 从后往前包：先改后面的节点，前面节点的偏移量仍然有效
    pieces.reverse().forEach(function (piece) {
      var node = nodes[piece.nodeIndex];
      if (!node || !node.parentNode) { return; }
      var range = document.createRange();
      try {
        range.setStart(node, piece.from);
        range.setEnd(node, piece.to);
        var mark = document.createElement("mark");
        mark.className = "atm-annotation atm-annotation-" + String(annotation.color || "yellow");
        mark.setAttribute("data-annotation-id", String(annotation.id || ""));
        if (annotation.note && piece.isLast) {
          mark.setAttribute("title", String(annotation.note) + "（点击可改评论 / 引用到追问）");
          mark.setAttribute("data-has-note", "1");
        } else {
          mark.setAttribute("title", "点击写评论 / 引用到追问");
        }
        range.surroundContents(mark);
      } catch (error) { /* 跨元素等特殊情况：跳过这一片，不影响阅读 */ }
    });
    return true;
  }

  function applyAnnotations() {
    clearAnnotationMarks();
    if (!state.root || !state.annotations.length) { return 0; }
    // 一次性在「没有标注」的干净 DOM 上算出所有匹配范围，再统一从后往前包。
    // 这样后一条标注即使落在前一条的范围里也能匹配上（逐条边找边包会导致后一条找不到）。
    var nodes = visibleTextNodes(state.root);
    if (!nodes.length) { return 0; }
    var prepared = normalizeTextMap(nodes);
    var matches = [];
    state.annotations.forEach(function (annotation) {
      var wanted = String((annotation && annotation.quote) || "").replace(/\s+/g, " ").trim();
      if (!wanted) { return; }
      var start = prepared.text.indexOf(wanted);
      var length = wanted.length;
      if (start < 0) {
        // 兜底：整句对不上（跨段落、末尾被截断）时，退一步只划前半句，总比什么都不显示好
        var shorter = wanted.slice(0, Math.max(8, Math.floor(wanted.length * 0.6)));
        start = prepared.text.indexOf(shorter);
        length = shorter.length;
        if (start < 0) { return; }
      }
      var startChar = prepared.map[start];
      var endChar = prepared.map[start + length - 1];
      if (startChar === undefined || endChar === undefined) { return; }
      matches.push({ annotation: annotation, startChar: startChar, endChar: endChar });
    });
    // 从后往前包：先改后面的，前面范围的偏移量依然有效
    matches.sort(function (left, right) { return right.startChar - left.startChar; });
    var applied = 0;
    matches.forEach(function (match) {
      if (wrapCharRange(prepared, nodes, match.startChar, match.endChar, match.annotation)) {
        applied += 1;
      }
    });
    return applied;
  }

  function setAnnotations(items) {
    state.annotations = Array.isArray(items) ? items.filter(function (item) { return item && item.quote; }) : [];
    return applyAnnotations();
  }

  function getAnnotations() {
    return state.annotations.slice();
  }

  window.ATM = {
    init: init,
    appendBlock: appendBlock,
    streamStart: streamStart,
    streamUpdate: streamUpdate,
    streamEnd: streamEnd,
    appendBlocks: appendBlocks,
    renderBlocks: renderBlocks,
    reset: reset,
    renderFallbackMarkdown: renderFallbackMarkdown,
    setTheme: setTheme,
    getState: getState,
    // 人设 / 标注 / JS → Python 事件
    setPersona: setPersona,
    getPersona: getPersona,
    setAnnotations: setAnnotations,
    getAnnotations: getAnnotations,
    drainEvents: drainEvents,
    pushEvent: pushEvent
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { init(); });
  } else {
    init();
  }
})();
