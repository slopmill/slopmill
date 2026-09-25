/* SPDX-License-Identifier: AGPL-3.0-or-later */
"use strict";
/* slopmill. Plan: your words and prompts. Draft: the words, each block editable and
   lockable. Proof: the issue as the design draws it, with notes, spelling and Edit. */

const S = {
  app: null, slug: null, screen: "compose",
  meta: {}, blocks: [], rev: null, review: { comments: [], proposals: [], chat: [] },
  history: [], problems: {}, job: null, jobBlocks: {}, log: [], seq: 0, seqAtLoad: 0, es: null,
  preview: null, focusId: null, dirty: false, saveTimer: null, saving: false,
  reveal: {}, undoArmed: false, pending: null,
  editSeq: 0, savingPromise: null, lastSeq: 0, loadGen: 0, previewGen: 0,
  design: null, designMissing: false, voiceInfo: null, nextRequest: null, fields: [], required: [],
  accents: {}, lintOn: false, usage: null, usageTimer: null,
  edit: null,      // the one open Edit box (SPEC-STEPS 6-12): {id, type, orig, start, ta, node, dialog, busy}
};
const PICTURE_RE = /^\s*(image|photo|picture|illustration)\s*:/i;
const isPicture = (t) => PICTURE_RE.test(t || "");
const CHART_RE = /^\s*(chart|graph|plot)\s*:/i;
const isChart = (t) => CHART_RE.test(t || "");
const kb = (n) => (n / 1024).toFixed(n < 10240 ? 1 : 0) + "KB";
const ktok = (n) => n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1000 ? (n / 1000).toFixed(n < 10000 ? 1 : 0) + "k" : String(n);
const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat()) if (k !== null && k !== undefined && k !== false) n.append(k.nodeType ? k : document.createTextNode(k));
  return n;
};
/* The slopmill mark: paper weaving over and under four rollers. With class "working" the
   print on the sheet runs through the mill and the rollers turn: the sign the model is at it.
   Drawn here (not an <img>) so CSS can move it; still for anyone who asks for less motion. */
const MILL_PAPER = "M6 -2 L10.67 15.29 A10.7 10.7 0 0 0 26.36 21.76 L37.64 15.24 A10.7 10.7 0 1 1 38.26 34.09 "
  + "L25.74 27.91 A10.7 10.7 0 1 0 26.05 46.93 L37.95 40.57 A10.7 10.7 0 1 1 44.61 60.58 L9 66";
const MILL_ROLLERS = [[21, 12.5, false], [43, 24.5, true], [21, 37.5, false], [43, 50, true]];   // x, y, clockwise
function millMark(cls, label) {
  const NS = "http://www.w3.org/2000/svg";
  const make = (tag, attrs) => { const n = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v); return n; };
  const svg = make("svg", { viewBox: "0 0 64 64", class: "mill" + (cls ? " " + cls : "") });
  // the cards are redrawn as a pass reports in: start each new mark in step with the clock,
  // so a redraw carries on the motion instead of jumping back to the start
  const now = performance.now();
  svg.style.setProperty("--feed", `-${(now % 800) / 1000}s`);
  svg.style.setProperty("--spin", `-${(now % 1400) / 1000}s`);
  if (label) { svg.setAttribute("role", "img"); svg.append(make("title", {})); svg.firstChild.textContent = label; }
  else svg.setAttribute("aria-hidden", "true");
  svg.append(make("rect", { width: 64, height: 64, rx: 14, class: "mill-bg" }));
  for (const [x, y, cw] of MILL_ROLLERS) {
    const g = make("g", { class: "mill-roller " + (cw ? "cw" : "ccw") });
    g.append(make("circle", { cx: x, cy: y, r: 7.6, class: "mill-body" }), make("circle", { cx: x, cy: y, r: 2.4, class: "mill-hub" }),
      make("circle", { cx: x, cy: y - 5.2, r: 1.1, class: "mill-hub" }));
    svg.append(g);
  }
  svg.append(make("path", { d: MILL_PAPER, class: "mill-paper" }), make("path", { d: MILL_PAPER, class: "mill-print", pathLength: 100 }));
  return svg;
}
/* The header's mark turns whenever this issue has the model busy: a pass, or a question. */
function paintMill() {
  const m = document.querySelector("header .logo .mill");
  if (m) m.classList.toggle("working", !!(running() || S.asking));
}

const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const ALPHA = "abcdefghjkmnpqrstuvwxyz23456789";
function newId() {
  const taken = new Set(S.blocks.map((b) => b.id));
  for (;;) {
    let s = "b-";
    for (let i = 0; i < 4; i++) s += ALPHA[Math.floor(Math.random() * ALPHA.length)];
    if (!taken.has(s)) return s;
  }
}
function toast(msg, bad) {
  const t = el("div", { class: "toast" + (bad ? " bad" : ""), role: "status", text: msg });
  document.body.append(t);
  setTimeout(() => t.remove(), bad ? 6000 : 3000);
}
function banner(msg, bad, actions) {
  const b = $("#banner");
  if (!msg) { b.hidden = true; b.replaceChildren(); return; }
  b.hidden = false; b.className = "banner" + (bad ? " bad" : "");
  b.replaceChildren(el("span", { text: msg }), ...(actions || []).map(([label, fn]) =>
    el("button", { class: "ghost small", type: "button", style: "margin-left:10px", text: label, onclick: fn })));
}

/* Unsaved work is copied to this browser on every keystroke, so a crash, a closed tab or a
   conflicting save on another computer never loses it. Cleared once the server has it. */
const stashKey = (slug) => "cmp-unsaved:" + slug;
function stash() {
  try { localStorage.setItem(stashKey(S.slug), JSON.stringify({ rev: S.rev, meta: S.meta, blocks: S.blocks, ts: Date.now() })); } catch (e) {}
}
function readStash(slug) {
  try { return JSON.parse(localStorage.getItem(stashKey(slug)) || "null"); } catch (e) { return null; }
}
function clearStash(slug) { try { localStorage.removeItem(stashKey(slug)); } catch (e) {} }
function pruneStashes() {   // recovery copies are for crashes, not an archive
  try {
    for (let i = localStorage.length - 1; i >= 0; i--) {
      const k = localStorage.key(i);
      if (!k || !k.startsWith("cmp-unsaved:")) continue;
      const v = JSON.parse(localStorage.getItem(k) || "null");
      if (!v || Date.now() - v.ts > 14 * 864e5) localStorage.removeItem(k);
    }
  } catch (e) {}
}
function sameBlocks(a, b) {
  const norm = (bs) => JSON.stringify(bs.filter((x) => x.type === "comment" || (x.text || "").trim())
    .map((x) => [x.type, x.id, (x.text || "").trim()]));
  return norm(a) === norm(b);
}
function offerRestore(slug, msg) {
  const st = readStash(slug);
  if (!st || slug !== S.slug) return;
  if (sameBlocks(st.blocks, S.blocks) && JSON.stringify(st.meta) === JSON.stringify(S.meta)) { clearStash(slug); return; }
  banner(msg + ` (from ${new Date(st.ts).toLocaleTimeString()})`, true, [
    ["Restore my version", () => { if (slug !== S.slug || running()) return; S.blocks = st.blocks; S.meta = st.meta; banner(null); render(); markDirty(); }],
    ["Discard it", () => { clearStash(slug); banner(null); }],
  ]);
}
async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: { "x-slopmill": "1" } };
  if (opts.body !== undefined) { init.headers["content-type"] = "application/json"; init.body = JSON.stringify(opts.body); }
  const r = await fetch(path, init);
  let data = null;
  try { data = await r.json(); } catch (e) { data = null; }
  if (!r.ok) {
    const err = new Error((data && data.error) || `request failed (${r.status})`);
    err.status = r.status; err.data = data;
    throw err;
  }
  return data;
}
const running = () => S.job && S.job.status === "running";
const mmss = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

/* ── theme ─────────────────────────────────────────────────────────────── */
function setTheme(t) {
  try { localStorage.setItem("cmp-theme", t); } catch (e) {}
  if (t === "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", t);
  document.querySelectorAll("[data-theme-set]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.themeSet === t)));
  if (S.screen === "review") paintPreview();
}

/* ── loading ───────────────────────────────────────────────────────────── */
async function init() {
  let theme = "auto";
  try { theme = localStorage.getItem("cmp-theme") || "auto"; } catch (e) {}
  setTheme(theme);
  document.querySelectorAll("[data-theme-set]").forEach((b) => b.addEventListener("click", () => setTheme(b.dataset.themeSet)));
  document.querySelectorAll(".steps button").forEach((b) => b.addEventListener("click", () => go(b.dataset.screen)));
  $("#issue-select").addEventListener("change", (e) => { location.hash = e.target.value; });
  $("#new-issue").addEventListener("click", newIssueDialog);
  $("#models-btn").addEventListener("click", modelsDialog);
  $("#meter").addEventListener("click", usageDialog);
  try { matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { if (S.screen === "review") paintPreview(); }); } catch (e) {}
  $("#modal-close").addEventListener("click", closeModal);
  $("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });
  $("#chat-form").addEventListener("submit", (e) => { e.preventDefault(); sendChat("ask"); });
  $("#chat-do").addEventListener("click", () => sendChat("do"));
  $("#chat-input").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); sendChat("ask"); } });
  window.addEventListener("hashchange", () => loadIssue(location.hash.slice(1)));
  window.addEventListener("beforeunload", (e) => { if (S.dirty || editDirty()) { e.preventDefault(); e.returnValue = ""; } });
  setupCommentPop();
  pruneStashes();
  $("header .logo").prepend(millMark("", "slopmill"));
  setInterval(tick, 1000);
  setInterval(() => { if (!document.hidden) loadUsage(); }, 120000);
  try {
    S.app = await api("/api/state");
  } catch (e) { banner(e.message, true); return; }
  fillIssueSelect();
  if (S.app.demo) $("#demo-bar").hidden = false;
  const want = location.hash.slice(1);
  const first = S.app.issues.find((i) => i.slug === want) || S.app.issues[0];
  if (!first) { renderEmpty(); return; }
  if (first.slug !== want) history.replaceState(null, "", "#" + first.slug);
  await loadIssue(first.slug);
}

function fillIssueSelect() {
  const sel = $("#issue-select");
  sel.replaceChildren(...S.app.issues.map((i) => el("option", { value: i.slug, text: (i.number ? `#${i.number} · ` : "") + i.title })));
  if (S.slug) sel.value = S.slug;
}

async function loadIssue(slug, keepScreen) {
  if (!slug) return;
  if (S.slug && slug !== S.slug && editDirty()) {
    $("#issue-select").value = S.slug;
    history.replaceState(null, "", "#" + S.slug);
    toast("Save or cancel the text you are editing first.", true);
    return;
  }
  if (S.slug && slug !== S.slug) closeEditor();
  if (S.slug && slug !== S.slug && !(await flush())) {
    $("#issue-select").value = S.slug;
    history.replaceState(null, "", "#" + S.slug);
    toast("This issue has changes that could not be saved. Fix or discard them first.", true);
    return;
  }
  const gen = ++S.loadGen;
  let st;
  try { st = await api(`/api/issues/${encodeURIComponent(slug)}`); }
  catch (e) { banner(e.message, true); return; }
  if (gen !== S.loadGen) return;
  const switching = slug !== S.slug;
  S.slug = slug; S.meta = st.meta; S.blocks = st.blocks; S.rev = st.rev; S.review = st.review;
  S.history = st.history || []; S.job = st.job; S.problems = st.problems || {};
  takeIssueInfo(st);
  if (switching) {
    S.jobBlocks = (st.job && st.job.status === "running") ? st.job.blocks : {};
    S.log = []; S.seqAtLoad = st.seq; S.lastSeq = 0; S.preview = null; S.reveal = {};
    S.dirty = false; S.editSeq = 0; S.undoArmed = false;
    if (!keepScreen) S.screen = running() ? "build" : "compose";
    connectEvents();
    S.usage = null; loadUsage();
  }
  $("#issue-select").value = slug;
  banner(null);
  render();
  if (S.screen === "review") loadPreview();
  offerRestore(slug, "This browser has unsaved changes to this issue");
}

/* What differs per issue: the design that draws it and the voice that writes it. */
function takeIssueInfo(st) {
  S.design = st.design; S.designMissing = !!st.design_missing; S.voiceInfo = st.voice;
  S.nextRequest = st.next_request; S.fields = st.fields || []; S.required = st.required || [];
  S.accents = st.accents || {}; S.lintOn = !!st.lint;
  S.asking = !!st.asking; S.sources = st.sources || {}; S.figure = st.figure !== false;
  paintMill();          // the server says whether a question is still being answered
}

async function reloadDoc(opts = {}) {
  const slug = S.slug, gen = S.loadGen;
  try {
    const st = await api(`/api/issues/${encodeURIComponent(slug)}`);
    if (slug !== S.slug || gen !== S.loadGen) return;
    // Unsaved typing wins the screen; its save will meet the new revision and be offered
    // back as a restore instead of silently overwriting what changed.
    if (S.dirty) { S.job = st.job; S.review = st.review; S.asking = !!st.asking; renderSide(); renderChat(); paintMill(); return; }
    S.meta = st.meta; S.blocks = st.blocks; S.rev = st.rev; S.review = st.review; S.history = st.history || []; S.job = st.job;
    S.problems = st.problems || {};
    takeIssueInfo(st);
    S.dirty = false;
    // keepFrame: repaint the Proof view in place, so the page stays where the author was
    if (opts.keepFrame && S.screen === "review" && $("#preview-frame")) { updateTabs(); renderSide(); renderChat(); }
    else render();
    if (S.screen === "review") loadPreview();
  } catch (e) { banner(e.message, true); }
}

function connectEvents() {
  if (S.es) S.es.close();
  const slug = S.slug;
  const es = new EventSource(`/api/issues/${encodeURIComponent(slug)}/events?since=${S.lastSeq}`);
  S.es = es;
  es.onmessage = (m) => {
    if (slug !== S.slug) return;
    let ev;
    try { ev = JSON.parse(m.data); } catch (e) { return; }
    if (ev.type === "gap") {
      S.log = [{ level: "warn", text: "some earlier run-log lines were missed while this page was disconnected", ts: Date.now() / 1000 }];
      reloadDoc(); return;
    }
    if (!ev.seq || ev.seq <= S.lastSeq) return;      // already handled (a reconnect replays)
    S.lastSeq = ev.seq;
    onEvent(ev, ev.seq <= S.seqAtLoad);
  };
  es.onerror = () => { /* the browser reconnects on its own; since= resumes */ };
}

function onEvent(ev, replay) {
  if (ev.type === "log") {
    S.log.push(ev);
    if (S.log.length > 2000) S.log = S.log.slice(-1500);
    if (S.screen === "build") appendLog(ev);
  } else if (ev.type === "block") {
    S.jobBlocks[ev.block] = { status: ev.status, detail: ev.detail };
    if (ev.status === "done" && !replay) S.reveal[ev.block] = Date.now();
    if (S.screen === "build") renderBuildFlow();
  } else if (ev.type === "job") {
    if (ev.status === "running") {
      S.job = { id: ev.job, kind: ev.kind, status: "running", started: ev.started, blocks: {} };
      if (!replay) { S.jobBlocks = {}; S.log = S.log.filter((l) => l.job === ev.job); }
    } else if (S.job && S.job.id === ev.job) {
      S.job.status = ev.status; S.job.elapsed = ev.elapsed;
    }
    updateTabs();
    // Edit buttons on Draft follow the model: disabled the moment a pass starts anywhere.
    if (!replay && S.screen === "build") renderBuildFlow();
    if (!replay && ev.status !== "running") {
      reloadDoc();
      loadUsage();
      // A generate pass stays on Draft: reading what it wrote is that step's job. A revise
      // was started from Proof, where its proposals are, so it goes back there.
      if (ev.status === "done" && ["revise", "build"].includes(ev.kind) && S.screen === "build") {
        setTimeout(() => { if (S.screen === "build" && !running()) go("review"); }, 1600);
      } else if (ev.status === "failed") toast("The pass failed. The run log says why.", true);
    }
    if (S.screen === "build" || S.screen === "review") renderSide();
  } else if (ev.type === "doc") {
    if (ev.rev && ev.rev === S.rev) return;     // this page made the change and already has it
    if (S.fixBusy) return;                       // a fix or an edit is being saved; that reloads in place
    if (!replay && !running()) reloadDoc();
  } else if (ev.type === "usage") {
    if (!replay) { clearTimeout(S.usageTimer); S.usageTimer = setTimeout(loadUsage, 400); }
  } else if (ev.type === "chat") {
    if (!replay) {
      S.review.chat.push({ id: ev.id, role: ev.role, text: ev.text, ts: ev.ts, kind: ev.kind, prompts: ev.prompts,
        change: ev.change, asked: ev.asked, sources: ev.sources });
      if (ev.kind === "answer" || ev.kind === "ask-failed") S.asking = false;
      renderChat(); paintChatButtons(); paintMill();
    }
  }
}

