import { ApiError } from "../api-client.js";
import { store } from "../state.js";
import { subscribeChat } from "../sse.js";
import { loadAnalysis } from "./evidence.js";

export class ChatController {
  constructor({ onChange, onError, onToast } = {}) {
    this.subscription = null;
    this.subscriptionTaskId = null;
    this.streamAnalysisIdByTask = {};
    this.onChange = onChange || (() => {});
    this.onError = onError || (() => {});
    this.onToast = onToast || (() => {});
  }

  activeAnalysis(taskId) {
    const id = store.get().activeAnalysisIdByTask[taskId];
    return id ? (store.get().analysesByTask[taskId] || []).find((item) => item.analysis_id === id) : null;
  }

  async start(taskId, datasetId, message, extra = {}) {
    if (!taskId || !datasetId || !String(message || "").trim()) throw new ApiError("QUESTION_EMPTY", "请输入业务问题", 422);
    if (this.subscription) this.subscription.unsubscribe();
    this.streamAnalysisIdByTask[taskId] = null;
    store.update((state) => {
      const current = state.conversations[taskId] || [];
      state.conversations[taskId] = [...current, { role: "user", content: String(message), analysis_id: null }];
      state.pendingClarifications[taskId] = null;
      state.lastError = null;
      state.chatPendingByTask[taskId] = true;
    });
    this.onChange();
    const payload = { message: String(message), dataset_id: datasetId, ...extra };
    this.subscription = subscribeChat(taskId, payload, {
      onEvent: (event, data) => this.handleEvent(taskId, event, data),
      onError: (error) => this.handleFailure(taskId, error),
      onComplete: () => {
        store.update((state) => { state.chatPendingByTask[taskId] = false; });
        this.streamAnalysisIdByTask[taskId] = null;
        this.subscription = null; this.subscriptionTaskId = null; this.onChange();
      },
    });
    this.subscriptionTaskId = taskId;
    return this.subscription.promise;
  }

  async respond(taskId, datasetId, clarification, response) {
    if (!clarification?.clarification_id) throw new ApiError("CLARIFICATION_MISMATCH", "澄清信息已失效", 409);
    if (this.subscription) this.subscription.unsubscribe();
    this.streamAnalysisIdByTask[taskId] = null;
    const record = (store.get().analysesByTask[taskId] || []).find((item) => item.analysis_id === clarification.analysis_id);
    store.update((state) => {
      state.pendingClarifications[taskId] = null;
      state.conversations[taskId] = [...(state.conversations[taskId] || []), { role: "user", content: response.message || (response.confirm ? "确认" : "拒绝"), analysis_id: clarification.analysis_id }];
      if (record) state.analysisStatusById[record.analysis_id] = "running";
      state.chatPendingByTask[taskId] = true;
    });
    this.onChange();
    const payload = {
      message: response.message || (response.confirm ? "确认" : "拒绝"),
      dataset_id: datasetId,
      clarification_id: clarification.clarification_id,
      analysis_id: clarification.analysis_id,
      draft_version: clarification.draft_version,
      confirm: response.confirm === true,
      physical_field_id: response.physical_field_id || null,
      metric_id: response.metric_id || null,
      name: response.name || null,
      formula: response.formula || null,
      unit: response.unit || null,
    };
    this.subscription = subscribeChat(taskId, payload, {
      onEvent: (event, data) => this.handleEvent(taskId, event, data),
      onError: (error) => this.handleFailure(taskId, error),
      onComplete: () => {
        store.update((state) => { state.chatPendingByTask[taskId] = false; });
        this.streamAnalysisIdByTask[taskId] = null;
        this.subscription = null; this.subscriptionTaskId = null; this.onChange();
      },
    });
    this.subscriptionTaskId = taskId;
    return this.subscription.promise;
  }

  async recover(taskId, analysisId) {
    if (!analysisId) return null;
    const analysis = await loadAnalysis(taskId, analysisId);
    if (analysis.status === "awaiting_clarification") store.update((state) => { state.pendingClarifications[taskId] = analysis.clarification; });
    this.onChange();
    return analysis;
  }

