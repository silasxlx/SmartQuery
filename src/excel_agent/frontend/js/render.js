const charts = new Map();

export function clear(node) { while (node) { node.replaceChildren(); break; } }

export function escapeHtml(value) {
  const text = document.createElement("div");
  text.textContent = value == null ? "" : String(value);
  return text.innerHTML;
}

function safeUrl(value) {
  const url = String(value || "").trim();
  return url.startsWith("#")
    || (url.startsWith("/") && !url.startsWith("//"))
    || url.startsWith("./")
    || url.startsWith("../");
}

export function sanitizeMarkdown(markdown) {
  const source = String(markdown || "");
  if (!window.marked || typeof window.marked.parse !== "function") return escapeHtml(source).replaceAll("\n", "<br>");
  const parsed = window.marked.parse(source, { gfm: true, breaks: true });
  const template = document.createElement("template");
  template.innerHTML = parsed;
  for (const node of template.content.querySelectorAll("script,style,iframe,object,embed,form,base,link,meta")) node.remove();
  for (const node of template.content.querySelectorAll("*")) {
    for (const attribute of [...node.attributes]) {
      const name = attribute.name.toLowerCase();
      if (name.startsWith("on") || name === "srcdoc" || name === "style") node.removeAttribute(attribute.name);
      if ((name === "href" || name === "src") && !safeUrl(attribute.value)) node.removeAttribute(attribute.name);
    }
  }
  return template.innerHTML;
}

export function formatNumber(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "number" && Number.isFinite(value)) return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 4 }).format(value);
  return escapeHtml(value);
}

export function renderMarkdown(node, markdown) { node.innerHTML = sanitizeMarkdown(markdown); }

export function renderTable(node, result) {
  node.replaceChildren();
  const rows = Array.isArray(result?.rows) ? result.rows : [];
  const columns = Array.isArray(result?.columns) && result.columns.length ? result.columns : (rows[0] ? Object.keys(rows[0]) : []);
  if (!columns.length || !rows.length) { node.innerHTML = '<div class="empty-state compact">没有可展示的数据</div>'; return; }
  const table = document.createElement("table"); table.className = "data-table";
  const head = document.createElement("thead"); const headRow = document.createElement("tr");
  for (const column of columns) { const th = document.createElement("th"); th.textContent = column; headRow.append(th); }
  head.append(headRow); table.append(head);
  const body = document.createElement("tbody");
  for (const row of rows.slice(0, 1000)) {
    const tr = document.createElement("tr");
    for (const column of columns) { const td = document.createElement("td"); td.textContent = row?.[column] == null ? "—" : String(row[column]); tr.append(td); }
    body.append(tr);
  }
  table.append(body); node.append(table);
}

function chartRows(spec) {
  return Array.isArray(spec?.data) ? spec.data.filter((row) => row && typeof row === "object").slice(0, 1000) : [];
}

export function renderChart(node, spec) {
  disposeChartsIn(node);
  node.replaceChildren();
  if (!spec || !window.echarts) return;
  const chartType = ["bar", "line", "pie"].includes(spec.chart_type) ? spec.chart_type : "bar";
  const rows = chartRows(spec);
  const dimension = spec.dimension || "dimension";
  const metrics = Array.isArray(spec.metrics) ? spec.metrics.filter((metric) => /^[A-Za-z0-9_:.\-]+$/.test(String(metric))).slice(0, 5) : [];
  if (!metrics.length || !rows.length) { node.innerHTML = '<div class="empty-state compact">暂无可视化数据</div>'; return; }
  const chartEl = document.createElement("div"); chartEl.className = "chart-container"; chartEl.setAttribute("role", "img"); chartEl.setAttribute("aria-label", spec.title || "分析图表"); node.append(chartEl);
  const instance = window.echarts.init(chartEl, null);
  const categories = rows.map((row) => String(row[dimension] ?? "—"));
  const series = metrics.map((metric) => ({
    name: metric,
    type: chartType === "pie" ? "pie" : chartType,
    data: chartType === "pie" ? rows.map((row) => ({ name: String(row[dimension] ?? "—"), value: Number(row[metric]) || 0 })) : rows.map((row) => Number(row[metric]) || 0),
    smooth: chartType === "line",
  }));
  const option = {
    title: { text: String(spec.title || "分析图表"), left: "left", textStyle: { fontSize: 14 } },
    tooltip: { trigger: chartType === "pie" ? "item" : "axis" },
    legend: { type: "scroll" },
    xAxis: chartType === "pie" ? undefined : { type: "category", data: categories },
    yAxis: chartType === "pie" ? undefined : { type: "value", name: String(spec.unit || "") },
    series,
  };
  instance.setOption(option);
  charts.set(node, instance);
  if (window.ResizeObserver) new ResizeObserver(() => instance.resize()).observe(chartEl);
  const summary = document.createElement("p"); summary.className = "chart-summary"; summary.textContent = `图表使用${rows.length}个数据点，维度为${dimension}，指标为${metrics.join("、")}。`;
  node.append(summary);
}

export function disposeCharts() { for (const chart of charts.values()) chart.dispose(); charts.clear(); }