/* ── navigation ────────────────────────────────────────────────────────── */
async function go(screen, focusId) {
  if (editDirty()) { toast("Save or cancel the text you are editing first.", true); return; }
  closeEditor();
  if (S.dirty && !(await flush())) { toast("Not saved yet; fix the problem shown at the top first.", true); return; }
  S.screen = screen;
  hidePop();
  render();
  if (screen === "review") loadPreview();
  window.scrollTo({ top: 0 });
  if (focusId) {
    const n = document.querySelector(`#flow [data-id="${CSS.escape(focusId)}"]`);
    if (n) { n.scrollIntoView({ block: "center" }); n.classList.add("flash"); setTimeout(() => n.classList.remove("flash"), 1400); }
  }
}
function updateTabs() {
  paintMill();
  document.querySelectorAll(".steps button").forEach((b) => {
    b.setAttribute("aria-selected", String(b.dataset.screen === S.screen));
    b.querySelector(".dot")?.remove();
    if (b.dataset.screen === "build" && running()) b.append(el("span", { class: "dot", "aria-label": "running" }));
  });
}
function render() {
  updateTabs();
  if (S.screen === "compose") renderCompose();
  else if (S.screen === "build") renderBuild();
  else renderReview();
  renderSide();
  renderChat();
}
function renderEmpty() {
  $("#main").replaceChildren(el("div", { class: "doc-head" },
    el("h1", { class: "doc-title", text: "No issues yet" }),
    el("p", { class: "doc-sub", text: "Start one with + New" })));
}

/* ── counts ────────────────────────────────────────────────────────────── */
function draftFor(pid) { return S.blocks.find((b) => b.type === "draft" && b.attrs.for === pid); }
function toWrite() {
  return S.blocks.filter((b) => b.type === "prompt" && (!draftFor(b.id) || draftFor(b.id).attrs.stale === "1"));
}
function counts() {
  const c = { prose: 0, prompt: 0, draft: 0, stale: 0, component: 0, words: 0 };
  for (const b of S.blocks) {
    if (c[b.type] !== undefined) c[b.type]++;
    if (b.type === "draft" && b.attrs.stale === "1") c.stale++;
    if (b.type === "prose" || b.type === "draft") c.words += (b.text.match(/\S+/g) || []).length;
  }
  c.write = toWrite().length;
  return c;
}

/* ── compose ───────────────────────────────────────────────────────────── */
function renderCompose() {
  const locked = running();
  const title = el("input", { class: "doc-title", value: S.meta.title || "", "aria-label": "Title", placeholder: "Title", disabled: locked,
    oninput: (e) => { S.meta.title = e.target.value; markDirty(); } });
  const sub = el("p", { class: "doc-sub" },
    `Issue ${S.meta.number || "—"} · one panel · select text to change what it is · `,
    el("button", { type: "button", text: "details", onclick: () => $("details.meta")?.setAttribute("open", "") }));
  const selbar = el("div", { class: "selbar", id: "selbar" },
    el("button", { type: "button", "data-kind": "prose", text: "Prose", onmousedown: (e) => e.preventDefault(), onclick: () => convertFocused("prose") }),
    el("button", { type: "button", "data-kind": "prompt", text: "Prompt", onmousedown: (e) => e.preventDefault(), onclick: () => convertFocused("prompt") }));
  const list = el("div", { class: "blocks", id: "blocks" });
  // Plan is your words and your prompts. What the model wrote is read and edited on Draft.
  S.blocks.forEach((b, i) => {
    if (b.type === "draft") return;
    list.append(adder(i));
    list.append(blockEl(b, locked));
  });
  const addEnd = el("div", { class: "add-end" }, ...addButtons(S.blocks.length, locked));
  const c = counts();
  const cta = el("button", { class: "cta", type: "button", disabled: locked, onclick: () => (c.write ? generate() : go("build")) },
    c.write ? "Write the drafts →" : "Read the draft →");
  const note = el("span", { class: "cta-note", id: "cta-note" });
  $("#main").replaceChildren(el("div", { class: "doc-head" }, title, sub), selbar,
    el("span", { class: "selhint", id: "selhint" }), list, addEnd, el("div", { class: "cta-row" }, cta, note));
  list.querySelectorAll(".blk:not(.prose):not(.draft):not(.component) textarea, .blk.editing textarea").forEach(autosize);
  updateSelbar();
  paintCtaNote();
}

/* The line under Generate: what will be written, and how big the request is, as of the
   last save (every save returns the size afresh). */
function paintCtaNote() {
  const note = $("#cta-note");
  if (!note) return;
  const c = counts(), nr = S.nextRequest;
  const size = nr && nr.bytes ? ` · sends about ${kb(nr.bytes)} (≈${ktok(nr.tokens)} tokens) of ${kb(nr.limit)}` : "";
  const over = nr && nr.bytes > nr.limit;
  note.classList.toggle("bad", !!over);
  note.textContent = running() ? "the model is working; editing is paused"
    : over ? `too big to send: about ${kb(nr.bytes)} of ${kb(nr.limit)}. Turn off some voice files (Voice → Manage)`
    : (c.write ? `${c.write} prompt${c.write > 1 ? "s" : ""} to write · your words are sent as they are` : c.prompt ? "every prompt has a draft" : "no prompts: it is all your words") + (c.write ? size : "");
}

function adder(i) {
  return el("div", { class: "adder" }, ...addButtons(i, false));
}
/* What can go at position i: your words, a prompt, a picture the model draws, your own
   picture, or a chart the model works out (and researches, if the prompt asks). */
function addButtons(i, locked) {
  return [
    el("button", { type: "button", text: "+ Prose", disabled: locked, onclick: () => insertBlock(i, "prose") }),
    el("button", { type: "button", text: "+ Prompt", disabled: locked, onclick: () => insertBlock(i, "prompt") }),
    el("button", { type: "button", text: "+ AI picture", disabled: locked, title: pictureHint(), onclick: () => insertBlock(i, "picture") }),
    el("button", { type: "button", text: "+ Your picture", disabled: locked, title: "upload a picture of your own", onclick: () => pictureDialog({ after: anchorBefore(i) }) }),
    el("button", { type: "button", text: "+ Chart", disabled: locked, title: "the model works out the numbers (say “research” to have it look them up) and slopmill draws the chart", onclick: () => insertBlock(i, "chart") })];
}
/* The saved block a new picture goes after: the nearest one above position i that has an
   ID and text (a new, still empty block is not in the file yet). "" is the top. */
function anchorBefore(i) {
  for (let j = i - 1; j >= 0; j--) { const b = S.blocks[j]; if (b.id && b.type !== "comment" && b.text.trim()) return b.id; }
  return "";
}
function pictureHint() {
  return S.app && S.app.pictures ? "A prompt that starts with Image: is drawn as a picture"
    : "Pictures are off on this server: see Models";
}

function blockEl(b, locked) {
  const problem = b.id && S.problems[b.id] ? { text: S.problems[b.id].join(" · ") } : null;
  const wrap = el("div", { class: `blk ${b.type}`, "data-id": b.id || "" });
  if (b.type === "prose" && /^#{1,6}\s/.test(b.text)) wrap.classList.add("heading");
  if (b.type === "draft" && b.attrs.stale === "1") wrap.classList.add("stale");
  if (locked) wrap.classList.add("locked");
  const viewable = b.type === "prose" || b.type === "draft" || b.type === "component";
  const ta = el("textarea", { rows: 1, spellcheck: b.type === "prose" || b.type === "prompt" || b.type === "draft",
    "aria-label": `${b.type} block`, readonly: locked || b.type === "comment", class: viewable ? "viewable" : null,
    placeholder: b.type === "prompt" ? (isPicture(b.text) ? "Image: describe the picture…"
      : isChart(b.text) ? "Chart: what to chart, with your numbers, or say “research” and it looks them up…"
      : "What should go here? Notes, links, how long, what tone… (Image: for a picture, Chart: for a chart, “research” to look things up)") : b.type === "prose" ? "Write…" : null });
  ta.value = b.text;
  if (viewable) {
    ta.addEventListener("blur", () => {
      if (!wrap.isConnected) return;
      wrap.classList.remove("editing");
      view.innerHTML = b.text.trim() ? md(b.type === "component" ? stripFence(b.text) : b.text, true) : '<span class="empty-view">empty</span>';
    });
  }
  const view = viewable ? el("div", { class: "view", tabindex: locked ? null : "0", role: "button",
    "aria-label": `Edit this ${b.type === "prose" ? "paragraph" : b.type}` }) : null;
  if (view) {
    view.innerHTML = b.text.trim() ? md(b.type === "component" ? stripFence(b.text) : b.text, true) : '<span class="empty-view">empty</span>';
    const enter = () => { if (!locked) { S.focusId = b.id; focusBlock(b.id, "end"); } };
    view.addEventListener("click", enter);
    view.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); enter(); } });
  }
  ta.addEventListener("input", () => onBlockInput(b, ta));
  ta.addEventListener("focus", () => { S.focusId = b.id; updateSelbar(); });
  ta.addEventListener("select", updateSelbar);
  ta.addEventListener("keyup", updateSelbar);
  ta.addEventListener("mouseup", updateSelbar);
  ta.addEventListener("keydown", (e) => onBlockKey(e, b, ta));
  if (b.type === "prompt") {
    const d = draftFor(b.id);
    const pic = isPicture(b.text), chart = isChart(b.text);
    if (pic || chart) wrap.classList.add("picture");
    wrap.append(el("span", { class: "tag" }, pic ? "Picture prompt" : chart ? "Chart prompt" : "Prompt", el("span", { class: "id", text: "#" + b.id }),
      pic && !(S.app && S.app.pictures) ? el("span", { class: "warn-note", text: "pictures are off: see Models" }) : null,
      el("span", { class: "acts" },
        d && !locked ? el("button", { class: "ghost small", type: "button", text: "Rewrite", title: "write its draft again from this prompt", onclick: () => generate([b.id]) }) : null,
        !d && !locked ? el("button", { class: "ghost small danger", type: "button", text: "Delete", onclick: () => deleteBlock(b) }) : null)));
    if (d) {
      const stale = d.attrs.stale === "1";
      wrap.classList.add("has-draft");
      wrap.append(el("div", { class: "drafted" + (stale ? " stale" : "") },
        el("span", { text: stale ? "Changed since its draft" : "Drafted" }),
        el("button", { class: "linkish", type: "button", text: "Read it on Draft →", onclick: () => go("build", d.id) })));
    }
  } else if (b.type === "draft") {
    const stale = b.attrs.stale === "1";
    const drawn = b.attrs.picture === "1";
    wrap.append(el("span", { class: "tag" }, stale ? "Draft · prompt changed since" : drawn ? "Picture drawn from the prompt above" : "Written from the prompt above",
      el("span", { class: "acts" },
        !locked ? el("button", { class: "ghost small", type: "button", text: stale ? "Rewrite it" : "Regenerate", onclick: () => generate([b.attrs.for]) }) : null,
        !locked ? el("button", { class: "ghost small", type: "button", text: "Keep as my words", onclick: () => keepDraft(b) }) : null,
        !locked ? el("button", { class: "ghost small danger", type: "button", text: "Discard", onclick: () => deleteBlock(b) }) : null)));
  } else if (b.type === "component") {
    wrap.append(el("span", { class: "tag" }, b.attrs.class || "component", el("span", { class: "id", text: "#" + b.id }),
      el("span", { class: "acts" }, !locked ? el("button", { class: "ghost small danger", type: "button", text: "Delete", onclick: () => deleteBlock(b) }) : null)));
  } else if (b.type === "comment") {
    wrap.append(el("span", { class: "tag", text: "Note · not published" }));
  }
  if (view) wrap.append(view);
  wrap.append(ta);
  if (problem) { wrap.classList.add("has-problem"); wrap.append(el("p", { class: "problem", text: problem.text })); }
  return wrap;
}

function autosize(ta) {
  ta.style.height = "auto";
  ta.style.height = ta.scrollHeight + 2 + "px";
}

