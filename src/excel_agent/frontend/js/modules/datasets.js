import { api } from "../api-client.js";
import { store, setRequestStatus } from "../state.js";

export async function loadDatasets(taskId) {
  if (!taskId) return [];
  setRequestStatus("datasets", "loading");
  const response = await api.listDatasets(taskId);
  const datasets = response.datasets || [];
  const saved = store.get().activeDatasetIdByTask[taskId];
  const ready = datasets.find((dataset) => dataset.status === "ready");
  const active = datasets.some((dataset) => dataset.dataset_id === saved) ? saved : (ready?.dataset_id || null);
  store.update((state) => { state.datasetsByTask[taskId] = datasets; state.activeDatasetIdByTask[taskId] = active; });
  setRequestStatus("datasets", "ready");
  return datasets;
}

export async function inspectUpload(taskId, file, signal) {
  const inspection = await api.inspectUpload(taskId, file, signal);
  store.update((state) => { state.inspections[inspection.upload_id] = inspection; });
  return inspection;
}

export async function createDataset(taskId, inspection, options = {}) {
  const payload = {
    upload_id: inspection.upload_id,
    object_name: options.object_name || null,
    encoding: options.encoding || null,
    delimiter: options.delimiter || null,
    display_name: options.display_name || inspection.display_filename || null,
  };
  const dataset = await api.createDataset(taskId, payload);
  await loadDatasets(taskId);
  store.update((state) => { state.activeDatasetIdByTask[taskId] = dataset.dataset_id; });
  return dataset;
}

export async function selectDataset(taskId, datasetId) {
  const dataset = await api.getDataset(taskId, datasetId);
  store.update((state) => { state.activeDatasetIdByTask[taskId] = datasetId; });
  return dataset;
}

export async function loadDatasetDetails(taskId, datasetId) {
  if (!taskId || !datasetId) return null;
  const [dataset, preview] = await Promise.all([
    api.getDataset(taskId, datasetId),
    api.preview(taskId, datasetId, 20).catch(() => null),
  ]);
  store.update((state) => {
    const datasets = state.datasetsByTask[taskId] || [];
    state.datasetsByTask[taskId] = datasets.map((item) => item.dataset_id === datasetId ? { ...item, ...dataset } : item);
    state.activeDatasetIdByTask[taskId] = datasetId;
    if (preview) state.previews[datasetId] = preview;
  });
  return { dataset, preview };
}

export async function decideNormalization(taskId, datasetId, decisionId, choice) {
  const dataset = await api.normalizationDecision(taskId, datasetId, { decision_id: decisionId, choice });
  await loadDatasets(taskId);
  store.update((state) => { state.activeDatasetIdByTask[taskId] = dataset.dataset_id; });
  return dataset;
}

export function activeDatasetId(taskId) { return store.get().activeDatasetIdByTask[taskId] || null; }
