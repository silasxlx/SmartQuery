import { ApiError, api } from "./api-client.js";
import { store, setRequestStatus } from "./state.js";
import { ChatController } from "./modules/chat.js";
import { createDataset, decideNormalization, inspectUpload, loadDatasetDetails, loadDatasets, selectDataset, activeDatasetId } from "./modules/datasets.js";
import { loadQuality } from "./modules/quality.js";
import { decideBinding, loadSemantics } from "./modules/semantics.js";
import { cancelAnalysis, deleteAnalysis, loadAnalyses } from "./modules/evidence.js";
import { deleteKnowledge, loadKnowledge, uploadKnowledge } from "./modules/knowledge.js";
import { clearInvalidTask, createTask, currentTaskId, deleteTask, loadTasks, selectTask } from "./modules/tasks.js";
import { formatNumber, renderChart, renderEvidence, renderMarkdown, renderTable, resultToMarkdown, disposeChartsIn } from "./render.js";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const el = (tag, className, text) => { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = text; return node; };

const ui = {
  taskList: $("#task-list"), datasetList: $("#dataset-list"), datasetSummary: $("#dataset-summary"), datasetPreview: $("#dataset-preview"), inspection: $("#inspection-card"),
  conversation: $("#conversation"), conversationTitle: $("#conversation-title"), conversationContext: $("#conversation-context"), analysisStage: $("#analysis-stage"), clarification: $("#clarification-banner"),
  quality: $("#quality-content"), semantics: $("#semantics-content"), evidence: $("#evidence-content"), joins: $("#join-content"), knowledge: $("#knowledge-content"),
  upload: $("#dataset-upload"), knowledgeUpload: $("#knowledge-upload"), uploadLabel: $("#upload-label"), knowledgeUploadLabel: $("#knowledge-upload-label"),
  chatInput: $("#chat-input"), send: $("#send-question"), cancel: $("#cancel-analysis"), taskName: $("#task-name"), deleteTask: $("#delete-task"), serviceStatus: $("#service-status"), toast: $("#toast-region"),
};

let activeContextToken = 0;

function toast(message, type = "info", detail = "") {
  const item = el("div", `toast ${type}`); const title = el("div", "toast-title", message); item.append(title);
  if (detail) item.append(el("div", "toast-detail", detail));
  ui.toast.append(item); window.setTimeout(() => item.remove(), 5000);
}

function handleError(error, fallback = "操作失败") {
  const payload = error instanceof ApiError ? error : error || {};
  const message = payload.message || fallback;
  const detail = [payload.code, payload.requestId ? `request_id: ${payload.requestId}` : ""].filter(Boolean).join(" · ");
  toast(message, "error", detail);
  store.update((state) => { state.lastError = { code: payload.code || "CLIENT_ERROR", message, details: payload.details || {}, request_id: payload.requestId || "" }; });
}

function task() { return store.get().tasks.find((item) => item.task_id === currentTaskId()) || null; }
function datasets() { return currentTaskId() ? (store.get().datasetsByTask[currentTaskId()] || []) : []; }
function dataset() { const id = activeDatasetId(currentTaskId()); return datasets().find((item) => item.dataset_id === id) || null; }
function activeAnalysis() { const id = store.get().activeAnalysisIdByTask[currentTaskId()]; return (store.get().analysesByTask[currentTaskId()] || []).find((item) => item.analysis_id === id) || null; }
function isBusy() { const state = store.get(); const record = activeAnalysis(); return Boolean(state.chatPendingByTask[currentTaskId()] || (record && ["created", "running", "awaiting_clarification", "cancel_requested"].includes(record.status))); }
function isReadyDataset(item) { return item?.status === "ready"; }

function renderTasks(state) {
  ui.taskList.replaceChildren();
  if (!state.tasks.length) { ui.taskList.append(el("div", "empty-state compact", "尚未创建任务")); return; }
  for (const item of state.tasks) {
    const button = el("button", `task-item ${item.task_id === state.activeTaskId ? "active" : ""}`); button.type = "button"; button.dataset.taskId = item.task_id; button.setAttribute("role", "listitem");
    button.append(el("span", "task-item-icon", "▣"));
    const main = el("span", "task-item-main"); main.append(el("span", "task-item-name", item.name || item.task_id.slice(0, 8)), el("span", "task-item-meta", `${(item.dataset_ids || []).length} 个数据集${item.created_at ? ` · ${String(item.created_at).slice(0, 10)}` : ""}`));
    const status = el("span", "task-item-status", item.status || "active"); button.append(main, status); ui.taskList.append(button);
  }
  ui.deleteTask.disabled = !state.activeTaskId || isBusy();
}