function onBlockInput(b, ta) {
  b.text = ta.value;
  autosize(ta);
  if (b.type === "prose") {
    // A blank line inside prose starts a new block: that is what a paragraph is.
    const m = ta.value.match(/\n[ \t]*\n/);
    if (m) {
      const before = ta.value.slice(0, m.index).replace(/\s+$/, "");
      const after = ta.value.slice(m.index + m[0].length);
      b.text = before;
      const i = S.blocks.indexOf(b);
      const parts = after.split(/\n[ \t]*\n/).map((s) => s.trim()).filter(Boolean);
      const created = (parts.length ? parts : [""]).map((t) => ({ type: "prose", id: newId(), text: t, attrs: {} }));
      S.blocks.splice(i + 1, 0, ...created);
      markDirty();
      renderCompose();
      focusBlock(created[created.length - 1].id, created.length > 1 || parts.length ? "end" : "start");
      return;
    }
    ta.closest(".blk").classList.toggle("heading", /^#{1,6}\s/.test(ta.value));
  }
  if (b.type === "prompt") {
    const w = ta.closest(".blk"), was = w.classList.contains("picture");
    if (was !== isPicture(ta.value)) {
      w.classList.toggle("picture", !was);
      const t = w.querySelector(".tag");
      if (t && t.firstChild && t.firstChild.nodeType === 3) t.firstChild.textContent = was ? "Prompt" : "Picture prompt";
    }
  }
  markDirty();
}

function onBlockKey(e, b, ta) {
  if (e.key === "Backspace" && ta.selectionStart === 0 && ta.selectionEnd === 0) {
    const i = S.blocks.indexOf(b);
    const prev = S.blocks[i - 1];
    if (!ta.value && b.type !== "draft") {
      e.preventDefault();
      if (b.type === "prompt" && draftFor(b.id)) return;
      S.blocks.splice(i, 1);
      markDirty(); renderCompose();
      if (prev) focusBlock(prev.id, "end");
    } else if (b.type === "prose" && prev && prev.type === "prose") {
      e.preventDefault();
      const at = prev.text.length;
      prev.text = prev.text + "\n" + b.text;
      S.blocks.splice(i, 1);
      markDirty(); renderCompose();
      focusBlock(prev.id, at + 1);
    }
  }
}

function focusBlock(id, pos) {
  const wrap = document.querySelector(`.blk[data-id="${CSS.escape(id)}"]`);
  const ta = wrap && wrap.querySelector("textarea");
  if (!ta) return;
  wrap.classList.add("editing");
  autosize(ta);
  ta.focus();
  const p = pos === "end" ? ta.value.length : pos === "start" ? 0 : pos;
  ta.setSelectionRange(p, p);
}

function focusedBlock() { return S.blocks.find((b) => b.id === S.focusId); }

function updateSelbar() {
  const bar = $("#selbar");
  if (!bar) return;
  const b = focusedBlock();
  const hint = $("#selhint");
  const ta = b && document.querySelector(`.blk[data-id="${CSS.escape(b.id)}"] textarea`);
  const partial = ta && ta.selectionEnd > ta.selectionStart && !(ta.selectionStart === 0 && ta.selectionEnd === ta.value.length);
  bar.querySelectorAll("button").forEach((btn) => {
    const kind = btn.dataset.kind;
    btn.classList.toggle("on", !!b && b.type === kind);
    btn.disabled = !b || running() || !(b.type === "prose" || b.type === "prompt") || (b.type === kind && !partial)
      || (b.type === "prompt" && kind === "prose" && !!draftFor(b.id));
  });
  if (hint) {
    hint.textContent = !b ? "click into a block" :
      (b.type === "prompt" && draftFor(b.id)) ? "this prompt has a draft: lock or discard it on Draft to turn it into prose" :
      partial ? `turn the selection into ${b.type === "prose" ? "a prompt" : "prose"}` : "";
  }
}

function convertFocused(kind) {
  const b = focusedBlock();
  if (!b || running() || !(b.type === "prose" || b.type === "prompt")) return;
  const ta = document.querySelector(`.blk[data-id="${CSS.escape(b.id)}"] textarea`);
  const i = S.blocks.indexOf(b);
  const s = ta ? ta.selectionStart : 0, e = ta ? ta.selectionEnd : 0;
  const partial = ta && e > s && !(s === 0 && e === ta.value.length);
  if (b.type === kind && !partial) return;
  let focusId;
  if (!partial) {
    if (kind === "prose") {
      const parts = b.text.split(/\n[ \t]*\n/).map((t) => t.trim()).filter(Boolean);
      const made = parts.map((t, j) => ({ type: "prose", id: j === 0 ? b.id : newId(), text: t, attrs: {} }));
      S.blocks.splice(i, 1, ...(made.length ? made : [{ ...b, type: "prose" }]));
    } else { b.type = kind; }
    focusId = b.id;
  } else {
    const before = b.text.slice(0, s).trim(), mid = b.text.slice(s, e).trim(), after = b.text.slice(e).trim();
    const other = kind === b.type ? (b.type === "prose" ? "prompt" : "prose") : kind;
    const pieces = [];
    if (before) pieces.push({ type: b.type, id: b.id, text: before, attrs: {} });
    const midId = before ? newId() : b.id;
    pieces.push({ type: other, id: midId, text: mid, attrs: {} });
    if (after) pieces.push({ type: b.type, id: newId(), text: after, attrs: {} });
    // prose pieces must not carry blank lines
    const flat = [];
    for (const p of pieces) {
      if (p.type === "prose" && /\n[ \t]*\n/.test(p.text)) {
        p.text.split(/\n[ \t]*\n/).map((t) => t.trim()).filter(Boolean).forEach((t, j) => flat.push({ ...p, id: j === 0 ? p.id : newId(), text: t }));
      } else flat.push(p);
    }
    S.blocks.splice(i, 1, ...flat);
    focusId = midId;
  }
  markDirty(); renderCompose();
  S.focusId = focusId; focusBlock(focusId, "end");
}

function insertBlock(i, type) {
  const lead = { picture: "Image: ", chart: "Chart: " }[type];
  const b = { type: lead ? "prompt" : type, id: newId(), text: lead || "", attrs: {} };
  S.blocks.splice(i, 0, b);
  renderCompose();
  S.focusId = b.id;
  focusBlock(b.id, lead ? "end" : "start");
}

/* Your own picture: a JPEG, PNG or WebP with alt text and an optional caption. It goes in as
   your figure (the model never rewrites it), after a block, or in place of a drawn draft. */
function pictureDialog(opts) {
  if (running()) { toast("The model is working; add a picture when it finishes.", true); return; }
  if (!S.figure) { toast(`The design ${S.design} has no figure block, so it cannot hold a picture.`, true); return; }
  const limit = S.app && S.app.demo ? 2e6 : 15e6;
  const file = el("input", { type: "file", id: "pic-file", accept: "image/jpeg,image/png,image/webp" });
  const preview = el("img", { class: "pic-preview", alt: "", hidden: true });
  const alt = el("input", { id: "pic-alt", maxlength: "400", autocomplete: "off", placeholder: "What the picture shows, for someone who cannot see it" });
  const cap = el("input", { id: "pic-cap", maxlength: "600", autocomplete: "off", placeholder: "Optional" });
  const msg = el("p", { class: "edit-msg", role: "status" });
  const say = (t, bad) => { msg.textContent = t; msg.classList.toggle("bad", !!bad); };
  const submit = el("button", { class: "send", type: "submit", text: opts.replace ? "Use this picture" : "Add picture" });
  file.addEventListener("change", () => {
    const f = file.files[0];
    preview.hidden = true; say("");
    if (!f) return;
    if (f.size > limit) { say(`That file is ${(f.size / 1e6).toFixed(1)}MB; a picture here is at most ${limit / 1e6}MB.`, true); return; }
    const rd = new FileReader();           // a data: URL, which the page's security policy allows
    rd.onload = () => { preview.src = rd.result; preview.hidden = false; };
    rd.readAsDataURL(f);
  });
  const form = el("form", { class: "pic-form", onsubmit: async (e) => {
    e.preventDefault();
    const f = file.files[0];
    if (!f) { say("Choose a picture first.", true); return; }
    if (f.size > limit) { say(`That file is over ${limit / 1e6}MB.`, true); return; }
    if (!alt.value.trim()) { say("Describe the picture in a few words: it is read out to people who cannot see it.", true); alt.focus(); return; }
    submit.disabled = true; say("Uploading…");
    const slug = S.slug;
    try {
      if (!(await flush())) throw new Error("other changes could not be saved first: the problem is shown at the top");
      // the words ride in headers, so they stay out of access logs; ids go in the query
      const qs = new URLSearchParams({ base: S.rev || "", after: opts.after || "", replace: opts.replace || "" });
      const r = await fetch(`/api/issues/${encodeURIComponent(slug)}/pictures?${qs}`,
        { method: "POST", body: f, headers: { "x-slopmill": "1", "content-type": "application/octet-stream",
          "x-picture-alt": encodeURIComponent(alt.value), "x-picture-caption": encodeURIComponent(cap.value),
          "x-picture-name": encodeURIComponent(f.name) } });
      let data = null;
      try { data = await r.json(); } catch (x) { data = null; }
      if (!r.ok) throw new Error((data && data.error) || `the upload failed (${r.status})`);
      closeModal(true);
      if (slug !== S.slug) return;
      toast(opts.replace ? "Your picture is in. The drawn one and its prompt are gone." : "Picture added.");
      await reloadDoc();
    } catch (err) { say(err.message, true); submit.disabled = false; }
  } },
    el("div", { class: "field" }, el("label", { for: "pic-file", text: "Picture (JPEG, PNG or WebP)" }), file),
    preview,
    el("div", { class: "field" }, el("label", { for: "pic-alt" }, "Alt text", el("span", { class: "req", text: " *" })), alt),
    el("div", { class: "field" }, el("label", { for: "pic-cap", text: "Caption" }), cap),
    msg,
    el("div", { class: "edit-row" }, el("button", { class: "ghost", type: "button", text: "Cancel", onclick: () => closeModal(true) }), submit));
  openModal(opts.replace ? "Use your own picture instead" : "Add your own picture", form);
}

async function deleteBlock(b) {
  if (b.type === "prompt" && draftFor(b.id)) { toast("Lock or discard its draft on Draft first."); return; }
  S.blocks = S.blocks.filter((x) => x !== b);
  markDirty(); render();
}

async function keepDraft(b) {
  if (!(await flush())) return;
  try {
    await api(`/api/issues/${encodeURIComponent(S.slug)}/keep`, { method: "POST", body: { draft: b.id, base: S.rev } });
    toast("Locked: these are your words now. The model can only suggest changes to them.");
    await reloadDoc();
  } catch (e) { toast(e.message, true); }
}

/* saving */
function markDirty() {
  S.dirty = true;
  S.editSeq++;
  stash();
  setSave("Unsaved…");
  clearTimeout(S.saveTimer);
  S.saveTimer = setTimeout(saveNow, 700);
}
/* Save until nothing is left unsaved. False if a save was refused. */
async function flush() {
  for (let i = 0; i < 5 && S.dirty; i++) {
    if (!(await saveNow())) return false;
  }
  return !S.dirty;
}
function setSave(text, bad) {
  const s = $("#save-state");
  s.textContent = text; s.classList.toggle("bad", !!bad);
}
function saveNow() {
  clearTimeout(S.saveTimer);
  if (S.saving) return S.savingPromise.then(() => (S.dirty ? saveNow() : true));
  if (!S.dirty || !S.slug) return Promise.resolve(true);
  S.saving = true;
  S.savingPromise = doSave().finally(() => { S.saving = false; });
  return S.savingPromise;
}
async function doSave() {
  setSave("Saving…");
  const slug = S.slug, seq = S.editSeq;
  const blocks = S.blocks.filter((b) => b.type === "comment" || b.text.trim())
    .map((b) => ({ type: b.type, id: b.id, text: b.text, attrs: Object.fromEntries(Object.entries(b.attrs || {}).filter(([k]) => k !== "stale")) }));
  try {
    const r = await api(`/api/issues/${encodeURIComponent(slug)}/doc`, { method: "PUT", body: { base: S.rev, meta: S.meta, blocks } });
    if (slug !== S.slug) return true;
    S.rev = r.rev;
    // Typing that happened while this save was in flight is still unsaved.
    if (S.editSeq === seq) { S.dirty = false; clearStash(slug); }
    else { clearTimeout(S.saveTimer); S.saveTimer = setTimeout(saveNow, 300); }
    const had = JSON.stringify(S.problems);
    S.problems = r.problems || {};
    if (r.next_request) { S.nextRequest = r.next_request; paintCtaNote(); }
    setSave("Saved"); banner(S.problems[""] ? "Problem: " + S.problems[""].join(" · ") : null, !!S.problems[""]);
    if (had !== JSON.stringify(S.problems) && S.screen === "compose") paintProblems();
    if (!S.dirty) setSave("Saved");
    // a prompt's text may have changed: refresh stale flags without disturbing the editor
    refreshStale();
    return true;
  } catch (e) {
    if (e.status === 409 && /changed somewhere else/.test(e.message)) {
      // Another computer saved first. Load theirs; ours stays in this browser to restore.
      setSave("Changed elsewhere", true);
      S.dirty = false;
      await reloadDoc();
      offerRestore(slug, "This issue was changed on another computer. Your unsaved version is kept");
      return false;
    }
    setSave("Not saved", true);
    banner("Not saved: " + e.message + ". Your text is still here; change it and it will save.", true);
    return false;
  }
}
function paintProblems() {
  document.querySelectorAll(".blk").forEach((w) => {
    const msgs = S.problems[w.dataset.id];
    w.classList.toggle("has-problem", !!msgs);
    w.querySelector(".problem")?.remove();
    if (msgs) w.append(el("p", { class: "problem", text: msgs.join(" · ") }));
  });
}

async function refreshStale() {
  try {
    const st = await api(`/api/issues/${encodeURIComponent(S.slug)}`);
    if (st.rev !== S.rev || S.dirty) return;
    const flags = Object.fromEntries(st.blocks.filter((b) => b.type === "draft").map((b) => [b.id, b.attrs.stale]));
    let changed = false;
    for (const b of S.blocks) if (b.type === "draft" && flags[b.id] !== undefined && b.attrs.stale !== flags[b.id]) { b.attrs.stale = flags[b.id]; changed = true; }
    if (changed && S.screen === "compose") {
      const f = document.activeElement && document.activeElement.closest(".blk")?.dataset.id;
      const pos = document.activeElement && document.activeElement.selectionStart;
      renderCompose(); if (f) focusBlock(f, pos);
    }
    if (S.screen === "compose") renderSide();
  } catch (e) { /* the next save will say */ }
}

/* ── generate / revise ─────────────────────────────────────────────────── */
async function generate(targets, direction) {
  const slug = S.slug;
  if (!(await flush())) { toast("Save the issue first: the problem is shown at the top.", true); return false; }
  try {
    const r = await api(`/api/issues/${encodeURIComponent(slug)}/generate`, { method: "POST", body: { targets: targets || null, direction: direction || "" } });
    if (slug !== S.slug) return true;        // the author moved on; the pass still runs
    S.jobBlocks = {}; S.reveal = {};
    if (r.job) { S.job = r.job; S.log = []; }
    go("build");
    return true;
  } catch (e) { toast(e.message, true); return false; }
}

async function sendEdits(general) {
  const slug = S.slug;
  if (!(await flush())) { toast("Save the issue first: the problem is shown at the top.", true); return false; }
  try {
    await api(`/api/issues/${encodeURIComponent(slug)}/revise`, { method: "POST", body: { general: general || "" } });
    if (slug !== S.slug) return true;
    S.jobBlocks = {}; S.reveal = {}; S.log = [];
    const st = await api(`/api/issues/${encodeURIComponent(slug)}`);
    if (slug !== S.slug) return true;
    S.review = st.review; S.job = st.job;
    go("build");
    return true;
  } catch (e) { toast(e.message, true); return false; }
}

/* Ask: the model answers in the chat and nothing changes. The other button is what the
   chat always did: write the drafts with this as direction, or revise the issue with it. */
async function sendChat(mode) {
  const input = $("#chat-input");
  const text = input.value.trim();
  const slug = S.slug;
  if (!slug) return;
  if (mode === "ask") {
    if (!text) { toast("Type a question first."); input.focus(); return; }
    if (S.asking) return;
    S.asking = true; renderChat(); paintChatButtons(); paintMill();
    try {
      // the answer should see what is on screen, so typing not yet saved is saved first
      if (!(await flush())) throw new Error("save the issue first: the problem is shown at the top");
      await api(`/api/issues/${encodeURIComponent(slug)}/ask`, { method: "POST", body: { text } });
      if (input.value.trim() === text) input.value = "";
    } catch (e) {
      if (slug === S.slug) { S.asking = e.status === 409; renderChat(); paintChatButtons(); paintMill(); }
      toast(e.message, true);
    }
    return;
  }
  if (running()) return;
  const queued = S.review.comments.filter((c) => c.status === "queued").length;
  const ok = (S.screen !== "review" && toWrite().length && !queued) ? await generate(null, text) : await sendEdits(text);
  if (ok && input.value.trim() === text) input.value = "";
}

function paintChatButtons() {
  const ask = $("#chat-send"), act = $("#chat-do"), hint = $("#chat-hint");
  if (!ask || !act) return;
  const queued = (S.review.comments || []).filter((c) => c.status === "queued").length;
  const writes = S.screen !== "review" && toWrite().length && !queued;
  act.textContent = writes ? "Write drafts with this" : "Change the issue" + (queued ? ` (+${queued} edit${queued > 1 ? "s" : ""})` : "");
  act.title = writes ? "write the drafts, with your message as direction" : "the model changes the issue as your message says";
  act.disabled = running();
  ask.disabled = !!S.asking;
  hint.textContent = S.asking ? "the model is answering…"
    : running() ? "the model is working · Ask still works · changes wait until it finishes"
    : "Ask answers here and changes nothing · say “research” to have it look things up";
}

async function stop() {
  try { await api(`/api/issues/${encodeURIComponent(S.slug)}/stop`, { method: "POST", body: {} }); }
  catch (e) { toast(e.message, true); }
}

/* ── draft ─────────────────────────────────────────────────────────────── */
/* The whole issue as text, in order. During a pass it shows the work in place; after it,
   every block can be edited, and saving locks it as the author's words. */
function renderBuild() {
  const r = running();
  const kind = S.job ? S.job.kind : null;
  const head = el("div", { class: "doc-head" },
    el("h1", { class: "doc-title", text: r ? ({ revise: "Revising", proof: "Proofreading" }[kind] || "Drafting") + " " + (S.meta.title || "the issue") : (S.meta.title || "Untitled issue") }),
    el("p", { class: "doc-sub", text: r ? (kind === "revise" ? "only the blocks you commented on are sent back"
      : kind === "proof" ? "checking spelling and grammar · the fixes show on Proof"
      : "your words are locked · only prompts are being written")
      : "read it through · Edit any block to change it · saving locks it as your words" }));
  const flow = el("div", { class: "flow", id: "flow" });
  const empty = !S.blocks.some((b) => b.type !== "comment");
  const next = el("div", { class: "cta-row" },
    !r && !empty ? el("button", { class: "cta", type: "button", onclick: () => go("review") }, "Proof it →") : null,
    empty ? el("span", { class: "cta-note", text: "nothing here yet · start on Plan" }) : null);
  $("#main").replaceChildren(head, flow, next);
  renderBuildFlow();
}

function renderBuildFlow() {
  const flow = $("#flow");
  if (!flow) return;
  const kids = [];
  const now = Date.now();
  const r = running();
  let editShown = false;
  for (const b of S.blocks) {
    const st = S.jobBlocks[b.id];
    if (b.type === "comment") continue;
    if (b.type === "prompt") {
      const d = draftFor(b.id);
      if (st && st.status === "writing") kids.push(filling(isPicture(b.text) ? "Describing the picture" : isChart(b.text) ? "Working out the chart" : "Writing from your prompt", b.text));
      else if (st && st.status === "drawing") kids.push(filling(isChart(b.text) ? "Drawing the chart" : "Drawing the picture", b.text));
      else if (st && st.status === "failed" && !d) kids.push(el("div", { class: "failed", "data-id": b.id },
        el("span", { class: "tag" }, "Not written",
          !r ? el("button", { class: "ghost small", type: "button", text: "Write it", onclick: () => generate([b.id]) }) : null),
        el("div", { class: "detail", text: st.detail }), el("div", { class: "instr", text: b.text })));
      else if (st && st.status === "failed") kids.push(el("div", { class: "failed", "data-id": b.id }, el("span", { class: "tag", text: "Not rewritten: the draft below is the one from before" }), el("div", { class: "detail", text: st.detail })));
      else if (!d) kids.push(el("div", { class: "waiting", "data-id": b.id },
        el("span", { class: "tag" }, "Not written yet",
          !r ? el("button", { class: "ghost small", type: "button", text: "Write it", onclick: () => generate([b.id]) }) : null),
        el("div", { class: "instr", text: b.text })));
      continue;
    }
    if (st && st.status === "writing") { kids.push(filling("Revising from your comments", "")); continue; }
    if (st && st.status === "drawing") { kids.push(filling("Redrawing the picture", "")); continue; }
    if (st && st.status === "failed" && b.type !== "prose") kids.push(el("div", { class: "failed" }, el("span", { class: "tag", text: "Revision discarded" }), el("div", { class: "detail", text: st.detail })));
    if (S.edit && !S.edit.dialog && S.edit.id === b.id) { kids.push(S.edit.node); editShown = true; continue; }
    // A draft being rewritten: its prompt's progress card above stands in its place.
    const redo = b.type === "draft" && S.jobBlocks[b.attrs.for];
    if (redo && (redo.status === "writing" || redo.status === "drawing")) continue;
    const node = draftBlock(b, r);
    const fresh = b.type === "draft" && S.reveal[b.attrs.for] && now - S.reveal[b.attrs.for] < 4000;
    if (fresh) { typeIn(node.querySelector(".body")); S.reveal[b.attrs.for] = 0; }   // once, not on every repaint
    if (st && st.status === "proposed") node.append(el("p", { class: "muted", text: "↳ a change to this was proposed; see Proof" }));
    kids.push(node);
  }
  // An open box whose block went away (removed on another computer) keeps its text.
  if (S.edit && !S.edit.dialog && !editShown) kids.push(S.edit.node);
  flow.replaceChildren(...kids);
  if (S.edit && !S.edit.dialog) autosize(S.edit.ta);
}

/* One block on Draft: whose words these are, the text, and what can be done to it. */
function draftBlock(b, r) {
  const act = (text, fn, cls, title) => el("button", { class: "ghost small" + (cls ? " " + cls : ""), type: "button", text, title, disabled: r, onclick: fn });
  const body = el("div", { class: "body" });
  body.innerHTML = b.text.trim() ? md(b.text) : '<span class="empty-view">empty</span>';
  let tag, from = null, acts;
  if (b.type === "draft") {
    const stale = b.attrs.stale === "1";
    const p = S.blocks.find((x) => x.type === "prompt" && x.id === b.attrs.for);
    tag = stale ? "Out of date: its prompt changed" : b.attrs.picture === "1" ? "Draft · picture drawn from your prompt"
      : b.attrs.chart === "1" ? "Draft · chart drawn from your prompt" : "Draft · written from your prompt";
    // The whole prompt, clamped to three lines until clicked, so it can be checked against the draft.
    if (p) from = el("p", { class: "from clamp", text: "Prompt: " + p.text, title: "click to show all of it",
      onclick: (e) => e.currentTarget.classList.toggle("clamp") });
    acts = [act("Edit", () => openEditor(b)),
      act("Lock", () => keepDraft(b), null, "keep it exactly as it is, as your words"),
      act(stale ? "Rewrite it" : "Rewrite", () => generate([b.attrs.for]), null, "write it again from the prompt"),
      b.attrs.picture === "1" || b.attrs.chart === "1" ? act("Use my own picture", () => pictureDialog({ replace: b.id }), null, "upload your own picture in place of this one") : null,
      act("Discard", () => deleteBlock(b), "danger")];
  } else if (b.type === "prose") {
    tag = "Your words · locked";
    acts = [act("Edit", () => openEditor(b))];
  } else {
    tag = b.attrs.class || "component";
    acts = [act("Edit", () => openEditor(b))];
  }
  const cls = `dblk ${b.type}` + (b.type === "draft" && b.attrs.stale === "1" ? " stale" : "") + (b.type === "prose" && /^#{1,6}\s/.test(b.text) ? " heading" : "");
  const srcs = ((S.sources || {})[b.id] || []).filter((x) => /^https:\/\//.test(x.url || ""));
  const found = srcs.length ? el("p", { class: "sources" }, "Sources: ",
    ...srcs.map((x, i) => [i ? " · " : "", el("a", { href: x.url, target: "_blank", rel: "noopener noreferrer", text: x.title || x.url }),
      x.snippet_only ? el("span", { class: "muted", text: " (from the search result; the page could not be read)" }) : null]).flat()) : null;
  return el("div", { class: cls, "data-id": b.id },
    el("span", { class: "tag" }, el("span", { text: tag }), el("span", { class: "acts" }, ...acts.filter(Boolean))), from, body, found);
}

/* ── the Edit box (Draft in place, Proof in a dialog) ──────────────────── */
/* The block changed somewhere else while the box was open. Show that version under the
   box; once seen, Save replaces it with what is in the box (the author merged by hand). */
function offerNewVersion(e, current) {
  e.node.querySelector(".other-version")?.remove();
  const btn = el("button", { class: "ghost small", type: "button", text: "Show the new version under mine", onclick: () => {
    e.orig = current;
    btn.replaceWith(el("div", { class: "other-version" },
      el("p", { class: "edit-msg", text: "The version saved elsewhere. Copy what you want into your box above, then Save: it replaces this one." }),
      el("pre", { text: current })));
  } });
  e.node.querySelector(".edit-msg").after(el("div", { class: "other-version" }, btn));
}
function editDirty() { return !!(S.edit && S.edit.ta.value !== S.edit.start); }
function closeEditor() {
  const e = S.edit;
  if (!e) return;
  S.edit = null;
  if (e.dialog && !$("#modal").hidden) closeModal(true);
  else if (S.screen === "build") renderBuildFlow();
}
function editMsg(text, bad) {
  const m = S.edit && S.edit.node.querySelector(".edit-msg");
  if (m) { m.textContent = text; m.classList.toggle("bad", !!bad); }
}
function openEditor(b, dialog) {
  if (running()) { toast("The model is working; edit when it finishes.", true); return; }
  if (S.edit && S.edit.id === b.id && !!S.edit.dialog === !!dialog) { S.edit.ta.focus(); return; }
  if (editDirty()) { toast("Save or cancel the text you are already editing first.", true); S.edit.ta.focus(); return; }
  closeEditor();
  const lock = b.type === "draft";
  const ta = el("textarea", { class: "edit-box", spellcheck: true, "aria-label": "The text of this block, in Markdown" });
  ta.value = b.text;
  const save = el("button", { class: "send", type: "button", text: lock ? "Save & lock" : "Save", onclick: () => saveEdit() });
  const cancel = el("button", { class: "ghost", type: "button", text: "Cancel", onclick: () => closeEditor() });
  const node = el("div", { class: "dblk editing " + b.type, "data-id": b.id },
    dialog ? null : el("span", { class: "tag" }, el("span", { text: lock ? "Editing the draft" : "Editing" })),
    ta,
    el("p", { class: "edit-msg", role: "status", text: lock ? "Saving makes it your words: the model will only suggest changes to it." : "" }),
    el("div", { class: "edit-row" }, cancel, save));
  ta.addEventListener("input", () => { if (!dialog) autosize(ta); });
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); saveEdit(); }
    else if (e.key === "Escape" && !dialog) { e.preventDefault(); if (!editDirty()) closeEditor(); else editMsg("Unsaved changes: Save, or Cancel to throw them away.", true); }
  });
  S.edit = { id: b.id, type: b.type, orig: b.text, start: b.text, ta, node, dialog: !!dialog, busy: false, save };
  if (dialog) {
    openModal(b.type === "draft" ? "Edit the draft" : "Edit your words", node);
    setTimeout(() => { ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); }, 0);
  } else {
    renderBuildFlow();
    ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length);
  }
}
async function saveEdit() {
  const e = S.edit;
  if (!e || e.busy) return;
  const slug = S.slug, text = e.ta.value;
  if (running()) { editMsg("The model is working. Save when it finishes; your text stays here.", true); return; }
  if (!text.trim()) { editMsg("The box is empty. To remove the block, use Discard on Draft or Delete on Plan.", true); return; }
  if (S.dirty && !(await flush())) { editMsg("Other changes could not be saved first: the problem is shown at the top.", true); return; }
  e.busy = true; e.save.disabled = true; editMsg("Saving…");
  S.fixBusy = true;
  try {
    const r = await api(`/api/issues/${encodeURIComponent(slug)}/blocks/${encodeURIComponent(e.id)}/edit`, { method: "POST", body: { orig: e.orig, text } });
    if (S.edit === e) closeEditor();
    if (slug !== S.slug) return;
    if (r.rev) S.rev = r.rev;
    toast(r.unchanged ? "No changes." : e.type === "draft" ? "Saved and locked: these are your words now." : "Saved.");
    await reloadDoc({ keepFrame: true });
  } catch (err) {
    // The box stays open with the author's text in it, whatever went wrong.
    editMsg(err.message, true);
    if (err.status === 409 && err.data && typeof err.data.current === "string" && S.edit === e) offerNewVersion(e, err.data.current);
  } finally {
    e.busy = false; e.save.disabled = false;
    S.fixBusy = false;
  }
}

