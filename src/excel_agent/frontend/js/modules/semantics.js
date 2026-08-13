import { api } from "../api-client.js";
import { store } from "../state.js";

export async function loadSemantics(taskId, datasetId) {
  if (!taskId) return null;
  const [model, bindings, metrics] = await Promise.all([
    api.semanticModel(),
    api.semanticBindings(taskId, datasetId),
    api.semanticMetrics(taskId),
  ]);
  store.update((state) => {
    state.semanticModels[taskId] = model;
    state.bindings[datasetId || taskId] = bindings.bindings || [];
    state.semanticModels[`${taskId}:metrics`] = metrics;
  });
  return { model, bindings: bindings.bindings || [], metrics };
}

export async function decideBinding(taskId, datasetId, bindingId, physicalFieldId, confirm = true) {
  const binding = await api.semanticBindingDecision(taskId, datasetId, {
    binding_id: bindingId,
    physical_field_id: physicalFieldId || null,
    confirm,
  });
  const current = store.get().bindings[datasetId] || [];
  store.update((state) => { state.bindings[datasetId] = current.map((item) => item.binding_id === bindingId ? binding : item); });
  return binding;
}

export function semanticMember(model, memberId) { return (model?.members || []).find((item) => item.member_id === memberId || item.id === memberId); }
