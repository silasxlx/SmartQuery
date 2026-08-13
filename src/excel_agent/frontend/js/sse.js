import { api } from "./api-client.js";

function decodeData(lines) {
  const data = lines.filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trimStart()).join("\n");
  if (!data) return {};
  try { return JSON.parse(data); } catch { return { raw: data }; }
}

export async function consumeSSE(response, onEvent) {
  if (!response.body) return;
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let eventName = "message";
  let dataLines = [];
  const flush = async () => {
    if (!dataLines.length) return;
    await onEvent(eventName, decodeData(dataLines));
    eventName = "message";
    dataLines = [];
  };
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split(/\r?\n/);
    buffer = lines.pop() || "";
    for (const line of lines) {
      if (!line) { await flush(); continue; }
      if (line.startsWith("event:")) eventName = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line);
    }
  }
  buffer += decoder.decode();
  if (buffer.trim()) {
    for (const line of buffer.split(/\r?\n/)) {
      if (line.startsWith("event:")) eventName = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line);
    }
  }
  await flush();
}

export function subscribeChat(taskId, payload, handlers = {}) {
  const controller = new AbortController();
  const promise = (async () => {
    try {
      const response = await api.postStream(`/tasks/${encodeURIComponent(taskId)}/chat/stream`, payload, controller.signal);
      await consumeSSE(response, async (eventName, data) => {
        if (handlers.onEvent) await handlers.onEvent(eventName, data);
      });
      if (handlers.onComplete) handlers.onComplete();
    } catch (error) {
      if (controller.signal.aborted) return;
      if (handlers.onError) handlers.onError(error);
    }
  })();
  return { controller, promise, unsubscribe: () => controller.abort() };
}