function filling(label, instr) {
  const since = S.job && S.job.started ? Math.max(0, Date.now() / 1000 - S.job.started) : 0;
  return el("div", { class: "filling" },
    el("span", { class: "tag" }, label, " · ", el("span", { class: "elapsed", text: mmss(since) })),
    instr ? el("div", { class: "instr", style: "font:14px var(--sans);color:var(--ink-3);margin-bottom:6px", text: instr }) : null,
    el("div", { class: "filling-mill" }, millMark("working"), el("div", { class: "bars" }, el("i"), el("i"), el("i"))),
    el("span", { class: "cursor", "aria-hidden": "true" }));
}

function typeIn(node) {
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
  const texts = [];
  while (walker.nextNode()) texts.push([walker.currentNode, walker.currentNode.textContent]);
  texts.forEach(([n]) => { n.textContent = ""; });
  const total = texts.reduce((a, [, t]) => a + t.length, 0);
  const per = Math.max(3, Math.ceil(total / 90));
  let ti = 0, ci = 0;
  const step = () => {
    let budget = per;
    while (budget > 0 && ti < texts.length) {
      const [n, t] = texts[ti];
      const take = Math.min(budget, t.length - ci);
      n.textContent += t.slice(ci, ci + take);
      ci += take; budget -= take;
      if (ci >= t.length) { ti++; ci = 0; }
    }
    if (ti < texts.length && node.isConnected) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

function appendLog(ev) {
  const box = $("#runlog");
  if (!box) return;
  box.querySelector(".live")?.remove();
  box.append(logLine(ev));
  if (running()) box.append(el("span", { class: "live", text: "› working · " + mmss(Date.now() / 1000 - (S.job.started || Date.now() / 1000)) }));
  box.scrollTop = box.scrollHeight;
}
function logLine(ev) {
  const mark = { ok: "✓", warn: "!", error: "✗", info: "·" }[ev.level] || "·";
  const t = S.job && S.job.started ? mmss(Math.max(0, ev.ts - S.job.started)) : "";
  return el("span", { class: ev.level }, el("span", { class: "t", text: t }), `${mark} ${ev.text}`);
}

function tick() {
  if (!running()) return;
  const since = Date.now() / 1000 - (S.job.started || Date.now() / 1000);
  document.querySelectorAll(".elapsed").forEach((n) => { n.textContent = mmss(since); });
  const live = $("#runlog .live");
  if (live) live.textContent = "› working · " + mmss(since);
}

/* A small Markdown reader for the editor and build views. It only has to be readable:
   the real render, through the pack, is on the Proof screen. */
function accentStyle(name) {
  const c = (S.accents && S.accents[name]) || (S.app && S.app.accents && S.app.accents[name]);
  return c && /^#[0-9a-fA-F]{3,8}$/.test(c) ? ` style="color:${c}"` : "";
}
function stripFence(t) {
  return t.split("\n").filter((l) => !/^:{3,}/.test(l.trim()) && !/^\{#[^}]*\}$/.test(l.trim())).join("\n");
}
function md(src, forEditor) {
  const link = forEditor ? '<span class="lnk">$1</span>' : '<a href="$2" target="_blank" rel="noopener">$1</a>';
  const inline = (s) => esc(s)
    .replace(/!\[((?:\\.|[^\]\\])*)\]\(([A-Za-z0-9._-]+|https?:[^)\s]+)\)(\{[^}]*\})?/g, (m, alt, src) => {
      alt = alt.replace(/\\(.)/g, "$1");
      const url = /^https?:/.test(src) ? src : `/api/issues/${encodeURIComponent(S.slug)}/asset/${encodeURIComponent(src)}`;
      return /\.(mp4|webm)$/i.test(src) ? `<span class="cap">▶ video: ${alt}</span>` : `<img src="${url}" alt="${alt}" loading="lazy">`;
    })
    .replace(/\*\*([^*]+)\*\*\{\.(\w+)\}/g, (m, t, a) => `<strong${accentStyle(a)}>${t}</strong>`)
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>")
    .replace(/\[([^\]]+)\]\{\.(\w+)\}/g, (m, t, a) => `<span${accentStyle(a)}>${t}</span>`)
    .replace(/\[([^\]]+)\]\(<?(https?:[^)\s>]+)>?\)(\{[^}]*\})?/g, link)
    .replace(/\\([\\*_`\[\]{}<>#.!-])/g, "$1");
  const out = [];
  for (const para of src.split(/\n[ \t]*\n/)) {
    const p = para.split("\n").filter((l) => !/^:{3,}\s*$/.test(l.trim())).join("\n").trim();
    if (!p) continue;
    if (/^:::/.test(p)) {
      // A component: its label, and what is inside it (a figure shows its picture).
      const lines = p.split("\n");
      const inner = lines.slice(1).filter((l) => !/^:{3,}\s*$/.test(l.trim())).join("\n").trim();
      // "{.poem label="A Poem" #b-x1}" reads as "poem · A Poem"
      const head = lines[0].replace(/^:+\s*/, "");
      const cls = (head.match(/\.([\w-]+)/) || [, "box"])[1];
      const lab = (head.match(/label="([^"]*)"/) || [])[1];
      const label = esc(cls + (lab ? " · " + lab : ""));
      out.push(/^\{\.figure\b/.test(lines[0].replace(/^:+\s*/, "")) && inner ? `<div class="fig">${inline(inner)}</div>`
        : `<div class="comp">${label}</div>` + (inner ? `<p>${inline(inner)}</p>` : ""));
      continue;
    }
    if (/^!\[/.test(p)) { out.push(`<div class="fig">${inline(p)}</div>`); continue; }
    if (/^#{1,6}\s/.test(p)) { out.push(`<h3>${inline(p.replace(/^#+\s*/, ""))}</h3>`); continue; }
    if (/^[-*]\s/.test(p)) { out.push("<ul>" + p.split("\n").map((l) => `<li>${inline(l.replace(/^[-*]\s+/, ""))}</li>`).join("") + "</ul>"); continue; }
    if (/^>\s?/.test(p)) { out.push(`<blockquote>${inline(p.replace(/^>\s?/gm, ""))}</blockquote>`); continue; }
    out.push(`<p>${inline(p)}</p>`);
  }
  return out.join("");
}

/* ── review ────────────────────────────────────────────────────────────── */
/* The issue is drawn inside a sandboxed frame, with the design's own CSS. No script can run
   in it (sandbox without allow-scripts, and the page's CSP); this page reaches into it to
   read selections and to put notes in the text, which same-origin allows. */
function renderReview() {
  const drawnWith = (S.preview && S.preview.design) || S.design || "";
  const sub = el("p", { class: "doc-sub" }, "click a block or select words to comment or edit · drawn with the design ",
    el("b", { id: "drawn-with", text: drawnWith }),
    S.designMissing ? el("span", { class: "warn-note", text: " (this issue's own design is missing, so the default drew it)" }) : null);
  const head = el("div", { class: "doc-head" },
    el("h1", { class: "doc-title", text: (S.meta.title || "Issue") + " · proof" }), sub);
  const frame = el("iframe", { class: "preview-frame", id: "preview-frame", title: "The issue as it will look",
    sandbox: "allow-same-origin allow-popups allow-popups-to-escape-sandbox" });
  $("#main").replaceChildren(head, el("div", { id: "problems" }), el("div", { class: "preview-wrap" }, frame));
  if (S.preview) paintPreview();
}

async function loadPreview() {
  const slug = S.slug, gen = S.loadGen, mine = ++S.previewGen;
  try {
    const p = await api(`/api/issues/${encodeURIComponent(slug)}/preview`);
    if (slug !== S.slug || gen !== S.loadGen || mine !== S.previewGen) return;   // a newer one is coming
    S.preview = p;
    if (S.screen === "review") { paintPreview(); renderSide(); }
  } catch (e) { banner(e.message, true); }
}

function effectiveTheme() {
  const t = document.documentElement.getAttribute("data-theme");
  return t || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
}
function frameDoc() {
  const f = $("#preview-frame");
  try { return f && f.contentDocument && f.contentDocument.getElementById("issue-body") ? f.contentDocument : null; }
  catch (e) { return null; }
}

function paintPreview() {
  const frame = $("#preview-frame");
  if (!frame || !S.preview) return;
  const p = S.preview;
  const dw = $("#drawn-with");
  if (dw && p.design) dw.textContent = p.design;
  const y = window.scrollY;
  frame.onload = () => {
    const d = frameDoc();
    if (!d) return;
    d.querySelectorAll("a[href]").forEach((a) => { a.target = "_blank"; a.rel = "noopener noreferrer"; });
    d.addEventListener("mouseup", (e) => setTimeout(() => onFrameUp(e), 0));
    d.addEventListener("touchend", (e) => setTimeout(() => onFrameUp(e.changedTouches && e.changedTouches[0] ? { target: e.target, clientX: e.changedTouches[0].clientX, clientY: e.changedTouches[0].clientY } : e), 300));
    d.addEventListener("keyup", (e) => { if (e.key === "Escape") hidePop(); if (e.key === "Shift") onFrameUp({ target: d.body }); });
    d.addEventListener("click", onFrameClick);
    d.addEventListener("pointerup", onFramePointerUp);
    d.querySelectorAll("img, video").forEach((m) => m.addEventListener("load", sizeFrame));
    try { new ResizeObserver(sizeFrame).observe(d.body); } catch (e) { /* the image load events still size it */ }
    highlightComments();
    highlightFixes();
    renderNotes();
    sizeFrame();
    window.scrollTo({ top: y });
  };
  frame.srcdoc = `<!doctype html><html data-theme="${effectiveTheme()}"><head><meta charset="utf-8">`
    + `<meta name="viewport" content="width=device-width,initial-scale=1"><style>${p.page_css || ""}</style>${p.site_head || ""}`
    + `<link rel="stylesheet" href="/static/frame.css"></head><body><div class="issue-body" id="issue-body">${p.html}</div></body></html>`;
  const real = (p.problems || []).filter((x) => !/^front matter/.test(x.text));
  const box = $("#problems");
  if (box) box.replaceChildren(...(real.length ? [el("div", { class: "panel", style: "border-color:var(--stop);margin-bottom:14px" },
    el("h4", { style: "margin:0 0 6px;font:500 10.5px var(--mono);letter-spacing:.2em;text-transform:uppercase;color:var(--stop)", text: `${real.length} problem${real.length > 1 ? "s" : ""} in the source` }),
    ...real.slice(0, 8).map((x) => el("div", { style: "font-size:13px;color:var(--stop)", text: (x.line ? `line ${x.line}: ` : "") + x.text })))] : []));
}

/* The frame is as tall as the issue. Measured from where the issue's content ends, not from
   the document's scroll height: a design whose page is min-height:100vh would otherwise grow
   the frame, which grows 100vh, which grows the frame, forever. */
function sizeFrame() {
  const f = $("#preview-frame"), d = frameDoc();
  if (!f || !d) return;
  const body = d.getElementById("issue-body");
  const w = d.defaultView;
  const pad = (n) => { const cs = w.getComputedStyle(n); return (parseFloat(cs.paddingBottom) || 0) + (parseFloat(cs.marginBottom) || 0) + (parseFloat(cs.borderBottomWidth) || 0); };
  const h = Math.min(400000, Math.ceil(body.getBoundingClientRect().bottom + w.scrollY + pad(body) + pad(d.body) + pad(d.documentElement)) + 4);
  if (Math.abs((parseInt(f.style.height, 10) || 0) - h) > 2) f.style.height = h + "px";
}

/* Repaint only the marks and notes (after a note is added or removed), not the whole frame. */
function refreshMarks() {
  const d = frameDoc();
  if (!d) return;
  d.querySelectorAll("mark.hl, mark.fix").forEach((m) => { const p = m.parentNode; while (m.firstChild) p.insertBefore(m.firstChild, m); p.removeChild(m); p.normalize(); });
  highlightComments();
  highlightFixes();
  renderNotes();
  sizeFrame();
}

function highlightComments() {
  const d = frameDoc();
  if (!d) return;
  const body = d.getElementById("issue-body");
  for (const c of S.review.comments) {
    if (!c.quote || c.status === "resolved") continue;
    let skip = c.nth || 0;
    for (const part of body.querySelectorAll(`[data-block="${CSS.escape(c.block || "")}"]`)) {
      const r = markText(part, c.quote, c.status === "sent" ? "hl sent" : "hl", skip, c.id);
      if (r === true) break;
      skip -= r;          // occurrences passed over in this element
    }
  }
}

/* Mark the (skip+1)th occurrence of quote inside root. Returns true when marked, else the
   number of occurrences found here, so the caller can carry the count to the next element. */
function markText(root, quote, cls, skip = 0, cid = "", fid = "", title = "") {
  // The page is typeset: ' and " come out curly, ... as … and -- as a dash. Quotes map one
  // character to one, so they are straightened on both sides; the rest is typeset in the needle.
  const needle = straight(quote).replace(/\.\.\./g, "…").replace(/---/g, "—").replace(/--/g, "–").replace(/\s+/g, " ").trim();
  if (!needle) return 0;
  const doc_ = root.ownerDocument;
  const walker = doc_.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const nodes = [];
  let full = "";
  while (walker.nextNode()) {
    if (walker.currentNode.parentElement && walker.currentNode.parentElement.closest(".cmp-note")) continue;
    nodes.push([walker.currentNode, full.length]); full += walker.currentNode.textContent;
  }
  const flat = straight(full).replace(/\s+/g, " ");
  // map positions in the collapsed string back to the raw one
  const map = [];
  for (let i = 0, j = 0; i < full.length; i++) {
    if (/\s/.test(full[i]) && i > 0 && /\s/.test(full[i - 1])) continue;
    map[j++] = i;
  }
  let at = -1, seen = 0;
  for (let from = 0; ; ) {
    const i = flat.indexOf(needle, from);
    if (i < 0) break;
    if (seen === skip) { at = i; break; }
    seen++; from = i + 1;
  }
  if (at < 0) return seen;
  const start = map[at], end = map[at + needle.length - 1] + 1;
  for (const [node, off] of nodes) {
    const nEnd = off + node.textContent.length;
    if (nEnd <= start || off >= end) continue;
    const s0 = Math.max(0, start - off), e0 = Math.min(node.textContent.length, end - off);
    const range = doc_.createRange();
    range.setStart(node, s0); range.setEnd(node, e0);
    const m = doc_.createElement("mark");
    m.className = cls;
    if (cid) m.dataset.cid = cid;
    if (fid) m.dataset.fid = fid;
    if (title) m.title = title;
    try { range.surroundContents(m); } catch (err) { /* crosses an element; skip that piece */ }
  }
  return true;
}

/* A queued note sits in the text, directly under the block it is about. */
function renderNotes() {
  const d = frameDoc();
  if (!d) return;
  d.querySelectorAll(".cmp-note").forEach((n) => n.remove());
  const body = d.getElementById("issue-body");
  const queued = S.review.comments.filter((c) => c.status === "queued");
  const byBlock = new Map();
  for (const c of queued) {
    if (!byBlock.has(c.block || "")) byBlock.set(c.block || "", []);
    byBlock.get(c.block || "").push(c);
  }
  for (const [block, list] of byBlock) {
    const parts = block ? body.querySelectorAll(`[data-block="${CSS.escape(block)}"]`) : [];
    let anchor = parts.length ? parts[parts.length - 1] : null;
    for (const c of list) {
      const n = d.createElement("div");
      n.className = "cmp-note";
      n.dataset.cid = c.id;
      const who = d.createElement("span");
      who.className = "cmp-who";
      who.textContent = c.quote ? `your note · on “${c.quote.length > 60 ? c.quote.slice(0, 60) + "…" : c.quote}”` : "your note · on this block";
      const t = d.createElement("p");
      t.className = "cmp-text";
      t.textContent = c.note;
      const x = d.createElement("button");
      x.className = "cmp-x"; x.type = "button"; x.textContent = "Remove"; x.dataset.del = c.id;
      n.append(who, t);
      if (c.error) { const e = d.createElement("p"); e.className = "cmp-err"; e.textContent = "Not done last time: " + c.error; n.append(e); }
      n.append(x);
      if (anchor) { anchor.after(n); anchor = n; } else body.append(n);
    }
  }
}

/* A spelling fix switches when the highlight is pressed and released: pointerup covers a
   mouse, a finger and a pen alike, where a click event is not always sent for a tap on plain
   text inside the frame. The click that follows is then ignored. */
function onFramePointerUp(e) {
  const t = e.target && e.target.nodeType === 1 ? e.target : e.target && e.target.parentElement;
  const fixMark = t && t.closest && t.closest("mark.fix[data-fid]");
  if (!fixMark || e.button > 0) return;
  const d = frameDoc();
  const sel = d && d.getSelection();
  if (sel && !sel.isCollapsed && sel.toString().trim()) return;     // selecting words, not switching
  const f = proofFixes().find((x) => x.id === fixMark.dataset.fid);
  if (!f) return;
  S.fixPressed = Date.now();
  toggleFix(f);
}
function onFrameClick(e) {
  const t = e.target;
  const del = t.closest && t.closest("[data-del]");
  if (del) { e.preventDefault(); removeComment(del.dataset.del); return; }
  const fixMark = t.closest && t.closest("mark.fix[data-fid]");
  if (fixMark) {
    e.preventDefault();
    if (Date.now() - (S.fixPressed || 0) < 1000) return;            // already switched on pointerup
    const sel = frameDoc()?.getSelection();
    if (sel && !sel.isCollapsed && sel.toString().trim()) return;   // a selection, not a press
    const f = proofFixes().find((x) => x.id === fixMark.dataset.fid);
    if (f) toggleFix(f);
    return;
  }
  const mark = t.closest && t.closest("mark.hl:not(.sent)");
  if (mark && mark.dataset.cid) {
    const d = frameDoc();
    const n = d && d.querySelector(`.cmp-note[data-cid="${CSS.escape(mark.dataset.cid)}"]`);
    if (n) {
      const f = $("#preview-frame").getBoundingClientRect();
      const r = n.getBoundingClientRect();
      window.scrollTo({ top: window.scrollY + f.top + r.top - 120, behavior: "smooth" });
      n.classList.add("flash"); setTimeout(() => n.classList.remove("flash"), 1400);
    }
  }
}

async function removeComment(cid) {
  try {
    await api(`/api/issues/${encodeURIComponent(S.slug)}/comments/${encodeURIComponent(cid)}`, { method: "DELETE" });
    const st = await api(`/api/issues/${encodeURIComponent(S.slug)}`);
    S.review = st.review;
    refreshMarks(); renderSide();
  } catch (err) { toast(err.message, true); }
}

/* comment popover */
function setupCommentPop() {
  const pop = $("#comment-pop");
  document.addEventListener("mousedown", (e) => { if (!pop.contains(e.target) && $("#pop-form").hidden) hidePop(); });
  document.addEventListener("keyup", (e) => { if (e.key === "Escape") hidePop(); });
  $("#pop-edit").addEventListener("click", () => {
    const b = S.pending && editableBlock(S.pending.block);
    hidePop();
    if (b) openEditor(b, true);
  });
  $("#pop-open").addEventListener("click", () => {
    $("#pop-open").hidden = true; $("#pop-edit").hidden = true; $("#pop-form").hidden = false;
    $("#pop-quote").textContent = S.pending.quote || "The whole block";
    $("#pop-note").value = ""; $("#pop-note").focus();
  });
  $("#pop-cancel").addEventListener("click", hidePop);
  $("#pop-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const note = $("#pop-note").value.trim();
    if (!note || !S.pending) return;
    try {
      await api(`/api/issues/${encodeURIComponent(S.slug)}/comments`, { method: "POST", body: { block: S.pending.block, quote: S.pending.quote, nth: S.pending.nth, note } });
      hidePop();
      const d = frameDoc();
      if (d) d.getSelection().removeAllRanges();
      const st = await api(`/api/issues/${encodeURIComponent(S.slug)}`);
      S.review = st.review;
      refreshMarks(); renderSide();
    } catch (err) { toast(err.message, true); }
  });
  $("#pop-note").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) $("#pop-form").requestSubmit(); });
}

