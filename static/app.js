// DAX Slayer — frontend.
//
// Deliberately no force-directed/animated graph: a moving node cloud looks
// impressive but is hard to actually read when deciding what's safe to
// delete. Instead: a static, fixed-layout chart per selected object
// (depends-on / used-by, laid out once, no simulation), plus a flat table
// for bulk-selecting everything not used on any report visual. Both feed
// the same pending-deletion list and the same single delete button.

const state = {
  connected: false,
  model: null,
  nodesById: {},
  edgesBySource: {}, // nodeId -> [target ids]  (what it depends on)
  edgesByTarget: {}, // nodeId -> [source ids]  (what uses it)
  filter: "all",
  search: "",
  selectedId: null,
  pending: {},        // id -> {id, type, table, name}
  selectedInstance: null,
  instances: [],
  activeView: "chart",
  bulkFilter: "unused", // "unused" (level === unused) | "all" (also includes used-by-measure-only)
  bulkSearch: "",
  statFilter: "all", // "all" | one of OBJECT_CATEGORIES' keys — set by clicking a sidebar stat card
  daxFunctions: {},   // name -> {name,category,description,syntax,example}, fetched once
  explainCache: {},   // nodeId -> {summary, functionsUsed} from /api/explain
};

const el = (id) => document.getElementById(id);
const toast = (msg, kind = "") => {
  const t = el("toast");
  t.textContent = msg;
  t.className = "toast show" + (kind ? " " + kind : "");
  clearTimeout(toast._h);
  toast._h = setTimeout(() => (t.className = "toast"), 4200);
};

// ==================== THEME (light/dark) ====================