function renderDatasets(state) {
  const taskId = state.activeTaskId; ui.datasetList.replaceChildren();
  if (!taskId) { ui.datasetList.append(el("div", "empty-state compact", "创建任务后上传数据")); return; }
  const items = state.datasetsByTask[taskId] || [];
  if (!items.length) { ui.datasetList.append(el("div", "empty-state compact", "当前任务尚未上传数据")); return; }
  const activeId = state.activeDatasetIdByTask[taskId];
  for (const item of items) {
    const button = el("button", `dataset-item ${item.dataset_id === activeId ? "active" : ""}`); button.type = "button"; button.dataset.datasetId = item.dataset_id; button.disabled = isBusy();
    const icon = item.kind === "csv" ? "▤" : "▥"; button.append(el("span", "dataset-item-icon", icon));
    const main = el("span", "dataset-item-main"); main.append(el("span", "dataset-item-name", item.display_name || item.dataset_id));
    const rows = item.profile?.row_count ?? item.row_count ?? "—"; const cols = item.profile?.column_count ?? item.column_count; main.append(el("span", "dataset-item-meta", `${item.kind || "dataset"} · ${formatNumber(rows)} 行${cols != null ? ` · ${formatNumber(cols)} 列` : ""}`));
    const status = el("span", `dataset-kind status-pill ${item.status === "ready" ? "status-ok" : item.status === "blocked" ? "status-warning" : "status-muted"}`, item.status || "unknown"); button.append(main, status); ui.datasetList.append(button);
  }
}

function renderInspection(state) {
  const inspection = state.activeInspectionByTask[state.activeTaskId]; ui.inspection.replaceChildren();
  if (!inspection) { ui.inspection.classList.add("hidden"); return; }
  ui.inspection.classList.remove("hidden");
  const title = el("h3", null, `待导入：${inspection.display_filename || "文件"}`); ui.inspection.append(title);
  const grid = el("div", "inspection-grid"); grid.append(stat("格式", inspection.format), stat("大小", `${formatNumber(Math.round((inspection.size_bytes || 0) / 1024))} KB`), stat("对象", `${(inspection.objects || []).length}`), stat("状态", (inspection.validation_errors || []).length ? "有校验问题" : "可检查")); ui.inspection.append(grid);
  const objects = inspection.objects || [];
  if (objects.length) {
    const label = el("label", null, inspection.format === "xlsx" ? "选择Sheet" : "CSV导入选项"); label.htmlFor = "inspection-object";
    const select = el("select"); select.id = "inspection-object"; select.dataset.inspectionObject = "true";
    for (const object of objects) { const option = el("option"); option.value = object.name || ""; option.textContent = `${object.name || "对象"}${object.rows ? ` · ${object.rows} 行` : ""}`; select.append(option); }
    ui.inspection.append(label, select);
  }
  if (inspection.encoding_candidates?.length) {
    const select = el("select"); select.id = "inspection-encoding"; select.dataset.inspectionEncoding = "true"; for (const item of inspection.encoding_candidates) { const option = el("option"); option.value = item; option.textContent = item; select.append(option); } ui.inspection.append(el("label", null, "编码"), select);
  }
  if (inspection.delimiter_candidates?.length) {
    const select = el("select"); select.id = "inspection-delimiter"; select.dataset.inspectionDelimiter = "true"; for (const item of inspection.delimiter_candidates) { const option = el("option"); option.value = item; option.textContent = item === "\t" ? "Tab" : item; select.append(option); } ui.inspection.append(el("label", null, "分隔符"), select);
  }
  const button = el("button", "button button-primary button-small"); button.type = "button"; button.dataset.createDataset = "true"; button.textContent = "创建Dataset"; button.disabled = isBusy(); ui.inspection.append(button);
}

function stat(label, value) { const node = el("div", "stat"); node.append(el("span", "stat-label", label), el("span", "stat-value", value)); return node; }

function renderSummary(state) {
  const item = dataset(); ui.datasetSummary.replaceChildren(); ui.datasetPreview.replaceChildren();
  if (!item) { ui.datasetSummary.className = "summary-card empty-state compact"; ui.datasetSummary.textContent = "请选择一个可用数据集"; ui.datasetPreview.classList.add("hidden"); return; }
  ui.datasetSummary.className = "summary-card";
  ui.datasetSummary.append(el("h3", null, item.display_name || "当前Dataset"));
  const grid = el("div", "metric-grid"); const profile = item.profile || state.profiles[item.dataset_id] || {};
  grid.append(stat("状态", item.status || "unknown"), stat("行数", formatNumber(profile.row_count ?? "—")), stat("列数", formatNumber(profile.column_count ?? "—")), stat("版本", String(item.version ?? "—"))); ui.datasetSummary.append(grid);
  if (item.pending_decisions?.length) {
    const warning = el("div", "warning-list"); warning.append(el("strong", null, "需要完成规范化确认"));
    for (const decision of item.pending_decisions) { const row = el("div", "binding-actions"); const select = el("select"); select.dataset.normalizationChoice = decision.decision_id; select.disabled = isBusy(); for (const choice of decision.options || []) { const option = el("option"); option.value = choice; option.textContent = choice; select.append(option); } const button = el("button", "button button-warning button-small", "确认"); button.type = "button"; button.dataset.normalizationId = decision.decision_id; button.disabled = isBusy(); row.append(el("span", null, decision.message || decision.field_name), select, button); warning.append(row); }
    ui.datasetSummary.append(warning);
  }
  const preview = state.previews[item.dataset_id]; if (preview) { ui.datasetPreview.classList.remove("hidden"); renderTable(ui.datasetPreview, preview); }
}

