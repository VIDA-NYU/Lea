// The adapter owns requests and snapshots; this module only transports them
// between that backend and the document's connected content script.
import { publishEvent } from "./eventBus.mjs";
import { slugProjectId } from "../shared/leanStub.mjs";

export function registerSourceClient(state, documentId, clientId) {
  state.sourceClients ||= new Map();
  const clients = state.sourceClients.get(documentId) || new Map();
  const sockets = clients.get(clientId) || new Set();
  const socket = Symbol();
  sockets.add(socket);
  clients.set(clientId, sockets);
  state.sourceClients.set(documentId, clients);
  return () => {
    sockets.delete(socket);
    if (!sockets.size) clients.delete(clientId);
    if (!clients.size) state.sourceClients.delete(documentId);
  };
}

async function adapter(state, baseUrl, route, body) {
  const headers = { "Content-Type": "application/json" };
  if (state.env?.LEA_API_KEY) headers.Authorization = `Bearer ${state.env.LEA_API_KEY}`;
  const response = await (state.fetchImpl || fetch)(`${baseUrl}${route}`, {
    method: body ? "POST" : "GET", headers, ...(body ? { body: JSON.stringify(body) } : {}),
    signal: AbortSignal.timeout(5000)
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.detail || "Could not refresh Overleaf source.");
  return result;
}

export async function pendingSourceRequests(state, baseUrl, documentId = null) {
  const result = await adapter(state, baseUrl, "/api/overleaf/source-requests");
  state.pendingSourceRequests = new Map((result.requests || []).map((request) => [request.request_id, request]));
  return (result.requests || []).filter((request) => !documentId || request.overleafProjectId === documentId);
}

export async function pollSourceRequests(state, baseUrl) {
  for (const request of await pendingSourceRequests(state, baseUrl)) {
    const clients = state.sourceClients?.get(request.overleafProjectId);
    if (clients?.size > 1) {
      await adapter(state, baseUrl, `/api/overleaf/source-requests/${encodeURIComponent(request.request_id)}`, {
        document_id: request.overleafProjectId,
        error: "This document is open in multiple Overleaf tabs. Close the extra tabs, then send again."
      });
    } else if (clients?.size === 1) {
      publishEvent(state, "source-refresh-requested", request);
    }
  }
}

export async function respondSourceRequest(state, baseUrl, payload) {
  const request = state.pendingSourceRequests?.get(payload.request_id)
    || (await pendingSourceRequests(state, baseUrl)).find((row) => row.request_id === payload.request_id);
  if (!request || request.overleafProjectId !== payload.overleafProjectId) {
    throw new Error("Source request expired or belongs to a different document.");
  }
  const clients = state.sourceClients?.get(request.overleafProjectId);
  if (clients?.size !== 1 || !clients.has(payload.clientId)) {
    throw new Error("Reconnect one Overleaf tab for this document, then send again.");
  }
  const source = payload.sourceContext?.sourceBundle;
  if (source && (source.targetKey !== request.targetLabel || source.targetKind !== request.targetKind)) {
    throw new Error("Source response belongs to a different theorem or definition.");
  }
  if (source?.mirror?.verified) {
    // The server also checks the active file; the full revision prevents stale
    // dependency evidence after another mirror update during acquisition.
    const snapshot = state.texMirrorSnapshots?.[slugProjectId(request.overleafProjectId)];
    const hashes = payload.sourceContext?.mirroredFiles;
    if (source.mirror.revision !== snapshot?.mirrorRevision || !hashes
        || Object.keys(hashes).length !== Object.keys(snapshot.files).length
        || Object.entries(hashes).some(([path, hash]) => snapshot.files[path] !== hash)) {
      throw new Error("The source mirror changed during refresh. Send again.");
    }
  }
  return adapter(state, baseUrl, `/api/overleaf/source-requests/${encodeURIComponent(request.request_id)}`, {
    document_id: request.overleafProjectId, source_bundle: source || null,
    mirrored_files: payload.sourceContext?.mirroredFiles || null,
    error: payload.error || (source ? null : "The current Overleaf target could not be read.")
  });
}