(function initTheme() {
  const saved = localStorage.getItem("daxslayer-theme");
  const theme = saved || (window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
  document.documentElement.setAttribute("data-theme", theme);
  const setIcon = () => {
    const isLight = document.documentElement.getAttribute("data-theme") === "light";
    el("themeToggleBtn").innerHTML = isLight ? "☀️ <span>Light mode</span>" : "🌙 <span>Dark mode</span>";
  };
  setIcon();
  el("themeToggleBtn").addEventListener("click", () => {
    const next = document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("daxslayer-theme", next);
    setIcon();
  });
})();

// ==================== SIDEBAR COLLAPSE ====================

(function initSidebarToggle() {
  const collapsed = localStorage.getItem("daxslayer-sidebar-collapsed") === "1";
  if (collapsed) { el("sidebarAside").classList.add("collapsed"); el("sidebarToggle").textContent = "›"; }
  el("sidebarToggle").addEventListener("click", () => {
    const isCollapsed = el("sidebarAside").classList.toggle("collapsed");
    el("sidebarToggle").textContent = isCollapsed ? "›" : "‹";
    localStorage.setItem("daxslayer-sidebar-collapsed", isCollapsed ? "1" : "0");
  });
})();

async function api(path, opts) {
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}

function iconFor(type) {
  if (type === "measure") return "ƒ";
  if (type === "column") return "▤";
  return "▦";
}

// Everything that transitively depends on `id` (follows "used by" edges
// outward, however deep the chain). Computed client-side from the edge
// list we already have from /api/analyze — no extra round trip needed.
function transitiveDependents(id) {
  const seen = new Set();
  const stack = [id];
  while (stack.length) {
    const cur = stack.pop();
    for (const src of state.edgesByTarget[cur] || []) {
      if (!seen.has(src)) { seen.add(src); stack.push(src); }
    }
  }
  return seen;
}

// Two ways deleting something can break a visible page: the object itself
// sits directly on a visual, OR something that transitively depends on it
// does. Checking only the second (as this used to) produces a contradiction
// for anything placed directly on a card/chart with nothing else built on
// top of it: "used in report" badge on, yet "nothing will break" underneath.
function visualImpact(id) {
  const deps = transitiveDependents(id);
  const via = [];
  const self = state.nodesById[id];
  if (self?.usedInReport) via.push(id);
  for (const d of deps) {
    if (state.nodesById[d]?.usedInReport) via.push(d);
  }
  return { breaks: via.length > 0, via, direct: !!self?.usedInReport };
}

// ==================== CONNECT WIZARD ====================

async function loadInstances() {
  const list = el("instanceList");
  try {
    const data = await api("/api/instances");
    state.instances = data.instances || [];
    state._reportFolders = data.report_folder_full_paths || [];
    state._reportFolderNames = data.report_folders || [];

    if (!state.instances.length) {
      list.innerHTML = `<div class="empty-instances"><div class="big">🔌</div>No open Power BI Desktop reports found.<br/>Open a .pbix/.pbip in Power BI Desktop, then click Refresh.</div>`;
      return;
    }
    list.innerHTML = "";
    for (const inst of state.instances) {
      const card = document.createElement("div");
      card.className = "instance-card";
      card.innerHTML = `
        <div class="ic-icon">📊</div>
        <div>
          <div class="ic-title">${escapeHtml(inst.report_title)}</div>
          <div class="ic-meta">localhost:${inst.port}${inst.suggested_report_folder ? " · report folder found" : ""}</div>
        </div>
        <div class="ic-go">→</div>
      `;
      card.addEventListener("click", () => chooseInstance(inst));
      list.appendChild(card);
    }
  } catch (e) {
    list.innerHTML = `<div class="empty-instances">Failed to list instances: ${escapeHtml(e.message)}</div>`;
  }
}

function chooseInstance(inst) {
  state.selectedInstance = inst;
  el("stepInstance").style.display = "none";
  el("stepFolder").style.display = "block";

  const sel = el("reportFolderSelect");
  sel.innerHTML = `<option value="">(none — DAX cross-reference detection only)</option>`;
  (state._reportFolders || []).forEach((full, idx) => {
    const opt = document.createElement("option");
    opt.value = full;
    opt.textContent = state._reportFolderNames[idx];
    sel.appendChild(opt);
  });

  const box = el("folderSuggestBox");
  if (inst.auto_report_folder) {
    sel.value = inst.auto_report_folder;
    box.innerHTML = `<div class="folder-suggest">✓ Auto-detected from Power BI Desktop's own open file — "${escapeHtml(inst.report_title)}" is a Power BI Project (.pbip), so its report folder was found automatically. Nothing to do here.</div>`;
  } else if (inst.is_pbix) {
    box.innerHTML = `<div class="folder-suggest">✓ <b>.pbix file detected</b> — DAX Slayer will automatically extract and scan its visual layout. You'll get full visual/card/page detection <b>without saving or converting</b>. Go ahead and click Analyze to proceed.</div>`;
  } else if (inst.suggested_report_folder) {
    sel.value = inst.suggested_report_folder;
    box.innerHTML = `<div class="folder-suggest">✓ Matched by name to a nearby .Report folder — override below if wrong.</div>`;
  } else {
    box.innerHTML = `<div class="folder-none">No .Report folder found nearby, and Power BI Desktop's open file path couldn't be read. Usage detection will rely on DAX cross-references only, unless you point at one below.</div>`;
  }
  el("reportFolderManual").value = "";
}

el("backToInstances").addEventListener("click", () => {
  el("stepFolder").style.display = "none";
  el("stepInstance").style.display = "block";
});

el("refreshInstancesBtn").addEventListener("click", loadInstances);

el("connectConfirmBtn").addEventListener("click", async () => {
  const inst = state.selectedInstance;
  if (!inst) return;
  const manual = el("reportFolderManual").value.trim();
  const reportFolder = manual || el("reportFolderSelect").value || null;

  const btn = el("connectConfirmBtn");
  btn.disabled = true;
  btn.textContent = "Connecting…";
  try {
    await api("/api/connect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ port: inst.port, report_folder: reportFolder }),
    });
    state.connected = true;
    onConnected(inst, reportFolder);
    el("connectScreen").style.display = "none";
    await analyze();
  } catch (e) {
    toast("Connect failed: " + e.message, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "Connect & Analyze";
  }
});

function onConnected(inst, reportFolder) {
  el("statusDot").className = "status-pill on";
  el("statusDot").title = "Connected · port " + inst.port;
  el("reportPill").style.display = "flex";
  el("reportPillName").textContent = inst.report_title;
  el("reportPillMeta").textContent = reportFolder ? "Report folder linked" : "No report folder linked";
  el("changeReportBtn").style.display = "inline-flex";
  el("analyzeBtn").style.display = "inline-flex";
  el("appRoot").classList.add("show");
  state.reportFolderLinked = !!reportFolder;
  el("noFolderBanner").style.display = state.reportFolderLinked ? "none" : "flex";
}

function openConnectWizard() {
  el("connectScreen").style.display = "flex";
  el("stepFolder").style.display = "none";
  el("stepInstance").style.display = "block";
  loadInstances();
}
el("changeReportBtn").addEventListener("click", openConnectWizard);
el("linkFolderBtn").addEventListener("click", openConnectWizard);

// ==================== ANALYZE ====================

async function analyze() {
  el("analyzeBtn").disabled = true;
  const origText = el("analyzeBtn").textContent;
  el("analyzeBtn").textContent = "Analyzing…";
  el("statusDot").className = "status-pill busy";
  el("statusDot").title = "Analyzing…";
  try {
    const data = await api("/api/analyze", { method: "POST" });
    state.model = data;
    state.nodesById = Object.fromEntries(data.nodes.map((n) => [n.id, n]));

    state.edgesBySource = {};
    state.edgesByTarget = {};
    for (const e of data.edges) {
      (state.edgesBySource[e.source] ||= []).push(e.target);
      (state.edgesByTarget[e.target] ||= []).push(e.source);
    }

    renderStats();
    renderObjectList();
    renderBulkCards();
    loadDaxFunctions(); // fire-and-forget, cached after first successful fetch
    el("statusDot").className = "status-pill on";
    el("statusDot").title = "Connected — model analyzed";
    toast(`Model analyzed: ${data.summary.measureCount} measures, ${data.summary.columnCount} columns, ${data.summary.unusedCount} flagged fully unused.`, "success");
  } catch (e) {
    el("statusDot").className = "status-pill off";
    el("statusDot").title = "Connection error: " + e.message;
    toast(e.message, "error");
  } finally {
    el("analyzeBtn").disabled = false;
    el("analyzeBtn").textContent = origText;
  }
}

// ==================== SIDEBAR: OBJECT LIST ====================
//
// This tool is specifically for cleaning up unused measures & calculated
// columns, so the left panel only ever shows those four buckets — no plain
// source-data columns, no generic usage-level filter chips. The bucket
// itself IS the filter.

const OBJECT_CATEGORIES = [
  { key: "used-measure", label: "Used Measures", match: (n) => n.type === "measure" && n.usageLevel !== "unused" },
  { key: "unused-measure", label: "Unused Measures", match: (n) => n.type === "measure" && n.usageLevel === "unused" },
  { key: "used-column", label: "Used Calculated Columns", match: (n) => n.type === "column" && n.isCalculated && n.usageLevel !== "unused" },
  { key: "unused-column", label: "Unused Calculated Columns", match: (n) => n.type === "column" && n.isCalculated && n.usageLevel === "unused" },
];

function renderStats() {
  const counts = Object.fromEntries(OBJECT_CATEGORIES.map((c) => [c.key, state.model.nodes.filter(c.match).length]));
  const totalMeasures = counts["used-measure"] + counts["unused-measure"];
  const active = (key) => (state.statFilter === key ? " active" : "");
  el("statRow").innerHTML = `
    <div class="stat-card total${active("all")}" data-stat-filter="all"><div class="l">Total Measures</div><div class="n">${totalMeasures}</div></div>
    <div class="stat-card good${active("used-measure")}" data-stat-filter="used-measure"><div class="n">${counts["used-measure"]}</div><div class="l">Used Measures</div></div>
    <div class="stat-card danger${active("unused-measure")}" data-stat-filter="unused-measure"><div class="n">${counts["unused-measure"]}</div><div class="l">Unused Measures</div></div>
    <div class="stat-card good${active("used-column")}" data-stat-filter="used-column"><div class="n">${counts["used-column"]}</div><div class="l">Used Calc. Columns</div></div>
    <div class="stat-card danger${active("unused-column")}" data-stat-filter="unused-column"><div class="n">${counts["unused-column"]}</div><div class="l">Unused Calc. Columns</div></div>
  `;
  el("statRow").querySelectorAll(".stat-card").forEach((card) => {
    card.addEventListener("click", () => {
      state.statFilter = state.statFilter === card.dataset.statFilter ? "all" : card.dataset.statFilter;
      renderStats();
      renderObjectList();
    });
  });
}

function renderObjectList() {
  const list = el("objectList");
  const q = state.search.toLowerCase();

  const buckets = OBJECT_CATEGORIES
    .filter((cat) => state.statFilter === "all" || cat.key === state.statFilter)
    .map((cat) => ({
      ...cat,
      items: state.model.nodes
        .filter((n) => cat.match(n) && (!q || n.name.toLowerCase().includes(q) || n.table.toLowerCase().includes(q)))
        .sort((a, b) => a.table.localeCompare(b.table) || a.name.localeCompare(b.name)),
    }));

  if (!buckets.some((b) => b.items.length)) {
    list.innerHTML = `<div class="empty-state" style="position:static;height:100%;"><div class="big">🔍</div>No objects match "${escapeHtml(state.search)}"</div>`;
    return;
  }

  list.innerHTML = "";
  for (const bucket of buckets) {
    const group = document.createElement("div");
    group.className = "table-group";
    const header = document.createElement("div");
    header.className = "table-header" + (bucket.items.length === 0 ? " collapsed" : "");
    header.innerHTML = `<span class="chevron">▾</span><span>${escapeHtml(bucket.label)}</span><span class="table-count">${bucket.items.length}</span>`;
    const body = document.createElement("div");
    body.style.display = bucket.items.length === 0 ? "none" : "block";
    header.addEventListener("click", () => {
      header.classList.toggle("collapsed");
      body.style.display = header.classList.contains("collapsed") ? "none" : "block";
    });
    group.appendChild(header);

    for (const n of bucket.items) {
      const row = document.createElement("div");
      row.className = "obj-row" + (n.id === state.selectedId ? " selected" : "");
      row.innerHTML = `<span class="obj-icon">${iconFor(n.type)}</span><span class="obj-name">${escapeHtml(n.name)}</span><span class="obj-table-tag" title="${escapeHtml(n.table)}">${escapeHtml(n.table)}</span>`;
      row.addEventListener("click", () => selectNode(n.id));
      body.appendChild(row);
    }
    group.appendChild(body);
    list.appendChild(group);
  }
}

el("searchInput").addEventListener("input", (e) => {
  state.search = e.target.value;
  renderObjectList();
});

document.querySelectorAll(".chip[data-bulk-filter]").forEach((chip) => {
  chip.addEventListener("click", () => {
    document.querySelectorAll(".chip[data-bulk-filter]").forEach((c) => c.classList.remove("active"));
    chip.classList.add("active");
    state.bulkFilter = chip.dataset.bulkFilter;
    renderBulkCards();
  });
});

el("bulkSearchInput").addEventListener("input", (e) => {
  state.bulkSearch = e.target.value;
  renderBulkCards();
});

// ==================== VIEW TABS ====================

document.querySelectorAll(".view-tab").forEach((tab) => {
  tab.addEventListener("click", () => setActiveView(tab.dataset.view));
});

function setActiveView(view) {
  document.querySelectorAll(".view-tab").forEach((t) => t.classList.toggle("active", t.dataset.view === view));
  state.activeView = view;
  el("viewChart").style.display = view === "chart" ? "block" : "none";
  el("viewBulk").style.display = view === "bulk" ? "block" : "none";
  el("viewOrganize").style.display = view === "organize" ? "block" : "none";
  el("viewSnippets").style.display = view === "snippets" ? "block" : "none";
  if (view === "organize") renderOrganizeList();
  if (view === "snippets") renderSnippets();
}

function switchToChartView() {
  setActiveView("chart");
}

// ==================== STATIC DEPENDENCY CHART ====================

function selectNode(id) {
  state.selectedId = id;
  renderObjectList();
  switchToChartView();
  renderDependencyChart(id);
}

function makeCard(id, opts = {}) {
  const n = state.nodesById[id];
  const card = document.createElement("div");
  if (!n) {
    card.className = "dep-card empty-note";
    card.textContent = "(unresolved reference)";
    return card;
  }
  card.className = "dep-card";
  card.dataset.id = id;
  card.innerHTML = `
    <div class="dc-name"><span class="obj-icon">${iconFor(n.type)}</span>${escapeHtml(n.name)}</div>
    <div class="dc-table">${escapeHtml(n.table)}</div>
  `;
  card.addEventListener("click", () => renderDependencyChart(id, { push: true }));
  return card;
}

function emptyNote(text) {
  const d = document.createElement("div");
  d.className = "dep-card empty-note";
  d.textContent = text;
  return d;
}

async function renderDependencyChart(id, opts = {}) {
  if (opts.push) state.selectedId = id;
  const n = state.nodesById[id];
  el("chartEmpty").style.display = n ? "none" : "flex";
  const chart = el("depChart");
  chart.style.display = n ? "block" : "none";
  if (!n) return;

  if (opts.push) renderObjectList();

  chart.innerHTML = "";

  if (n.type === "table") {
    renderTableChart(n, chart);
    return;
  }

  const dependsOnIds = state.edgesBySource[id] || [];
  const usedByIds = state.edgesByTarget[id] || [];

  const wrap = document.createElement("div");
  wrap.className = "dep-columns";
  wrap.style.position = "relative";

  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.id = "depLines";
  wrap.appendChild(svg);

  const leftCol = document.createElement("div");
  leftCol.innerHTML = `<div class="dep-col-title left">Depends on</div>`;
  const leftCards = document.createElement("div");
  leftCards.className = "dep-col left-col";
  leftCards.id = "depColLeft";
  if (!dependsOnIds.length) leftCards.appendChild(emptyNote("References nothing else"));
  else dependsOnIds.forEach((did) => leftCards.appendChild(makeCard(did)));
  leftCol.appendChild(leftCards);

  const centerCol = document.createElement("div");
  const centerCard = document.createElement("div");
  centerCard.className = "dep-center-card";
  centerCard.id = "depCenterCard";
  const pillHtml = `<span class="pill ${n.usageLevel}">${n.usageLevel.replace(/-/g, " ")}</span>`;
  const locationsHtml = n.usedInReport && (n.reportSources || []).length
    ? `<ul class="report-locations">${n.reportSources.map((s) => `<li>📍 ${escapeHtml(s)}</li>`).join("")}</ul>`
    : "";
  centerCard.innerHTML = `
    <div class="dep-center-icon ${n.type}">${iconFor(n.type)}</div>
    <div class="dep-center-name">${escapeHtml(n.name)}</div>
    <div class="dep-center-table" id="centerTableJump">from table: ${escapeHtml(n.table)}</div>
    ${pillHtml}
    <div id="daxBox"></div>
    ${locationsHtml}
    <div id="impactLine"></div>
    <div class="dep-center-actions">
      <button class="danger" id="stageBtn" style="flex:1;justify-content:center;"></button>
    </div>
  `;
  centerCol.appendChild(centerCard);
  if (n.expression) renderDaxExpression(centerCard.querySelector("#daxBox"), n, id);

  const rightCol = document.createElement("div");
  rightCol.innerHTML = `<div class="dep-col-title right">Used by other measures</div>`;
  const rightCards = document.createElement("div");
  rightCards.className = "dep-col right-col";
  rightCards.id = "depColRight";
  if (!usedByIds.length) rightCards.appendChild(emptyNote("Not referenced by any other measure"));
  else usedByIds.forEach((uid) => rightCards.appendChild(makeCard(uid)));
  rightCol.appendChild(rightCards);

  wrap.appendChild(leftCol);
  wrap.appendChild(centerCol);
  wrap.appendChild(rightCol);
  chart.appendChild(wrap);

  centerCard.querySelector("#centerTableJump").addEventListener("click", () => renderDependencyChart("table::" + n.table, { push: true }));

  drawConnectorLines(wrap, svg, dependsOnIds, usedByIds, centerCard);

  // stage/unstage toggle — the only per-object action; the actual delete
  // happens once, later, via the single "Delete Selected" button.
  const stageBtn = centerCard.querySelector("#stageBtn");
  const refreshStageBtn = () => {
    const staged = !!state.pending[id];
    stageBtn.textContent = staged ? "✓ Selected for deletion — click to remove" : "Select for deletion";
    stageBtn.classList.toggle("primary", !staged);
  };
  refreshStageBtn();
  stageBtn.addEventListener("click", () => {
    toggleStage(n, usedByIds.length);
    refreshStageBtn();
  });

  // transitive impact, computed client-side (no round trip needed)
  const impactLine = centerCard.querySelector("#impactLine");
  const deps = transitiveDependents(id);
  const impact = visualImpact(id);
  if (impact.direct) {
    const otherVia = impact.via.filter((v) => v !== id);
    const extra = otherVia.length
      ? ` It also feeds <b>${escapeHtml(otherVia.map((v) => state.nodesById[v]?.name).filter(Boolean).slice(0, 2).join(", "))}</b>, which would break too.`
      : "";
    impactLine.innerHTML = `<div class="impact-line risky">⚠ Deleting this WILL break a report visual — it's placed directly on ${escapeHtml((n.reportSources || [])[0] || "a visual")}.${extra}</div>`;
  } else if (impact.breaks) {
    const names = impact.via.map((v) => state.nodesById[v]?.name).filter(Boolean).slice(0, 3).join(", ");
    const more = impact.via.length > 3 ? ` (+${impact.via.length - 3} more)` : "";
    impactLine.innerHTML = `<div class="impact-line risky">⚠ Deleting this WILL break a report visual — it feeds into <b>${escapeHtml(names)}</b>${more}, which ${impact.via.length > 1 ? "are" : "is"} used on a report page.</div>`;
  } else if (deps.size) {
    impactLine.innerHTML = `<div class="impact-line risky">${deps.size} measure(s) transitively depend on this, but none reach a report visual — deleting it breaks those measures only, not anything visible on a page.</div>`;
  } else {
    impactLine.innerHTML = `<div class="impact-line safe">✓ Nothing depends on this, directly or indirectly — and no report visual will break.</div>`;
  }
}

// ==================== DAX FUNCTION EXPLAINER ====================
//
// Two layers: (1) a static, offline reference library for every DAX
// function (fetched once), shown as a popover when you click a function
// name; (2) a rule-based, per-measure "what does this actually calculate"
// summary computed server-side by walking the expression's call structure
// — no AI, no network call beyond this app's own backend, so it works
// fully offline and never sends your DAX to a third party.

let daxFunctionsLoaded = false;
function loadDaxFunctions() {
  if (daxFunctionsLoaded) return;
  api("/api/dax-functions").then((data) => {
    state.daxFunctions = Object.fromEntries((data.functions || []).map((f) => [f.name, f]));
    daxFunctionsLoaded = true;
  }).catch(() => {}); // reference popovers just won't resolve names if this fails — non-critical
}

// ==================== MOST USED DAX (snippet library) ====================
//
// A static reference library of common DAX patterns (Date table, time
// intelligence, ranking, ...) — not tied to the connected model, just
// something to browse and copy-paste. Fetched once and cached, same as
// the function reference.

state.snippets = null; // [{name, category, description, dax, functionsUsed}], fetched once
state.snippetSearch = "";

function renderSnippets() {
  const list = el("snippetList");
  if (!state.snippets) {
    list.innerHTML = `<div class="empty-state" style="position:static;height:100%;">Loading…</div>`;
    api("/api/dax-snippets").then((data) => {
      state.snippets = data.snippets || [];
      renderSnippets();
    }).catch((e) => {
      list.innerHTML = `<div class="empty-state" style="position:static;height:100%;">Failed to load: ${escapeHtml(e.message)}</div>`;
    });
    return;
  }

  const q = state.snippetSearch.trim().toLowerCase();
  const matches = state.snippets.filter((s) =>
    !q || s.name.toLowerCase().includes(q) || s.description.toLowerCase().includes(q) || s.category.toLowerCase().includes(q) || s.dax.toLowerCase().includes(q)
  );

  if (!matches.length) {
    list.innerHTML = `<div class="empty-state" style="position:static;height:100%;"><div class="big">🔍</div>No patterns match "${escapeHtml(state.snippetSearch)}"</div>`;
    return;
  }

  const byCategory = {};
  for (const s of matches) (byCategory[s.category] ||= []).push(s);

  list.innerHTML = "";
  for (const category of Object.keys(byCategory)) {
    const section = document.createElement("div");
    const title = document.createElement("div");
    title.className = "snippet-category-title";
    title.textContent = category;
    section.appendChild(title);

    const grid = document.createElement("div");
    grid.className = "snippet-grid";
    for (const s of byCategory[category]) {
      const card = document.createElement("div");
      card.className = "snippet-card";
      card.innerHTML = `
        <div class="sc-name">${escapeHtml(s.name)}</div>
        <div class="sc-desc">${escapeHtml(s.description)}</div>
        <pre class="dax">${highlightDax(s.dax, s.functionsUsed)}</pre>
        <div class="sc-toolbar">
          <button class="sc-deploy">🚀 Deploy to Report</button>
          <button class="ghost sc-copy">📋 Copy</button>
        </div>
      `;
      card.querySelectorAll(".fn").forEach((elFn) => {
        elFn.addEventListener("click", (e) => showFnPopover(e.currentTarget, e.currentTarget.dataset.fn));
      });
      const copyBtn = card.querySelector(".sc-copy");
      copyBtn.addEventListener("click", () => {
        navigator.clipboard.writeText(s.dax).then(() => {
          copyBtn.textContent = "✓ Copied";
          copyBtn.classList.add("copied");
          setTimeout(() => { copyBtn.textContent = "📋 Copy"; copyBtn.classList.remove("copied"); }, 1500);
        }).catch(() => toast("Couldn't copy — select the text manually.", "error"));
      });
      const deployBtn = card.querySelector(".sc-deploy");
      if (deployBtn) deployBtn.addEventListener("click", () => openSnippetDeployModal(s));
      grid.appendChild(card);
    }
    section.appendChild(grid);
    list.appendChild(section);
  }
}

el("snippetSearchInput").addEventListener("input", (e) => {
  state.snippetSearch = e.target.value;
  renderSnippets();
});

// ==================== SNIPPET DEPLOY MODAL + AUTOCOMPLETE ====================
//
// The templates in the snippet library reference placeholder tables/columns
// (Sales[Amount], 'Date'[Date], ...) that won't exist in the user's real
// model. This modal lets them rename the object, pick a real target table,
// and edit the DAX with Power-BI-style autocomplete pulled from the
// already-connected model (state.model.nodes) — no extra API call needed.

function openSnippetDeployModal(snippet) {
  const isTable = snippet.kind === "table";
  el("snippetDeployKindLabel").textContent = snippet.kind === "column" ? "Column name" : isTable ? "Table name" : "Measure name";
  el("snippetDeployName").value = snippet.defaultObjectName;
  el("snippetDeployExpr").value = snippet.expression;
  el("snippetDeployWarning").style.display = "none";
  el("snippetDeployTableRow").style.display = isTable ? "none" : "block";

  const tableSelect = el("snippetDeployTable");
  const tables = (state.model?.nodes || []).filter((n) => n.type === "table").map((n) => n.name).sort();
  tableSelect.innerHTML = tables.map((t) => `<option value="${escapeHtml(t)}">${escapeHtml(t)}</option>`).join("");
  if (!isTable && !tables.length) {
    el("snippetDeployWarning").style.display = "block";
    el("snippetDeployWarning").textContent = "No tables found — connect and analyze a report first.";
  }

  el("snippetDeployModal").classList.add("show");
  el("snippetDeployExpr").focus();

  el("snippetDeployConfirm").onclick = async () => {
    const kind = snippet.kind;
    const table = isTable ? "" : tableSelect.value;
    const name = el("snippetDeployName").value.trim();
    const expression = el("snippetDeployExpr").value.trim();
    if ((!isTable && !table) || !name || !expression) {
      toast("Fill in a name" + (isTable ? "" : " and table") + " before deploying.", "error");
      return;
    }
    el("snippetDeployConfirm").disabled = true;
    try {
      const res = await api("/api/deploy-snippet", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind, table, name, expression }),
      });
      el("snippetDeployModal").classList.remove("show");
      toast(`Deployed "${res.name}"${isTable ? "" : ` to ${res.table}`}. Backup: ${res.backup}`, "success");
      await analyze(); // pulls the new object into the sidebar/stats immediately
    } catch (e) {
      toast("Deploy failed: " + e.message, "error");
    } finally {
      el("snippetDeployConfirm").disabled = false;
    }
  };
}