function renderQuality(state) {
  ui.quality.replaceChildren(); const item = dataset(); const profile = item ? (state.profiles[item.dataset_id] || item.profile) : null;
  if (!profile) { ui.quality.append(el("div", "empty-state compact", "选择数据集查看质量报告")); return; }
  const fields = profile.schema?.fields || [];
  const nullFields = fields.filter((field) => Number(field.null_ratio || 0) > 0).length;
  const overview = el("div", "quality-overview"); overview.append(stat("行数", formatNumber(profile.row_count)), stat("列数", formatNumber(profile.column_count)), stat("空值字段", formatNumber(nullFields)), stat("告警", formatNumber((profile.warnings || []).length))); ui.quality.append(overview);
  if (profile.warnings?.length) { const box = el("div", "quality-alert"); box.append(el("strong", null, "数据质量提醒")); const ul = el("ul", "warning-list"); for (const warning of profile.warnings) ul.append(el("li", null, warning)); box.append(ul); ui.quality.append(box); }
  const bindings = state.bindings[item.dataset_id] || [];
  const confirmed = bindings.filter((binding) => binding.status === "confirmed").length;
  const totalBindings = bindings.length;
  if (totalBindings) {
    const progress = el("div", "binding-progress"); const head = el("div", "binding-progress-head"); head.append(el("span", null, "语义绑定进度"), el("span", null, `${confirmed} / ${totalBindings}`)); const track = el("div", "binding-progress-track"); const value = el("div", "binding-progress-value"); value.style.width = `${Math.min(100, (confirmed / totalBindings) * 100)}%`; track.append(value); progress.append(head, track); ui.quality.append(progress);
  }
  ui.quality.append(el("h3", "quality-section-title", "字段质量"));
  for (const field of fields) {
    const row = el("div", "quality-row"); const main = el("div", "quality-row-main"); main.append(el("span", "quality-row-title", field.original_name || field.normalized_name), el("span", "quality-row-meta", `${field.physical_type} · 空值 ${(Number(field.null_ratio || 0) * 100).toFixed(1)}% · 唯一率 ${(Number(field.unique_ratio || 0) * 100).toFixed(1)}%`)); row.append(main, el("span", "status-pill status-muted", field.is_metric_candidate ? "指标候选" : field.is_dimension_candidate ? "维度候选" : "字段")); ui.quality.append(row);
  }
  const recent = (state.analysesByTask[state.activeTaskId] || []).filter((record) => record.status && ["completed", "failed", "timed_out", "cancelled"].includes(record.status)).slice(-3).reverse();
  if (recent.length) {
    const section = el("div", "recent-analysis"); section.append(el("h3", "quality-section-title", "最近分析"));
    for (const record of recent) { const row = el("div", "recent-analysis-row"); const main = el("div", "analysis-row-main"); main.append(el("span", "analysis-row-title", record.question || record.analysis_id), el("span", "analysis-row-meta", `${stageLabel(record.status)} · ${record.analysis_id || ""}`)); row.append(main, el("span", `status-pill ${record.status === "completed" ? "status-ok" : "status-muted"}`, stageLabel(record.status))); section.append(row); }
    ui.quality.append(section);
  }
}

function renderSemantics(state) {
  ui.semantics.replaceChildren(); const item = dataset(); const key = item?.dataset_id; const bindings = key ? (state.bindings[key] || []) : []; const model = state.semanticModels[state.activeTaskId];
  if (!item || !model) { ui.semantics.append(el("div", "empty-state compact", "选择数据集查看语义绑定")); return; }
  const counts = bindings.reduce((acc, binding) => { acc[binding.status] = (acc[binding.status] || 0) + 1; return acc; }, {});
  const summary = el("div", "metric-grid"); summary.append(stat("已确认", String(counts.confirmed || 0)), stat("待确认", String(counts.pending || 0)), stat("建议", String(counts.suggested || 0)), stat("冲突/拒绝", String((counts.rejected || 0) + (counts.conflict || 0)))); ui.semantics.append(summary);
  const total = bindings.length; const progress = el("div", "binding-progress"); const head = el("div", "binding-progress-head"); head.append(el("span", null, "绑定完成度"), el("span", null, `${counts.confirmed || 0} / ${total}`)); const track = el("div", "binding-progress-track"); const value = el("div", "binding-progress-value"); value.style.width = `${total ? Math.min(100, ((counts.confirmed || 0) / total) * 100) : 0}%`; track.append(value); progress.append(head, track); ui.semantics.append(progress);
  const fields = item.physical_schema?.fields || item.profile?.schema?.fields || [];
  for (const binding of bindings) {
    const member = (model.members || []).find((candidate) => candidate.member_id === binding.semantic_member_id || candidate.id === binding.semantic_member_id) || {};
    const row = el("div", "semantic-row"); const main = el("div", "semantic-row-main"); main.append(el("span", "semantic-row-title", member.name || binding.semantic_member_id), el("span", "semantic-row-meta", `${binding.semantic_member_kind || member.kind || "concept"} · 来源 ${binding.source || "—"}`));
    const status = el("span", `binding-status ${binding.status}`, binding.status || "unknown"); main.append(status); row.append(main);
    if (binding.status !== "confirmed" && binding.status !== "rejected") {
      const actions = el("div", "binding-actions"); const select = el("select"); select.dataset.bindingSelect = binding.binding_id; const candidates = binding.candidate_field_ids?.length ? binding.candidate_field_ids : fields.map((field) => field.field_id);
      const placeholder = el("option"); placeholder.value = ""; placeholder.textContent = "选择物理字段"; select.append(placeholder);
      for (const fieldId of candidates) { const field = fields.find((candidate) => candidate.field_id === fieldId); const option = el("option"); option.value = fieldId; option.textContent = field ? `${field.original_name} (${field.physical_type})` : fieldId; select.append(option); }
      const confirm = el("button", "button button-primary button-small", "确认绑定"); confirm.type = "button"; confirm.dataset.bindingConfirm = binding.binding_id; confirm.disabled = isBusy(); select.disabled = isBusy(); actions.append(select, confirm); row.append(actions);
    }
    ui.semantics.append(row);
  }
  const metrics = state.semanticModels[`${state.activeTaskId}:metrics`];
  const temporary = metrics?.task_metrics || [];
  if (temporary.length) {
    ui.semantics.append(el("h3", "subsection-title", "任务临时指标"));
    for (const metric of temporary) {
      const row = el("div", "semantic-row");
      const main = el("div", "semantic-row-main");
      main.append(
        el("span", "semantic-row-title", metric.name || metric.metric_id || "临时指标"),
        el("span", "semantic-row-meta", `${metric.unit || "无单位"} · ${metric.formula || "受限公式"}`),
      );
      const remove = el("button", "button button-danger button-small", "删除");
      remove.type = "button";
      remove.dataset.deleteMetric = metric.metric_id || metric.id || "";
      remove.disabled = isBusy();
      row.append(main, remove);
      ui.semantics.append(row);
    }
  }
}