/* After a mouse-up or a tap in the frame: words selected → comment on them; a plain click
   on a block → offer a note on the whole block. */
function onFrameUp(e) {
  if (S.screen !== "review" || running() || !$("#pop-form").hidden) return;
  const d = frameDoc();
  if (!d) return;
  const t = e && e.target && e.target.nodeType === 1 ? e.target : e && e.target && e.target.parentElement;
  if (t && t.closest && t.closest("a, button, .cmp-note, mark.hl, mark.fix, video")) return;
  const body = d.getElementById("issue-body");
  const sel = d.getSelection();
  const blockOf = (n) => (n.nodeType === 1 ? n : n.parentElement).closest("[data-block]");
  if (!sel || sel.isCollapsed || !sel.rangeCount) {
    const b = t && t.closest && t.closest("[data-block]");
    if (!b || !body.contains(b) || b.dataset.kind === "prompt" || e.clientX === undefined) { hidePop(); return; }
    // A block that failed to compile can be fixed by hand, but not commented on.
    S.pending = { block: b.dataset.block, quote: "", nth: 0, editOnly: b.dataset.kind === "error" };
    placePop(e.clientX, e.clientY + 14, "Comment", "Edit text");
    return;
  }
  const range = sel.getRangeAt(0);
  if (!body.contains(range.commonAncestorContainer)) { hidePop(); return; }
  const startBlock = blockOf(range.startContainer), endBlock = blockOf(range.endContainer);
  const quote = sel.toString().replace(/\s+/g, " ").trim();
  if (!startBlock || !quote) { hidePop(); return; }
  if (!endBlock || endBlock.dataset.block !== startBlock.dataset.block) {
    hidePop(); toast("Select text inside one paragraph or block to comment on it."); return;
  }
  // Which occurrence of these words in the block, so a repeated phrase is not confused.
  const parts = [...body.querySelectorAll(`[data-block="${CSS.escape(startBlock.dataset.block)}"]`)];
  const pre = d.createRange();
  pre.setStart(parts[0], 0); pre.setEnd(range.startContainer, range.startOffset);
  const whole = d.createRange();
  whole.setStart(parts[0], 0); whole.setEnd(parts[parts.length - 1], parts[parts.length - 1].childNodes.length);
  const before = pre.toString().replace(/\s+/g, " ").length;
  const full = whole.toString().replace(/\s+/g, " ");
  let nth = 0;
  for (let i = full.indexOf(quote); i >= 0 && i < before - 1; i = full.indexOf(quote, i + 1)) nth++;
  S.pending = { block: startBlock.dataset.block, quote: quote.slice(0, 600), nth };
  const rect = range.getBoundingClientRect();
  placePop(rect.left, rect.bottom + 8, "Comment", "Edit this block");
}
function editableBlock(id) {
  return S.blocks.find((x) => x.id === id && ["prose", "draft", "component"].includes(x.type)) || null;
}
function placePop(x, y, label, editLabel) {
  const f = $("#preview-frame").getBoundingClientRect();
  const pop = $("#comment-pop");
  pop.hidden = false; $("#pop-form").hidden = true;
  $("#pop-open").hidden = !!(S.pending && S.pending.editOnly);
  $("#pop-open").textContent = label;
  $("#pop-edit").hidden = !(S.pending && editableBlock(S.pending.block));
  $("#pop-edit").textContent = editLabel || "Edit text";
  if ($("#pop-open").hidden && $("#pop-edit").hidden) { pop.hidden = true; return; }
  const left = Math.min(window.scrollX + f.left + x, window.scrollX + document.documentElement.clientWidth - 340);
  pop.style.left = Math.max(8, left) + "px";
  pop.style.top = window.scrollY + f.top + y + "px";
}
function hidePop() {
  $("#comment-pop").hidden = true; $("#pop-form").hidden = true; $("#pop-open").hidden = false; $("#pop-edit").hidden = false;
}