el("snippetDeployCancel").addEventListener("click", () => el("snippetDeployModal").classList.remove("show"));

// -- autocomplete --------------------------------------------------------

function snippetAutocompleteSuggestions() {
  // Built fresh each open rather than cached, since the model can change
  // between visits to this tab (re-analyze, deploys, deletions).
  const out = [];
  for (const n of state.model?.nodes || []) {
    if (n.type === "column") out.push({ text: `${n.table}[${n.name}]`, sortKey: n.name, icon: "▤", table: n.table });
    else if (n.type === "measure") out.push({ text: `[${n.name}]`, sortKey: n.name, icon: "ƒ", table: n.table });
  }
  return out;
}

// Computes the pixel position of a textarea's caret by mirroring its text
// into an identically-styled hidden div — textareas have no native API for
// this. Standard technique (same one the "textarea-caret-position" library
// uses), inlined here since it's the only place in the app that needs it.
function textareaCaretPixelPosition(textarea, position) {
  const div = document.createElement("div");
  const style = getComputedStyle(textarea);
  const mirrored = [
    "boxSizing", "width", "borderTopWidth", "borderRightWidth", "borderBottomWidth", "borderLeftWidth",
    "paddingTop", "paddingRight", "paddingBottom", "paddingLeft", "fontStyle", "fontWeight", "fontSize",
    "lineHeight", "fontFamily", "letterSpacing", "textTransform", "whiteSpace", "wordWrap",
  ];
  mirrored.forEach((p) => { div.style[p] = style[p]; });
  div.style.position = "absolute";
  div.style.visibility = "hidden";
  div.style.whiteSpace = "pre-wrap";
  div.style.wordWrap = "break-word";
  div.style.top = "0";
  div.style.left = "-9999px";
  document.body.appendChild(div);
  div.textContent = textarea.value.substring(0, position);
  const span = document.createElement("span");
  span.textContent = textarea.value.substring(position) || ".";
  div.appendChild(span);
  const rect = textarea.getBoundingClientRect();
  const coords = {
    top: rect.top + span.offsetTop - textarea.scrollTop,
    left: rect.left + span.offsetLeft - textarea.scrollLeft,
    lineHeight: parseFloat(style.lineHeight) || 16,
  };
  document.body.removeChild(div);
  return coords;
}

