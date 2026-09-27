// Runs the review app's own <script> against a minimal DOM, so tests can drive it the way a
// reviewer does (tick a box, press a key, import a file) and read back what the page shows and
// what it stored. tests/test_review_app.py parses the rendered page and pipes it in as JSON:
//
//   {"tree": <body element tree>, "script": "<the page's script>",
//    "storage": {"<key>": "<value>"}, "actions": [...], "full": ["<key prefix>"],
//    "hash": "<the URL's fragment as the page loads>"}
//
// A write to a key starting with one of "full" throws, as a full localStorage does.
//
// and gets back {"storage": ..., "rows": [...], "claims": [...], "notice": ..., "alerts": [...],
// "tab": ..., "tabs": [...], "overview": [...], "hash": ..., "lost": ..., "copied": [...],
// "opened": [...]}.
//
// The DOM supports only what the page uses. A selector it doesn't know throws, so a template
// change that needs more fails the test loudly rather than passing against a stub that
// silently matched nothing.
"use strict";
const vm = require("vm");

class ClassList {
  constructor(names) { this.set = new Set(names); }
  contains(n) { return this.set.has(n); }
  add(n) { this.set.add(n); }
  remove(n) { this.set.delete(n); }
  toggle(n, force) {
    const on = force === undefined ? !this.set.has(n) : !!force;
    if (on) this.set.add(n); else this.set.delete(n);
    return on;
  }
}

function matcher(sel) {
  if (/^\.[\w-]+$/.test(sel)) return el => el.classList.contains(sel.slice(1));
  throw new Error("harness: unsupported selector " + JSON.stringify(sel));
}