function stageLabel(stage) { return ({ started: "已开始", semantic_resolving: "语义解析", plan_validated: "计划已校验", query_executed: "查询完成", evidence: "证据生成", answering: "生成回答", answered: "回答完成", chart: "图表生成", completed: "已完成", awaiting_clarification: "等待确认", cancelled: "已取消", failed: "失败", timed_out: "超时" })[stage] || stage || "处理中"; }

function analysisView(state, message) {
  const records = state.analysesByTask[state.activeTaskId] || [];
  const record = message.analysis_id ? records.find((item) => item.analysis_id === message.analysis_id) : null;
  return {
    ...(record || {}),
    ...message,
    answer: record?.answer ?? message.answer ?? message.content ?? "",
    evidence: record?.evidence ?? message.evidence ?? null,
    chart: record?.chart ?? message.chart ?? null,
    result: record?.result ?? message.result ?? record?.evidence?.result ?? message.evidence?.result ?? null,
    status: record?.status ?? message.status ?? message.stage,
    question: record?.question ?? message.question ?? "",
  };
}

function renderAnalysisResult(analysis, item, busy) {
  const card = el("article", "analysis-result-card"); card.dataset.analysisId = analysis.analysis_id || "";
  const header = el("div", "analysis-result-header"); const titleWrap = el("div"); titleWrap.append(el("h3", null, analysis.question || "分析结果")); titleWrap.append(el("div", "analysis-result-meta", `${stageLabel(analysis.status)}${analysis.analysis_id ? ` · ${analysis.analysis_id}` : ""}`)); header.append(titleWrap);
  const stateBadge = el("span", `status-pill ${analysis.status === "completed" ? "status-ok" : "status-muted"}`, stageLabel(analysis.status)); header.append(stateBadge); card.append(header);
  const answer = String(analysis.answer || "").trim();
  if (answer) { const section = el("section", "analysis-result-section analysis-result-conclusion"); section.append(el("h4", null, "核心结论")); const body = el("div", "analysis-result-answer"); renderMarkdown(body, answer); section.append(body); card.append(section); }
  const layout = el("div", "analysis-result-layout");
  const result = analysis.evidence?.result || analysis.result;
  if (result) { const section = el("section", "analysis-result-section"); section.append(el("h4", null, "结果表格")); const table = el("div", "analysis-result-table"); renderTable(table, result); section.append(table); layout.append(section); }
  if (analysis.chart) { const section = el("section", "analysis-result-section analysis-result-chart"); section.append(el("h4", null, "图表")); const chart = el("div"); renderChart(chart, analysis.chart); section.append(chart); layout.append(section); }
  if (layout.children.length) card.append(layout);
  const evidence = analysis.evidence || {};
  const datasetLabel = item?.dataset_id === (evidence.dataset_id || analysis.dataset_id) ? item.display_name : (evidence.dataset_id || analysis.dataset_id || "—");
  const source = el("div", "analysis-result-source"); source.append(el("span", "source-chip", `数据集：${datasetLabel}`)); source.append(el("span", "source-chip", `语义版本：${evidence.semantic_model_version || "—"}`)); if (evidence.output_rows != null) source.append(el("span", "source-chip", `输出 ${formatNumber(evidence.output_rows)} 行`)); card.append(source);
  if (evidence.warnings?.length) { const warning = el("div", "analysis-result-warning", `告警：${evidence.warnings.join("；")}`); card.append(warning); }
  if (analysis.status === "completed" && analysis.analysis_id) { const actions = el("div", "analysis-result-actions"); const copy = el("button", "button button-secondary button-small", "复制结果"); copy.type = "button"; copy.dataset.copyAnalysis = analysis.analysis_id; const rerun = el("button", "button button-primary button-small", "重新分析"); rerun.type = "button"; rerun.dataset.rerunAnalysis = analysis.analysis_id; rerun.disabled = busy; actions.append(copy, rerun); card.append(actions); }
  return card;
}

