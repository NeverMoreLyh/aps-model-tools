/* APSGraph 元数据查询工作台前端（vanilla JS，无第三方依赖；mermaid 随包内置，加载失败自动降级为列表） */
"use strict";

const PAGES = [
  { id: "top", label: "顶层模型", group: "top" },
  { id: "table", label: "表", group: "table" },
  { id: "service_file", label: "服务文件", group: "service" },
  { id: "service", label: "服务", group: "service_operation" },
  { id: "transaction", label: "交易 flowtran", group: "transaction" },
  { id: "batch", label: "批量交易", group: "batch", kindSelect: true },
  { id: "complex_type", label: "复合类型", group: "complex_type" },
  { id: "dict_element", label: "字典数据项", group: "dict_element" },
  { id: "dictionary", label: "数据字典", group: "dictionary" },
  { id: "enum", label: "枚举类型", group: "enum", enumMaster: true },
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
  try {
    if (PAGES.find((p) => p.id === state.pageId).enumMaster) {
      const payload = await api("/api/enums", { q: query, page: state.page });
      state.total = payload.total || 0;
      state.pageSize = payload.page_size || 50;
      renderEnumMaster(payload.results);
      updatePager();
      return;
    }
    const params = { group: state.group, q: query, field: state.dimension, page: state.page };
    if (state.group === "batch" && $("#kind-select").value) {
      params.kinds = $("#kind-select").value;
    }
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
  if (state.group === "basetype") {
    renderBasetypes(payload.results);
    meta.textContent = "基础类型（APS SimpleType 内置清单）";
    updatePager(); return;
  }
  meta.textContent = `${groupLabel}：共 ${payload.total} 条` +
    (state.query ? `，匹配 “${state.query}”` : "，浏览全部");
  if (!payload.results.length) {
    body.innerHTML = `<div class="empty-tip">没有匹配的记录。</div>`;
    updatePager(); return;
  }
  if (state.group === "top") { renderTopResults(payload.results); updatePager(); return; }
  renderTableResults(payload.results);
  updatePager();
}

/* 结果列只保留 fullId/id：中文名，其余信息在详情面板查看 */
function displayName(item) {
  const id = item.full_id || item.raw_id || item.stable_id;
  return item.chinese_name ? `${id}：${item.chinese_name}` : id;
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
    <td class="ellipsis" title="${esc(item.full_id)}">${esc(displayName(item))}</td></tr>`).join("");
  $("#results-body").innerHTML = `<table class="result-table"><tbody>${rows}</tbody></table>`;
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
    html += `<table class="result-table"><tbody>` +
      groupItems.map((item) => `<tr data-id="${esc(item.stable_id)}">
        <td class="ellipsis" title="${esc(item.full_id)}">${esc(displayName(item))}</td></tr>`).join("") +
      `</tbody></table>`;
  }
  $("#results-body").innerHTML = html;
  bindRowClick();
}

/* 枚举类型页：左（中）列枚举 Fullid，点击右侧展示枚举详情与枚举值 */
function renderEnumMaster(items) {
  const meta = $("#results-meta");
  meta.textContent = `枚举类型：共 ${state.total} 个枚举` +
    (state.query ? `，匹配 “${state.query}”` : "");
  if (!items.length) {
    $("#results-body").innerHTML = `<div class="empty-tip">没有匹配的枚举。</div>`;
    return;
  }
  const rows = items.map((item) => `<tr data-id="${esc(item.stable_id)}">
    <td class="ellipsis" title="${esc(item.full_id)}">${esc(item.full_id || item.raw_id)}${item.chinese_name ? "：" + esc(item.chinese_name) : ""}
      <span class="kind-badge">${item.value_count} 值</span></td></tr>`).join("");
  $("#results-body").innerHTML = `<table class="result-table"><tbody>${rows}</tbody></table>`;
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
  // 过滤带命名空间的 XML 属性（如 {http://www.w3.org/2001/XMLSchema-instance}noNamespaceSchemaLocation）
  const rows = list.filter((key) => !key.includes("{") && props[key] !== undefined && props[key] !== "")
    .map((key) => `<span class="k">${esc(key)}</span><span class="v">${esc(props[key])}</span>`).join("");
  return rows ? `<div class="props-grid">${rows}</div>` : "";
}

/* 按元数据模型对象定义的表格列。keys 为按序回退的属性名；
   link=true 的列（ref/type）在值为模型 fullId（含 "."）时渲染为跳转链接 */
const DESC_KEYS = ["desc", "description", "remark"];
const DEFAULT_KEYS = ["default", "defaultValue"];
const IO_COLS = [
  { label: "字典ID", keys: ["ref"], link: true },
  { label: "字段", keys: ["id"] },
  { label: "中文名", keys: ["longname", "name"] },
  { label: "类型", keys: ["type"], link: true },
  { label: "必填", keys: ["required"] },
  { label: "多值", keys: ["multi"] },
  { label: "默认值", keys: DEFAULT_KEYS },
  { label: "固定值", keys: ["fixedValue"] },
  { label: "描述", keys: DESC_KEYS },
  { label: "别名", keys: ["alias"] },
];
const TABLE_COLS = [
  { label: "字典ID", keys: ["ref"], link: true },
  { label: "字段", keys: ["id"] },
  { label: "DbName", keys: ["dbname"] },
  { label: "中文名", keys: ["longname", "name"] },
  { label: "类型", keys: ["type"], link: true },
  { label: "可为空", keys: ["nullable"] },
  { label: "默认值", keys: DEFAULT_KEYS },
  { label: "描述", keys: DESC_KEYS },
  { label: "是否主键", keys: ["primarykey"] },
];
const ENUM_COLS = [
  { label: "枚举值ID", keys: ["id", "raw_id"] },
  { label: "值", keys: ["value"] },
  { label: "中文名", keys: ["longname"] },
  { label: "描述", keys: DESC_KEYS },
];

function fieldsTable(rows, columns) {
  if (!rows || !rows.length) return `<div class="muted">（无）</div>`;
  const cols = (columns || ["id", "longname", "type", "ref"]).map((col) =>
    typeof col === "string" ? { label: col, keys: [col] } : col);
  const head = cols.map((col) => `<th>${esc(col.label)}</th>`).join("");
  const body = rows.map((row) => {
    const props = row.properties || row;
    return "<tr>" + cols.map((col) => {
      let value = "";
      for (const key of col.keys) {
        if (props[key] !== undefined && props[key] !== "") { value = props[key]; break; }
      }
      if (col.link && typeof value === "string" && value.includes(".")) {
        return `<td><a class="node-link" data-id="${esc(value)}">${esc(value)}</a></td>`;
      }
      return `<td>${esc(value)}</td>`;
    }).join("") + "</tr>";
  }).join("");
  return `<table class="detail-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
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

/* ---------- 流程编排 mermaid 渲染 ---------- */
function cleanMermaidLabel(value) {
  return String(value == null ? "" : value)
    .replace(/"/g, "#quot;").replace(/[\[\]|<>()]/g, " ").replace(/\s+/g, " ").trim();
}

function buildMermaidCode(steps) {
  const lines = ["flowchart TD", `S(["开始"])`];
  let seq = 0;
  const leaves = [];
  function walk(nodes, parentId) {
    let last = parentId;
    for (const node of nodes) {
      const id = `N${seq++}`;
      const isCase = node.xml_tag === "case";
      const base = node.label || node.raw_id || "";
      const text = cleanMermaidLabel(base === node.longname ? base : `${base} ${node.longname || ""}`);
      if (isCase) {
        lines.push(`${last} --> ${id}{"${text}"}`);
      } else {
        let edgeText = "";
        if (node.xml_tag === "when") {
          edgeText = cleanMermaidLabel(node.longname || node.test).slice(0, 40);
        }
        lines.push(`${last} -->${edgeText ? `|${edgeText}|` : ""} ${id}["${text}"]`);
      }
      if (node.children && node.children.length) {
        walk(node.children, id);
      } else {
        leaves.push(id);
      }
      last = id;
    }
    return last;
  }
  walk(steps, "S");
  lines.push(`E(["结束"])`);
  for (const leaf of (leaves.length ? leaves : ["S"])) lines.push(`${leaf} --> E`);
  return lines.join("\n");
}

function flowTreeList(steps) {
  if (!steps || !steps.length) return `<div class="muted">（无）</div>`;
  function render(nodes) {
    return `<ul>` + nodes.map((node) => {
      const target = node.resolved_target
        ? `<a class="node-link" data-id="${esc(node.resolved_target)}">${esc(node.label || node.raw_id)}</a>`
        : esc(node.label || node.raw_id);
      const test = node.test ? ` <span class="muted">test: ${esc(node.test)}</span>` : "";
      return `<li class="tree-node"><span class="kind-badge">${esc(node.xml_tag || node.kind)}</span> ${target}${test}` +
        (node.children && node.children.length ? flowTreeList(node.children) : "") + `</li>`;
    }).join("") + `</ul>`;
  }
  return `<div class="tree">` + render(steps) + `</div>`;
}

async function renderFlow(steps, holder) {
  const fallback = flowTreeList(steps);
  if (typeof window.mermaid === "undefined") { holder.innerHTML = fallback; return; }
  try {
    window.mermaid.initialize({ startOnLoad: false, securityLevel: "strict" });
    const { svg } = await window.mermaid.render(`flow-${Date.now()}`, buildMermaidCode(steps));
    holder.innerHTML = `
      <div class="mermaid-toolbar">
        <button data-act="zoom-in">放大 +</button>
        <button data-act="zoom-out">缩小 −</button>
        <button data-act="reset">重置</button>
        <button data-act="fullscreen">全屏</button>
        <span class="muted">滚轮缩放 · 拖拽平移</span>
      </div>
      <div class="mermaid-wrap"><div class="mermaid-inner">${svg}</div></div>
      <details><summary>列表视图</summary>${fallback}</details>`;
    setupPanZoom(holder);
    holder.querySelectorAll("a.node-link").forEach((link) =>
      link.addEventListener("click", () => showDetail(link.dataset.id)));
  } catch (error) {
    holder.innerHTML = fallback;
  }
}

function setupPanZoom(holder) {
  const wrap = holder.querySelector(".mermaid-wrap");
  const inner = holder.querySelector(".mermaid-inner");
  if (!wrap || !inner) return;
  const view = { scale: 1, tx: 0, ty: 0 };
  const apply = () => {
    inner.style.transform = `translate(${view.tx}px, ${view.ty}px) scale(${view.scale})`;
  };
  const clampScale = (value) => Math.min(6, Math.max(0.2, value));
  holder.querySelectorAll(".mermaid-toolbar button").forEach((btn) => {
    btn.addEventListener("click", () => {
      const act = btn.dataset.act;
      if (act === "zoom-in") { view.scale = clampScale(view.scale * 1.25); apply(); }
      else if (act === "zoom-out") { view.scale = clampScale(view.scale / 1.25); apply(); }
      else if (act === "reset") { view.scale = 1; view.tx = 0; view.ty = 0; apply(); }
      else if (act === "fullscreen") {
        const on = wrap.classList.toggle("fullscreen");
        btn.textContent = on ? "退出全屏" : "全屏";
        document.body.style.overflow = on ? "hidden" : "";
        view.scale = 1; view.tx = 0; view.ty = 0; apply();
      }
    });
  });
  wrap.addEventListener("wheel", (event) => {
    event.preventDefault();
    view.scale = clampScale(view.scale * (event.deltaY < 0 ? 1.1 : 0.9));
    apply();
  }, { passive: false });
  wrap.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    event.preventDefault();
    wrap.setPointerCapture(event.pointerId);
    wrap.classList.add("panning");
    const start = { x: event.clientX, y: event.clientY, tx: view.tx, ty: view.ty };
    const onMove = (moveEvent) => {
      view.tx = start.tx + (moveEvent.clientX - start.x);
      view.ty = start.ty + (moveEvent.clientY - start.y);
      apply();
    };
    const onUp = () => {
      wrap.classList.remove("panning");
      wrap.removeEventListener("pointermove", onMove);
      wrap.removeEventListener("pointerup", onUp);
    };
    wrap.addEventListener("pointermove", onMove);
    wrap.addEventListener("pointerup", onUp);
  });
}

function renderDetailSections(data) {
  const node = data.node;
  const detail = data.detail || {};
  let html = "";
  if (node.kind === "TABLE") {
    html += section("字段（fields）", fieldsTable(detail.fields, TABLE_COLS));
    html += section("物理索引（indexes）", detail.indexes && detail.indexes.length
      ? fieldsTable(detail.indexes, ["id", "type", "fields"]) : `<div class="muted">（无）</div>`);
    html += section("ODB 索引（odbindexes）", detail.odbindexes && detail.odbindexes.length
      ? fieldsTable(detail.odbindexes, ["id", "type", "fields", "operate"]) : `<div class="muted">（无）</div>`);
    if (detail.sequences && detail.sequences.length)
      html += section("序列（dbSequence）", fieldsTable(detail.sequences, ["id", "longname"]));
  } else if (node.kind === "SERVICE_TYPE") {
    for (const op of detail.operations || []) {
      const opNode = op.node || {};
      html += section(`服务操作：${opNode.raw_id || ""} ${opNode.properties && opNode.properties.longname ? "· " + esc(opNode.properties.longname) : ""}`,
        section("输入（input）", fieldsTable(op.input, IO_COLS)) +
        section("输出（output）", fieldsTable(op.output, IO_COLS)));
    }
    if (!detail.operations || !detail.operations.length) html += section("服务操作", `<div class="muted">（无）</div>`);
  } else if (node.kind === "SERVICE_OPERATION") {
    html += section("输入（input）", fieldsTable(detail.input, IO_COLS));
    html += section("输出（output）", fieldsTable(detail.output, IO_COLS));
  } else if (node.kind === "TRANSACTION") {
    html += section("输入（input）", fieldsTable(detail.input, IO_COLS));
    html += section("输出（output）", fieldsTable(detail.output, IO_COLS));
    const steps = detail.flow_steps || [];
    html += section("流程编排（flow）", `<div class="flow-holder">${steps.length ? "渲染中…" : `<div class="muted">（无）</div>`}</div>`);
  } else if (node.kind === "BATCH_TRANSACTION") {
    html += section("输入字段", fieldsTable(detail.input, IO_COLS));
    if (detail.steps && detail.steps.length) html += section("批量步骤", fieldsTable(detail.steps, ["raw_id", "full_id", "longname"]));
    if (detail.groups && detail.groups.length) html += section("步骤组", fieldsTable(detail.groups, ["raw_id", "full_id", "longname"]));
  } else if (node.kind === "ERRORCONF") {
    for (const group of detail.groups || []) {
      const title = group.node ? `错误分组：${group.node.raw_id} ${group.node.properties && group.node.properties.longname ? "· " + esc(group.node.properties.longname) : ""}` : "错误码";
      html += section(title, fieldsTable(group.errors, ["raw_id", "type", "message"]));
    }
    if (!detail.groups || !detail.groups.length) html += section("错误码", `<div class="muted">（无）</div>`);
  } else if (node.kind === "DICTIONARY" || node.kind === "COMPLEX_TYPE" || node.kind === "RESTRICTION_TYPE") {
    if (detail.elements && detail.elements.length) html += section("数据项（element）", fieldsTable(detail.elements, IO_COLS));
    if (detail.enum_values && detail.enum_values.length)
      html += section("枚举值", fieldsTable(detail.enum_values, ENUM_COLS));
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
    const flowHolder = pane.querySelector(".flow-holder");
    if (flowHolder && (data.detail || {}).flow_steps) await renderFlow(data.detail.flow_steps, flowHolder);
  } catch (error) {
    pane.innerHTML = `<div class="error-banner">${esc(error.message)}</div>`;
  }
}

/* ---------- 初始化 ---------- */
function initLayoutControls() {
  const layout = $("#layout");
  $("#nav-toggle").addEventListener("click", () => {
    layout.classList.add("nav-collapsed");
    $("#nav-open").classList.remove("hidden");
  });
  $("#nav-open").addEventListener("click", () => {
    layout.classList.remove("nav-collapsed");
    $("#nav-open").classList.add("hidden");
  });
  const resizer = $("#pane-resizer");
  const results = $("#results-pane");
  resizer.addEventListener("mousedown", (event) => {
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = results.getBoundingClientRect().width;
    const onMove = (moveEvent) => {
      const width = Math.min(window.innerWidth * 0.7,
                             Math.max(220, startWidth + moveEvent.clientX - startX));
      results.style.width = `${width}px`;
    };
    const onUp = () => {
      document.body.classList.remove("resizing");
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
    document.body.classList.add("resizing");
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  });
}

async function init() {
  renderNav();
  initLayoutControls();
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