  unsubscribe() {
    if (this.subscription) this.subscription.unsubscribe();
    const taskId = this.subscriptionTaskId;
    if (taskId) store.update((state) => { state.chatPendingByTask[taskId] = false; });
    this.subscription = null; this.subscriptionTaskId = null; this.onChange();
    if (taskId) this.streamAnalysisIdByTask[taskId] = null;
  }

  handleEvent(taskId, event, data) {
    const streamId = this.streamAnalysisIdByTask[taskId];
    const eventId = data?.analysis_id || null;
    if (streamId && eventId && streamId !== eventId) return;
    if (eventId) this.streamAnalysisIdByTask[taskId] = eventId;
    const analysisId = data?.analysis_id || store.get().activeAnalysisIdByTask[taskId];
    if (analysisId) store.update((state) => { state.activeAnalysisIdByTask[taskId] = analysisId; state.analysisStatusById[analysisId] = event === "done" ? (data.status || state.analysisStatusById[analysisId]) : (event === "clarification_required" ? "awaiting_clarification" : "running"); });
    if (event === "started") {
      store.update((state) => {
        state.conversations[taskId] = [...(state.conversations[taskId] || []), { role: "assistant", content: "", analysis_id: analysisId, stage: "started" }];
        state.answerSequenceByAnalysis[analysisId] = -1;
      });
    } else if (event === "semantic_resolving") this.updateAssistant(taskId, analysisId, { stage: "semantic_resolving" });
    else if (event === "clarification_required") {
      store.update((state) => { state.pendingClarifications[taskId] = data; });
      this.updateAssistant(taskId, analysisId, { stage: "clarification_required", clarification: data, content: "在继续计算前，需要确认当前语义口径。" });
    } else if (event === "plan_validated") this.updateAssistant(taskId, analysisId, { stage: "plan_validated" });
    else if (event === "query_executed") this.updateAssistant(taskId, analysisId, { stage: "query_executed", result: data.result });
    else if (event === "evidence") this.updateAssistant(taskId, analysisId, { stage: "evidence", evidence: data });
    else if (event === "answer_delta") {
      const sequence = Number(data.sequence);
      const last = store.get().answerSequenceByAnalysis[analysisId] ?? -1;
      if (Number.isFinite(sequence) && sequence > last) {
        store.update((state) => { state.answerSequenceByAnalysis[analysisId] = sequence; });
        this.updateAssistant(taskId, analysisId, { append: String(data.text || ""), stage: "answering" });
      }
    } else if (event === "answer") this.updateAssistant(taskId, analysisId, { content: String(data.answer || ""), stage: "answered" });
    else if (event === "chart") this.updateAssistant(taskId, analysisId, { chart: data, stage: "chart" });
    else if (event === "error") {
      store.update((state) => { state.lastError = data; });
      this.updateAssistant(taskId, analysisId, { error: data, stage: "error" });
      this.onError(data);
    } else if (event === "done") {
      store.update((state) => { state.analysisStatusById[analysisId] = data.status || "completed"; });
      this.updateAssistant(taskId, analysisId, { stage: data.status || "completed" });
      loadAnalysis(taskId, analysisId).then(() => this.onChange()).catch(() => {});
    }
    this.onChange();
  }

  updateAssistant(taskId, analysisId, patch) {
    store.update((state) => {
      const conversation = state.conversations[taskId] || [];
      let index = -1;
      for (let i = conversation.length - 1; i >= 0; i -= 1) if (conversation[i].role === "assistant" && conversation[i].analysis_id === analysisId) { index = i; break; }
      if (index < 0) return;
      const current = conversation[index];
      const next = { ...current, ...patch };
      if (patch.append) next.content = `${current.content || ""}${patch.append}`;
      delete next.append;
      conversation[index] = next;
      state.conversations[taskId] = conversation;
    });
  }

  handleFailure(taskId, error) {
    if (error?.name === "AbortError") return;
    store.update((state) => {
      state.chatPendingByTask[taskId] = false;
      state.lastError = { code: error.code || "NETWORK_ERROR", message: error.message || "网络请求失败", details: error.details || {}, request_id: error.requestId || "" };
    });
    this.streamAnalysisIdByTask[taskId] = null;
    this.onError(error);
    this.onChange();
  }
}
