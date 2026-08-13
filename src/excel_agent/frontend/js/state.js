const STORAGE_KEY = "excelmind.v2.workbench";

const initialState = {
  tasks: [],
  activeTaskId: null,
  datasetsByTask: {},
  activeDatasetIdByTask: {},
  inspections: {},
  activeInspectionByTask: {},
  profiles: {},
  previews: {},
  bindings: {},
  semanticModels: {},
  conversations: {},
  pendingClarifications: {},
  analysesByTask: {},
  activeAnalysisIdByTask: {},
  analysisStatusById: {},
  answerSequenceByAnalysis: {},
  chatPendingByTask: {},
  knowledgeByTask: {},
  joinSuggestionsByTask: {},
  requestStatus: {},
  lastError: null,
};

function cloneInitial() {
  return JSON.parse(JSON.stringify(initialState));
}

class Store {
  constructor() {
    this.state = cloneInitial();
    this.listeners = new Set();
  }

  get() { return this.state; }

  subscribe(listener) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  set(updater) {
    const next = typeof updater === "function" ? updater(this.state) : updater;
    this.state = next;
    this.persist();
    for (const listener of this.listeners) listener(this.state);
    return this.state;
  }

  update(mutator) {
    return this.set((current) => {
      const next = { ...current };
      mutator(next);
      return next;
    });
  }

  patch(partial) { return this.set((current) => ({ ...current, ...partial })); }

  hydrate() {
    try {
      const saved = JSON.parse(sessionStorage.getItem(STORAGE_KEY) || "{}");
      this.patch({
        activeTaskId: saved.task_id || null,
        activeDatasetIdByTask: saved.task_id && saved.dataset_id
          ? { [saved.task_id]: saved.dataset_id }
          : {},
      });
    } catch { /* stale or unavailable session storage is safe to ignore */ }
  }

  persist() {
    try {
      sessionStorage.setItem(STORAGE_KEY, JSON.stringify({
        task_id: this.state.activeTaskId,
        dataset_id: this.state.activeTaskId
          ? this.state.activeDatasetIdByTask[this.state.activeTaskId] || null
          : null,
      }));
    } catch { /* private browsing may disable storage */ }
  }
}

export const store = new Store();
export const clone = (value) => JSON.parse(JSON.stringify(value));

export function setRequestStatus(key, value) {
  store.update((state) => { state.requestStatus = { ...state.requestStatus, [key]: value }; });
}
