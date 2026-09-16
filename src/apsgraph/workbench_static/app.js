/* APSGraph 元数据查询工作台前端（vanilla JS，无第三方依赖；mermaid 随包内置，加载失败自动降级为列表） */
"use strict";

const PAGES = [
  { id: "dashboard", label: "总览", group: "dashboard", dashboard: true },
  { id: "top", label: "顶层模型", group: "top" },
  { id: "table", label: "表", group: "table" },
  { id: "service_file", label: "服务文件", group: "service" },
  { id: "service", label: "服务", group: "service_operation" },
  { id: "transaction", label: "交易", group: "transaction" },
  { id: "batch", label: "批量交易", group: "batch", kindSelect: true },
  { id: "complex_type", label: "复合类型", group: "complex_type" },
  { id: "dict_element", label: "数据字典", group: "dict_element" },
  { id: "dictionary", label: "数据字典文件", group: "dictionary" },
  { id: "enum", label: "枚举类型", group: "enum", enumMaster: true },
  { id: "base_type", label: "基础类型", group: "base_type" },
  { id: "error_code", label: "错误码文件", group: "error_code" },
  { id: "error_item", label: "错误码", group: "error_item" },
  { id: "constant", label: "常量", group: "constant" },
  { id: "file_batch", label: "文件批量", group: "file_batch" },
  { id: "nsql", label: "命名SQL文件", group: "nsql" },
  { id: "nsql_item", label: "命名SQL", group: "nsql_item" },
  { id: "sharding", label: "分片", group: "sharding" },
  { id: "parse_failed", label: "解析失败", group: "parse_failed" },
];

/* 左侧菜单一二级聚合：一级菜单之外统一归入“其他” */
const NAV_PRIMARY = ["transaction", "service", "table", "dict_element", "enum", "base_type"];
const NAV_OTHER = ["top", "service_file", "batch", "file_batch", "nsql", "nsql_item",
                   "sharding", "complex_type", "dictionary", "error_code", "error_item",
                   "constant", "parse_failed"];
let navOtherOpen = false;
/* 折叠窄条上的单字徽标（未指定的取中文名首字） */
const NAV_SHORT = { dashboard: "总", transaction: "F", service: "S", table: "T",
                    dict_element: "D", enum: "E", base_type: "U" };

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
  const collapsed = $("#layout").classList.contains("nav-collapsed");
  const makeItem = (page) => {
    const li = document.createElement("li");
    li.textContent = collapsed ? (NAV_SHORT[page.id] || page.label.charAt(0)) : page.label;
    li.title = page.label;
    li.dataset.page = page.id;
    if (page.id === state.pageId) li.classList.add("active");
    li.addEventListener("click", () => switchPage(page.id));
    return li;
  };
  list.appendChild(makeItem(PAGES.find((p) => p.id === "dashboard")));
  for (const id of NAV_PRIMARY) list.appendChild(makeItem(PAGES.find((p) => p.id === id)));
  const group = document.createElement("li");
  group.className = "nav-group";
  group.title = "其他";
  group.textContent = collapsed ? "其他" : `${navOtherOpen ? "▾" : "▸"} 其他`;
  group.addEventListener("click", () => { navOtherOpen = !navOtherOpen; renderNav(); });
  list.appendChild(group);
  if (navOtherOpen) {
    for (const id of NAV_OTHER) {
      const li = makeItem(PAGES.find((p) => p.id === id));
      li.classList.add("nav-sub");
      list.appendChild(li);
    }
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
  detailHistory.stack = [];
  detailHistory.current = null;
  renderNav();
  $("#content").classList.toggle("dashboard-mode", !!page.dashboard);
  if (page.dashboard) { renderDashboard(); return; }
  runSearch();
}

const DASH_CARDS = [
  { group: "top", label: "顶层模型" },
  { group: "table", label: "表" },
  { group: "service", label: "服务文件" },
  { group: "service_operation", label: "服务" },
  { group: "transaction", label: "交易" },
  { group: "batch", label: "批量交易" },
  { group: "file_batch", label: "文件批量" },
  { group: "nsql", label: "命名SQL文件" },
  { group: "nsql_item", label: "命名SQL" },
  { group: "sharding", label: "分片" },
  { group: "complex_type", label: "复合类型" },
  { group: "dictionary", label: "数据字典文件" },
  { group: "dict_element", label: "数据字典" },
  { group: "base_type", label: "基础类型" },
  { group: "enum", label: "枚举类型" },
  { group: "error_code", label: "错误码文件" },
  { group: "error_item", label: "错误码" },
  { group: "constant", label: "常量" },
];

