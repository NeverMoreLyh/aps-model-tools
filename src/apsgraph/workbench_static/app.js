/* APSGraph 元数据查询工作台前端（vanilla JS，无第三方依赖） */
"use strict";

const PAGES = [
  { id: "top", label: "顶层模型", group: "top" },
  { id: "table", label: "表", group: "table" },
  { id: "service", label: "服务", group: "service" },
  { id: "transaction", label: "交易 flowtran", group: "transaction" },
  { id: "batch", label: "批量交易", group: "batch", kindSelect: true },
  { id: "complex_type", label: "复合类型", group: "complex_type" },
  { id: "dictionary", label: "数据字典", group: "dictionary" },
  { id: "enum", label: "枚举类型", group: "enum" },
  { id: "error_code", label: "错误码", group: "error_code" },
  { id: "basetype", label: "基础类型", group: "basetype" },
];

const BATCH_KINDS = ["BATCH_TRANSACTION", "FILE_BATCH_TRANSACTION", "BATCH_STEP", "BATCH_GROUP"];
const BATCH_KIND_LABELS = {
  BATCH_TRANSACTION: "批量交易", FILE_BATCH_TRANSACTION: "文件批量交易",
  BATCH_STEP: "批量步骤", BATCH_GROUP: "批量步骤组",
};

const state = {
  pageId: "top", group: "top", query: "", dimension: "", kinds: [],
  page: 1, total: 0, pageSize: 50, selected: null,
};

function $(sel) { return document.querySelector(sel); }
function esc(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
async function api(path, params) {
  const url = path + (params ? "?" + new URLSearchParams(params) : "");
  const response = await fetch(url);
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || response.statusText);
  return payload;
}

/* ---------- 导航 ---------- */
function renderNav() {
  const list = $("#nav-list");
  list.innerHTML = "";
  for (const page of PAGES) {
    const li = document.createElement("li");
    li.textContent = page.label;
    li.dataset.page = page.id;
    if (page.id === state.pageId) li.classList.add("active");
    li.addEventListener("click", () => switchPage(page.id));
    list.appendChild(li);
  }
}

function switchPage(pageId) {
  state.pageId = pageId;
  const page = PAGES.find((p) => p.id === pageId);
  state.group = page.group;
  state.query = ""; state.page = 1; state.selected = null;
  $("#query").value = "";
  const kindSelect = $("#kind-select");
  if (page.kindSelect) {
    kindSelect.classList.remove("hidden");
    kindSelect.innerHTML = BATCH_KINDS.map((kind) =>
      `<option value="${kind}">${BATCH_KIND_LABELS[kind]}</option>`).join("");
    state.kinds = [];
  } else {
    kindSelect.classList.add("hidden");
    kindSelect.innerHTML = "";
    state.kinds = [];
  }
  $("#detail-pane").innerHTML = "";
  renderNav();
  runSearch();
}

/* ---------- 搜索 ---------- */
async function runSearch() {
  const query = $("#query").value.trim();
  state.query = query;
  state.dimension = $("#dimension").value;
  const params = { group: state.group, q: query, field: state.dimension, page: state.page };
  if (state.group === "batch" && $("#kind-select").value) {
    params.kinds = $("#kind-select").value;
  }
  try {
    const payload = await api("/api/search", params);
    state.total = payload.total || 0;
    state.pageSize = payload.page_size || 50;
    renderResults(payload);
  } catch (error) {
    $("#results-body").innerHTML = `<div class="error-banner">${esc(error.message)}</div>`;
  }
}

function renderResults(payload) {
  const meta = $("#results-meta");
  const body = $("#results-body");
  const groupLabel = PAGES.find((p) => p.group === state.group).label;
  if (state.group === "basetype") { renderBasetypes(payload.results); meta.textContent = `基础类型（APS SimpleType 内置清单）`; updatePager(); return; }
  meta.textContent = `${groupLabel}：共 ${payload.total} 条` +
    (state.query ? `，匹配 “${state.query}”` : "，浏览全部");
  if (!payload.results.length) {
    body.innerHTML = `<div class="empty-tip">没有匹配的记录。</div>`;
    updatePager(); return;
  }
  if (state.group === "enum") { renderEnumResults(payload.results); updatePager(); return; }
  if (state.group === "top") { renderTopResults(payload.results); updatePager(); return; }
  renderTableResults(payload.results);
  updatePager();
}

