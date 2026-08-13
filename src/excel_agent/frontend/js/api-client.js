const API_ROOT = "/api/v2";

export class ApiError extends Error {
  constructor(code, message, status = 500, details = {}, requestId = "") {
    super(message || code || "请求失败");
    this.name = "ApiError";
    this.code = code || "HTTP_ERROR";
    this.status = status;
    this.details = details || {};
    this.requestId = requestId || "";
  }
}

function joinPath(path) {
  const value = String(path || "");
  return value.startsWith("/api/") ? value : `${API_ROOT}${value.startsWith("/") ? value : `/${value}`}`;
}

async function parseResponse(response) {
  const requestId = response.headers.get("X-Request-ID") || "";
  const text = await response.text();
  let payload = {};
  if (text) {
    try { payload = JSON.parse(text); } catch { payload = { message: text }; }
  }
  if (!response.ok) {
    const error = payload?.error || payload || {};
    throw new ApiError(
      error.code || `HTTP_${response.status}`,
      error.message || "请求失败",
      response.status,
      error.details || {},
      error.request_id || requestId,
    );
  }
  return payload;
}

async function request(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (!(options.body instanceof FormData) && options.body !== undefined && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(joinPath(path), {
    credentials: "same-origin",
    ...options,
    headers,
  });
  return parseResponse(response);
}

async function upload(path, file, signal) {
  const body = new FormData();
  body.append("file", file, file.name);
  return request(path, { method: "POST", body, signal });
}

export const api = {
  request,
  async health() { return request("/health"); },
  async listTasks() { return request("/tasks"); },
  async createTask(name) { return request("/tasks", { method: "POST", body: JSON.stringify({ name: name || null }) }); },
  async getTask(taskId) { return request(`/tasks/${encodeURIComponent(taskId)}`); },
  async deleteTask(taskId) { return request(`/tasks/${encodeURIComponent(taskId)}`, { method: "DELETE" }); },
  async inspectUpload(taskId, file, signal) { return upload(`/tasks/${encodeURIComponent(taskId)}/uploads`, file, signal); },
  async createDataset(taskId, payload, signal) {
    return request(`/tasks/${encodeURIComponent(taskId)}/datasets`, { method: "POST", body: JSON.stringify(payload), signal });
  },
  async listDatasets(taskId) { return request(`/tasks/${encodeURIComponent(taskId)}/datasets`); },
  async getDataset(taskId, datasetId) { return request(`/tasks/${encodeURIComponent(taskId)}/datasets/${encodeURIComponent(datasetId)}`); },
  async preview(taskId, datasetId, limit = 20) { return request(`/tasks/${encodeURIComponent(taskId)}/datasets/${encodeURIComponent(datasetId)}/preview?limit=${limit}`); },
  async profile(taskId, datasetId) { return request(`/tasks/${encodeURIComponent(taskId)}/datasets/${encodeURIComponent(datasetId)}/profile`); },
  async normalizationDecision(taskId, datasetId, payload) {
    return request(`/tasks/${encodeURIComponent(taskId)}/datasets/${encodeURIComponent(datasetId)}/normalization-decisions`, { method: "POST", body: JSON.stringify(payload) });
  },
  async semanticBindings(taskId, datasetId) {
    const suffix = datasetId ? `?dataset_id=${encodeURIComponent(datasetId)}` : "";
    return request(`/tasks/${encodeURIComponent(taskId)}/semantic-bindings${suffix}`);
  },
  async semanticBindingDecision(taskId, datasetId, payload) {
    return request(`/tasks/${encodeURIComponent(taskId)}/datasets/${encodeURIComponent(datasetId)}/semantic-binding-decisions`, { method: "POST", body: JSON.stringify(payload) });
  },
  async semanticModel(version = "") { return request(`/semantic-model${version ? `/${encodeURIComponent(version)}` : ""}`); },
  async semanticMetrics(taskId) { return request(`/tasks/${encodeURIComponent(taskId)}/semantic-metrics`); },
  async deleteSemanticMetric(taskId, metricId) { return request(`/tasks/${encodeURIComponent(taskId)}/semantic-metrics/${encodeURIComponent(metricId)}`, { method: "DELETE" }); },
  async chat(taskId, payload, signal) {
    return request(`/tasks/${encodeURIComponent(taskId)}/chat`, {
      method: "POST",
      body: JSON.stringify(payload),
      signal,
    });
  },
  async postStream(path, payload, signal) {
    const headers = { "Content-Type": "application/json", Accept: "text/event-stream" };
    const response = await fetch(joinPath(path), { method: "POST", credentials: "same-origin", headers, body: JSON.stringify(payload), signal });
    if (!response.ok) await parseResponse(response);
    return response;
  },
  async analysis(taskId, analysisId) { return request(`/tasks/${encodeURIComponent(taskId)}/analyses/${encodeURIComponent(analysisId)}`); },
  async listAnalyses(taskId) { return request(`/tasks/${encodeURIComponent(taskId)}/analyses`); },
  async cancelAnalysis(taskId, analysisId) { return request(`/tasks/${encodeURIComponent(taskId)}/analyses/${encodeURIComponent(analysisId)}/cancel`, { method: "POST" }); },
  async deleteAnalysis(taskId, analysisId) { return request(`/tasks/${encodeURIComponent(taskId)}/analyses/${encodeURIComponent(analysisId)}`, { method: "DELETE" }); },
  async joinSuggestions(taskId, leftDatasetId, rightDatasetId) {
    return request(`/tasks/${encodeURIComponent(taskId)}/join-suggestions`, { method: "POST", body: JSON.stringify({ left_dataset_id: leftDatasetId, right_dataset_id: rightDatasetId }) });
  },
  async createJoin(taskId, payload) { return request(`/tasks/${encodeURIComponent(taskId)}/joins`, { method: "POST", body: JSON.stringify(payload) }); },
  async listKnowledge(taskId) { return request(`/tasks/${encodeURIComponent(taskId)}/knowledge/documents`); },
  async uploadKnowledge(taskId, file, signal) { return upload(`/tasks/${encodeURIComponent(taskId)}/knowledge/documents`, file, signal); },
  async getKnowledge(taskId, documentId) { return request(`/tasks/${encodeURIComponent(taskId)}/knowledge/documents/${encodeURIComponent(documentId)}`); },
  async deleteKnowledge(taskId, documentId) { return request(`/tasks/${encodeURIComponent(taskId)}/knowledge/documents/${encodeURIComponent(documentId)}`, { method: "DELETE" }); },
};

export function isNotFound(error) { return error instanceof ApiError && error.status === 404; }