function fmtBytes(size) {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  if (size < 1024 * 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`;
  return `${(size / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

async function renderDashboard() {
  const body = $("#results-body");
  const meta = $("#results-meta");
  meta.textContent = "";
  body.innerHTML = `<div class="empty-tip">加载中…</div>`;
  try {
    const payload = await api("/api/dashboard");
    const stats = payload.stats || {};
    const counts = payload.counts || {};
    const statsHtml = [
      ["SQLite 大小", fmtBytes(payload.db_size || 0)],
      ["文件", stats.files],
      ["解析成功", stats.parsed],
      ["解析失败", stats.parse_failed],
      ["节点", stats.nodes],
      ["边", stats.edges],
      ["未解析引用", stats.unresolved],
    ].map(([label, value]) => {
      const clickable = label === "解析失败";
      const warn = label === "解析失败" && value > 0 ? " dash-warn" : "";
      const click = clickable ? " dash-click" : "";
      const pageAttr = clickable ? ` data-page="parse_failed"` : "";
      return `<div class="dash-stat${warn}${click}"${pageAttr}>` +
        `<span class="dash-num">${esc(value)}</span><span class="dash-label">${esc(label)}</span></div>`;
    }).join("");
    const cards = DASH_CARDS.map((card) => {
      const page = PAGES.find((p) => p.group === card.group);
      return `<div class="dash-card" data-page="${page ? page.id : "top"}">
        <div class="num">${esc(counts[card.group] == null ? "—" : counts[card.group])}</div>
        <div class="label">${esc(card.label)}</div></div>`;
    }).join("");
    body.innerHTML = `<div class="dash-stats">${statsHtml}</div><div class="dash-grid">${cards}</div>`;
    body.querySelectorAll(".dash-card, .dash-stat[data-page]").forEach((card) =>
      card.addEventListener("click", () => switchPage(card.dataset.page)));
  } catch (error) {
    body.innerHTML = `<div class="error-banner">${esc(error.message)}</div>`;
  }
}

/* ---------- 搜索 ---------- */
async function runSearch() {
  const query = $("#query").value.trim();
  state.query = query;
  state.dimension = $("#dimension").value;
  try {
    if (state.pageId === "parse_failed") {
      const payload = await api("/api/parse-failures", { q: query });
      renderParseFailures(payload.results, payload.total);
      updatePager();
      return;
    }
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

function renderParseFailures(items, total) {
  const meta = $("#results-meta");
  meta.textContent = `解析失败：共 ${total} 个文件` + (state.query ? `，匹配 “${state.query}”` : "");
  if (!items.length) {
    $("#results-body").innerHTML = `<div class="empty-tip">没有解析失败的文件。</div>`;
    return;
  }
  const rows = items.map((item) => `<tr class="static-row">
    <td class="ellipsis" title="${esc(item.path)}">${esc(item.path)}</td>
    <td><span class="kind-badge">${esc(item.suffix)}</span></td>
    <td class="error-msg">${esc(item.error_message || "（无错误信息）")}</td></tr>`).join("");
  $("#results-body").innerHTML = `<table class="result-table">
    <thead><tr><th>文件路径</th><th>后缀</th><th>错误信息</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
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

/* 模型 fullId 形态：大写开头的分段 + 点分（如 BaseType.U_ADDR、BpDict.E.entp_scale）。
   Java 包名等小写开头带点的值不会命中，避免误加链接 */
const MODEL_ID_RE = /^[A-Z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$/;

/* 详情跳转历史，支持“返回上一级” */
const detailHistory = { stack: [], current: null };

/* ---------- 详情 ---------- */
function propGrid(props, keys) {
  const list = keys || Object.keys(props || {}).sort();
  // 过滤带命名空间的 XML 属性（如 {http://www.w3.org/2001/XMLSchema-instance}noNamespaceSchemaLocation）
  const rows = list.filter((key) => !key.includes("{") && props[key] !== undefined && props[key] !== "")
    .map((key) => {
      const value = props[key];
      const rendered = (typeof value === "string" && MODEL_ID_RE.test(value))
        ? `<a class="node-link" data-id="${esc(value)}">${esc(value)}</a>`
        : esc(value);
      return `<span class="k">${esc(key)}</span><span class="v">${rendered}</span>`;
    }).join("");
  return rows ? `<div class="props-grid">${rows}</div>` : "";
}

/* 按元数据模型对象定义的表格列。keys 为按序回退的属性名；
   link=true 的列（ref/type）在值为模型 fullId（含 "."）时渲染为跳转链接 */
const DESC_KEYS = ["desc", "description", "remark"];
const DEFAULT_KEYS = ["default", "defaultValue"];
const IO_COLS = [
  { label: "字典ID", keys: ["ref"], fallback: "full_id", link: true },
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
const ERROR_COLS = [
  { label: "id", keys: ["id"] },
  { label: "类型", keys: ["type"] },
  { label: "错误码", keys: ["full_id"], link: true },
  { label: "参数", keys: ["parameters"] },
  { label: "message", keys: ["message"] },
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
    // 数据项/字段行允许“字典ID”在 ref 缺省时回退为节点自身的 full_id（如 BpDict.A.addr）
    const props = Object.assign({}, row, row.properties || {}, { full_id: row.full_id });
    return "<tr>" + cols.map((col) => {
      let value = "";
      for (const key of col.keys) {
        if (props[key] !== undefined && props[key] !== "") { value = props[key]; break; }
      }
      if (!value && col.fallback && props[col.fallback]) value = props[col.fallback];
      if (col.link && typeof value === "string" && MODEL_ID_RE.test(value)) {
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
  return `<div class="detail-section"><h3>${title}</h3>${inner}</div>`;
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
  const lines = ["flowchart TB", `S(["开始"])`];
  let seq = 0;
  function define(node, shape) {
    const id = `N${seq++}`;
    const base = node.label || node.raw_id || "";
    const text = cleanMermaidLabel(base === node.longname ? base : `${base} ${node.longname || ""}`);
    lines.push(`${id}${shape === "case" ? "{" : "["}"${text}"${shape === "case" ? "}" : "]"}`);
    return id;
  }
  function connect(frontier, target, label) {
    const edge = label ? `|${cleanMermaidLabel(label).slice(0, 40)}|` : "";
    for (const from of frontier) lines.push(`${from} -->${edge} ${target}`);
  }
  /* 执行一个兄弟节点序列；返回序列结束时的出口节点集合（分支各自汇合） */
  function emitSequence(nodes, frontier) {
    for (const node of nodes) {
      if (node.xml_tag === "case") {
        const caseId = define(node, "case");
        connect(frontier, caseId);
        let branchExits = [];
        for (const child of node.children || []) {
          const whenId = define(child, "step");
          connect([caseId], whenId);
          branchExits = branchExits.concat(
            (child.children && child.children.length)
              ? emitSequence(child.children, [whenId]).exits
              : [whenId]);
        }
        frontier = branchExits;
      } else {
        const id = define(node, "step");
        connect(frontier, id);
        frontier = (node.children && node.children.length)
          ? emitSequence(node.children, [id]).exits
          : [id];
      }
    }
    return { exits: frontier };
  }
  const { exits } = emitSequence(steps, ["S"]);
  lines.push(`E(["结束"])`);
  for (const exit of exits) lines.push(`${exit} --> E`);
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

const MERMAID_DEFAULT_SCALE = 0.6; // 初始与重置时的默认缩放，避免大图初始铺满

async function renderFlow(steps, holder) {
  const fallback = flowTreeList(steps);
  if (typeof window.mermaid === "undefined") { holder.innerHTML = fallback; return; }
  try {
    window.mermaid.initialize({ startOnLoad: false, securityLevel: "strict" });
    const { svg } = await window.mermaid.render(`flow-${Date.now()}`, buildMermaidCode(steps));
    holder.innerHTML = `
      <div class="mermaid-stage">
        <div class="mermaid-toolbar">
          <button data-act="zoom-in">放大 +</button>
          <button data-act="zoom-out">缩小 −</button>
          <button data-act="reset">重置</button>
          <button data-act="fullscreen">全屏</button>
          <span class="muted">滚轮缩放 · 拖拽平移</span>
        </div>
        <div class="mermaid-wrap"><div class="mermaid-inner">${svg}</div></div>
        <button class="mermaid-close hidden" title="退出全屏">✕ 关闭全屏</button>
      </div>
      <details><summary>列表视图</summary>${fallback}</details>`;
    setupPanZoom(holder);
    holder.querySelectorAll("a.node-link").forEach((link) =>
      link.addEventListener("click", () => showDetail(link.dataset.id)));
  } catch (error) {
    holder.innerHTML = fallback;
  }
}

function setupPanZoom(holder) {
  const stage = holder.querySelector(".mermaid-stage");
  const wrap = holder.querySelector(".mermaid-wrap");
  const inner = holder.querySelector(".mermaid-inner");
  if (!stage || !wrap || !inner) return;
  const view = { scale: MERMAID_DEFAULT_SCALE, tx: 0, ty: 0 };
  const apply = () => {
    inner.style.transform = `translate(${view.tx}px, ${view.ty}px) scale(${view.scale})`;
  };
  apply();
  const clampScale = (value) => Math.min(6, Math.max(0.2, value));
  const closeBtn = stage.querySelector(".mermaid-close");
  const fullscreenBtn = stage.querySelector('[data-act="fullscreen"]');
  const setFullscreen = (on) => {
    stage.classList.toggle("fullscreen", on);
    if (fullscreenBtn) fullscreenBtn.textContent = on ? "退出全屏" : "全屏";
    closeBtn.classList.toggle("hidden", !on);
    document.body.style.overflow = on ? "hidden" : "";
    view.scale = MERMAID_DEFAULT_SCALE; view.tx = 0; view.ty = 0; apply();
  };
  stage.querySelectorAll(".mermaid-toolbar button").forEach((btn) => {
    btn.addEventListener("click", () => {
      const act = btn.dataset.act;
      if (act === "zoom-in") { view.scale = clampScale(view.scale * 1.25); apply(); }
      else if (act === "zoom-out") { view.scale = clampScale(view.scale / 1.25); apply(); }
      else if (act === "reset") { view.scale = MERMAID_DEFAULT_SCALE; view.tx = 0; view.ty = 0; apply(); }
      else if (act === "fullscreen") setFullscreen(!stage.classList.contains("fullscreen"));
    });
  });
  closeBtn.addEventListener("click", () => setFullscreen(false));
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
    html += `<button id="btn-gen-ddl" class="action-btn">生成 DDL</button>`;
    html += section("字段（fields）", fieldsTable(detail.fields, TABLE_COLS));
    for (const ext of detail.extensions || []) {
      const title = ext.resolved
        ? `公共字段表：<a class="node-link" data-id="${esc(ext.stable_id)}">${esc(ext.full_id)}</a>`
        : `公共字段表：<span class="unresolved">未解析：${esc(ext.raw_target || "")}</span>`;
      html += section(title, ext.resolved ? fieldsTable(ext.fields, TABLE_COLS) : `<div class="muted">（目标不可达，无法展示字段）</div>`);
    }
    html += section("物理索引（indexes）", detail.indexes && detail.indexes.length
      ? fieldsTable(detail.indexes, ["id", "type", "fields"]) : `<div class="muted">（无）</div>`);
    html += section("ODB 索引（odbindexes）", detail.odbindexes && detail.odbindexes.length
      ? fieldsTable(detail.odbindexes, ["id", "type", "fields", "operate"]) : `<div class="muted">（无）</div>`);
    if (detail.sequences && detail.sequences.length)
      html += section("序列（dbSequence）", fieldsTable(detail.sequences, ["id", "longname"]));
  } else if (node.kind === "SERVICE_TYPE") {
    for (const op of detail.operations || []) {
      const opNode = op.node || {};
      html += section(`服务操作：${esc(opNode.raw_id || "")} ${opNode.properties && opNode.properties.longname ? "· " + esc(opNode.properties.longname) : ""}`,
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
  } else if (node.kind === "BATCH_TRANSACTION" || node.kind === "FILE_BATCH_TRANSACTION") {
    html += section("输入字段", fieldsTable(detail.input, IO_COLS));
    if (detail.steps && detail.steps.length) html += section("批量步骤", fieldsTable(detail.steps, ["raw_id", "full_id", "longname"]));
    if (detail.groups && detail.groups.length) html += section("步骤组", fieldsTable(detail.groups, ["raw_id", "full_id", "longname"]));
  } else if (node.kind === "SQL_GROUP") {
    if (detail.statements && detail.statements.length)
      html += section("命名SQL（statements）", fieldsTable(detail.statements, ["raw_id", "method", "longname", "desc"]));
    else html += section("命名SQL", `<div class="muted">（无）</div>`);
  } else if (node.kind === "NAMED_SQL") {
    html += section("参数（parameter）", fieldsTable(detail.parameters, ["id", "property", "type", "javaType", "ref", "mode", "longname"]));
    const sqls = (detail.sqls || []).slice()
      .sort((a, b) => (a.type === "NONE" ? -1 : b.type === "NONE" ? 1 : 0));
    html += section("SQL语句（按数据库类型）", sqls.length ? sqls.map((item) =>
      `<div class="sql-block"><div class="sql-type">${esc(item.type)}</div>` +
      `<pre class="sql-text">${esc(item.text)}</pre></div>`).join("")
      : `<div class="muted">（源文件不可达或未包含 SQL 文本）</div>`);
  } else if (node.kind === "SHARDINGSTRATEGY") {
    html += section("分片策略（strategies）", detail.strategies && detail.strategies.length
      ? fieldsTable(detail.strategies, ["id", "name", "clazzImpl"]) : `<div class="muted">（无）</div>`);
  } else if (node.kind === "ERROR") {
    html += section("参数（parameter）", fieldsTable(detail.parameters, ["id", "type", "longname", "ref"]));
  } else if (node.kind === "ERRORCONF") {
    for (const group of detail.groups || []) {
      const title = group.node ? `错误分组：${esc(group.node.raw_id)} ${group.node.properties && group.node.properties.longname ? "· " + esc(group.node.properties.longname) : ""}` : "错误码";
      html += section(title, fieldsTable(group.errors, ERROR_COLS));
    }
    if (!detail.groups || !detail.groups.length) html += section("错误码", `<div class="muted">（无）</div>`);
  } else if (node.kind === "DICTIONARY" || node.kind === "COMPLEX_TYPE" || node.kind === "RESTRICTION_TYPE") {
    if (detail.elements && detail.elements.length) html += section("数据项（element）", fieldsTable(detail.elements, IO_COLS));
    if (detail.enum_values && detail.enum_values.length)
      html += section("枚举值", fieldsTable(detail.enum_values, ENUM_COLS));
  }
  return html;
}

/* ---------- DDL 预览弹窗（只生成，不执行） ---------- */
async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (error) {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } finally { ta.remove(); }
    return true;
  }
}

function showDdlDialog(node) {
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.innerHTML = `
    <div class="modal">
      <div class="modal-head">
        <strong>生成 DDL：${esc(node.full_id || node.raw_id)}</strong>
        <button class="modal-close" title="关闭">✕</button>
      </div>
      <div class="modal-body">
        <div>
          <label>数据库类型
            <select id="ddl-dialect">
              <option value="mysql">MySQL</option>
              <option value="oracle">Oracle</option>
              <option value="postgresql">PostgreSQL</option>
            </select>
          </label>
          <button id="ddl-generate" class="action-btn" style="margin-left:10px; margin-bottom:0;">生成</button>
        </div>
        <pre id="ddl-output" class="ddl-empty">选择数据库类型后点击“生成”。</pre>
        <button id="ddl-copy" class="action-btn hidden" style="align-self:flex-start;">复制 DDL</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  const close = () => overlay.remove();
  overlay.querySelector(".modal-close").addEventListener("click", close);
  overlay.addEventListener("click", (event) => { if (event.target === overlay) close(); });
  const output = overlay.querySelector("#ddl-output");
  const copyBtn = overlay.querySelector("#ddl-copy");
  overlay.querySelector("#ddl-generate").addEventListener("click", async () => {
    const dialect = overlay.querySelector("#ddl-dialect").value;
    output.className = "ddl-empty";
    output.textContent = "生成中…";
    copyBtn.classList.add("hidden");
    try {
      const payload = await api("/api/ddl", { id: node.stable_id, dialect });
      const lines = [];
      if (payload.errors && payload.errors.length) lines.push(`-- 错误:\n-- ${payload.errors.join("\n-- ")}`);
      if (payload.warnings && payload.warnings.length) lines.push(`-- 警告:\n-- ${payload.warnings.join("\n-- ")}`);
      if (payload.sql) lines.push(payload.sql);
      if (lines.length) {
        output.className = "ddl-output";
        output.textContent = lines.join("\n\n");
        copyBtn.classList.remove("hidden");
      } else {
        output.textContent = "未生成任何 DDL。";
      }
      renderDdlValidation(output, payload.validation);
    } catch (error) {
      output.className = "ddl-empty";
      output.textContent = `生成失败：${error.message}`;
    }
  });
  copyBtn.addEventListener("click", async () => {
    await copyText(output.textContent);
    copyBtn.textContent = "已复制 ✓";
    setTimeout(() => { copyBtn.textContent = "复制 DDL"; }, 1500);
  });
}

function renderDdlValidation(output, validation) {
  output.parentElement.querySelectorAll(".ddl-note,.ddl-valid,.ddl-invalid").forEach((el) => el.remove());
  const note = document.createElement("div");
  if (!validation || (!validation.available && !validation.hint)) return;
  if (!validation.available) {
    note.className = "ddl-note";
    note.textContent = `ℹ ${validation.hint || "SQL 校验不可用"}`;
  } else if (validation.valid) {
    note.className = "ddl-valid";
    note.textContent = `✓ sqlglot 校验通过（解析到 ${validation.statements} 条 CREATE TABLE）`;
  } else {
    note.className = "ddl-invalid";
    note.textContent = `✕ sqlglot 校验未通过：${(validation.errors || []).join("；")}`;
  }
  output.after(note);
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

function goBackDetail() {
  const prev = detailHistory.stack.pop();
  if (prev) {
    detailHistory.current = prev;
    showDetail(prev, true);
  }
}

async function showDetail(nodeRef, isBack = false) {
  const pane = $("#detail-pane");
  document.body.style.overflow = ""; // 重建面板前退出可能残留的全屏滚动锁
  pane.innerHTML = `<div class="empty-tip">加载中…</div>`;
  if (!isBack) {
    if (detailHistory.current && detailHistory.current !== nodeRef) {
      detailHistory.stack.push(detailHistory.current);
      if (detailHistory.stack.length > 50) detailHistory.stack.shift();
    }
    detailHistory.current = nodeRef;
  }
  try {
    const data = await api("/api/node", { id: nodeRef });
    const node = data.node;
    const props = node.properties || {};
    const backHtml = detailHistory.stack.length
      ? `<button id="btn-back" class="back-btn">← 返回上一级</button>` : "";
    let html = backHtml + `<div class="detail-title">${esc(props.longname || props.name || node.raw_id || node.full_id)}</div>
      <div class="detail-sub"><span class="kind-badge">${esc(node.kind)}</span>
      fullId: ${esc(node.full_id)} · id: ${esc(node.raw_id)}<br>来源：${esc(node.file_path)}</div>`;
    html += propGrid(props);
    html += renderDetailSections(data);
    if (data.children && data.children.length)
      html += section("包含子模型（点击展开）", `<div class="tree">${treeHtml(data.children)}</div>`);
    if (data.xml_fragment)
      html += section(`原始 XML 片段（${esc(data.xml_fragment.path)}）`,
        `<pre class="sql-text">${esc(data.xml_fragment.xml)}</pre>`);
    html += edgeSection("引用（出）", data.out_edges || []);
    html += edgeSection("被引用（入）", data.in_edges || []);
    pane.innerHTML = html;
    pane.scrollTop = 0;
    const backBtn = pane.querySelector("#btn-back");
    if (backBtn) backBtn.addEventListener("click", goBackDetail);
    const ddlBtn = pane.querySelector("#btn-gen-ddl");
    if (ddlBtn) ddlBtn.addEventListener("click", () => showDdlDialog(node));
    pane.querySelectorAll("a.node-link").forEach((link) =>
      link.addEventListener("click", () => showDetail(link.dataset.id)));
    const tree = pane.querySelector(".tree");
    if (tree) bindTree(tree);
    const flowHolder = pane.querySelector(".flow-holder");
    if (flowHolder && (data.detail || {}).flow_steps) await renderFlow(data.detail.flow_steps, flowHolder);
  } catch (error) {
    if (!isBack) detailHistory.current = detailHistory.stack.pop() || null;
    pane.innerHTML = `<div class="error-banner">${esc(error.message)}</div>`;
  }
}

/* ---------- 初始化 ---------- */
function initLayoutControls() {
  const layout = $("#layout");
  $("#nav-toggle").addEventListener("click", () => {
    layout.classList.toggle("nav-collapsed");
    renderNav();
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
  $("#query").addEventListener("keydown", (event) => { if (event.key === "Enter") { state.page = 1; runSearch(); } });
  $("#btn-prev").addEventListener("click", () => { if (state.page > 1) { state.page -= 1; runSearch(); } });
  $("#btn-next").addEventListener("click", () => { state.page += 1; runSearch(); });
  $("#kind-select").addEventListener("change", () => { state.page = 1; runSearch(); });
  $("#dimension").addEventListener("change", () => { state.page = 1; runSearch(); });
  switchPage("dashboard");
}

init();
