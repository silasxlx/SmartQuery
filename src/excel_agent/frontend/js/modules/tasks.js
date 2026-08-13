import { api, isNotFound } from "../api-client.js";
import { store, setRequestStatus } from "../state.js";

export async function loadTasks({ preserveLocal = false } = {}) {
  setRequestStatus("tasks", "loading");
  try {
    const response = await api.listTasks();
    const remoteTasks = response.tasks || [];
    store.update((state) => {
      const tasks = preserveLocal
        ? [...remoteTasks, ...state.tasks.filter((item) => !remoteTasks.some((remote) => remote.task_id === item.task_id))]
        : remoteTasks;
      const saved = state.activeTaskId;
      const activeTaskId = tasks.some((task) => task.task_id === saved) ? saved : (tasks[0]?.task_id || null);
      state.tasks = tasks;
      state.activeTaskId = activeTaskId;
    });
    setRequestStatus("tasks", "ready");
    return store.get().tasks;
  } catch (error) {
    setRequestStatus("tasks", "error");
    throw error;
  }
}

export async function createTask(name) {
  const task = await api.createTask(name);
  store.update((state) => { state.tasks = [...state.tasks, task]; state.activeTaskId = task.task_id; });
  return task;
}

export async function selectTask(taskId) {
  let task;
  try {
    task = await api.getTask(taskId);
  } catch (error) {
    if (isNotFound(error)) clearInvalidTask(taskId);
    throw error;
  }
  store.update((state) => { state.activeTaskId = task.task_id; });
  return task;
}

export async function deleteTask(taskId) {
  await api.deleteTask(taskId);
  store.update((state) => {
    const datasetIds = (state.datasetsByTask[taskId] || []).map((item) => item.dataset_id);
    const analysisIds = (state.analysesByTask[taskId] || []).map((item) => item.analysis_id);
    state.tasks = state.tasks.filter((item) => item.task_id !== taskId);
    delete state.datasetsByTask[taskId]; delete state.conversations[taskId]; delete state.analysesByTask[taskId];
    delete state.activeDatasetIdByTask[taskId]; delete state.knowledgeByTask[taskId]; delete state.chatPendingByTask[taskId];
    delete state.activeInspectionByTask[taskId]; delete state.pendingClarifications[taskId];
    delete state.semanticModels[taskId]; delete state.semanticModels[`${taskId}:metrics`];
    delete state.activeAnalysisIdByTask[taskId]; delete state.joinSuggestionsByTask[taskId];
    for (const analysisId of analysisIds) {
      delete state.analysisStatusById[analysisId]; delete state.answerSequenceByAnalysis[analysisId];
    }
    for (const datasetId of datasetIds) {
      delete state.profiles[datasetId]; delete state.previews[datasetId]; delete state.bindings[datasetId];
    }
    state.activeTaskId = state.tasks[0]?.task_id || null;
  });
}

export function currentTaskId() { return store.get().activeTaskId; }

export function clearInvalidTask(taskId) {
  if (!taskId) return;
  store.update((state) => {
    const datasetIds = (state.datasetsByTask[taskId] || []).map((item) => item.dataset_id);
    state.tasks = state.tasks.filter((task) => task.task_id !== taskId);
    if (state.activeTaskId === taskId) state.activeTaskId = null;
    delete state.datasetsByTask[taskId]; delete state.activeDatasetIdByTask[taskId]; delete state.chatPendingByTask[taskId];
    delete state.conversations[taskId]; delete state.analysesByTask[taskId]; delete state.knowledgeByTask[taskId];
    delete state.activeInspectionByTask[taskId]; delete state.pendingClarifications[taskId];
    delete state.semanticModels[taskId]; delete state.semanticModels[`${taskId}:metrics`]; delete state.joinSuggestionsByTask[taskId];
    for (const datasetId of datasetIds) { delete state.profiles[datasetId]; delete state.previews[datasetId]; delete state.bindings[datasetId]; }
  });
}

export function isTaskNotFound(error) { return isNotFound(error) || error?.code === "TASK_NOT_FOUND"; }