// Finds the identifier-ish run of characters immediately before the caret
// (letters/digits/underscore, plus [ ] ' for bracket/table refs) — that's
// the partial text we filter suggestions against and replace on selection.
function currentAutocompleteQuery(textarea) {
  const pos = textarea.selectionStart;
  const text = textarea.value;
  let start = pos;
  while (start > 0 && /[A-Za-z0-9_\[\]']/.test(text[start - 1])) start--;
  return { start, end: pos, query: text.slice(start, pos) };
}

let acActiveIndex = -1;
let acCurrentMatches = [];

function hideSnippetAutocomplete() {
  el("snippetAutocomplete").classList.remove("show");
  acActiveIndex = -1;
  acCurrentMatches = [];
}

function updateSnippetAutocomplete() {
  const textarea = el("snippetDeployExpr");
  const { query } = currentAutocompleteQuery(textarea);
  const cleanQuery = query.replace(/[\[\]']/g, "");
  if (!cleanQuery) { hideSnippetAutocomplete(); return; }

  const all = snippetAutocompleteSuggestions();
  const matches = all
    .filter((s) => s.sortKey.toLowerCase().includes(cleanQuery.toLowerCase()))
    .sort((a, b) => a.sortKey.length - b.sortKey.length) // shortest/closest match first
    .slice(0, 30);

  if (!matches.length) { hideSnippetAutocomplete(); return; }

  acCurrentMatches = matches;
  acActiveIndex = 0;
  const box = el("snippetAutocomplete");
  box.innerHTML = matches.map((m, i) =>
    `<div class="sdm-ac-item${i === 0 ? " active" : ""}" data-i="${i}"><span class="ac-icon">${m.icon}</span>${escapeHtml(m.text)}${m.table ? `<span class="ac-table">${escapeHtml(m.table)}</span>` : ""}</div>`
  ).join("");
  box.querySelectorAll(".sdm-ac-item").forEach((item) => {
    item.addEventListener("mousedown", (e) => {
      e.preventDefault(); // keep textarea focus so selectionStart is still valid
      applyAutocompleteSelection(Number(item.dataset.i));
    });
  });

  const pos = textareaCaretPixelPosition(textarea, textarea.selectionStart);
  box.style.top = `${pos.top + pos.lineHeight + 4}px`;
  box.style.left = `${pos.left}px`;
  box.classList.add("show");
}

function applyAutocompleteSelection(index) {
  const textarea = el("snippetDeployExpr");
  const match = acCurrentMatches[index];
  if (!match) return;
  const { start, end } = currentAutocompleteQuery(textarea);
  const before = textarea.value.slice(0, start);
  const after = textarea.value.slice(end);
  textarea.value = before + match.text + after;
  const newPos = before.length + match.text.length;
  textarea.setSelectionRange(newPos, newPos);
  hideSnippetAutocomplete();
  textarea.focus();
}

el("snippetDeployExpr").addEventListener("input", updateSnippetAutocomplete);
el("snippetDeployExpr").addEventListener("click", hideSnippetAutocomplete);
el("snippetDeployExpr").addEventListener("blur", () => setTimeout(hideSnippetAutocomplete, 150));
el("snippetDeployExpr").addEventListener("keydown", (e) => {
  if (!el("snippetAutocomplete").classList.contains("show")) return;
  if (e.key === "ArrowDown") {
    e.preventDefault();
    acActiveIndex = Math.min(acActiveIndex + 1, acCurrentMatches.length - 1);
  } else if (e.key === "ArrowUp") {
    e.preventDefault();
    acActiveIndex = Math.max(acActiveIndex - 1, 0);
  } else if (e.key === "Enter" || e.key === "Tab") {
    e.preventDefault();
    applyAutocompleteSelection(acActiveIndex);
    return;
  } else if (e.key === "Escape") {
    hideSnippetAutocomplete();
    return;
  } else {
    return;
  }
  el("snippetAutocomplete").querySelectorAll(".sdm-ac-item").forEach((item, i) => {
    item.classList.toggle("active", i === acActiveIndex);
  });
});

function highlightDax(expression, functionsUsed) {
  if (!functionsUsed || !functionsUsed.length) return escapeHtml(expression);
  const sorted = [...functionsUsed].sort((a, b) => a.start - b.start);
  let out = "";
  let cursor = 0;
  for (const f of sorted) {
    if (f.start < cursor) continue; // overlapping/duplicate — skip
    out += escapeHtml(expression.slice(cursor, f.start));
    out += `<span class="fn" data-fn="${escapeHtml(f.name)}">${escapeHtml(expression.slice(f.start, f.end))}</span>`;
    cursor = f.end;
  }
  out += escapeHtml(expression.slice(cursor));
  return out;
}

state.formatCache = {}; // nodeId -> {formatted, functionsUsed} from /api/format
state.lintCache = {};   // nodeId -> {findings} from /api/dax-lint

function renderLintPanel(n, lint) {
  const findings = lint.findings || [];
  const sevIcon = (s) => (s === "warning" ? "⚠" : "ℹ");
  return `
    <div class="explain-panel lint-panel">
      <div class="explain-header" id="lintHeader">
        <span>🛠️ Suggestions ${findings.length ? `<span class="lint-count-badge">${findings.length}</span>` : ""}</span>
        <span class="chevron">▾</span>
      </div>
      <div class="explain-body" id="lintBody">
        ${findings.length ? findings.map((f) => `
          <div class="lint-finding lint-${f.severity}">
            <div class="lf-head">
              <span class="lf-sev">${sevIcon(f.severity)}</span>
              <span class="lf-title">${escapeHtml(f.title)}</span>
              <span class="lf-cat">${escapeHtml(f.category)}</span>
            </div>
            <div class="lf-msg">${escapeHtml(f.message)}</div>
            <code class="lf-snippet">${escapeHtml(n.expression.slice(f.start, f.end))}</code>
          </div>
        `).join("") : `<div class="lint-clean">✓ No issues found by these checks.</div>`}
      </div>
    </div>
  `;
}

function renderDaxExpression(box, n, nodeId) {
  // Render the raw (unhighlighted) expression immediately so there's no
  // blank flash, then upgrade to the beautified, highlighted version (plus
  // the explain + lint panels) once /api/format, /api/explain, and
  // /api/dax-lint all resolve — each cached per node so re-selecting the
  // same object is instant.
  box.innerHTML = `<pre class="dax">${escapeHtml(n.expression)}</pre>`;

  const apply = (fmt, result, lint) => {
    // the user may have clicked to a different node before this resolved
    if (state.selectedId !== nodeId) return;
    const canDeploy = n.type === "measure";
    box.innerHTML = `
      <pre class="dax">${highlightDax(fmt.formatted, fmt.functionsUsed)}</pre>
      ${canDeploy ? `
        <div class="dax-toolbar">
          <button class="ghost" id="formatDeployBtn">🪄 Save formatted DAX to Power BI</button>
          <span class="dax-status" id="daxFormatStatus">${fmt.formatted !== n.expression ? "Reformatted for display — not yet saved to the model" : "Already matches the live model's formatting"}</span>
        </div>
      ` : ""}
      <div class="explain-panel">
        <div class="explain-header" id="explainHeader"><span>💡 Explain this measure</span><span class="chevron">▾</span></div>
        <div class="explain-body" id="explainBody">
          ${escapeHtml(result.summary)}
          ${result.functionsUsed && result.functionsUsed.length ? `<div class="explain-fnlist">${[...new Set(result.functionsUsed.map((f) => f.name))].map((name) => `<span class="fn-chip" data-fn="${escapeHtml(name)}">${escapeHtml(name)}</span>`).join("")}</div>` : ""}
        </div>
      </div>
      ${renderLintPanel(n, lint)}
    `;
    box.querySelectorAll(".explain-header").forEach((header) => {
      const body = header.nextElementSibling;
      header.addEventListener("click", () => {
        header.classList.toggle("open");
        body.classList.toggle("open");
      });
    });
    box.querySelectorAll(".fn, .fn-chip").forEach((el) => {
      el.addEventListener("click", (e) => showFnPopover(e.currentTarget, e.currentTarget.dataset.fn));
    });
    const deployBtn = box.querySelector("#formatDeployBtn");
    if (deployBtn) {
      deployBtn.addEventListener("click", () => openFormatDeployModal(n, nodeId, fmt.formatted));
    }
  };

  const formatP = state.formatCache[nodeId] ? Promise.resolve(state.formatCache[nodeId])
    : api(`/api/format?id=${encodeURIComponent(nodeId)}`).then((fmt) => { state.formatCache[nodeId] = fmt; return fmt; });
  const explainP = state.explainCache[nodeId] ? Promise.resolve(state.explainCache[nodeId])
    : api(`/api/explain?id=${encodeURIComponent(nodeId)}`).then((result) => { state.explainCache[nodeId] = result; return result; });
  const lintP = state.lintCache[nodeId] ? Promise.resolve(state.lintCache[nodeId])
    : api(`/api/dax-lint?id=${encodeURIComponent(nodeId)}`).then((lint) => { state.lintCache[nodeId] = lint; return lint; });

  Promise.all([formatP, explainP, lintP]).then(([fmt, result, lint]) => apply(fmt, result, lint)).catch(() => {}); // leave the plain, raw expression showing — still useful on its own
}

function openFormatDeployModal(n, nodeId, formattedText) {
  el("formatDeployName").textContent = n.name;
  el("formatDeployPreview").textContent = formattedText;
  el("formatDeployModal").classList.add("show");
  el("formatDeployConfirm").onclick = async () => {
    el("formatDeployModal").classList.remove("show");
    try {
      const res = await api("/api/format-deploy", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: nodeId }),
      });
      toast(`Saved formatted DAX for "${n.name}" to Power BI. Backup: ${res.backup}`, "success");
      delete state.explainCache[nodeId];
      delete state.formatCache[nodeId];
      delete state.lintCache[nodeId];
      await analyze();
      renderDependencyChart(nodeId);
    } catch (e) {
      toast("Save failed: " + e.message, "error");
    }
  };
}
el("formatDeployCancel").addEventListener("click", () => el("formatDeployModal").classList.remove("show"));

// ==================== FORMAT ALL MEASURES ====================
//
// Same beautifier as the per-measure "Save formatted DAX" action, just
// batched across the whole model in one backup + one TOM transaction
// instead of one round-trip per measure.

el("formatAllBtn").addEventListener("click", async () => {
  el("formatAllModal").classList.add("show");
  el("formatAllSummary").textContent = "Checking which measures need reformatting…";
  el("formatAllList").style.display = "none";
  el("formatAllConfirm").disabled = true;
  try {
    const preview = await api("/api/format-all-preview");
    if (preview.count === 0) {
      el("formatAllSummary").textContent = "Every measure already matches its beautified formatting — nothing to do.";
      return;
    }
    el("formatAllSummary").textContent = `${preview.count} measure(s) will be reformatted and saved to Power BI:`;
    el("formatAllList").style.display = "block";
    el("formatAllList").innerHTML = preview.names.map((n) => escapeHtml(n)).join("<br>");
    el("formatAllConfirm").disabled = false;
  } catch (e) {
    el("formatAllSummary").textContent = "Couldn't check measures: " + e.message;
  }
});

el("formatAllConfirm").addEventListener("click", async () => {
  el("formatAllConfirm").disabled = true;
  try {
    const res = await api("/api/format-all-deploy", { method: "POST" });
    el("formatAllModal").classList.remove("show");
    toast(`Reformatted ${res.count} measure(s). Backup: ${res.backup}`, "success");
    state.explainCache = {};
    state.formatCache = {};
    state.lintCache = {};
    await analyze();
  } catch (e) {
    toast("Format All failed: " + e.message, "error");
  } finally {
    el("formatAllConfirm").disabled = false;
  }
});

el("formatAllCancel").addEventListener("click", () => el("formatAllModal").classList.remove("show"));

function showFnPopover(anchorEl, fnName) {
  const pop = el("fnPopover");
  const info = state.daxFunctions[fnName];
  if (!info) {
    // library hasn't loaded yet (e.g. clicked immediately after connect) — try once more
    loadDaxFunctions();
    return;
  }
  pop.innerHTML = `
    <div class="fp-name">${escapeHtml(info.name)}</div>
    <div class="fp-cat">${escapeHtml(info.category)}</div>
    <div class="fp-desc">${escapeHtml(info.description)}</div>
    <div class="fp-label">Syntax</div>
    <pre>${escapeHtml(info.syntax)}</pre>
    <div class="fp-label">Example</div>
    <pre>${escapeHtml(info.example)}</pre>
  `;
  const rect = anchorEl.getBoundingClientRect();
  pop.classList.add("show");
  const popRect = pop.getBoundingClientRect();
  let left = rect.left;
  let top = rect.bottom + 6;
  if (left + popRect.width > window.innerWidth - 12) left = window.innerWidth - popRect.width - 12;
  if (top + popRect.height > window.innerHeight - 12) top = rect.top - popRect.height - 6;
  pop.style.left = `${Math.max(12, left)}px`;
  pop.style.top = `${Math.max(12, top)}px`;
}

document.addEventListener("click", (e) => {
  const pop = el("fnPopover");
  if (!pop.classList.contains("show")) return;
  if (pop.contains(e.target) || e.target.closest(".fn, .fn-chip")) return;
  pop.classList.remove("show");
});

function renderTableChart(n, chart) {
  const measures = state.model.nodes.filter((x) => x.type === "measure" && x.table === n.name);
  const columns = state.model.nodes.filter((x) => x.type === "column" && x.table === n.name);
  const wrap = document.createElement("div");
  wrap.style.maxWidth = "900px";
  wrap.style.margin = "0 auto";
  wrap.innerHTML = `
    <div class="dep-center-card" style="max-width:none;">
      <div class="dep-center-icon" style="background:linear-gradient(135deg,#7c5cff,#5b3fd6);">▦</div>
      <div class="dep-center-name">${escapeHtml(n.name)}</div>
      <div style="color:var(--text-dim);font-size:12px;margin-top:2px;">Table · ${measures.length} measure(s), ${columns.length} column(s)</div>
    </div>
  `;
  const grid = document.createElement("div");
  grid.style.cssText = "display:grid;grid-template-columns:1fr 1fr;gap:24px;margin-top:20px;";
  const mCol = document.createElement("div");
  mCol.innerHTML = `<div class="dep-col-title">Measures</div>`;
  const mList = document.createElement("div");
  mList.className = "dep-col";
  if (!measures.length) mList.appendChild(emptyNote("No measures on this table"));
  else measures.forEach((m) => mList.appendChild(makeCard(m.id)));
  mCol.appendChild(mList);

  const cCol = document.createElement("div");
  cCol.innerHTML = `<div class="dep-col-title">Columns</div>`;
  const cList = document.createElement("div");
  cList.className = "dep-col";
  if (!columns.length) cList.appendChild(emptyNote("No columns on this table"));
  else columns.forEach((c) => cList.appendChild(makeCard(c.id)));
  cCol.appendChild(cList);

  grid.appendChild(mCol);
  grid.appendChild(cCol);
  wrap.appendChild(grid);
  chart.appendChild(wrap);
}

function drawConnectorLines(wrap, svg, dependsOnIds, usedByIds, centerCard) {
  // Synchronous, not rAF-deferred: layout from the appendChild calls above
  // is already committed by the time this runs, getBoundingClientRect()
  // doesn't need a frame boundary, and rAF can be starved in background/
  // unfocused tabs, which would leave the chart with no connector lines.
  const wrapRect = wrap.getBoundingClientRect();
  svg.setAttribute("width", wrapRect.width);
  svg.setAttribute("height", wrapRect.height);
  svg.innerHTML = "";

  const centerRect = centerCard.getBoundingClientRect();
  const centerLeft = { x: centerRect.left - wrapRect.left, y: centerRect.top - wrapRect.top + centerRect.height / 2 };
  const centerRight = { x: centerRect.right - wrapRect.left, y: centerRect.top - wrapRect.top + centerRect.height / 2 };

  const drawPath = (x1, y1, x2, y2, color) => {
    const midX = (x1 + x2) / 2;
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", `M ${x1} ${y1} C ${midX} ${y1}, ${midX} ${y2}, ${x2} ${y2}`);
    path.setAttribute("stroke", color);
    path.setAttribute("stroke-width", "1.5");
    path.setAttribute("fill", "none");
    svg.appendChild(path);
  };

  wrap.querySelectorAll("#depColLeft .dep-card:not(.empty-note)").forEach((cardEl) => {
    const r = cardEl.getBoundingClientRect();
    const anchor = { x: r.right - wrapRect.left, y: r.top - wrapRect.top + r.height / 2 };
    drawPath(anchor.x, anchor.y, centerLeft.x, centerLeft.y, "rgba(77,127,255,0.45)");
  });
  wrap.querySelectorAll("#depColRight .dep-card:not(.empty-note)").forEach((cardEl) => {
    const r = cardEl.getBoundingClientRect();
    const anchor = { x: r.left - wrapRect.left, y: r.top - wrapRect.top + r.height / 2 };
    drawPath(centerRight.x, centerRight.y, anchor.x, anchor.y, "rgba(251,191,36,0.5)");
  });
}

// ==================== BULK CLEANUP: DIAGRAM CARDS ====================
//
// Each row is a tiny flow diagram — [measure] → [outcome] — instead of a
// text table. The outcome box is the whole point: green means nothing
// happens if you delete it, amber means only another unused measure feels
// it, red means an actual report visual breaks. Color + one icon is
// readable at a glance, no legend required.

function outcomeFor(n) {
  const depCount = (state.edgesByTarget[n.id] || []).length;
  const impact = visualImpact(n.id);
  if (impact.breaks) {
    const names = impact.via.map((v) => state.nodesById[v]?.name).filter(Boolean);
    return {
      kind: "danger",
      icon: "📊",
      label: `Breaks ${impact.via.length} report visual${impact.via.length > 1 ? "s" : ""}`,
      title: "Feeds into: " + names.join(", "),
    };
  }
  if (depCount > 0) {
    return { kind: "warn", icon: "🔗", label: `Used by ${depCount} other measure${depCount > 1 ? "s" : ""}`, title: "" };
  }
  return { kind: "safe", icon: "✅", label: "Nothing depends on this", title: "" };
}

function getBulkRows() {
  const q = state.bulkSearch.trim().toLowerCase();
  const rows = state.model.nodes.filter((n) => {
    if (n.type !== "measure" || n.usedInReport) return false;
    if (state.bulkFilter === "unused" && n.usageLevel !== "unused") return false;
    if (q && !n.name.toLowerCase().includes(q)) return false;
    return true; // "all" — everything not on a report visual, including still-referenced-by-other-measures
  });
  rows.sort((a, b) => a.table.localeCompare(b.table) || a.name.localeCompare(b.name));
  return rows;
}

function renderBulkCards() {
  const wrap = el("bulkCardsWrap");
  const q = state.bulkSearch.trim().toLowerCase();
  const rows = getBulkRows();

  el("bulkToolbarTitle").textContent = state.bulkFilter === "unused"
    ? "Truly unused — safe to delete, nothing depends on these"
    : "Not placed on any report visual — includes some still used by other measures";

  const tag = el("bulkCountTag");
  if (rows.length) { tag.style.display = "inline-block"; tag.textContent = rows.length; }
  else tag.style.display = "none";

  if (!rows.length) {
    el("bulkEmpty").style.display = "flex";
    el("bulkEmpty").innerHTML = q
      ? `<div class="big">🔍</div>No measures match "${escapeHtml(state.bulkSearch)}"`
      : `<div class="big">✅</div>Nothing unreferenced — every measure is used somewhere.`;
    wrap.innerHTML = "";
    return;
  }
  el("bulkEmpty").style.display = "none";

  wrap.innerHTML = "";
  for (const n of rows) {
    const outcome = outcomeFor(n);
    const card = document.createElement("div");
    card.className = "bulk-card" + (state.pending[n.id] ? " checked" : "");
    card.title = outcome.title;
    card.innerHTML = `
      <input type="checkbox" title="Select for deletion" ${state.pending[n.id] ? "checked" : ""} />
      <div class="mini-flow" title="Click to view its full dependency chart">
        <div class="flow-node measure-node">ƒ ${escapeHtml(n.name)}</div>
        <div class="flow-arrow">→</div>
        <div class="flow-node outcome ${outcome.kind}">${outcome.icon} ${escapeHtml(outcome.label)}</div>
      </div>
    `;
    const checkbox = card.querySelector("input");
    const flow = card.querySelector(".mini-flow");
    checkbox.addEventListener("click", (e) => {
      e.stopPropagation();
      toggleStage(n);
      card.classList.toggle("checked", !!state.pending[n.id]);
      checkbox.checked = !!state.pending[n.id];
    });
    // clicking the diagram itself (not the checkbox) opens the same static
    // dependency chart used everywhere else in the tool, so you can verify
    // before deciding — not just trust the one-line outcome badge.
    flow.addEventListener("click", () => selectNode(n.id));
    wrap.appendChild(card);
  }
}

el("bulkSelectSafeBtn").addEventListener("click", () => {
  let count = 0;
  for (const n of state.model.nodes) {
    if (n.type !== "measure" || n.usedInReport) continue;
    if (!visualImpact(n.id).breaks) { stage(n); count++; }
  }
  renderBulkCards();
  renderPending();
  toast(`Selected ${count} measure(s) that don't break any report visual.`, "success");
});
el("bulkSelectAllBtn").addEventListener("click", () => {
  for (const n of state.model.nodes) {
    if (n.type !== "measure" || n.usedInReport) continue;
    stage(n);
  }
  renderBulkCards();
  renderPending();
});
// Stages only whatever's currently on screen — respects both the search box
// and the "Truly unused" / "All not on a visual" chip, unlike "Select all"
// which always grabs every not-on-a-visual measure regardless of filters.
el("bulkSelectShownBtn").addEventListener("click", () => {
  const rows = getBulkRows();
  for (const n of rows) stage(n);
  renderBulkCards();
  renderPending();
  toast(`Selected ${rows.length} shown measure(s).`, "success");
});
el("bulkClearBtn").addEventListener("click", () => {
  state.pending = {};
  renderBulkCards();
  renderPending();
});

// ==================== STAGING (shared by chart + bulk table) ====================

function stage(n) {
  state.pending[n.id] = { id: n.id, type: n.type, table: n.table, name: n.name };
}
function unstage(id) {
  delete state.pending[id];
}
function toggleStage(n, depCount) {
  if (state.pending[n.id]) {
    unstage(n.id);
  } else {
    stage(n);
  }
  renderPending();
}

function renderPending() {
  const items = Object.values(state.pending);
  const countEl = el("pendingCount");
  if (items.length) { countEl.style.display = "inline-block"; countEl.textContent = items.length; }
  else countEl.style.display = "none";

  const list = el("pendingList");
  el("deployBtn").disabled = items.length === 0;
  if (!items.length) {
    list.innerHTML = "Nothing staged yet. Select objects from the dependency chart or the Bulk Cleanup list.";
    return;
  }
  list.innerHTML = "";
  for (const p of items) {
    const row = document.createElement("div");
    row.className = "pending-item";
    row.innerHTML = `<span>${escapeHtml(p.table)}[${escapeHtml(p.name)}]</span><span class="x" title="Remove from selection">✕</span>`;
    row.querySelector(".x").addEventListener("click", () => {
      unstage(p.id);
      renderPending();
      renderBulkCards();
      if (state.selectedId === p.id) renderDependencyChart(p.id);
    });
    list.appendChild(row);
  }
}

// ==================== THE ONE DELETE BUTTON ====================

el("deployBtn").addEventListener("click", () => {
  el("deployCount").textContent = Object.keys(state.pending).length;
  el("deployModal").classList.add("show");
});
el("deployModalCancel").onclick = () => el("deployModal").classList.remove("show");
el("deployModalConfirm").onclick = async () => {
  el("deployModal").classList.remove("show");
  try {
    const deletions = Object.values(state.pending).map((p) => ({ type: p.type, table: p.table, name: p.name }));
    const res = await api("/api/deploy", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ deletions }),
    });
    toast(`Deleted ${res.count} object(s) from the model. Backup: ${res.backup}`, "success");
    state.pending = {};
    renderPending();
    analyze();
  } catch (e) {
    toast("Delete failed: " + e.message, "error");
  }
};