function renderBasetypes(items) {
  const rows = items.map((item) => `<tr>
    <td><strong>${esc(item.name)}</strong></td><td>${esc(item.java)}</td>
    <td>${esc(item.mysql || "—")}</td><td>${esc(item.oracle || "—")}</td>
    <td>${esc(item.postgresql || "—")}</td></tr>`).join("");
  $("#results-body").innerHTML = `<table class="result-table">
    <thead><tr><th>基础类型</th><th>Java 语义</th><th>MySQL</th><th>Oracle</th><th>PostgreSQL</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

function renderTableResults(items) {
  const rows = items.map((item) => `<tr data-id="${esc(item.stable_id)}">
    <td><span class="kind-badge">${esc(item.kind)}</span></td>
    <td class="ellipsis" title="${esc(item.full_id)}">${esc(item.full_id || item.raw_id)}</td>
    <td class="ellipsis">${esc(item.chinese_name)}</td>
    <td class="ellipsis muted">${esc(item.description)}</td>
    <td class="ellipsis muted" title="${esc(item.file_path)}">${esc(item.file_path)}</td></tr>`).join("");
  $("#results-body").innerHTML = `<table class="result-table">
    <thead><tr><th>类型</th><th>fullId / id</th><th>中文名</th><th>描述</th><th>来源文件</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
  bindRowClick();
}

function renderTopResults(items) {
  const byKind = new Map();
  for (const item of items) {
    if (!byKind.has(item.kind)) byKind.set(item.kind, []);
    byKind.get(item.kind).push(item);
  }
  let html = "";
  for (const [kind, groupItems] of [...byKind.entries()].sort()) {
    html += `<div class="result-group-title">${esc(kind)}（${groupItems.length}）</div>`;
    html += `<table class="result-table"><thead><tr><th>fullId / id</th><th>中文名</th><th>来源文件</th></tr></thead><tbody>` +
      groupItems.map((item) => `<tr data-id="${esc(item.stable_id)}">
        <td class="ellipsis" title="${esc(item.full_id)}">${esc(item.full_id || item.raw_id)}</td>
        <td class="ellipsis">${esc(item.chinese_name)}</td>
        <td class="ellipsis muted" title="${esc(item.file_path)}">${esc(item.file_path)}</td></tr>`).join("") +
      `</tbody></table>`;
  }
  $("#results-body").innerHTML = html;
  bindRowClick();
}

function renderEnumResults(items) {
  const groups = new Map();
  for (const item of items) {
    const key = item.owner_id || "(无所属枚举)";
    if (!groups.has(key)) groups.set(key, { title: item.owner_full_id || key, name: item.owner_name || "", values: [] });
    groups.get(key).values.push(item);
  }
  let html = "";
  for (const [ownerId, group] of groups) {
    html += `<div class="result-group-title" data-owner="${esc(ownerId)}">` +
      `${esc(group.title)}${group.name ? " · " + esc(group.name) : ""}（${group.values.length} 个枚举值）</div>`;
    html += `<table class="result-table"><thead><tr><th>枚举值</th><th>值</th><th>中文名</th><th>描述</th></tr></thead><tbody>` +
      group.values.map((item) => `<tr data-id="${esc(item.stable_id)}">
        <td class="ellipsis">${esc(item.raw_id)}</td>
        <td>${esc((item.properties || {}).value)}</td>
        <td class="ellipsis">${esc(item.chinese_name)}</td>
        <td class="ellipsis muted">${esc(item.description)}</td></tr>`).join("") +
      `</tbody></table>`;
  }
  $("#results-body").innerHTML = html;
  bindRowClick();
}

function bindRowClick() {
  document.querySelectorAll("#results-body tr[data-id]").forEach((row) => {
    row.addEventListener("click", () => {
      document.querySelectorAll("#results-body tr.selected").forEach((el) => el.classList.remove("selected"));
      row.classList.add("selected");
      showDetail(row.dataset.id);
    });
  });
}

function updatePager() {
  const pages = Math.max(1, Math.ceil(state.total / state.pageSize));
  $("#page-info").textContent = `第 ${state.page} / ${pages} 页`;
  $("#btn-prev").disabled = state.page <= 1;
  $("#btn-next").disabled = state.page >= pages;
}

/* ---------- 详情 ---------- */
function propGrid(props, keys) {
  const list = keys || Object.keys(props || {}).sort();
  const rows = list.filter((key) => props[key] !== undefined && props[key] !== "")
    .map((key) => `<span class="k">${esc(key)}</span><span class="v">${esc(props[key])}</span>`).join("");
  return rows ? `<div class="props-grid">${rows}</div>` : "";
}