function renderChat(state) {
  const current = task(); const item = dataset(); const messages = current ? (state.conversations[current.task_id] || []) : [];
  ui.conversationTitle.textContent = current ? current.name : "请先创建任务";
  ui.conversationContext.textContent = item ? `${item.display_name || "当前Dataset"} · ${item.status === "ready" ? "已就绪，可以提问" : "需要完成数据处理"}` : "上传并完成语义绑定后，可以用业务语言提问。";
  disposeChartsIn(ui.conversation);
  ui.conversation.replaceChildren();
  if (!messages.length) { const empty = el("div", "empty-state large"); empty.append(el("div", "empty-icon", "⌁"), el("h3", null, "从一个业务问题开始"), el("p", null, "例如：按月份统计销售额，比较本月和上月的变化。")); ui.conversation.append(empty); }
  for (const message of messages) {
    const wrapper = el("article", `conversation-message ${message.role === "user" ? "user" : "assistant"}`); wrapper.dataset.analysisId = message.analysis_id || "";
    wrapper.append(el("div", "message-avatar", message.role === "user" ? "我" : "S")); const body = el("div", "message-body"); body.append(el("div", "message-meta", message.role === "user" ? "你的问题" : stageLabel(message.stage)));
    const content = el("div", "message-content");
    if (message.role === "user") content.textContent = message.content || "";
    else if (message.error) content.append(el("div", "toast error", `${message.error.code || "ERROR"}：${message.error.message || "分析失败"}`));
    else {
      const analysis = analysisView(state, message); const hasResult = Boolean(analysis.evidence || analysis.result || analysis.chart || (analysis.status === "completed" && analysis.answer));
      if (hasResult) content.append(renderAnalysisResult(analysis, item, isBusy()));
      else if (message.content) renderMarkdown(content, message.content);
      else { const track = el("div", "progress-track"); track.append(el("div", "progress-bar")); content.append(track); content.append(el("p", "muted", stageLabel(message.stage))); }
    }
    body.append(content);
    if (message.role === "assistant" && message.clarification) body.append(renderClarification(message.clarification));
    wrapper.append(body); ui.conversation.append(wrapper);
  }
  ui.conversation.scrollTop = ui.conversation.scrollHeight;
  const pending = state.pendingClarifications[current?.task_id]; ui.clarification.classList.toggle("hidden", !pending); ui.clarification.replaceChildren(); if (pending) ui.clarification.append(el("strong", null, "当前分析等待澄清确认"), el("span", "muted", " 请在对话卡片中确认、修改或拒绝。"));
}

function renderClarification(clarification) {
  const card = el("div", "clarification-card"); card.append(el("strong", null, clarification.kind || "需要确认"));
  const summary = clarification.summary || clarification.clarification_draft || {};
  const list = el("ul"); const safeKeys = ["concept", "semantic_member_id", "field_name", "physical_field_id", "formula", "unit", "time_grain", "message", "impact"];
  for (const key of safeKeys) if (summary[key] !== undefined && summary[key] !== null) list.append(el("li", null, `${key}：${typeof summary[key] === "object" ? JSON.stringify(summary[key]) : String(summary[key])}`));
  if (list.children.length) card.append(list); else card.append(el("p", "muted", "系统需要确认字段、指标口径或粒度。"));
  const actions = el("div", "clarification-actions"); const confirm = el("button", "button button-primary button-small", "确认"); confirm.type = "button"; confirm.dataset.clarifyAction = "confirm"; confirm.dataset.clarificationId = clarification.clarification_id; const revise = el("button", "button button-secondary button-small", "修改"); revise.type = "button"; revise.dataset.clarifyAction = "revise"; revise.dataset.clarificationId = clarification.clarification_id; const reject = el("button", "button button-danger button-small", "拒绝"); reject.type = "button"; reject.dataset.clarifyAction = "reject"; reject.dataset.clarificationId = clarification.clarification_id; actions.append(confirm, revise, reject); card.append(actions); return card;
}

function renderEvidencePanel(state) {
  const records = state.analysesByTask[state.activeTaskId] || []; const active = activeAnalysis(); renderEvidence(ui.evidence, active ? { ...active, analyses: records } : { analyses: records });
}

function renderKnowledge(state) {
  ui.knowledge.replaceChildren(); const documents = state.knowledgeByTask[state.activeTaskId] || [];
  if (!state.activeTaskId) { ui.knowledge.append(el("div", "empty-state compact", "创建任务后管理任务知识")); return; }
  if (!documents.length) { ui.knowledge.append(el("div", "empty-state compact", "当前任务尚未添加知识文档")); return; }
  for (const document of documents) {
    const row = el("div", "knowledge-row"); const main = el("div", "knowledge-row-main"); main.append(el("span", "knowledge-row-title", document.title || document.source_name || "文档"), el("span", "knowledge-row-meta", `${document.chunk_count || 0} 个片段 · ${document.status || "ready"}`)); row.append(main);
    const actions = el("div", "binding-actions"); const view = el("button", "button button-secondary button-small", "查看"); view.type = "button"; view.dataset.knowledgeView = document.document_id; const remove = el("button", "button button-danger button-small", "删除"); remove.type = "button"; remove.dataset.knowledgeDelete = document.document_id; remove.disabled = isBusy(); actions.append(view, remove); row.append(actions); ui.knowledge.append(row);
  }
}