// ==================== ORGANIZE FOLDERS (multi-select, multi-folder) ====================
//
// Not a one-shot "everything into one folder" tool: check any subset of
// measures, type a folder name, apply — then repeat with a different subset
// and a different folder name, as many times as needed in one sitting.
// Selection persists across repeated applies so grouping a whole model into
// several folders is a handful of clicks, not one modal per measure.

state.organizeSelected = {}; // id -> true
state.organizeSearch = "";
state.organizeUsageFilter = "all"; // "all" | "used" | "unused"

function organizeMatchesUsage(n) {
  if (state.organizeUsageFilter === "used") return n.usageLevel !== "unused";
  if (state.organizeUsageFilter === "unused") return n.usageLevel === "unused";
  return true;
}

function renderOrganizeList() {
  const list = el("organizeList");
  if (!state.model) {
    list.innerHTML = `<div class="empty-state" style="position:static;height:100%;">Connect and analyze a report first.</div>`;
    return;
  }
  const q = state.organizeSearch.toLowerCase();
  const byTable = {};
  for (const n of state.model.nodes) {
    if (n.type !== "measure") continue;
    if (!organizeMatchesUsage(n)) continue;
    if (q && !n.name.toLowerCase().includes(q) && !n.table.toLowerCase().includes(q)) continue;
    (byTable[n.table] ||= []).push(n);
  }

  const tables = Object.keys(byTable).sort();
  if (!tables.length) {
    list.innerHTML = `<div class="empty-state" style="position:static;height:100%;">No measures match this filter.</div>`;
    updateOrganizeSelCount();
    return;
  }

  list.innerHTML = "";
  for (const table of tables) {
    const group = document.createElement("div");
    group.className = "organize-table-group";
    group.innerHTML = `<div class="organize-table-header">${escapeHtml(table)}</div>`;
    for (const n of byTable[table].sort((a, b) => a.name.localeCompare(b.name))) {
      const row = document.createElement("div");
      const selected = !!state.organizeSelected[n.id];
      row.className = "organize-row" + (selected ? " selected" : "");
      const folderLabel = n.displayFolder ? n.displayFolder : "no folder";
      row.innerHTML = `
        <input type="checkbox" ${selected ? "checked" : ""} />
        <span class="om-name">ƒ ${escapeHtml(n.name)}</span>
        <span class="om-folder${n.displayFolder ? " has-folder" : ""}">📁 ${escapeHtml(folderLabel)}</span>
      `;
      const checkbox = row.querySelector("input");
      const toggle = () => {
        if (state.organizeSelected[n.id]) delete state.organizeSelected[n.id];
        else state.organizeSelected[n.id] = true;
        row.classList.toggle("selected", !!state.organizeSelected[n.id]);
        checkbox.checked = !!state.organizeSelected[n.id];
        updateOrganizeSelCount();
      };
      checkbox.addEventListener("click", (e) => { e.stopPropagation(); toggle(); });
      row.addEventListener("click", toggle);
      group.appendChild(row);
    }
    list.appendChild(group);
  }
  updateOrganizeSelCount();
}

