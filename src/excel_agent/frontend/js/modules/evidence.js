import { api } from "../api-client.js";
import { store } from "../state.js";

export async function loadAnalyses(taskId) {
  if (!taskId) return [];
  const response = await api.listAnalyses(taskId);
  const analyses = response.analyses || [];
  store.update((state) => {
    state.analysesByTask[taskId] = analyses;
    const active = [...analyses].reverse().find((item) =>
      ["created", "running", "awaiting_clarification", "cancel_requested"].includes(item.status)
    ) || analyses.at(-1);
    state.activeAnalysisIdByTask[taskId] = active?.analysis_id || null;
    for (const item of analyses) state.analysisStatusById[item.analysis_id] = item.status;
    if (!state.conversations[taskId]?.length) {
      state.conversations[taskId] = analyses.flatMap((item) => [
        { role: "user", content: item.question || "", analysis_id: item.analysis_id },
        {
          role: "assistant",
          content: item.answer || "",
          analysis_id: item.analysis_id,
          stage: item.status,
          error: item.error || null,
          clarification: item.clarification || null,
        },
      ]);
    }
    state.pendingClarifications[taskId] = active?.status === "awaiting_clarification"
      ? active.clarification
      : null;
  });
  return analyses;
}

export async function loadAnalysis(taskId, analysisId) {
  const analysis = await api.analysis(taskId, analysisId);
  store.update((state) => {
    const current = state.analysesByTask[taskId] || [];
    state.analysesByTask[taskId] = current.some((item) => item.analysis_id === analysisId) ? current.map((item) => item.analysis_id === analysisId ? analysis : item) : [...current, analysis];
    state.analysisStatusById[analysisId] = analysis.status;
  });
  return analysis;
}

export async function cancelAnalysis(taskId, analysisId) {
  const analysis = await api.cancelAnalysis(taskId, analysisId);
  return loadAnalysis(taskId, analysis.analysis_id || analysisId);
}

export async function deleteAnalysis(taskId, analysisId) {
  await api.deleteAnalysis(taskId, analysisId);
  store.update((state) => { state.analysesByTask[taskId] = (state.analysesByTask[taskId] || []).filter((item) => item.analysis_id !== analysisId); });
}
