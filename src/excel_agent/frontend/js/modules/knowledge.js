import { api } from "../api-client.js";
import { store } from "../state.js";

export async function loadKnowledge(taskId) {
  if (!taskId) return [];
  const response = await api.listKnowledge(taskId);
  const documents = response.documents || [];
  store.update((state) => { state.knowledgeByTask[taskId] = documents; });
  return documents;
}

export async function uploadKnowledge(taskId, file, signal) {
  const document = await api.uploadKnowledge(taskId, file, signal);
  await loadKnowledge(taskId);
  return document;
}

export async function deleteKnowledge(taskId, documentId) {
  await api.deleteKnowledge(taskId, documentId);
  store.update((state) => { state.knowledgeByTask[taskId] = (state.knowledgeByTask[taskId] || []).filter((item) => item.document_id !== documentId); });
}