function fieldsTable(fields, columns) {
  if (!fields || !fields.length) return `<div class="muted">（无）</div>`;
  const cols = columns || ["id", "longname", "type", "ref", "maxLength", "nullable", "primarykey", "defaultValue"];
  const head = cols.map((c) => `<th>${esc(c)}</th>`).join("");
  const rows = fields.map((field) => {
    const props = field.properties || field;
    return "<tr>" + cols.map((c) => `<td>${esc(props[c])}</td>`).join("") + "</tr>";
  }).join("");
  return `<table class="detail-table"><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table>`;
}

function nodeLink(node, unresolvedFallback) {
  if (!node) return `<span class="muted">${esc(unresolvedFallback || "")}</span>`;
  if (node.resolved === false) return `<span class="unresolved">未解析：${esc(node.raw_target || node.full_id || "")}</span>`;
  return `<a class="node-link" data-id="${esc(node.stable_id || node.full_id)}">${esc(node.full_id || node.stable_id)}</a>`;
}

function section(title, inner) {
  return `<div class="detail-section"><h3>${esc(title)}</h3>${inner}</div>`;
}

function edgeSection(title, edges) {
  if (!edges.length) return "";
  const items = edges.map((edge) => {
    const target = edge.target || {};
    const label = target.resolved === false
      ? `<span class="unresolved">未解析：${esc(target.raw_target || "")}</span>`
      : `<a class="node-link" data-id="${esc(target.stable_id || target.full_id)}">${esc(target.full_id || target.stable_id || target.raw_target)}</a>`;
    return `<li><span class="kind-badge">${esc(edge.relation_kind)}</span> ${label}</li>`;
  }).join("");
  return section(title, `<ul class="edge-list">${items}</ul>`);
}

function renderDetailSections(data) {
  const node = data.node;
  const detail = data.detail || {};
  const props = node.properties || {};
  let html = "";
  if (node.kind === "TABLE") {
    html += section("字段（fields）", fieldsTable(detail.fields));
    html += section("物理索引（indexes）", detail.indexes && detail.indexes.length
      ? fieldsTable(detail.indexes, ["id", "type", "fields"]) : `<div class="muted">（无）</div>`);
    html += section("ODB 索引（odbindexes）", detail.odbindexes && detail.odbindexes.length
      ? fieldsTable(detail.odbindexes, ["id", "type", "fields"]) : `<div class="muted">（无）</div>`);
    if (detail.sequences && detail.sequences.length)
      html += section("序列（dbSequence）", fieldsTable(detail.sequences, ["id", "longname"]));
  } else if (node.kind === "SERVICE_TYPE") {
    for (const op of detail.operations || []) {
      const opNode = op.node || {};
      html += section(`服务操作：${opNode.raw_id || ""} ${opNode.properties && opNode.properties.longname ? "· " + esc(opNode.properties.longname) : ""}`,
        section("输入（input）", fieldsTable(op.input)) +
        section("输出（output）", fieldsTable(op.output)));
    }
    if (!detail.operations || !detail.operations.length) html += section("服务操作", `<div class="muted">（无）</div>`);
  } else if (node.kind === "TRANSACTION") {
    html += section("输入（input）", fieldsTable(detail.input));
    html += section("输出（output）", fieldsTable(detail.output));
    const steps = (detail.flow_steps || []).map((step) => {
      const propsStep = step.properties || {};
      const target = propsStep.serviceName || propsStep.transactionId || propsStep.transaction || "";
      const inner = `${esc(step.raw_id || "")} ${target ? "→ " : ""}` +
        (target ? nodeLink({ stable_id: target, full_id: target, resolved: true }, target) : "") +
        (propsStep.longname ? ` <span class="muted">${esc(propsStep.longname)}</span>` : "");
      return `<div class="flow-step">${inner}</div>`;
    }).join("");
    html += section("流程编排（flow）", steps || `<div class="muted">（无）</div>`);
  } else if (node.kind === "BATCH_TRANSACTION") {
    html += section("输入字段", fieldsTable(detail.input));
    if (detail.steps && detail.steps.length) html += section("批量步骤", fieldsTable(detail.steps, ["raw_id", "full_id", "longname"]));
    if (detail.groups && detail.groups.length) html += section("步骤组", fieldsTable(detail.groups, ["raw_id", "full_id", "longname"]));
  } else if (node.kind === "DICTIONARY") {
    if (detail.elements && detail.elements.length) html += section("数据项（element）", fieldsTable(detail.elements));
    if (detail.enum_values && detail.enum_values.length)
      html += section(node.file_path && node.file_path.endsWith(".error.xml") ? "错误码明细" : "枚举值",
        fieldsTable(detail.enum_values, ["raw_id", "value", "longname", "description"]));
  } else if (node.kind === "COMPLEX_TYPE") {
    if (detail.elements && detail.elements.length) html += section("数据项（element）", fieldsTable(detail.elements));
    if (detail.enum_values && detail.enum_values.length) html += section("枚举值", fieldsTable(detail.enum_values, ["raw_id", "value", "longname", "description"]));
  }
  return html;
}