function updateOrganizeSelCount() {
  const n = Object.keys(state.organizeSelected).length;
  el("organizeSelCount").textContent = `${n} selected`;
  el("organizeApplyBtn").disabled = n === 0;
  el("organizeApplyBtn").textContent = n ? `Move ${n} Selected into Folder` : "Move Selected into Folder";
}

el("organizeSearchInput").addEventListener("input", (e) => {
  state.organizeSearch = e.target.value;
  renderOrganizeList();
});

el("organizeSelectAllBtn").addEventListener("click", () => {
  const q = state.organizeSearch.toLowerCase();
  for (const n of state.model.nodes) {
    if (n.type !== "measure") continue;
    if (!organizeMatchesUsage(n)) continue;
    if (q && !n.name.toLowerCase().includes(q) && !n.table.toLowerCase().includes(q)) continue;
    state.organizeSelected[n.id] = true;
  }
  renderOrganizeList();
});

document.querySelectorAll(".chip[data-organize-usage]").forEach((chip) => {
  chip.addEventListener("click", () => {
    document.querySelectorAll(".chip[data-organize-usage]").forEach((c) => c.classList.remove("active"));
    chip.classList.add("active");
    state.organizeUsageFilter = chip.dataset.organizeUsage;
    renderOrganizeList();
  });
});

