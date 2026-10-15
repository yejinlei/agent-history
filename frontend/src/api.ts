import type {
  ActionEntry, AgentDef, AgentTool, Book, ChatResp, Claim, ClaimConflict, Dynasty, EntityDetail, EntityKind, EventItem, KGNode, KGSchema, KGSubgraph, MergePayload, OntologyIndex, PendingOp, Proposal, TlEvent,
} from "./types";

/** 离线演示开关：VITE_OFFLINE=1 时，KG 相关接口读 public/demo/*.json 静态文件 */
const OFFLINE = (import.meta.env.VITE_OFFLINE ?? "") === "1";
// 相对路径，兼容 GHP 挂在 /repo-name/ 下的场景
const DEMO_BASE = "demo";

async function getJSON<T>(url: string): Promise<T> {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${r.status} ${url}`);
  return r.json() as Promise<T>;
}

/** URL 路径段编码：Python `str.isalnum()` 语义——Unicode 字母/数字保留，其他换成 _ */
function _safe(id: string): string {
  return id.replace(/[^\p{L}\p{N}]/gu, "_");
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
  claims: (about?: string, stance?: string, limit = 300) =>
    getJSON<{ items: Claim[]; total: number }>(
      `/api/claims?limit=${limit}` +
        (about ? `&about=${encodeURIComponent(about)}` : "") +
        (stance ? `&stance=${encodeURIComponent(stance)}` : ""),
    ),
  claimConflicts: (limit = 50) =>
    getJSON<{ items: ClaimConflict[]; total: number }>(
      `/api/claims/conflicts?limit=${limit}`,
    ),
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
  kgSchema: async () =>
    OFFLINE
      ? getJSON<KGSchema>(`${DEMO_BASE}/schema.json`)
      : getJSON<KGSchema>("/api/kg/schema"),
  kgNodes: async (table: string, q: string) => {
    if (OFFLINE) {
      const idx = await getJSON<Record<string, KGNode[]>>(`${DEMO_BASE}/search.json`);
      const pool = idx[table] ?? [];
      const needle = q.trim().toLowerCase();
      if (!needle) return pool;
      return pool.filter(
        (n) => n.name.toLowerCase().includes(needle));
    }
    return getJSON<KGNode[]>(
      `/api/kg/nodes?table=${encodeURIComponent(table)}&q=${encodeURIComponent(q)}`);
  },
  kgSubgraph: async (id: string, depth = 1) => {
    if (OFFLINE) {
      // 离线只支持 1-hop；depth>=2 复用 1-hop 结果（前端会做合并展示）
      return getJSON<KGSubgraph>(`${DEMO_BASE}/subgraphs/${_safe(id)}.json`);
    }
    return getJSON<KGSubgraph>(
      `/api/kg/subgraph/${encodeURIComponent(id)}${depth > 1 ? `?depth=${depth}` : ""}`);
  },
  kgHub: async (limit = 40) =>
    OFFLINE
      ? getJSON<KGSubgraph>(`${DEMO_BASE}/hub.json`)
      : getJSON<KGSubgraph>(`/api/kg/hub?limit=${limit}`),
  kgWhole: async (limitNodes = 0, limitEdges = 0) => {
    if (OFFLINE) {
      // 离线的"全图"就是 hub 图
      return getJSON<KGSubgraph>(`${DEMO_BASE}/hub.json`);
    }
    return getJSON<KGSubgraph>(
      `/api/kg/whole${limitNodes || limitEdges
        ? `?limit_nodes=${limitNodes}&limit_edges=${limitEdges}` : ""}`);
  },

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
