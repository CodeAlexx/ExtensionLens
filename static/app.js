/* ExtensionLens — app.js */
(function () {
  "use strict";

  const $ = (s) => document.querySelector(s);
  const pickerView = $("#picker-view"), viewerView = $("#viewer-view");
  const extGrid = $("#extension-grid"), filterInput = $("#picker-search");
  const uploadZone = $("#upload-zone"), uploadInput = $("#upload-input");
  const backBtn = $("#back-btn"), extName = $("#ext-name");
  const extVersion = $("#ext-version"), extProfile = $("#ext-profile");
  const fileTree = $("#file-tree"), searchInput = $("#global-search");
  const searchRegex = $("#search-regex"), searchCase = $("#search-case");
  const searchBtn = $("#search-btn");
  const codeWelcome = $("#code-welcome"), searchResults = $("#search-results");
  const codeViewer = $("#code-viewer"), codeFilePath = $("#code-file-path");
  const codeFileSize = $("#code-file-size"), codePre = $("#code-pre");
  const codeBlock = $("#code-block"), splitter = $("#splitter");
  const treePanel = $("#file-tree-panel");

  let allExtensions = [], currentExt = null, selectedNode = null;

  // -- Helpers --
  function fmtSize(bytes) {
    if (bytes == null) return "";
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1048576) return (bytes / 1024).toFixed(1) + " KB";
    return (bytes / 1048576).toFixed(1) + " MB";
  }
  function langFromExt(name) {
    const ext = (name.match(/\.([^.]+)$/) || [])[1];
    const map = {
      js: "javascript", mjs: "javascript", jsx: "javascript",
      ts: "javascript", tsx: "javascript",
      css: "css", scss: "css", less: "css",
      html: "markup", htm: "markup", svg: "markup", xml: "markup",
      json: "json", py: "python", sh: "bash", bash: "bash",
    };
    return map[(ext || "").toLowerCase()] || null;
  }
  function isImage(name) { return /\.(png|jpe?g|gif|svg|webp|ico|bmp)$/i.test(name); }
  function isBinary(text) {
    for (let i = 0, n = Math.min(text.length, 512); i < n; i++)
      if (text.charCodeAt(i) === 0) return true;
    return false;
  }
  function fileIcon(name, isDir) {
    if (isDir) return "\u{1F4C1}";
    if (/\.(js|ts|mjs|jsx|tsx)$/i.test(name)) return "\u{1F4C4}";
    if (/\.css$/i.test(name)) return "\u{1F3A8}";
    if (/\.html?$/i.test(name)) return "\u{1F4DD}";
    if (/\.json$/i.test(name)) return "\u{1F4CB}";
    if (isImage(name)) return "\u{1F5BC}\uFE0F";
    return "\u{1F4E6}";
  }
  function esc(s) { return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
  function showError(msg) {
    codeWelcome.hidden = true; searchResults.hidden = true; codeViewer.hidden = false;
    codeFilePath.textContent = "Error"; codeFileSize.textContent = "";
    codeBlock.className = ""; codeBlock.textContent = msg;
  }
  function hexDump(text) {
    const bytes = new TextEncoder().encode(text.slice(0, 1024));
    const lines = [];
    for (let off = 0; off < bytes.length; off += 16) {
      const chunk = bytes.slice(off, off + 16);
      const hex = Array.from(chunk).map(b => b.toString(16).padStart(2, "0")).join(" ");
      const ascii = Array.from(chunk).map(b => (b >= 32 && b < 127) ? String.fromCharCode(b) : ".").join("");
      lines.push(off.toString(16).padStart(8, "0") + "  " + hex.padEnd(48) + "  " + ascii);
    }
    return lines.join("\n");
  }

  // -- Module 1: Extension Picker --
  async function loadExtensions() {
    try {
      const res = await fetch("/api/extensions");
      if (!res.ok) throw new Error("Failed to load extensions");
      allExtensions = await res.json();
      renderGrid(allExtensions);
    } catch (e) {
      extGrid.innerHTML = '<p class="error">Could not load extensions.</p>';
      console.error(e);
    }
  }
  function renderGrid(list) {
    extGrid.innerHTML = "";
    if (!list.length) { extGrid.innerHTML = '<p class="empty">No extensions found.</p>'; return; }
    for (const ext of list) {
      const card = document.createElement("div");
      card.className = "ext-card";
      card.addEventListener("click", () => openExtension(ext));
      const icon = document.createElement("img");
      icon.className = "ext-icon";
      icon.src = ext.icon_path ? "/api/icon?path=" + encodeURIComponent(ext.icon_path) : "";
      icon.alt = ""; icon.onerror = function () { this.style.display = "none"; };
      const info = document.createElement("div");
      info.className = "ext-info";
      const title = document.createElement("div");
      title.className = "ext-card-name"; title.textContent = ext.name || ext.id;
      const meta = document.createElement("div");
      meta.className = "ext-card-meta";
      meta.innerHTML = '<span class="ext-card-version">v' + esc(ext.version || "?") +
        '</span><span class="profile-badge">' + esc(ext.profile || "") + '</span>';
      const desc = document.createElement("div");
      desc.className = "ext-card-desc"; desc.textContent = (ext.description || "").slice(0, 120);
      info.append(title, meta, desc);
      card.append(icon, info);
      extGrid.appendChild(card);
    }
  }
  function filterExtensions() {
    const q = filterInput.value.toLowerCase().trim();
    if (!q) { renderGrid(allExtensions); return; }
    renderGrid(allExtensions.filter(e =>
      (e.name || "").toLowerCase().includes(q) || (e.id || "").toLowerCase().includes(q)));
  }
  function setupUpload() {
    uploadZone.addEventListener("dragover", (e) => { e.preventDefault(); uploadZone.classList.add("drag-over"); });
    uploadZone.addEventListener("dragleave", () => uploadZone.classList.remove("drag-over"));
    uploadZone.addEventListener("drop", async (e) => {
      e.preventDefault(); uploadZone.classList.remove("drag-over");
      if (e.dataTransfer.files[0]) await uploadFile(e.dataTransfer.files[0]);
    });
    uploadZone.addEventListener("click", () => uploadInput.click());
    uploadInput.addEventListener("change", async () => {
      if (uploadInput.files[0]) await uploadFile(uploadInput.files[0]);
      uploadInput.value = "";
    });
  }
  async function uploadFile(file) {
    try {
      const fd = new FormData(); fd.append("file", file);
      const res = await fetch("/api/upload", { method: "POST", body: fd });
      if (!res.ok) throw new Error("Upload failed");
      const ext = await res.json();
      await loadExtensions();
      openExtension(ext);
    } catch (e) { console.error(e); alert("Upload failed: " + e.message); }
  }

  // -- Module 2: File Tree --
  async function openExtension(ext) {
    currentExt = ext;
    pickerView.hidden = true; viewerView.hidden = false;
    extName.textContent = ext.name || ext.id;
    extVersion.textContent = "v" + (ext.version || "?");
    extProfile.textContent = ext.profile || "";
    codeWelcome.hidden = false; searchResults.hidden = true;
    codeViewer.hidden = true; selectedNode = null;
    fileTree.innerHTML = '<p class="loading">Loading...</p>';
    try {
      const res = await fetch("/api/tree?ext=" + encodeURIComponent(ext.id) +
        "&profile=" + encodeURIComponent(ext.profile || ""));
      if (!res.ok) throw new Error("Failed to load file tree");
      const tree = await res.json();
      fileTree.innerHTML = "";
      renderTree(tree, fileTree, 0);
    } catch (e) {
      fileTree.innerHTML = '<p class="error">Failed to load tree.</p>';
      console.error(e);
    }
  }
  function sortChildren(children) {
    if (!children) return [];
    const dirs = children.filter(c => c.type === "directory");
    const files = children.filter(c => c.type !== "directory");
    const alpha = (a, b) => (a.name || "").localeCompare(b.name || "");
    dirs.sort(alpha);
    files.sort((a, b) => {
      if (a.name === "manifest.json") return -1;
      if (b.name === "manifest.json") return 1;
      return alpha(a, b);
    });
    return [...dirs, ...files];
  }
  function renderTree(node, container, depth) {
    for (const child of sortChildren(node.children)) {
      const isDir = child.type === "directory";
      const row = document.createElement("div");
      row.className = "tree-row" + (isDir ? " tree-dir" : " tree-file");
      row.style.paddingLeft = (12 + depth * 16) + "px";
      const icon = document.createElement("span");
      icon.className = "tree-icon"; icon.textContent = fileIcon(child.name, isDir);
      const label = document.createElement("span");
      label.className = "tree-label"; label.textContent = child.name;
      row.append(icon, label);
      if (!isDir && child.size != null) {
        const sz = document.createElement("span");
        sz.className = "tree-size"; sz.textContent = fmtSize(child.size);
        row.appendChild(sz);
      }
      container.appendChild(row);
      if (isDir) {
        const sub = document.createElement("div");
        sub.className = "tree-children"; sub.hidden = true;
        container.appendChild(sub);
        row.addEventListener("click", () => {
          const open = !sub.hidden; sub.hidden = open;
          icon.textContent = open ? "\u{1F4C1}" : "\u{1F4C2}";
        });
        if (child.children) renderTree(child, sub, depth + 1);
      } else {
        row.addEventListener("click", () => {
          if (selectedNode) selectedNode.classList.remove("selected");
          row.classList.add("selected"); selectedNode = row;
          openFile(child.path, child.name, child.size);
        });
      }
    }
  }

  // -- Module 3: Code Viewer --
  function removeElView(id) { const el = $("#" + id); if (el) el.hidden = true; }
  function removeToolbarButtons() {
    $("#code-file-bar").querySelectorAll(".toolbar-btn").forEach(b => b.remove());
  }
  function addBtn(label, title, fn) {
    const btn = document.createElement("button");
    btn.className = "toolbar-btn"; btn.textContent = label; btn.title = title;
    btn.addEventListener("click", fn);
    $("#code-file-bar").appendChild(btn);
  }
  function numberedHtml(text) {
    const lines = text.split("\n"), pad = String(lines.length).length;
    return lines.map((l, i) =>
      '<span class="line-num">' + String(i + 1).padStart(pad) + '</span>' + esc(l)
    ).join("\n");
  }

  async function openFile(path, name, size) {
    codeWelcome.hidden = true; searchResults.hidden = true; codeViewer.hidden = false;
    codeFilePath.textContent = name; codeFileSize.textContent = fmtSize(size);
    codePre.hidden = false; codeBlock.textContent = "Loading..."; codeBlock.className = "";
    removeElView("image-view-el"); removeElView("hex-view-el");
    codePre.classList.remove("word-wrap"); removeToolbarButtons();

    if (isImage(name)) {
      codePre.hidden = true;
      let iv = $("#image-view-el");
      if (!iv) { iv = document.createElement("div"); iv.id = "image-view-el"; iv.className = "image-view"; codeViewer.appendChild(iv); }
      iv.hidden = false; iv.innerHTML = "";
      const img = document.createElement("img");
      img.src = "/api/file?path=" + encodeURIComponent(path); img.alt = name; img.style.maxWidth = "100%";
      iv.appendChild(img);
      addBtn("Download", "Download file", () => triggerDownload(path, name));
      return;
    }
    try {
      const res = await fetch("/api/file?path=" + encodeURIComponent(path));
      if (!res.ok) throw new Error("Failed to load file");
      const text = await res.text();
      const hdrSize = res.headers.get("X-File-Size");
      if (hdrSize) codeFileSize.textContent = fmtSize(parseInt(hdrSize, 10));

      if (isBinary(text)) {
        codePre.hidden = true;
        let hv = $("#hex-view-el");
        if (!hv) { hv = document.createElement("pre"); hv.id = "hex-view-el"; hv.className = "hex-view"; codeViewer.appendChild(hv); }
        hv.hidden = false; hv.textContent = hexDump(text);
        addBtn("Download", "Download file", () => triggerDownload(path, name));
        return;
      }
      let content = text;
      const lang = langFromExt(name);
      if (lang === "json") { try { content = JSON.stringify(JSON.parse(text), null, 2); } catch (_) {} }

      if (lang) {
        codeBlock.className = "language-" + lang;
        codeBlock.innerHTML = numberedHtml(content);
        try { Prism.highlightElement(codeBlock); } catch (_) {}
      } else {
        codeBlock.className = "";
        codeBlock.innerHTML = numberedHtml(content);
      }
      addBtn("Copy", "Copy to clipboard", () => navigator.clipboard.writeText(text).catch(() => {}));
      addBtn("Wrap", "Toggle word wrap", () => codePre.classList.toggle("word-wrap"));
      addBtn("Download", "Download file", () => triggerDownload(path, name));
      if (lang === "javascript") {
        let beautified = false;
        addBtn("Beautify", "Rough-beautify minified code", function () {
          beautified = !beautified;
          const t = beautified ? simpleBeautify(text) : text;
          codeBlock.className = "language-" + lang;
          codeBlock.innerHTML = numberedHtml(t);
          try { Prism.highlightElement(codeBlock); } catch (_) {}
          this.textContent = beautified ? "Minified" : "Beautify";
        });
      }
    } catch (e) { showError("Failed to load file: " + e.message); console.error(e); }
  }

  function triggerDownload(path, name) {
    const a = document.createElement("a");
    a.href = "/api/file?path=" + encodeURIComponent(path);
    a.download = name; document.body.appendChild(a); a.click(); a.remove();
  }
  function simpleBeautify(code) {
    let out = "", indent = 0, inStr = false, strCh = "";
    for (let i = 0; i < code.length; i++) {
      const ch = code[i];
      if (inStr) { out += ch; if (ch === strCh && code[i - 1] !== "\\") inStr = false; continue; }
      if (ch === '"' || ch === "'" || ch === "`") { inStr = true; strCh = ch; out += ch; continue; }
      if (ch === "{" || ch === "[") { out += ch + "\n"; indent++; out += "  ".repeat(indent); continue; }
      if (ch === "}" || ch === "]") { indent = Math.max(0, indent - 1); out += "\n" + "  ".repeat(indent) + ch; continue; }
      if (ch === ";" && code[i + 1] !== "\n") { out += ";\n" + "  ".repeat(indent); continue; }
      out += ch;
    }
    return out;
  }
  function scrollToLine(lineNum) {
    const nums = codePre.querySelectorAll(".line-num");
    if (lineNum > 0 && lineNum <= nums.length) {
      const el = nums[lineNum - 1];
      el.scrollIntoView({ block: "center", behavior: "smooth" });
      el.classList.add("line-highlight");
      setTimeout(() => el.classList.remove("line-highlight"), 2000);
    }
  }

  // -- Module 4: Search --
  async function doSearch() {
    const query = searchInput.value.trim();
    if (!query || !currentExt) return;
    codeWelcome.hidden = true; codeViewer.hidden = true;
    searchResults.hidden = false;
    searchResults.innerHTML = '<p class="loading">Searching...</p>';
    try {
      const params = new URLSearchParams({
        ext: currentExt.id, profile: currentExt.profile || "", q: query,
        regex: searchRegex.checked ? "1" : "0", case: searchCase.checked ? "1" : "0",
      });
      const res = await fetch("/api/search?" + params.toString());
      if (!res.ok) throw new Error("Search failed");
      renderSearchResults(await res.json(), query);
    } catch (e) {
      searchResults.innerHTML = '<p class="error">Search failed: ' + esc(e.message) + "</p>";
      console.error(e);
    }
  }
  function renderSearchResults(results, query) {
    searchResults.innerHTML = "";
    if (!results.length) { searchResults.innerHTML = '<p class="empty">No results found.</p>'; return; }
    const total = results.length, display = results.slice(0, 100);
    const hdr = document.createElement("div");
    hdr.className = "search-header"; hdr.textContent = total + " result" + (total !== 1 ? "s" : "");
    searchResults.appendChild(hdr);
    for (const r of display) {
      const row = document.createElement("div"); row.className = "search-result";
      const loc = document.createElement("a");
      loc.className = "search-loc"; loc.href = "#";
      loc.textContent = r.file + ":" + r.line;
      loc.addEventListener("click", (e) => {
        e.preventDefault();
        const fullPath = currentExt.path + "/" + r.file;
        openFile(fullPath, r.file.split("/").pop(), null).then(() =>
          setTimeout(() => scrollToLine(r.line), 150));
      });
      const text = document.createElement("span"); text.className = "search-text";
      const escaped = esc(r.text), flags = searchCase.checked ? "g" : "gi";
      try {
        const re = new RegExp("(" + esc(query).replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + ")", flags);
        text.innerHTML = escaped.replace(re, '<mark>$1</mark>');
      } catch (_) { text.textContent = r.text; }
      row.append(loc, text); searchResults.appendChild(row);
    }
    if (total > 100) {
      const more = document.createElement("p");
      more.className = "search-more"; more.textContent = (total - 100) + " more results not shown.";
      searchResults.appendChild(more);
    }
  }

  // -- Module 5: Splitter --
  function setupSplitter() {
    const KEY = "esv-tree-width";
    const saved = localStorage.getItem(KEY);
    if (saved) treePanel.style.width = saved + "px";
    let dragging = false, startX, startW;
    splitter.addEventListener("mousedown", (e) => {
      dragging = true; startX = e.clientX;
      startW = treePanel.getBoundingClientRect().width;
      document.body.style.cursor = "col-resize"; document.body.style.userSelect = "none";
      e.preventDefault();
    });
    document.addEventListener("mousemove", (e) => {
      if (!dragging) return;
      const w = Math.max(150, Math.min(startW + e.clientX - startX, window.innerWidth - 200));
      treePanel.style.width = w + "px";
    });
    document.addEventListener("mouseup", () => {
      if (!dragging) return; dragging = false;
      document.body.style.cursor = ""; document.body.style.userSelect = "";
      localStorage.setItem(KEY, treePanel.getBoundingClientRect().width | 0);
    });
  }

  // -- Init --
  function goBack() {
    viewerView.hidden = true; pickerView.hidden = false;
    currentExt = null; searchInput.value = "";
  }
  document.addEventListener("DOMContentLoaded", () => {
    loadExtensions(); setupUpload(); setupSplitter();
    filterInput.addEventListener("input", filterExtensions);
    backBtn.addEventListener("click", goBack);
    searchBtn.addEventListener("click", doSearch);
    searchInput.addEventListener("keydown", (e) => { if (e.key === "Enter") doSearch(); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !viewerView.hidden) goBack(); });
  });
})();