class El {
  constructor(node, parent, doc) {
    this.tagName = node.tag.toUpperCase();
    this.attrs = {...node.attrs};   // what setAttribute() changes; dataset is read once, below
    this.id = node.attrs.id || "";
    this.classList = new ClassList((node.attrs.class || "").split(/\s+/).filter(Boolean));
    this.dataset = {};
    for (const [k, v] of Object.entries(node.attrs)) {
      if (k.startsWith("data-")) {
        this.dataset[k.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = v;
      }
    }
    this.parentNode = parent;
    this.doc = doc;
    this.style = {};
    this.checked = "checked" in node.attrs;
    this.value = node.attrs.value || "";
    this.textContent = "";
    this.listeners = {};
    this.children = node.children.map(c => new El(c, this, doc));
  }
  *walk() { for (const c of this.children) { yield c; yield* c.walk(); } }
  querySelectorAll(sel) { const m = matcher(sel); return [...this.walk()].filter(m); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  closest(sel) {
    const m = matcher(sel);
    for (let el = this; el; el = el.parentNode) if (m(el)) return el;
    return null;
  }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  removeAttribute(k) { delete this.attrs[k]; }
  focus() { this.doc.activeElement = this; }
  click() { this.doc.clicked.push(this); this.doc.dispatch("click", this); }
  scrollIntoView() {}
}

class Doc {
  constructor(tree) {
    this.activeElement = null;
    this.listeners = {};
    this.clicked = [];          // every element the page clicked itself, as el.click() does
    this.body = new El(tree, null, this);
  }
  querySelectorAll(sel) { return this.body.querySelectorAll(sel); }
  querySelector(sel) { return this.body.querySelector(sel); }
  getElementById(id) { return [...this.body.walk()].find(e => e.id === id) || null; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  createElement(tag) {
    return new El({tag, attrs: {}, children: []}, null, this);
  }
  // Every listener the page registers is on document or on the target itself, so this is
  // all the bubbling it needs.
  dispatch(type, target, extra = {}) {
    const ev = {type, target, preventDefault() {}, ...extra};
    for (const fn of target.listeners[type] || []) fn(ev);
    for (const fn of this.listeners[type] || []) fn(ev);
  }
}

// Every row has its own key (report.ROW_KEY_RE), so a key names exactly one.
function row(doc, key) {
  const found = doc.querySelectorAll(".src").filter(e => e.dataset.key === key);
  if (found.length !== 1) throw new Error("harness: " + found.length + " rows keyed " + key);
  return found[0];
}

// The panel a row sits in, and the fragment that opens it, as its tab's link names it.
function tabOf(el) { return el.closest(".tab").dataset.tab; }
function hashFor(tab) { return tab ? "#q=" + encodeURIComponent(tab) : "#overview"; }

async function main() {
  const input = JSON.parse(require("fs").readFileSync(0, "utf8"));
  const doc = new Doc(input.tree);
  const store = new Map(Object.entries(input.storage || {}));
  const alerts = [];
  const copied = [];            // what the page wrote to the clipboard
  // A browser fires hashchange when the fragment changes, and not when it is set to what it
  // already is. It fires it later, as a task; here it fires at once, which the page can't tell
  // apart, since every action is awaited.
  const win = {
    listeners: {},
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); },
    scrolled: 0,                // how often the page scrolled itself back to the top
    scrollTo() { this.scrolled++; },
  };
  let hash = input.hash || "";
  const location = {
    get hash() { return hash; },
    set hash(v) {
      v = String(v);
      if (v && !v.startsWith("#")) v = "#" + v;
      if (v === "#") v = "";
      if (v === hash) return;
      hash = v;
      for (const fn of win.listeners.hashchange || []) fn({type: "hashchange"});
    },
  };
  const sandbox = {
    document: doc,
    window: win,
    location,
    localStorage: {
      getItem: k => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => {
        if ((input.full || []).some(p => k.startsWith(p))) throw new Error("QuotaExceededError");
        store.set(k, String(v));
      },
    },
    navigator: {clipboard: {writeText: text => copied.push(String(text))}},
    alert: msg => alerts.push(String(msg)),
    Blob: class {},
    URL: {createObjectURL: () => "blob:"},
    setTimeout, console,
  };
  vm.runInNewContext(input.script, sandbox);

  for (const a of input.actions || []) {
    if (a.do === "tick") {
      const cb = row(doc, a.row).querySelector(".cb");
      cb.checked = a.checked;
      doc.dispatch("change", cb);
    } else if (a.do === "key") {
      if (a.row) {
        // A reviewer can only click a row they can see: open its tab, then select the row.
        location.hash = hashFor(tabOf(row(doc, a.row)));
        doc.dispatch("click", row(doc, a.row));
      }
      doc.dispatch("keydown", doc.body, {key: a.key, metaKey: !!a.meta, ctrlKey: !!a.ctrl,
                                         altKey: !!a.alt, shiftKey: !!a.shift});
    } else if (a.do === "key-on-toggle") {
      // Open the row's tab and select the row, as "key" does, then press a key with a
      // claim's disclosure focused.
      location.hash = hashFor(tabOf(row(doc, a.row)));
      doc.dispatch("click", row(doc, a.row));
      const toggle = row(doc, a.on).closest(".claim").querySelector(".toggle");
      doc.dispatch("keydown", toggle, {key: a.key});
    } else if (a.do === "nav") {
      location.hash = a.hash;                     // as a tab's link, or a pasted URL, does
    } else if (a.do === "flag") {
      doc.dispatch("click", row(doc, a.row).querySelector(".flag"));
    } else if (a.do === "filter") {
      const f = doc.getElementById("filter");
      f.value = a.value;
      doc.dispatch("change", f);
    } else if (a.do === "dismiss") {
      doc.getElementById("dismiss").click();
    } else if (a.do === "import") {
      const file = doc.getElementById("file");
      file.files = [{text: async () => a.text}];
      file.value = "progress.json";
      doc.dispatch("change", file);
    } else {
      throw new Error("harness: unknown action " + JSON.stringify(a));
    }
    await new Promise(r => setTimeout(r, 0));          // let an async import settle
  }

  const notice = doc.getElementById("migrated");
  const lost = doc.getElementById("lost");
  const shown = doc.querySelectorAll(".tab").filter(p => !p.classList.contains("hidden"));
  if (shown.length !== 1) throw new Error("harness: " + shown.length + " tabs shown");
  process.stdout.write(JSON.stringify({
    tab: shown[0].dataset.tab,
    hash,
    scrolled: win.scrolled,
    lost: lost.classList.contains("hidden") ? "" : lost.textContent,
    // Each tab's link: the counts it carries, whether it is the one open, and how it is marked.
    tabs: doc.querySelectorAll(".tlink").map(l => ({
      tab: l.dataset.tab,
      count: (l.querySelector(".tcount") || {textContent: ""}).textContent,
      cur: l.classList.contains("cur"), ariaCurrent: l.attrs["aria-current"] || "",
      done: l.classList.contains("done"),
      dim: l.classList.contains("dim"), review: l.classList.contains("review"),
    })),
    // The overview, a row per question: its progress in words, and whether the filter shows it.
    overview: doc.querySelectorAll(".orow").map(r => ({
      qid: r.dataset.qid, progress: r.querySelector(".oprog").textContent,
      shown: !r.classList.contains("hidden"),
    })),
    selected: doc.querySelectorAll(".sel").map(el => el.dataset.key),
    // Each question's panel says when the filter leaves nothing in it.
    empty: doc.querySelectorAll(".nomatch").filter(n => !n.classList.contains("hidden"))
      .map(tabOf),
    storage: Object.fromEntries(store),
    rows: doc.querySelectorAll(".src").map(el => ({
      key: el.dataset.key,
      fp: el.dataset.fp,
      checked: el.querySelector(".cb").checked,
      stale: !el.querySelector(".stale").classList.contains("hidden"),
      noteStale: !el.querySelector(".nstale").classList.contains("hidden"),
      flagged: el.classList.contains("flagged"),
      note: el.querySelector(".note").value,
    })),
    claims: doc.querySelectorAll(".claim").map(c => ({
      qid: c.dataset.qid, done: c.classList.contains("done"), shown: c.style.display !== "none",
      tab: tabOf(c),
      // Whether the claim says its researcher's note is new, changed or removed since its rows
      // were checked.
      noteChanged: !c.querySelector(".nchanged").classList.contains("hidden"),
      // Whether the answer behind "details" is open; null for a claim with no summary.
      answerOpen: c.querySelector(".more") ? !!c.querySelector(".more").open : null,
    })),
    progress: doc.getElementById("pct").textContent,
    notice: notice.classList.contains("hidden") ? ""
      : doc.getElementById("migrated-text").textContent,
    fileInput: doc.getElementById("file").value,
    alerts,
    copied,
    // The links the page followed by clicking them itself (o opens the source, a the archive).
    opened: doc.clicked.filter(el => el.tagName === "A").map(el => el.classList.contains("arch")
                                                              ? "archive" : "source"),
  }));
}

main().catch(e => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