function treeHtml(children) {
  if (!children.length) return `<div class="muted">（无子节点）</div>`;
  return `<ul>` + children.map((child) => `<li class="tree-node" data-id="${esc(child.stable_id)}">
    ${child.has_children ? `<span class="toggle" data-toggle="1">▸</span>` : `<span class="toggle">·</span>`}
    <span class="kind-badge">${esc(child.kind)}</span>
    <span class="node-link" data-id="${esc(child.stable_id)}">${esc(child.full_id || child.raw_id)}</span>
    ${child.chinese_name ? `<span class="muted">${esc(child.chinese_name)}</span>` : ""}
    <span class="children"></span></li>`).join("") + `</ul>`;
}

function bindTree(container) {
  container.querySelectorAll(".tree-node .toggle[data-toggle]").forEach((toggle) => {
    toggle.addEventListener("click", async (event) => {
      event.stopPropagation();
      const li = toggle.closest(".tree-node");
      const holder = li.querySelector(":scope > .children");
      if (holder.dataset.loaded) { holder.classList.toggle("hidden"); return; }
      try {
        const payload = await api("/api/children", { id: li.dataset.id });
        holder.innerHTML = treeHtml(payload.children);
        holder.dataset.loaded = "1";
        toggle.textContent = "▾";
        bindTree(holder);
      } catch (error) { holder.innerHTML = `<span class="unresolved">${esc(error.message)}</span>`; }
    });
  });
  container.querySelectorAll(".tree-node .node-link").forEach((link) => {
    link.addEventListener("click", (event) => { event.stopPropagation(); showDetail(link.dataset.id); });
  });
}

async function showDetail(nodeRef) {
  const pane = $("#detail-pane");
  pane.innerHTML = `<div class="empty-tip">加载中…</div>`;
  try {
    const data = await api("/api/node", { id: nodeRef });
    const node = data.node;
    const props = node.properties || {};
    let html = `<div class="detail-title">${esc(props.longname || props.name || node.raw_id || node.full_id)}</div>
      <div class="detail-sub"><span class="kind-badge">${esc(node.kind)}</span>
      fullId: ${esc(node.full_id)} · id: ${esc(node.raw_id)}<br>来源：${esc(node.file_path)}</div>`;
    html += propGrid(props);
    html += renderDetailSections(data);
    if (data.children && data.children.length)
      html += section("包含子模型（点击展开）", `<div class="tree">${treeHtml(data.children)}</div>`);
    html += edgeSection("引用（出）", data.out_edges || []);
    html += edgeSection("被引用（入）", data.in_edges || []);
    pane.innerHTML = html;
    pane.scrollTop = 0;
    pane.querySelectorAll("a.node-link").forEach((link) =>
      link.addEventListener("click", () => showDetail(link.dataset.id)));
    const tree = pane.querySelector(".tree");
    if (tree) bindTree(tree);
  } catch (error) {
    pane.innerHTML = `<div class="error-banner">${esc(error.message)}</div>`;
  }
}

/* ---------- 初始化 ---------- */
async function init() {
  renderNav();
  $("#btn-search").addEventListener("click", () => { state.page = 1; runSearch(); });
  $("#btn-clear").addEventListener("click", () => { $("#query").value = ""; state.page = 1; runSearch(); });
  $("#query").addEventListener("keydown", (event) => { if (event.key === "Enter") { state.page = 1; runSearch(); } });
  $("#btn-prev").addEventListener("click", () => { if (state.page > 1) { state.page -= 1; runSearch(); } });
  $("#btn-next").addEventListener("click", () => { state.page += 1; runSearch(); });
  $("#kind-select").addEventListener("change", () => { state.page = 1; runSearch(); });
  $("#dimension").addEventListener("change", () => { state.page = 1; runSearch(); });
  switchPage("top");
  try {
    const payload = await api("/api/stats");
    const stats = payload.stats || {};
    const top = (payload.top_groups || []).map((g) => `${g.kind}(${g.count})`).join("、");
    $("#sidebar-stats").innerHTML =
      `节点 ${stats.nodes} · 边 ${stats.edges} · 文件 ${stats.files}` +
      (stats.parse_failed ? ` · 解析失败 ${stats.parse_failed}` : "") +
      `<br><br>顶层模型：${esc(top || "（无）")}`;
  } catch (error) { /* 统计失败不阻塞工作台 */ }
}

init();