/* word diff for proposals */
function wordDiff(a, b) {
  const A = a.split(/(\s+)/), B = b.split(/(\s+)/);
  if (A.length * B.length > 250000) return `<del>${esc(a)}</del> <ins>${esc(b)}</ins>`;
  const dp = Array.from({ length: A.length + 1 }, () => new Uint16Array(B.length + 1));
  for (let i = A.length - 1; i >= 0; i--) for (let j = B.length - 1; j >= 0; j--)
    dp[i][j] = A[i] === B[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  let i = 0, j = 0, out = "";
  while (i < A.length && j < B.length) {
    if (A[i] === B[j]) { out += esc(A[i]); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { out += `<del>${esc(A[i])}</del>`; i++; }
    else { out += `<ins>${esc(B[j])}</ins>`; j++; }
  }
  while (i < A.length) out += `<del>${esc(A[i++])}</del>`;
  while (j < B.length) out += `<ins>${esc(B[j++])}</ins>`;
  return out;
}

async function decide(p, action) {
  try {
    await api(`/api/issues/${encodeURIComponent(S.slug)}/proposals/${p.id}/${action}`, { method: "POST", body: {} });
    toast(action === "accept" ? "Accepted: your paragraph now reads the new way." : "Rejected: your words stay as they were.");
    await reloadDoc();
  } catch (e) { toast(e.message, true); await reloadDoc(); }
}

async function undo() {
  const slug = S.slug;
  if (S.undoArmed !== slug) {
    S.undoArmed = slug; renderSide();
    setTimeout(() => { if (S.undoArmed === slug) { S.undoArmed = false; renderSide(); } }, 5000);
    return;
  }
  S.undoArmed = false;
  try {
    const r = await api(`/api/issues/${encodeURIComponent(S.slug)}/undo`, { method: "POST", body: {} });
    toast("Undone: the issue is back to how it was before the last model pass.");
    await reloadDoc();
  } catch (e) { toast(e.message, true); }
}

/* ── side panel ────────────────────────────────────────────────────────── */
function renderSide() {
  const top = $("#side-top");
  if (S.screen === "compose") top.replaceChildren(...sideCompose());
  else if (S.screen === "build") top.replaceChildren(...sideBuild());
  else top.replaceChildren(...sideReview());
  paintChatButtons();
}

function sideCompose() {
  const c = counts();
  const fields = (S.fields || []).map((f) => {
    const req = (S.required || []).includes(f);
    const long = f === "preview_text";
    const input = el(long ? "textarea" : "input", { id: "f-" + f, rows: long ? 3 : null, disabled: running(),
      oninput: (e) => { S.meta[f] = e.target.value; if (f === "title") { const t = $(".doc-title"); if (t && t !== e.target) t.value = e.target.value; } markDirty(); } });
    input.value = S.meta[f] == null ? "" : S.meta[f];
    return el("div", { class: "field" }, el("label", { for: "f-" + f }, f.replace(/_/g, " "), req ? el("span", { class: "req", text: " *" }) : null), input);
  });
  return [voicePanel(), designPanel(),
    el("div", {}, el("h4", { text: "What counts" }), el("div", { class: "panel kv" },
      el("b", { text: String(c.prose) }), el("span", { text: "prose blocks, sent as-is" }),
      el("b", { text: String(c.prompt) }), el("span", { text: `prompt${c.prompt === 1 ? "" : "s"} · ${c.write} to write` }),
      el("b", { text: String(c.draft) }), el("span", { text: `draft${c.draft === 1 ? "" : "s"}${c.stale ? ` · ${c.stale} out of date` : ""}` }),
      el("b", { text: String(c.words) }), el("span", { text: "words so far" }))),
    el("details", { class: "meta" }, el("summary", { text: "Issue details" }), el("div", { class: "panel" }, ...fields)),
  ];
}

/* The voice this issue is written in: which pack, how much of the request it takes. */
function voicePanel() {
  const v = S.voiceInfo;
  const names = (S.app.voices || []);
  const sel = el("select", { "aria-label": "Voice pack", disabled: running() || !names.length,
    onchange: (e) => setIssue({ voice: e.target.value }) },
    ...names.map((n) => el("option", { value: n, text: n, selected: v && v.name === n })));
  const on = v ? v.files.filter((f) => f.on && !f.missing) : [];
  const limit = S.nextRequest ? S.nextRequest.limit : null;
  return el("div", {}, el("h4", { text: "Voice" }), el("div", { class: "panel" },
    el("div", { class: "pick-row" }, sel, el("button", { class: "ghost small", type: "button", text: "Manage", onclick: () => voiceManager(v && v.name) })),
    v ? el("p", { class: "hint" }, `${on.length} of ${v.files.length} files on · ${kb(v.bytes)} a pass` + (limit ? ` · the writer takes ${kb(limit)} with the issue` : "")) :
      el("p", { class: "hint", text: "no voice pack yet: Manage → New voice pack" }),
    limit && v ? budgetBar(v.bytes, S.nextRequest.bytes || v.bytes, limit) : null));
}
function budgetBar(voiceBytes, total, limit) {
  const pv = Math.min(100, voiceBytes / limit * 100), pt = Math.min(100, total / limit * 100);
  return el("div", { class: "budget" + (total > limit ? " over" : ""), role: "img",
    "aria-label": `voice ${kb(voiceBytes)}, with the issue ${kb(total)}, of ${kb(limit)}` },
    el("i", { class: "all", style: `width:${pt}%` }), el("i", { class: "voice", style: `width:${pv}%` }));
}
function designPanel() {
  const names = (S.app.designs || []).map((d) => d.name);
  const sel = el("select", { "aria-label": "Design", disabled: running(), onchange: (e) => setIssue({ design: e.target.value }) },
    ...names.map((n) => el("option", { value: n, text: n, selected: S.design === n })));
  return el("div", {}, el("h4", { text: "Design" }), el("div", { class: "panel" },
    el("div", { class: "pick-row" }, sel, el("button", { class: "ghost small", type: "button", text: "Manage", onclick: designManager })),
    el("p", { class: "hint", text: S.designMissing ? "this issue's own design is missing; drawn with the default" : "how the issue looks: every tag and colour" })));
}
async function setIssue(change) {
  if (!(await flush())) { toast("Save the issue first: the problem is shown at the top.", true); renderSide(); return; }
  try {
    await api(`/api/issues/${encodeURIComponent(S.slug)}/settings`, { method: "PUT", body: change });
    S.preview = null;
    await reloadDoc();
    toast(change.design ? `Drawn with ${change.design} now.` : `Written in the ${change.voice} voice now.`);
  } catch (e) { toast(e.message, true); renderSide(); }
}

function sideBuild() {
  const r = running();
  const c = counts();
  const mine = S.blocks.filter((b) => b.type === "prose" || b.type === "component").length;
  const summary = el("div", {}, el("h4", { text: "In this draft" }), el("div", { class: "panel kv" },
    el("b", { text: String(c.draft) }), el("span", { text: `written by the model${c.stale ? ` · ${c.stale} out of date` : ""}` }),
    el("b", { text: String(mine) }), el("span", { text: `your words, locked` }),
    el("b", { text: String(c.write) }), el("span", { text: `prompt${c.write === 1 ? "" : "s"} not written yet` })));
  const box = el("div", { class: "log", id: "runlog" }, ...S.log.map(logLine));
  if (r) box.append(el("span", { class: "live", text: "› working · " + mmss(Date.now() / 1000 - (S.job.started || Date.now() / 1000)) }));
  if (!S.log.length && !r) box.append(el("span", { class: "info", text: S.job ? `last pass: ${S.job.status}` : "no pass yet" }));
  setTimeout(() => { box.scrollTop = box.scrollHeight; }, 0);
  return [summary, el("div", {}, el("h4", { text: "Run log" }), el("div", { class: "panel" }, box,
    r ? el("button", { class: "ghost danger stopbtn", type: "button", text: "Stop", onclick: stop }) : null,
    !r && S.job ? el("p", { class: "hint", text: `${S.job.kind} · ${S.job.status}${S.job.elapsed ? ` · ${S.job.elapsed}s` : ""}` }) : null))];
}

function sideReview() {
  const queued = S.review.comments.filter((c) => c.status === "queued");
  const props = S.review.proposals.filter((p) => p.status === "pending");
  const lint = S.preview && S.preview.lint;
  const kids = [proofPanel(), downloadPanel()];
  kids.push(el("div", {}, el("h4", { text: `${queued.length} edit${queued.length === 1 ? "" : "s"} queued` }),
    queued.length ? el("div", {}, ...queued.map((c) => el("div", { class: "note" },
      el("span", { class: "who", text: c.quote ? `on “${c.quote.slice(0, 40)}${c.quote.length > 40 ? "…" : ""}”` : blockLabel(c.block) }),
      el("button", { class: "x", type: "button", "aria-label": "Remove", text: "×", onclick: () => removeComment(c.id) }),
      el("p", { text: c.note }),
      c.error ? el("p", { style: "color:var(--stop);font-size:12px;margin-top:4px", text: "Not done last time: " + c.error }) : null)))
      : el("p", { class: "empty", text: "Select words in the issue, or click a paragraph, to leave a note. Notes show in the text and queue here until you send them." })));
  if (props.length) {
    kids.push(el("div", {}, el("h4", { text: `${props.length} change${props.length > 1 ? "s" : ""} to your words` }),
      ...props.map((p) => {
        const d = el("div", { class: "diff" });
        d.innerHTML = wordDiff(p.base, p.text);
        return el("div", { class: "note proposal" }, el("span", { class: "who", text: `proposed for #${p.block}` }), d,
          el("div", { class: "row" },
            el("button", { class: "send", type: "button", text: "Accept", onclick: () => decide(p, "accept") }),
            el("button", { class: "ghost", type: "button", text: "Reject", onclick: () => decide(p, "reject") })));
      })));
  }
  if (lint) {
    const errs = lint.findings.filter((f) => f.level === "error"), warns = lint.findings.filter((f) => f.level === "warn");
    kids.push(el("div", {}, el("h4", { text: `Lint · ${errs.length} error${errs.length === 1 ? "" : "s"} · ${warns.length} warning${warns.length === 1 ? "" : "s"}` }),
      el("div", { class: "panel lint" }, errs.length + warns.length ? el("ul", {}, ...[...errs, ...warns].slice(0, 12).map((f) =>
        el("li", { class: f.level }, f.text, f.hint ? el("small", { text: f.hint }) : null))) : el("p", { class: "empty", text: "Clean." }),
        errs.length ? el("button", { class: "ghost small", type: "button", style: "margin-top:6px", disabled: running(), text: "Ask the model to fix the errors", onclick: () =>
          sendEdits("Fix these linter errors without changing anything else:\n" + errs.map((f) => "- " + f.text + (f.hint ? " (" + f.hint + ")" : "")).join("\n")) }) : null)));
  }
  const canSend = queued.length > 0 && !running();
  kids.push(el("div", { class: "apply" },
    el("button", { class: "cta", type: "button", disabled: !canSend, onclick: () => {
      const text = $("#chat-input").value.trim();
      sendEdits(text).then((ok) => { if (ok && $("#chat-input").value.trim() === text) $("#chat-input").value = ""; });
    } },
      `Send ${queued.length || ""} edit${queued.length === 1 ? "" : "s"} →`),
    el("span", { class: "hint", style: "text-align:center", text: "one pass · only these blocks · your own words come back as proposals" }),
    S.review.last_pass ? el("button", { class: "ghost small", type: "button", text: S.undoArmed === S.slug ? "Click again to undo the last model pass" : "Undo last model pass", onclick: undo }) : null));
  return kids;
}

/* ── proof: spelling and grammar ───────────────────────────────────────── */
function straight(t) { return t.replace(/[\u2018\u2019]/g, "'").replace(/[\u201c\u201d]/g, '"'); }
function proofFixes() { return (S.review.proof && S.review.proof.fixes) || []; }

function proofPanel() {
  const pr = S.review.proof;
  const checking = running() && S.job && S.job.kind === "proof";
  const kids = [el("h4", { text: "Spelling & grammar" })];
  const box = el("div", { class: "panel proof" });
  if (checking) {
    box.append(el("p", { class: "proof-busy", text: "Checking spelling and grammar…" }),
      el("button", { class: "ghost danger small", type: "button", text: "Stop", onclick: stop }));
  } else if (pr) {
    const list = pr.fixes || [];
    const on = list.filter((f) => f.applied).length;
    box.append(el("p", { class: "hint", style: "margin:0 0 8px", text: !list.length
      ? (pr.skipped ? `Nothing changed: ${pr.skipped} suggestion${pr.skipped === 1 ? "" : "s"} could not be placed safely (the run log on Draft says why).` : "No mistakes found.")
      : `${on} of ${list.length} fix${list.length === 1 ? "" : "es"} applied · click a highlight, or a line here, to switch it` }));
    for (const f of list) {
      const d = el("span", { class: "diff" });
      d.innerHTML = wordDiff(f.before, f.after);
      box.append(el("button", { class: "fix-row" + (f.applied ? " on" : " off") + (f.stale ? " stale" : ""), type: "button",
        "data-fid": f.id, disabled: running() || !!f.stale, "aria-pressed": String(!!f.applied),
        title: f.stale ? "that sentence changed after the check" : f.applied ? "click to put your original words back" : "click to use the fix",
        onclick: () => toggleFix(f) },
        d, el("span", { class: "why", text: f.stale ? "changed since the check" : `${f.why || "fix"} · ${f.applied ? "fixed" : "your original"}` })));
    }
  }
  box.append(el("div", { class: "row" },
    S.app.quickcheck ? el("button", { class: pr ? "ghost small" : "send", type: "button", disabled: running() || checking,
      text: "Quick check (free)", title: "LanguageTool: spelling and grammar, no AI and no tokens; the text goes to LanguageTool",
      onclick: () => checkProof("quickcheck") }) : null,
    el("button", { class: pr || S.app.quickcheck ? "ghost small" : "send", type: "button", disabled: running() || checking,
      text: pr ? "Check again with AI" : "Check with AI", title: "the writing model, told what your voice does on purpose",
      onclick: () => checkProof("proof") }),
    pr && !checking ? el("button", { class: "ghost small", type: "button", disabled: running(), text: "Done",
      title: "clear the highlights; the text stays as it is", onclick: proofDone }) : null));
  if (!pr && !checking) box.append(el("p", { class: "hint", text: S.app.quickcheck
    ? "quick check: free, no AI, the text goes to LanguageTool · AI check: knows your voice, uses tokens" : "fixes mistakes only; your voice file says what is on purpose" }));
  kids.push(box);
  return el("div", {}, ...kids);
}

/* The finished issue as a file to take away (SPEC-EXPORT). */
const DOWNLOADS = [
  { kind: "html", text: "Web page", title: "one file with the pictures inside: open it, print it to PDF, or post it" },
  { kind: "docx", text: "Word", title: "for Word, Google Docs or Pages; words, headings, links and pictures (not the colours)" },
  { kind: "zip", text: "All files", title: "the web page, an email version, Markdown, Word, and the pictures" },
];
function downloadPanel() {
  const busy = S.downloading;
  return el("div", {}, el("h4", { text: "Download" }), el("div", { class: "panel" },
    el("div", { class: "row" }, ...DOWNLOADS.map((d) => el("button", { class: "ghost small", type: "button",
      "data-download": d.kind, disabled: !!busy, title: d.title,
      text: busy === d.kind ? "Making…" : d.text, onclick: () => download(d.kind) }))),
    el("p", { class: "hint", text: "web page: pictures inside · Word: no colours · all files: .zip" })));
}
async function download(kind) {
  if (S.downloading) return;
  if (!(await flush())) { toast("Save the issue first: the problem is shown at the top.", true); return; }
  const slug = S.slug;
  S.downloading = kind; renderSide();
  try {
    const r = await fetch(`/api/issues/${encodeURIComponent(slug)}/export/${kind}`, { headers: { "x-slopmill": "1" } });
    if (!r.ok) {
      let m = `download failed (${r.status})`;
      try { m = (await r.json()).error || m; } catch (e) { /* not JSON */ }
      throw new Error(m);
    }
    const blob = await r.blob();
    const m = /filename="([^"]+)"/.exec(r.headers.get("content-disposition") || "");
    const url = URL.createObjectURL(blob);
    const a = el("a", { href: url, download: m ? m[1] : `${slug}.${kind}`, hidden: true });
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  } catch (e) { toast(e.message, true); }
  finally { S.downloading = null; if (S.screen === "review") renderSide(); }
}