function renderJoins(state) {
  ui.joins.replaceChildren(); const ready = datasets().filter(isReadyDataset); if (ready.length < 2) { ui.joins.append(el("div", "empty-state compact", "需要同一任务中至少两个就绪数据集")); return; }
  const form = el("div", "join-card"); const left = el("select"); left.id = "join-left"; const right = el("select"); right.id = "join-right"; for (const item of ready) { for (const select of [left, right]) { const option = el("option"); option.value = item.dataset_id; option.textContent = item.display_name || item.dataset_id; select.append(option); } } if (ready[1]) right.value = ready[1].dataset_id;
  form.append(el("label", null, "左侧Dataset"), left, el("label", null, "右侧Dataset"), right); const suggest = el("button", "button button-secondary button-small", "生成安全联表建议"); suggest.type = "button"; suggest.dataset.joinSuggest = "true"; suggest.disabled = isBusy(); left.disabled = isBusy(); right.disabled = isBusy(); form.append(suggest); ui.joins.append(form);
  const suggestion = state.joinSuggestionsByTask[state.activeTaskId]; if (!suggestion) return;
  for (const candidate of (suggestion.candidates || []).slice(0, 5)) {
    const row = el("div", "join-card"); row.append(el("strong", null, `${candidate.left_key} ↔ ${candidate.right_key}`), el("p", "muted", `匹配率 ${(Number(candidate.match_ratio || 0) * 100).toFixed(1)}% · ${candidate.relation} · 预计 ${candidate.expected_output_rows} 行`)); const create = el("button", "button button-primary button-small", "确认创建joined Dataset"); create.type = "button"; create.dataset.joinCreate = "true"; create.dataset.leftKey = candidate.left_key; create.dataset.rightKey = candidate.right_key; create.dataset.leftDataset = suggestion.left_dataset_id; create.dataset.rightDataset = suggestion.right_dataset_id; if (candidate.relation === "many_to_many" || Number(candidate.expected_growth_factor || 0) > 2 || isBusy()) create.disabled = true; row.append(create); ui.joins.append(row);
  }
}

function render(state) {
  renderTasks(state); renderDatasets(state); renderInspection(state); renderSummary(state); renderQuality(state); renderSemantics(state); renderChat(state); renderEvidencePanel(state); renderJoins(state); renderKnowledge(state);
  const item = dataset(); const busy = isBusy(); const taskId = state.activeTaskId; const enabled = Boolean(taskId && item && item.status === "ready" && !state.pendingClarifications[taskId] && !busy);
  ui.uploadLabel.classList.toggle("disabled", !taskId || busy); ui.upload.disabled = !taskId || busy; ui.knowledgeUploadLabel.classList.toggle("disabled", !taskId || busy); ui.knowledgeUpload.disabled = !taskId || busy;
  ui.chatInput.disabled = !enabled; ui.send.disabled = !enabled; ui.cancel.classList.toggle("hidden", !busy); ui.cancel.disabled = !activeAnalysis() || activeAnalysis()?.status === "cancel_requested";
  ui.analysisStage.className = `analysis-stage ${busy ? "status-warning" : "status-muted"}`; ui.analysisStage.textContent = activeAnalysis() ? stageLabel(activeAnalysis().status) : "空闲";
  ui.taskName.value = taskId ? (task()?.name || "") : ""; ui.deleteTask.disabled = !taskId || busy;
}

const chat = new ChatController({ onChange: () => render(store.get()), onError: (error) => handleError(error, "分析失败"), onToast: toast });

async function loadTaskContext(taskId) {
  const token = ++activeContextToken; chat.unsubscribe();
  if (!taskId) { render(store.get()); return; }
  try {
    const items = await loadDatasets(taskId); if (token !== activeContextToken) return;
    await Promise.all([loadAnalyses(taskId), loadKnowledge(taskId)]);
    const analyses = store.get().analysesByTask[taskId] || [];
    const latestAnalysis = analyses.at(-1);
    if (latestAnalysis?.analysis_id) await chat.recover(taskId, latestAnalysis.analysis_id);
    const activeId = activeDatasetId(taskId);
    if (activeId) await loadDatasetContext(taskId, activeId, token);
    render(store.get());
  } catch (error) { if (error.code === "TASK_NOT_FOUND") clearInvalidTask(taskId); else handleError(error, "无法加载任务"); render(store.get()); }
}

async function loadDatasetContext(taskId, datasetId, token = activeContextToken) {
  const [details] = await Promise.all([loadDatasetDetails(taskId, datasetId), loadQuality(taskId, datasetId), loadSemantics(taskId, datasetId)]);
  if (token !== activeContextToken) return details;
  return details;
}

async function handleUpload(file) {
  const taskId = currentTaskId(); if (!taskId || !file) return;
  try { setRequestStatus("upload", "loading"); const inspection = await inspectUpload(taskId, file); store.update((state) => { state.activeInspectionByTask[taskId] = inspection; }); toast("文件检查完成", "success", "请选择Sheet或CSV参数后创建Dataset"); setRequestStatus("upload", "ready"); render(store.get()); }
  catch (error) { setRequestStatus("upload", "error"); handleError(error, "文件检查失败"); }
  finally { ui.upload.value = ""; }
}

async function handleCreateDataset() {
  const taskId = currentTaskId(); const inspection = store.get().activeInspectionByTask[taskId]; if (!taskId || !inspection) return;
  const objectName = $("#inspection-object")?.value || null; const encoding = $("#inspection-encoding")?.value || null; const delimiter = $("#inspection-delimiter")?.value || null;
  try { const item = await createDataset(taskId, inspection, { object_name: objectName, encoding, delimiter }); store.update((state) => { delete state.activeInspectionByTask[taskId]; }); await loadDatasetContext(taskId, item.dataset_id); await loadAnalyses(taskId); toast("Dataset创建完成", "success"); render(store.get()); }
  catch (error) { handleError(error, "Dataset创建失败"); }
}