export function disposeChartsIn(node) {
  for (const [owner, chart] of charts.entries()) {
    if (owner === node || node?.contains(owner)) {
      chart.dispose();
      charts.delete(owner);
    }
  }
}

export function renderEvidence(node, analysis) {
  disposeChartsIn(node);
  node.replaceChildren();
  const evidence = analysis?.evidence;
  const records = analysis?.analyses || [];
  if (!evidence && !analysis && !records.length) { node.innerHTML = '<div class="empty-state compact">完成一次分析后显示Evidence</div>'; return; }
  if (records.length) {
    const history = document.createElement("div"); history.className = "evidence-card";
    const title = document.createElement("h3"); title.textContent = "分析记录"; history.append(title);
    for (const record of records.slice().reverse()) {
      const row = document.createElement("div"); row.className = "analysis-row";
      const main = document.createElement("div"); main.className = "analysis-row-main";
      const name = document.createElement("span"); name.className = "analysis-row-title"; name.textContent = record.question || record.analysis_id;
      const meta = document.createElement("span"); meta.className = "analysis-row-meta"; meta.textContent = `${record.status || "unknown"} · ${record.analysis_id || ""}`;
      main.append(name, meta); row.append(main);
      if (record.status && ["completed", "failed", "timed_out", "cancelled"].includes(record.status) && record.resources_settled !== false) {
        const button = document.createElement("button"); button.className = "button button-danger button-small"; button.type = "button"; button.dataset.deleteAnalysis = record.analysis_id; button.textContent = "删除"; row.append(button);
      }
      history.append(row);
    }
    node.append(history);
  }
  if (!evidence) return;
  const card = document.createElement("div"); card.className = "evidence-card evidence-section";
  const title = document.createElement("h3"); title.textContent = "本次分析证据"; card.append(title);
  const list = document.createElement("dl"); list.className = "evidence-list";
  const values = [
    ["数据集", evidence.dataset_id], ["语义版本", evidence.semantic_model_version], ["输入行数", formatNumber(evidence.input_rows)], ["输出行数", formatNumber(evidence.output_rows)],
  ];
  for (const [label, value] of values) { const wrapper = document.createElement("div"); const dt = document.createElement("dt"); dt.textContent = label; const dd = document.createElement("dd"); dd.textContent = value == null ? "—" : String(value); wrapper.append(dt, dd); list.append(wrapper); }
  card.append(list);
  if (evidence.warnings?.length) { const ul = document.createElement("ul"); ul.className = "warning-list"; for (const warning of evidence.warnings) { const li = document.createElement("li"); li.textContent = warning; ul.append(li); } card.append(ul); }
  const detail = document.createElement("details"); detail.className = "evidence-section"; const summary = document.createElement("summary"); summary.textContent = "查看口径、过滤与计算步骤"; detail.append(summary);
  const resolution = evidence.semantic_resolution || {};
  const plan = evidence.query_plan || {};
  const planSummary = {
    intent: plan.intent,
    metric_ids: plan.metric_ids || plan.metrics,
    dimension_ids: plan.dimension_ids || plan.dimensions,
    time_grain: plan.time_grain,
    limit: plan.limit,
    query_steps: Array.isArray(plan.queries) ? plan.queries.length : undefined,
    post_calculations: Array.isArray(plan.calculations) ? plan.calculations.length : undefined,
  };
  const pre = document.createElement("pre"); pre.className = "code-summary"; pre.textContent = JSON.stringify({
    semantic_resolution: {
      intent: resolution.intent,
      metric_ids: resolution.metric_ids,
      dimension_ids: resolution.dimension_ids,
      time_range: resolution.time_range,
      time_grain: resolution.time_grain,
      comparison: resolution.comparison,
    },
    filters: evidence.filters,
    intermediate_values: evidence.intermediate_values,
    query_plan: planSummary,
  }, null, 2); detail.append(pre); card.append(detail);
  const result = evidence.result || {};
  const table = document.createElement("div"); table.className = "evidence-section"; renderTable(table, result); card.append(table);
  if (analysis.chart) { const chart = document.createElement("div"); chart.className = "evidence-section"; renderChart(chart, analysis.chart); card.append(chart); }
  node.append(card);
}

export function resultToMarkdown(analysis) {
  const evidence = analysis?.evidence || {};
  const result = evidence.result || analysis?.result || {};
  const rows = Array.isArray(result.rows) ? result.rows : [];
  const columns = Array.isArray(result.columns) && result.columns.length ? result.columns : (rows[0] ? Object.keys(rows[0]) : []);
  const lines = [`问题：${analysis?.question || ""}`, "", "回答：", String(analysis?.answer || analysis?.content || "")];
  if (columns.length) {
    lines.push("", "结果表格：", `| ${columns.join(" | ")} |`, `| ${columns.map(() => "---").join(" | ")} |`);
    for (const row of rows.slice(0, 1000)) lines.push(`| ${columns.map((column) => String(row?.[column] ?? "—").replaceAll("|", "\\|")).join(" | ")} |`);
  }
  lines.push("", `数据集：${evidence.dataset_id || analysis?.dataset_id || "—"}`, `语义模型版本：${evidence.semantic_model_version || "—"}`);
  if (evidence.warnings?.length) lines.push(`告警：${evidence.warnings.join("；")}`);
  return lines.join("\n");
}