async function checkProof(which) {
  const slug = S.slug;
  if (!(await flush())) { toast("Save the issue first: the problem is shown at the top.", true); return; }
  try {
    const r = await api(`/api/issues/${encodeURIComponent(slug)}/${which === "quickcheck" ? "quickcheck" : "proof"}`, { method: "POST", body: {} });
    if (slug !== S.slug) return;
    if (r.job) S.job = r.job;
    renderSide(); updateTabs();
  } catch (e) { toast(e.message, true); }
}

async function toggleFix(f) {
  if (running() || f.stale || S.fixBusy) return;
  S.fixBusy = true;
  try {
    const r = await api(`/api/issues/${encodeURIComponent(S.slug)}/proof/${encodeURIComponent(f.id)}`,
      { method: "POST", body: { applied: !f.applied } });
    await reloadDoc({ keepFrame: true });
    toast(r.fix.applied ? "Fixed." : "Your original words are back.");
  } catch (e) {
    toast(e.message, true);
    await reloadDoc({ keepFrame: true });
  } finally { S.fixBusy = false; }
}

async function proofDone() {
  try {
    await api(`/api/issues/${encodeURIComponent(S.slug)}/proof`, { method: "DELETE" });
    await reloadDoc({ keepFrame: true });
  } catch (e) { toast(e.message, true); }
}

function highlightFixes() {
  const d = frameDoc();
  if (!d) return;
  const body = d.getElementById("issue-body");
  for (const f of proofFixes()) {
    if (f.stale) continue;
    const shown = f.applied ? f.after : f.before;
    const title = f.applied ? `fixed (${f.why || "fix"}) · was: ${f.before} · click for your original`
      : `your original · fix: ${f.after} (${f.why || "fix"}) · click to fix`;
    for (const part of body.querySelectorAll(`[data-block="${CSS.escape(f.block)}"]`)) {
      if (markText(part, shown, f.applied ? "fix" : "fix orig", 0, "", f.id, title) === true) break;
    }
  }
}

function blockLabel(id) {
  const b = S.blocks.find((x) => x.id === id);
  if (!b) return "on a block";
  const words = b.text.replace(/[#*_>\[\]{}()!:]|\{[^}]*\}/g, " ").split(/\s+/).filter(Boolean).slice(0, 6).join(" ");
  return `on the block “${words}…”`;
}

/* ── chat ──────────────────────────────────────────────────────────────── */
function renderChat() {
  const log = $("#chat-log");
  const items = (S.review.chat || []).slice(-40).map((m) => {
    const node = el("div", { class: "msg " + m.role },
      el("b", { text: m.role === "model" ? S.app.model : m.role === "user" ? "You" : "slopmill" }), m.text);
    const srcs = (m.sources || []).filter((x) => /^https:\/\//.test(x.url || ""));
    if (srcs.length) node.append(el("div", { class: "msg-sources" }, "Sources: ",
      ...srcs.map((x, i) => [i ? " · " : "", el("a", { href: x.url, target: "_blank", rel: "noopener noreferrer", text: x.title || x.url })]).flat()));
    for (const p of (m.prompts || [])) {
      node.append(el("div", { class: "sugg" }, el("p", { text: p }),
        el("button", { class: "ghost small", type: "button", text: "Add to Plan", onclick: (e) => addSuggested(p, e.currentTarget) })));
    }
    if (m.change && m.asked) node.append(el("button", { class: "ghost small do-change", type: "button", text: "Make this change",
      title: "send your message to be done: the model changes the issue", disabled: running(), onclick: () => sendEdits(m.asked) }));
    return node;
  });
  if (S.asking) items.push(el("div", { class: "msg model thinking", "aria-live": "polite" }, el("b", { text: S.app.model }),
    el("span", { class: "thinking-row" }, millMark("working"), "thinking…")));
  if (!items.length) items.push(el("p", { class: "empty", text: "Ask the model anything about this issue: it answers here and changes nothing. “Change the issue” sends your message to be done." }));
  log.replaceChildren(...items);
  log.scrollTop = log.scrollHeight;
}

/* A prompt the model suggested in the chat, added to the end of Plan like any typing. */
function addSuggested(text, btn) {
  if (running()) { toast("The model is working; add it when it finishes.", true); return; }
  const clean = text.split("\n").filter((l) => !/^\s*:{3,}/.test(l)).join("\n").trim();
  if (!clean) return;
  S.blocks.push({ type: "prompt", id: newId(), text: clean, attrs: {} });
  markDirty();
  if (S.screen === "compose") renderCompose();
  btn.textContent = "Added ✓"; btn.disabled = true;
  toast("Added to the end of Plan.");
}

/* ── dialogs ───────────────────────────────────────────────────────────── */
let modalOpener = null;
function openModal(title, ...body) {
  if (S.edit && S.edit.dialog && body[0] !== S.edit.node) {
    if (editDirty()) { toast("Save or cancel the text you are editing first.", true); return; }
    S.edit = null;
  }
  modalOpener = document.activeElement;
  $("#modal-title").textContent = title;
  $("#modal-body").replaceChildren(...body.filter((n) => n != null && n !== false));
  $("#modal").hidden = false;
  setTimeout(() => ($("#modal-body").querySelector("input, textarea, button") || $("#modal-close")).focus(), 0);
}
function closeModal(force) {
  if (!force && S.edit && S.edit.dialog) {
    if (editDirty()) { editMsg("Unsaved changes: Save, or Cancel to throw them away.", true); S.edit.ta.focus(); return; }
    S.edit = null;
  }
  $("#modal").hidden = true;
  if (modalOpener && modalOpener.isConnected) modalOpener.focus();
}
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("#modal").hidden) closeModal();
  if (e.key === "Tab" && !$("#modal").hidden) {
    const f = [...$("#modal").querySelectorAll("button, input, textarea, select")].filter((n) => !n.disabled);
    if (!f.length) return;
    if (e.shiftKey && document.activeElement === f[0]) { e.preventDefault(); f[f.length - 1].focus(); }
    else if (!e.shiftKey && document.activeElement === f[f.length - 1]) { e.preventDefault(); f[0].focus(); }
  }
});

/* ── voice packs ───────────────────────────────────────────────────────── */
async function refreshApp() {
  try { S.app = await api("/api/state"); } catch (e) { /* keep what we had */ }
}

async function voiceManager(name) {
  let v = null;
  const names = S.app.voices || [];
  name = name || (S.voiceInfo && S.voiceInfo.name) || names[0];
  if (name) {
    try { v = await api(`/api/voices/${encodeURIComponent(name)}`); } catch (e) { toast(e.message, true); }
  }
  const status = el("p", { class: "hint", role: "status" });
  const pick = el("select", { "aria-label": "Voice pack", onchange: (e) => voiceManager(e.target.value) },
    ...names.map((n) => el("option", { value: n, text: n, selected: n === name })));
  const head = el("div", { class: "pick-row" }, names.length ? pick : el("span", { class: "empty", text: "No voice packs yet." }),
    el("button", { class: "ghost small", type: "button", text: "New voice pack", onclick: newVoiceDialog }),
    el("button", { class: "ghost small", type: "button", text: "How to build one", onclick: () => guideDialog("voice-packs") }));
  if (!v) { openModal("Voice packs", head); return; }
  const files = v.files.map((f) => ({ path: f.path, role: f.role, on: f.on }));
  // Each change is built from this rendering of the list, so a second change must wait until
  // the list has been rebuilt from what the server stored; otherwise it would undo the first.
  let busy = false;
  const save = async (next) => {
    if (busy) return;
    busy = true;
    $("#modal-body").querySelectorAll(".vlist input, .vlist select, .vlist button").forEach((n) => { n.disabled = true; });
    try { await api(`/api/voices/${encodeURIComponent(v.name)}/arrange`, { method: "PUT", body: { files: next } }); }
    catch (e) { toast(e.message, true); }
    await refreshApp(); if (S.slug) await reloadDoc();
    await voiceManager(v.name);
  };
  const rows = v.files.map((f, i) => {
    const role = el("select", { "aria-label": `Role of ${f.path}`, onchange: (e) => {
      const next = files.map((x) => ({ ...x }));
      if (e.target.value === "brief") next.forEach((x) => { if (x.role === "brief") x.role = "rules"; });
      next[i].role = e.target.value; save(next); } },
      ...["brief", "rules", "sample"].map((r) => el("option", { value: r, text: r, selected: f.role === r })));
    const on = el("input", { type: "checkbox", checked: f.on, "aria-label": `Send ${f.path}`, onchange: (e) => {
      const next = files.map((x) => ({ ...x })); next[i].on = e.target.checked; save(next); } });
    const move = (d) => { const next = files.map((x) => ({ ...x })); const [x] = next.splice(i, 1); next.splice(i + d, 0, x); save(next); };
    let armed = false;
    const del = el("button", { class: "ghost small danger", type: "button", text: "Delete", onclick: async (e) => {
      if (!armed) { armed = true; e.target.textContent = "Sure?"; setTimeout(() => { armed = false; e.target.textContent = "Delete"; }, 4000); return; }
      try { await api(`/api/voices/${encodeURIComponent(v.name)}/file?path=${encodeURIComponent(f.path)}`, { method: "DELETE" }); }
      catch (err) { toast(err.message, true); }
      await refreshApp(); if (S.slug) await reloadDoc(); voiceManager(v.name); } });
    return el("li", { class: "vrow" + (f.on ? "" : " off") },
      el("label", { class: "von" }, on),
      el("button", { class: "vname", type: "button", text: f.path, title: "Read or edit", onclick: () => voiceEditor(v.name, f.path) }),
      el("span", { class: "vsize", text: f.missing ? "missing" : kb(f.size) }),
      role,
      el("span", { class: "vmove" },
        el("button", { class: "ghost small", type: "button", text: "↑", "aria-label": "Move up", disabled: i === 0, onclick: () => move(-1) }),
        el("button", { class: "ghost small", type: "button", text: "↓", "aria-label": "Move down", disabled: i === v.files.length - 1, onclick: () => move(1) })),
      del);
  });
  const input = el("input", { type: "file", multiple: true, accept: ".md,.txt,text/markdown,text/plain", hidden: true,
    onchange: (e) => uploadVoiceFiles(v.name, e.target.files, status) });
  const drop = el("div", { class: "drop", tabindex: "0", role: "button", "aria-label": "Upload voice files",
    onclick: () => input.click(), onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } } },
    el("b", { text: "Upload files" }), " · drop .md or .txt files here, or click. New files come in as samples.");
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); uploadVoiceFiles(v.name, e.dataTransfer.files, status); });
  const limit = v.limit;
  openModal("Voice pack: " + v.name, head,
    el("p", { class: "hint", style: "margin:10px 0 4px" },
      `A pass sends the brief as the model's instruction and every file that is on, in this order: ${kb(v.bytes)} of the ${kb(limit)} a call can take, before the issue itself.`),
    budgetBar(v.bytes, v.bytes, limit),
    el("ul", { class: "vlist" }, ...rows),
    drop, input, status,
    el("p", { class: "hint", text: "brief: the standing instruction (one) · rules: what you do and never do · sample: your own unedited writing, the part that matters most. Old versions are kept in .history." }));
}

async function uploadVoiceFiles(name, fileList, status) {
  const files = [], refusedHere = [];
  const all = [...fileList];
  const strict = new TextDecoder("utf-8", { fatal: true });
  for (const f of all.slice(0, 40)) {
    if (f.size > 60000) { refusedHere.push(`${f.name} is ${kb(f.size)}; a voice file is at most 60KB`); continue; }
    try { files.push({ name: f.name, text: strict.decode(await f.arrayBuffer()) }); }
    catch (e) { refusedHere.push(`${f.name} is not UTF-8 text`); }
  }
  if (all.length > 40) refusedHere.push(`${all.length - 40} more not sent: upload at most 40 files at a time (${all.slice(40).map((f) => f.name).join(", ")})`);
  if (!files.length) { if (refusedHere.length) toast("Not added: " + refusedHere.join("; "), true); return; }
  status.textContent = "Uploading…";
  try {
    const r = await api(`/api/voices/${encodeURIComponent(name)}/files`, { method: "POST", body: { files } });
    await refreshApp(); if (S.slug) await reloadDoc();
    await voiceManager(name);
    const refused = [...refusedHere, ...r.refused];
    const msg = (r.added.length ? `Added ${r.added.join(", ")}.` : "") + (refused.length ? ` Not added: ${refused.join("; ")}` : "");
    toast(msg.trim() || "Nothing was added.", !!refused.length);
  } catch (e) { status.textContent = e.message; }
}