async function refreshDataset(taskId, datasetId) { await loadDatasetContext(taskId, datasetId); await loadDatasets(taskId); render(store.get()); }

async function handleClarification(action, clarificationId) {
  const taskId = currentTaskId(); const pending = store.get().pendingClarifications[taskId]; const item = dataset(); if (!pending || pending.clarification_id !== clarificationId || !item) return;
  if (action === "revise") {
    const message = window.prompt("请描述希望修改的字段、指标口径或粒度：", ""); if (!message) return;
    try { await chat.respond(taskId, item.dataset_id, pending, { confirm: false, message }); } catch (error) { handleError(error, "澄清修改失败"); } return;
  }
  try { await chat.respond(taskId, item.dataset_id, pending, { confirm: action === "confirm", message: action === "confirm" ? "确认" : "拒绝" }); } catch (error) { handleError(error, "澄清提交失败"); }
}

async function handleKnowledgeView(documentId) {
  try { const document = await api.getKnowledge(currentTaskId(), documentId); const content = String(document.content || ""); ui.knowledge.append(el("div", "knowledge-card", "")); const view = ui.knowledge.lastElementChild; view.append(el("h3", null, document.title || document.source_name || "文档")); const body = el("div"); renderMarkdown(body, content); view.append(body); } catch (error) { handleError(error, "无法查看文档"); }
}

async function copyAnalysisResult(analysisId) {
  const record = (store.get().analysesByTask[currentTaskId()] || []).find((item) => item.analysis_id === analysisId);
  if (!record) return;
  const text = resultToMarkdown(record);
  try {
    if (navigator.clipboard?.writeText) {
      try { await navigator.clipboard.writeText(text); }
      catch { const input = document.createElement("textarea"); input.value = text; input.setAttribute("readonly", "true"); input.style.position = "fixed"; input.style.opacity = "0"; document.body.append(input); input.select(); document.execCommand("copy"); input.remove(); }
    } else { const input = document.createElement("textarea"); input.value = text; input.setAttribute("readonly", "true"); input.style.position = "fixed"; input.style.opacity = "0"; document.body.append(input); input.select(); document.execCommand("copy"); input.remove(); }
    toast("结果已复制", "success");
  } catch (error) { handleError(error, "复制结果失败"); }
}

async function rerunAnalysis(analysisId) {
  const record = (store.get().analysesByTask[currentTaskId()] || []).find((item) => item.analysis_id === analysisId);
  const item = dataset();
  if (!record || !item) return;
  if (isBusy()) { toast("当前任务已有分析正在执行", "error"); return; }
  if (record.dataset_id && record.dataset_id !== item.dataset_id) { toast("原数据集已切换，请先选择原数据集", "error"); return; }
  try { await chat.start(currentTaskId(), item.dataset_id, record.question || ""); } catch (error) { handleError(error, "重新分析失败"); }
}

