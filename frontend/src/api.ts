import type {
  ActionEntry, AgentDef, AgentTool, Book, ChatResp, Dynasty, EntityDetail, EntityKind, EventItem, KGNode, KGSchema, KGSubgraph, MergePayload, OntologyIndex, PendingOp, Proposal, TlEvent,
} from "./types";

async function getJSON<T>(url: string): Promise<T> {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${r.status} ${url}`);
  return r.json() as Promise<T>;
}

async function postJSON<T>(url: string, body?: unknown): Promise<T> {
  const init: RequestInit = { method: "POST" };
  if (body !== undefined) {
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(body);
  }
  const r = await fetch(url, init);
  if (!r.ok) {
    const t = await r.text();
    throw new Error(`${r.status} ${t}`);
  }
  return r.json() as Promise<T>;
}

async function deleteJSON<T>(url: string): Promise<T> {
  const r = await fetch(url, { method: "DELETE" });
  if (!r.ok) {
    const t = await r.text();
    throw new Error(`${r.status} ${t}`);
  }
  return r.json() as Promise<T>;
}

export const api = {
  dynasties: () => getJSON<Dynasty[]>("/api/dynasties"),
  timeline: (year: number, dynastyId?: string) =>
    getJSON<{ year: number; dynasty: Dynasty; parallel: Dynasty[]; events: EventItem[] }>(
      `/api/timeline?year=${year}${dynastyId ? `&dynasty_id=${encodeURIComponent(dynastyId)}` : ""}`,
    ),
  allEvents: () => getJSON<TlEvent[]>("/api/events"),
  maps: () =>
    getJSON<{ catalog: Record<string, unknown>; tile_tpl: string; attribution: string }>(
      "/api/maps",
    ),
  dynastyMaps: (dynastyId: string) =>
    getJSON<{ tile: string; title: string; primary?: boolean }[]>(
      `/api/maps/${dynastyId}`,
    ),
  search: (q: string) =>
    getJSON<
      (EntityDetail | { kind: EntityKind; id: string; name: string })[]
    >(`/api/search?q=${encodeURIComponent(q)}`),
  entity: (kind: EntityKind, id: string) =>
    getJSON<EntityDetail>(`/api/entity/${kind}/${id}`),
  chat: (question: string, agentId?: string) =>
    postJSON<ChatResp>("/api/chat", { question, agent_id: agentId ?? null }),
  books: () =>
    getJSON<{ pipeline_stages: string[]; books: Book[] }>("/api/books"),
  mountStart: (book_id: string) =>
    postJSON<{ thread_id: string; proposals: Proposal[]; pipeline_stages: string[] }>(
      "/api/mounts/start",
      { book_id },
    ),
  mountCommit: (thread_id: string, accepted: boolean[]) =>
    postJSON<{ result: Record<string, number>; stage: string }>(
      "/api/mounts/commit",
      { thread_id, accepted },
    ),
  unmount: (book_id: string) =>
    postJSON<{ ok: boolean }>("/api/mounts/unmount", { book_id }),
  ontology: () => getJSON<OntologyIndex>("/api/ontology"),
  kgSchema: () => getJSON<KGSchema>("/api/kg/schema"),
  kgNodes: (table: string, q: string) =>
    getJSON<KGNode[]>(`/api/kg/nodes?table=${encodeURIComponent(table)}&q=${encodeURIComponent(q)}`),
  kgSubgraph: (id: string) => getJSON<KGSubgraph>(`/api/kg/subgraph/${encodeURIComponent(id)}`),
  kgWhole: () => getJSON<KGSubgraph>("/api/kg/whole"),

  /* ---- 书库上传 / 删除 ---- */
  uploadBook: (file: File, title?: string) => {
    const fd = new FormData();
    fd.append("file", file);
    if (title) fd.append("title", title);
    return fetch("/api/library/upload", { method: "POST", body: fd }).then(async (r) => {
      if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
      return r.json() as Promise<{
        book: Book; chunks: number; proposals: number; extraction_mode: string;
      }>;
    });
  },
  deleteBook: (book_id: string) =>
    deleteJSON<{ ok: boolean; trash: string }>(`/api/library/${book_id}`),

  /* ---- 数据治理 ---- */
  merges: () => getJSON<MergePayload>("/api/merges"),
  actions: () =>
    getJSON<{ defs: unknown[]; entries: ActionEntry[] }>("/api/actions"),
  undoAction: (seq: number) =>
    postJSON<unknown>(`/api/actions/${seq}/undo`, undefined as never),

  /* ---- Agent 管理 ---- */
  agentTools: () => getJSON<AgentTool[]>("/api/agents/tools"),
  agents: () => getJSON<AgentDef[]>("/api/agents"),
  createAgent: (name: string, system_prompt: string, tools: string[]) =>
    postJSON<AgentDef>("/api/agents", { name, system_prompt, tools }),
  updateAgent: (id: string, patch: Partial<Pick<AgentDef, "name" | "system_prompt" | "tools">>) =>
    postJSON<AgentDef>(`/api/agents/${id}`, patch),
  deleteAgent: (id: string) =>
    deleteJSON<{ ok: boolean }>(`/api/agents/${id}`),

  /* ---- 确认卡 ---- */
  operations: (status?: string) =>
    getJSON<PendingOp[]>(`/api/operations${status ? `?status=${status}` : ""}`),
  confirmOp: (id: string) =>
    postJSON<PendingOp>(`/api/operations/${id}/confirm`, undefined as never),
  cancelOp: (id: string) =>
    postJSON<PendingOp>(`/api/operations/${id}/cancel`, undefined as never),
};