function newVoiceDialog() {
  const name = el("input", { id: "nv-name", placeholder: "my-voice", autocomplete: "off" });
  const form = el("form", { onsubmit: async (e) => {
    e.preventDefault();
    try {
      const v = await api("/api/voices", { method: "POST", body: { name: name.value.trim().toLowerCase() } });
      await refreshApp();
      voiceManager(v.name);
    } catch (err) { toast(err.message, true); }
  } },
    el("div", { class: "field" }, el("label", { for: "nv-name", text: "Name (lowercase letters, digits, dashes)" }), name),
    el("p", { class: "hint", text: "It starts with a short brief to edit. Then upload three to five pieces you wrote yourself." }),
    el("div", { class: "row", style: "margin-top:12px;justify-content:flex-end" }, el("button", { class: "send", type: "submit", text: "Create" })));
  openModal("New voice pack", form);
  setTimeout(() => name.focus(), 0);
}

async function voiceEditor(name, path) {
  try {
    const f = await api(`/api/voices/${encodeURIComponent(name)}/file?path=${encodeURIComponent(path)}`);
    const ta = el("textarea", { class: "big", spellcheck: false });
    ta.value = f.text;
    openModal(`${name} / ${path}`,
      el("p", { class: "hint", style: "margin:0 0 8px", text: "Sent to the model with every pass of every issue that uses this voice. The old version is kept." }),
      ta,
      el("div", { class: "row", style: "margin-top:10px;justify-content:flex-end" },
        el("button", { class: "ghost", type: "button", text: "Back", onclick: () => voiceManager(name) }),
        el("button", { class: "send", type: "button", text: "Save", onclick: async () => {
          try { await api(`/api/voices/${encodeURIComponent(name)}/file`, { method: "PUT", body: { path, text: ta.value } }); toast("Saved " + path); await refreshApp(); if (S.slug) await reloadDoc(); voiceManager(name); }
          catch (e) { toast(e.message, true); } } })));
  } catch (e) { toast(e.message, true); }
}

/* ── designs ───────────────────────────────────────────────────────────── */
async function designManager() {
  await refreshApp();
  const status = el("pre", { class: "design-status", role: "status", hidden: true });
  const rows = (S.app.designs || []).map((d) => {
    let armed = false;
    return el("li", { class: "vrow" },
      el("span", { class: "vname static", text: d.name }),
      el("span", { class: "vsize", text: d.source + (d.default ? " · default" : "") + (S.design === d.name ? " · this issue" : "") }),
      el("a", { class: "ghost small", href: `/api/designs/${encodeURIComponent(d.name)}/download`, download: `${d.name}.design.toml`, text: "Download" }),
      S.slug && S.design !== d.name ? el("button", { class: "ghost small", type: "button", text: "Use for this issue", onclick: async () => { closeModal(); await setIssue({ design: d.name }); } }) : null,
      d.source === "uploaded" ? el("button", { class: "ghost small danger", type: "button", text: "Remove", onclick: async (e) => {
        if (!armed) { armed = true; e.target.textContent = "Sure?"; setTimeout(() => { armed = false; e.target.textContent = "Remove"; }, 4000); return; }
        try { await api(`/api/designs/${encodeURIComponent(d.name)}`, { method: "DELETE" }); } catch (err) { toast(err.message, true); }
        designManager(); if (S.slug) reloadDoc(); } }) : null);
  });
  const input = el("input", { type: "file", accept: ".toml", hidden: true, onchange: async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    status.hidden = false; status.classList.remove("bad");
    if (f.size > 512000) {
      status.classList.add("bad"); status.textContent = `Not stored: ${f.name} is ${kb(f.size)}; a design file is at most 500KB.`;
      e.target.value = ""; return;
    }
    status.textContent = "Checking " + f.name + "…";
    try {
      const r = await api("/api/designs", { method: "POST", body: { text: await f.text() } });
      S.app.designs = r.designs;
      toast(`${r.name} is ready. Pick it for an issue under Design.`);
      designManager();
    } catch (err) { status.classList.add("bad"); status.textContent = "Not stored: " + err.message; }
    e.target.value = "";
  } });
  openModal("Designs",
    el("p", { class: "hint", style: "margin:0 0 10px", text: "A design is how an issue looks, as one file. Download one to see how it is made, change it, give it a new name, and upload it." }),
    el("ul", { class: "vlist" }, ...rows),
    el("div", { class: "row", style: "margin-top:12px" },
      el("button", { class: "send", type: "button", text: "Upload a design file", onclick: () => input.click() }),
      el("button", { class: "ghost", type: "button", text: "How to write one", onclick: () => guideDialog("design-files") })),
    input, status);
}

/* ── models and the meter ──────────────────────────────────────────────── */
function modelsDialog() {
  const p = S.app.providers || {};
  const line = (label, info) => {
    if (!info) return el("div", { class: "kvrow" }, el("b", { text: label }), el("span", { text: "off" }));
    const bits = [info.provider, info.model || info.label, info.program ? `runs ${info.program}` : null, info.base_url].filter(Boolean);
    const key = info.key_env ? el("span", { class: info.key_set ? "ok-note" : "warn-note", text: info.key_set ? ` · key from ${info.key_env}` : ` · ${info.key_env} is not set` }) : null;
    return el("div", { class: "kvrow" }, el("b", { text: label }), el("span", {}, bits.join(" · "), key));
  };
  openModal("Models",
    el("div", { class: "panel" }, line("Writes", p.writer), line("Draws pictures", p.images),
      line("Plan meter", p.plan ? { provider: p.plan.provider, label: "subscription windows" } : null)),
    el("p", { class: "hint", style: "margin:10px 0" }, p.config ? `Set in ${p.config} on the server.` : "No config file: the defaults and the command-line flags are in use.",
      " Providers, commands and keys can only be changed there, never from this page. Keys are read from environment variables and never shown."),
    el("button", { class: "ghost", type: "button", text: "Choosing a model, or using your own API key", onclick: () => guideDialog("providers") }));
}

async function loadUsage() {
  if (!S.slug) return;
  const slug = S.slug, gen = S.loadGen;
  try {
    const u = await api(`/api/usage?slug=${encodeURIComponent(slug)}`);
    if (slug !== S.slug || gen !== S.loadGen) return;      // another issue is open now
    S.usage = u;
    renderMeter();
    const plan = S.usage.plan;
    if (plan && plan.busy && !plan.windows) setTimeout(loadUsage, 8000);   // first fetch is slow
  } catch (e) { /* the meter is a convenience; never an error */ }
}
function renderMeter() {
  const m = $("#meter"), u = S.usage;
  if (!m || !u || (S.app && S.app.demo)) return;          // the demo spends no tokens
  const est = u.issue.exact ? "" : "≈";
  const dest = u.today.exact ? "" : "≈";
  const kids = [el("span", { class: "mt", text: `${est}${ktok(u.issue.in + u.issue.out)} this issue` }),
    el("span", { class: "mt mt-today", text: `${dest}${ktok(u.today.in + u.today.out)} today` })];
  const wins = u.plan && u.plan.windows;
  if (wins && wins.length) {
    // the plan's windows are the whole subscription (everything on that account), not slopmill's share
    kids.push(el("small", { class: "mw-lead", text: "whole plan" }));
    for (const w of wins.slice(0, 2)) {
      const used = Math.max(0, Math.min(100, Number(w.used) || 0));
      kids.push(el("span", { class: "mw", title: `${w.label}: ${used}% of your whole plan used, by everything on that account, not only slopmill` }, el("small", { text: w.label.toLowerCase() }),
        el("span", { class: "mbar" + (used >= 80 ? " hot" : "") }, el("i", { style: `width:${used}%` }))));
    }
  }
  m.replaceChildren(...kids);
  m.hidden = false;
  m.title = `This issue: ${est}${u.issue.in.toLocaleString()} tokens in, ${est}${u.issue.out.toLocaleString()} out` +
    (u.issue.images ? `, ${u.issue.images} picture${u.issue.images > 1 ? "s" : ""}` : "") +
    `. Today, all issues: ${dest}${(u.today.in + u.today.out).toLocaleString()} tokens. These count slopmill's own calls` +
    (wins && wins.length ? "; the bars are your whole plan, everything on that account." : ".") + " Click for more.";
}
function when(ts) {
  if (!ts) return "";
  const s = ts - Date.now() / 1000;
  if (s <= 0) return "now";
  const h = Math.floor(s / 3600), d = Math.floor(h / 24);
  return d >= 1 ? `in ${d}d ${h % 24}h` : h >= 1 ? `in ${h}h ${Math.floor(s % 3600 / 60)}m` : `in ${Math.max(1, Math.round(s / 60))}m`;
}
async function usageDialog() {
  await loadUsage();
  const u = S.usage;
  if (!u) return;
  const tot = (t, label) => el("div", { class: "kvrow" }, el("b", { text: label }),
    el("span", { text: `${t.exact ? "" : "≈"}${t.in.toLocaleString()} in · ${t.exact ? "" : "≈"}${t.out.toLocaleString()} out · ${t.calls} call${t.calls === 1 ? "" : "s"}` + (t.images ? ` · ${t.images} picture${t.images > 1 ? "s" : ""}` : "") }));
  const plan = u.plan;
  const planBox = !plan ? el("p", { class: "hint", text: "No plan meter configured (see Models)." })
    : plan.windows ? el("div", { class: "panel" }, el("h4", { text: `Your whole plan: ${plan.name || "subscription"}${plan.plan ? " · " + plan.plan : ""}` }),
        el("p", { class: "hint", text: "Everything that uses this subscription counts here, not only slopmill. The numbers above are slopmill's own calls." }),
        ...plan.windows.map((w) => el("div", { class: "kvrow" }, el("b", { text: w.label }),
          el("span", {}, el("span", { class: "mbar wide" + (w.used >= 80 ? " hot" : "") }, el("i", { style: `width:${Math.min(100, w.used)}%` })),
            ` ${w.used}% used · resets ${when(w.reset_at)}`))),
        plan.error ? el("p", { class: "hint", text: "last refresh failed: " + plan.error }) : null,
        el("p", { class: "hint", text: `checked ${plan.ts ? new Date(plan.ts * 1000).toLocaleTimeString() : "never"} · refreshed at most every five minutes` }))
    : el("p", { class: "hint", text: plan.error ? "Plan usage unavailable: " + plan.error : "Plan usage is being fetched (it takes about 20 seconds)…" });
  const recent = (u.recent || []).map((r) => el("tr", {},
    el("td", { text: new Date(r.ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) }),
    el("td", { text: `${r.pass_kind || ""}${r.call === "image" ? " · picture" : ""}` }),
    el("td", { text: r.call === "image" ? "1 picture" : `${r.exact ? "" : "≈"}${(r.in || 0).toLocaleString()} / ${r.exact ? "" : "≈"}${(r.out || 0).toLocaleString()}` }),
    el("td", { text: r.model || "" })));
  openModal("Token use",
    el("div", { class: "panel" }, tot(u.issue, "This issue"), tot(u.today, "Today, all issues (slopmill only)")),
    el("p", { class: "hint", style: "margin:6px 0 12px", text: u.issue.exact && u.today.exact ? "Exact counts, as the provider reported them." : "≈ means estimated from the text sent and received (about four characters a token): the command route reports no counts." }),
    planBox,
    recent.length ? el("div", { class: "table-wrap" }, el("table", { class: "usage" },
      el("thead", {}, el("tr", {}, el("th", { text: "When" }), el("th", { text: "Pass" }), el("th", { text: "Tokens in / out" }), el("th", { text: "Model" }))),
      el("tbody", {}, ...recent))) : null);
}

/* ── guides (docs/*.md, drawn here) ────────────────────────────────────── */
function guideHtml(src) {
  const inl = (t) => esc(t)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*\s][^*]*)\*/g, "<em>$1</em>")
    .replace(/\[([^\]]+)\]\(([A-Z-]+)\.md\)/g, (m, t, f) => `<a href="#" data-guide="${f.toLowerCase()}">${t}</a>`)
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  const out = [];
  const lines = src.split("\n");
  for (let i = 0; i < lines.length; ) {
    const l = lines[i];
    if (/^```/.test(l)) {
      const buf = []; i++;
      while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
      i++; out.push(`<pre><code>${esc(buf.join("\n"))}</code></pre>`); continue;
    }
    if (/^#{1,6}\s/.test(l)) { const n = Math.min(4, l.match(/^#+/)[0].length + 1); out.push(`<h${n}>${inl(l.replace(/^#+\s*/, ""))}</h${n}>`); i++; continue; }
    if (/^\|/.test(l)) {
      const rows = [];
      while (i < lines.length && /^\|/.test(lines[i])) rows.push(lines[i++]);
      const cells = (r) => r.replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      const body = rows.filter((r, j) => j !== 1 || !/^\|[\s:-|]+\|$/.test(r));
      out.push(`<div class="table-wrap"><table><thead><tr>${cells(body[0]).map((c) => `<th>${inl(c)}</th>`).join("")}</tr></thead><tbody>` +
        body.slice(1).map((r) => `<tr>${cells(r).map((c) => `<td>${inl(c)}</td>`).join("")}</tr>`).join("") + "</tbody></table></div>");
      continue;
    }
    if (/^[-*]\s/.test(l)) {
      const items = [];
      while (i < lines.length && (/^[-*]\s/.test(lines[i]) || (/^\s{2,}\S/.test(lines[i]) && items.length))) {
        if (/^[-*]\s/.test(lines[i])) items.push(lines[i].replace(/^[-*]\s+/, "")); else items[items.length - 1] += " " + lines[i].trim();
        i++;
      }
      out.push("<ul>" + items.map((t) => `<li>${inl(t)}</li>`).join("") + "</ul>"); continue;
    }
    if (/^>\s?/.test(l)) {
      const buf = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) buf.push(lines[i++].replace(/^>\s?/, ""));
      out.push(`<blockquote>${inl(buf.join(" "))}</blockquote>`); continue;
    }
    if (!l.trim()) { i++; continue; }
    const buf = [];
    while (i < lines.length && lines[i].trim() && !/^(```|#{1,6}\s|\||[-*]\s|>)/.test(lines[i])) buf.push(lines[i++]);
    out.push(`<p>${inl(buf.join(" "))}</p>`);
  }
  return out.join("");
}
async function guideDialog(name) {
  try {
    const g = await api(`/api/docs/${encodeURIComponent(name)}`);
    const box = el("div", { class: "guide" });
    box.innerHTML = guideHtml(g.text);
    box.querySelectorAll("[data-guide]").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); guideDialog(a.dataset.guide); }));
    const title = (g.text.match(/^#\s+(.+)$/m) || [, "Guide"])[1];
    box.querySelector("h2")?.remove();
    openModal(title, box);
  } catch (e) { toast(e.message, true); }
}

function newIssueDialog() {
  const num = el("input", { id: "ni-num", inputmode: "numeric", placeholder: "23" });
  const title = el("input", { id: "ni-title", placeholder: "What is it called?" });
  const cur = S.app.default_design;          // new issues get the defaults (SPEC-V2 6, 14)
  const design = el("select", { id: "ni-design" }, ...(S.app.designs || []).map((d) => el("option", { value: d.name, text: d.name, selected: d.name === cur })));
  const vcur = S.app.default_voice || (S.app.voices || [])[0];
  const voice = el("select", { id: "ni-voice" }, ...(S.app.voices || []).map((n) => el("option", { value: n, text: n, selected: n === vcur })));
  const form = el("form", { onsubmit: async (e) => {
    e.preventDefault();
    try {
      const r = await api("/api/issues", { method: "POST", body: { number: num.value.trim() || null, title: title.value.trim(),
        design: design.value || null, voice: voice.value || null } });
      closeModal();
      S.app = await api("/api/state");
      fillIssueSelect();
      location.hash = r.slug;
    } catch (err) { toast(err.message, true); }
  } },
    el("div", { class: "field" }, el("label", { for: "ni-num", text: "Issue number" }), num),
    el("div", { class: "field" }, el("label", { for: "ni-title", text: "Title" }), title),
    el("div", { class: "field" }, el("label", { for: "ni-design", text: "Design" }), design),
    (S.app.voices || []).length ? el("div", { class: "field" }, el("label", { for: "ni-voice", text: "Voice" }), voice) : null,
    el("div", { class: "row", style: "margin-top:12px;justify-content:flex-end" }, el("button", { class: "send", type: "submit", text: "Create" })));
  openModal("New issue", form);
  setTimeout(() => num.focus(), 0);
}

init();
