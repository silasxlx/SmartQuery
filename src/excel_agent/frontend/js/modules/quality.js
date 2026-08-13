import { api } from "../api-client.js";
import { store } from "../state.js";

export async function loadQuality(taskId, datasetId) {
  if (!taskId || !datasetId) return null;
  const profile = await api.profile(taskId, datasetId);
  store.update((state) => { state.profiles[datasetId] = profile; });
  return profile;
}