el("organizeClearBtn").addEventListener("click", () => {
  state.organizeSelected = {};
  renderOrganizeList();
});

el("organizeApplyBtn").addEventListener("click", async () => {
  const folderName = el("organizeFolderName").value.trim();
  if (!folderName) {
    toast("Type a folder name first.", "error");
    return;
  }
  const targets = Object.keys(state.organizeSelected)
    .map((id) => state.nodesById[id])
    .filter(Boolean)
    .map((n) => ({ table: n.table, name: n.name }));
  if (!targets.length) return;

  el("organizeApplyBtn").disabled = true;
  try {
    const res = await api("/api/organize-measures", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folder_name: folderName, measures: targets }),
    });
    toast(`Moved ${res.count} measure(s) into "${res.folderName}". Backup: ${res.backup}`, "success");
    state.organizeSelected = {};
    el("organizeFolderName").value = "";
    await analyze(); // pulls fresh displayFolder values so the list reflects the new grouping
    setActiveView("organize");
  } catch (e) {
    toast("Organize failed: " + e.message, "error");
  } finally {
    updateOrganizeSelCount();
  }
});

// ==================== BOOT ====================

el("analyzeBtn").addEventListener("click", analyze);

async function checkAutoConnected() {
  try {
    const s = await api("/api/state");
    if (s.connected) {
      const inst = { port: s.port, report_title: "Connected report", suggested_report_folder: null };
      state.connected = true;
      onConnected(inst, s.report_folder);
      el("connectScreen").style.display = "none";
      await analyze();
    }
  } catch (e) { /* ignore */ }
}

loadInstances();
checkAutoConnected();