document.addEventListener("click", async (event) => {
  const target = event.target.closest("button,label"); if (!target) return;
  try {
    if (target.id === "create-task") { const created = await createTask(ui.taskName.value.trim() || null); ui.taskName.value = ""; await loadTaskContext(created.task_id); toast("任务创建完成", "success"); return; }
    if (target.id === "delete-task") { const current = task(); if (current && window.confirm(`确定删除任务“${current.name}”？`)) { await deleteTask(current.task_id); await loadTasks(); await loadTaskContext(currentTaskId()); toast("任务已删除", "success"); } return; }
    if (target.dataset.taskId) { await selectTask(target.dataset.taskId); await loadTaskContext(target.dataset.taskId); return; }
    if (target.dataset.datasetId) { await selectDataset(currentTaskId(), target.dataset.datasetId); await loadDatasetContext(currentTaskId(), target.dataset.datasetId); render(store.get()); return; }
    if (target.dataset.createDataset) { await handleCreateDataset(); return; }
    if (target.dataset.normalizationId) { const decisionId = target.dataset.normalizationId; const choice = $(`[data-normalization-choice="${CSS.escape(decisionId)}"]`)?.value; await decideNormalization(currentTaskId(), activeDatasetId(currentTaskId()), decisionId, choice); await refreshDataset(currentTaskId(), activeDatasetId(currentTaskId())); toast("规范化决策已提交", "success"); return; }
    if (target.dataset.bindingConfirm) { const bindingId = target.dataset.bindingConfirm; const selected = $(`[data-binding-select="${CSS.escape(bindingId)}"]`)?.value; if (!selected) { toast("请选择物理字段", "error"); return; } await decideBinding(currentTaskId(), activeDatasetId(currentTaskId()), bindingId, selected, true); await loadSemantics(currentTaskId(), activeDatasetId(currentTaskId())); render(store.get()); toast("语义绑定已确认", "success"); return; }
    if (target.dataset.deleteMetric) { await api.deleteSemanticMetric(currentTaskId(), target.dataset.deleteMetric); await loadSemantics(currentTaskId(), activeDatasetId(currentTaskId())); render(store.get()); toast("任务临时指标已删除", "success"); return; }
    if (target.id === "refresh-quality") { await loadQuality(currentTaskId(), activeDatasetId(currentTaskId())); render(store.get()); return; }
    if (target.id === "refresh-semantics") { await loadSemantics(currentTaskId(), activeDatasetId(currentTaskId())); render(store.get()); return; }
    if (target.id === "refresh-analyses") { await loadAnalyses(currentTaskId()); render(store.get()); return; }
    if (target.id === "cancel-analysis") { const record = activeAnalysis(); if (record) { await cancelAnalysis(currentTaskId(), record.analysis_id); toast("已请求取消，等待底层步骤退出", "success"); render(store.get()); } return; }
    if (target.dataset.deleteAnalysis) { if (window.confirm("确定删除这条终态分析及其Evidence吗？")) { await deleteAnalysis(currentTaskId(), target.dataset.deleteAnalysis); toast("分析记录已删除", "success"); render(store.get()); } return; }
    if (target.dataset.copyAnalysis) { await copyAnalysisResult(target.dataset.copyAnalysis); return; }
    if (target.dataset.rerunAnalysis) { await rerunAnalysis(target.dataset.rerunAnalysis); return; }
    if (target.dataset.clarifyAction) { await handleClarification(target.dataset.clarifyAction, target.dataset.clarificationId); return; }
    if (target.dataset.tab) { activateTab(target); return; }
    if (target.dataset.joinSuggest) { const left = $("#join-left")?.value; const right = $("#join-right")?.value; if (!left || !right || left === right) { toast("请选择两个不同的数据集", "error"); return; } const suggestion = await api.joinSuggestions(currentTaskId(), left, right); store.update((state) => { state.joinSuggestionsByTask[currentTaskId()] = suggestion; }); render(store.get()); return; }
    if (target.dataset.joinCreate) { const payload = { left_dataset_id: target.dataset.leftDataset, right_dataset_id: target.dataset.rightDataset, left_keys: [target.dataset.leftKey], right_keys: [target.dataset.rightKey], join_type: "inner", display_name: "安全联表Dataset" }; const joined = await api.createJoin(currentTaskId(), payload); await loadDatasets(currentTaskId()); await loadDatasetContext(currentTaskId(), joined.dataset_id); toast("joined Dataset创建完成", "success"); render(store.get()); return; }
    if (target.dataset.knowledgeView) { await handleKnowledgeView(target.dataset.knowledgeView); return; }
    if (target.dataset.knowledgeDelete) { if (window.confirm("确定删除该任务知识文档？")) { await deleteKnowledge(currentTaskId(), target.dataset.knowledgeDelete); toast("知识文档已删除", "success"); render(store.get()); } return; }
  } catch (error) { handleError(error); }
});

ui.upload.addEventListener("change", () => handleUpload(ui.upload.files?.[0]));
ui.knowledgeUpload.addEventListener("change", async () => { const file = ui.knowledgeUpload.files?.[0]; if (!file || !currentTaskId()) return; try { if (!/[.]((md)|(txt)|(markdown))$/i.test(file.name)) throw new ApiError("KNOWLEDGE_FILE_TYPE_INVALID", "仅支持Markdown或TXT文档", 422); await uploadKnowledge(currentTaskId(), file); toast("任务知识已上传", "success"); render(store.get()); } catch (error) { handleError(error, "知识上传失败"); } finally { ui.knowledgeUpload.value = ""; } });
$("#chat-form").addEventListener("submit", async (event) => { event.preventDefault(); const text = ui.chatInput.value.trim(); const item = dataset(); if (!text || !item || isBusy() || store.get().pendingClarifications[currentTaskId()]) return; ui.chatInput.value = ""; try { await chat.start(currentTaskId(), item.dataset_id, text); } catch (error) { handleError(error, "分析请求失败"); } });
store.subscribe(render);

function activateTab(target) {
  const tabs = $$(".tab");
  tabs.forEach((tab) => {
    const active = tab === target;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
  });
  $$(".tab-panel").forEach((panel) => {
    const active = panel.id === `tab-${target.dataset.tab}`;
    panel.classList.toggle("active", active);
    panel.hidden = !active;
  });
  target.focus();
}

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !ui.cancel.classList.contains("hidden") && !ui.cancel.disabled) {
    event.preventDefault();
    ui.cancel.click();
    return;
  }
  const target = event.target.closest?.(".tab");
  if (!target || !["ArrowRight", "ArrowDown", "ArrowLeft", "ArrowUp", "Home", "End"].includes(event.key)) return;
  const tabs = $$(".tab");
  const current = tabs.indexOf(target);
  const next = event.key === "Home" ? 0
    : event.key === "End" ? tabs.length - 1
      : (current + (["ArrowRight", "ArrowDown"].includes(event.key) ? 1 : -1) + tabs.length) % tabs.length;
  event.preventDefault();
  activateTab(tabs[next]);
});

async function boot() {
  render(store.get());
  try { const health = await api.health(); ui.serviceStatus.className = "status-pill status-ok"; ui.serviceStatus.textContent = health.status === "ok" ? "服务正常" : "服务正在关闭"; } catch (error) { ui.serviceStatus.className = "status-pill status-error"; ui.serviceStatus.textContent = "服务不可用"; }
  try { await loadTasks({ preserveLocal: true }); const taskId = currentTaskId(); if (taskId) await loadTaskContext(taskId); render(store.get()); } catch (error) { handleError(error, "无法加载任务列表"); render(store.get()); }
}

store.hydrate(); boot();
