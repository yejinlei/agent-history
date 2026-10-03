export type Audience = "student" | "scholar" | "fan";
export type EntityKind = "event" | "person" | "place";

export interface Capital {
  name: string;
  lon: number;
  lat: number;
}

export interface Dynasty {
  id: string;
  name: string;
  span: [number, number];
  color: string;
  capital: Capital;
  map_tile: string | null;
  emperors: { n: string; s: number; e: number; era?: string; es?: number }[];
  group?: string | null;
  lane?: number;
}

export interface SourceRef {
  level: number;
  work: string;
  text: string;
  translation: string;
  confidence: number;
  from_book: string | null;
}

export interface EventItem {
  id: string;
  name: string;
  year: number;
  month?: number | null;
  day?: number | null;
  /** 纪年置信度：exact | month | year | disputed */
  date_confidence?: string;
  dynasty_id: string;
  /** 坐标派生自 Place；未解析到地点时为 null（后端严禁填 0.0） */
  lon: number | null;
  lat: number | null;
  place: string;
  actors: string[];
  actor_names?: string[];
  significance: string | null;
  dispute: string | null;
  /** 行军/迁徙/航海路径 [lon, lat][] */
  route?: [number, number][] | null;
  route_label?: string | null;
  from_book: string | null;
  sources?: SourceRef[];
}

/** 时间轴落点用轻量事件 */
export interface TlEvent {
  id: string;
  name: string;
  year: number;
  month?: number | null;
  day?: number | null;
  dynasty_id: string;
  from_book?: string | null;
}

export interface PersonItem {
  id: string;
  name: string;
  years: [string, string];
  dynasty_id: string;
  home: string;
  lon?: number | null;
  lat?: number | null;
  from_book: string | null;
  /** 后端无此字段，仅用于统一 EntityDetail 访问 */
  dispute?: string | null;
  sources?: SourceRef[];
}

export interface PlaceItem {
  id: string;
  name: string;
  ancient: string;
  modern: string;
  lon: number;
  lat: number;
  /** 后端无此字段，仅用于统一 EntityDetail 访问 */
  from_book?: string | null;
  dispute?: string | null;
  dynasty_id?: string;
}

export interface Narrative {
  student: string;
  scholar: string;
  fan: string;
}

export type EntityDetail =
  | (EventItem & { kind: "event"; narrative: Narrative; edges?: OntoEdgeBrief[] })
  | (PersonItem & { kind: "person"; narrative: Narrative; edges?: OntoEdgeBrief[] })
  | (PlaceItem & { kind: "place"; narrative: Narrative; edges?: OntoEdgeBrief[] });

export interface Extract {
  kind: "person" | "event" | "link";
  name: string;
  summary: string;
  confidence: number;
  new_id?: string;
  target_id?: string;
  years?: string;
  year?: number;
  lon?: number;
  lat?: number;
  place?: string;
}

export interface Book {
  id: string;
  title: string;
  author: string;
  year: number;
  level: number;
  color: string;
  dynasty_id: string;
  span: [number, number];
  tags: string[];
  description: string;
  mounted: boolean;
  /** 挂载时的接受位图（与 extracts 一一对应）；null=未挂载或旧数据 */
  accepted?: boolean[] | null;
  /** builtin=内置书源（不可删除）；upload=界面上传（可删除） */
  origin?: "builtin" | "upload";
  extracts: Extract[];
}

export interface Proposal {
  kind: "person" | "event" | "link";
  name: string;
  summary: string;
  confidence: number;
  new_id?: string;
  target_id?: string;
  default_accepted: boolean;
}

export interface ChatResp {
  answer: string;
  entity: { kind: EntityKind; id: string; name: string } | null;
  evidence: SourceRef[];
  warnings: string[];
  actions?: UIAction[];
  /** 指定 Agent 时：等待用户确认的写操作（确认卡） */
  operations?: PendingOp[];
}

/* ---------- Agent ---------- */
export interface AgentDef {
  id: string;
  name: string;
  builtin: boolean;
  color: string;
  system_prompt: string;
  tools: string[];
}

export interface AgentTool {
  id: string;
  label: string;
  write: boolean;
  desc: string;
}

/** 确认卡：Agent 生成的写操作，pending 时可确认/取消 */
export interface PendingOp {
  id: string;
  agent_id: string;
  action: string;
  params: Record<string, string>;
  summary: string;
  status: "pending" | "confirmed" | "cancelled";
  ts?: string;
}

/* ---------- 数据治理：融合 / 动作日志 ---------- */
export interface MergeRecord {
  ts: string;
  kind: string;
  kept_id: string;
  merged_name: string;
  merged_id?: string;
}

export interface MergeProposal {
  kind: "merge";
  entity_kind: "person" | "place";
  norm_name: string;
  candidates: { id: string; name: string }[];
}

export interface MergePayload {
  merges: MergeRecord[];
  proposals: MergeProposal[];
}

export interface ActionEntry {
  seq: number;
  ts: string;
  action: string;
  actor: string;
  params: Record<string, unknown>;
  result: Record<string, unknown>;
  undone: boolean;
}

/** Agent 驱动的 UI 动作（由后端规则/LLM 规划，前端执行器消费） */
export type UIAction =
  | { type: "set_year"; year: number }
  | { type: "select_entity"; kind: EntityKind; id: string }
  | { type: "show_route"; id: string }
  | { type: "fly_to"; lon: number; lat: number; zoom?: number }
  | { type: "play"; from: number; to: number; step?: number }
  /* ---- 动态效果库：Agent 按情境选用 ---- */
  | { type: "pulse"; lon: number; lat: number; label?: string }          // 地点波纹强调
  | { type: "highlight_events"; ids: string[] }                           // 多事件金色高亮
  | { type: "region"; lon: number; lat: number; km: number; label?: string } // 影响范围圈
  | { type: "dim"; on: boolean }                                          // 其余事件变暗聚焦
  | { type: "shake" }                                                     // 地图震动强调
  | { type: "clear_effects" };                                            // 清除动态效果

/* ---------- 本体导航 ---------- */
export interface OntoStats {
  dynasty: number;
  event: number;
  person: number;
  place: number;
  book: number;
  institution: number;
  artifact: number;
  concept: number;
  edge: number;
}

export interface OntoEdgeBrief {
  rel: string;
  direction: "in" | "out";
  node: { kind: string; id: string; name: string };
  from_book?: string | null;
}

export interface OntologyIndex {
  stats: OntoStats;
  relations: { rel: string; count: number }[];
  dynasties: { id: string; name: string; span: [number, number]; lane: number }[];
  events: { id: string; name: string; year: number; dynasty_id: string; from_book: string | null }[];
  people: { id: string; name: string; years: [string, string]; dynasty_id: string; from_book: string | null }[];
  places: { id: string; name: string; ancient: string }[];
  books: { id: string; title: string; mounted: boolean }[];
  institutions: { id: string; name: string; kind: string; dynasty_id: string | null }[];
  artifacts: { id: string; name: string; kind: string; year: number | null; museum: string | null }[];
  concepts: { id: string; name: string; kind: string; dynasty_id: string | null }[];
}

/* ---------- 内嵌图库（Kuzu） ---------- */
export type KGLabel =
  | "Dynasty" | "Event" | "Person" | "Place" | "Book"
  | "Institution" | "Artifact" | "Concept";

export interface KGNode {
  id: string;
  label: KGLabel;
  name: string;
  /** 副标题：朝代起止 / 事件年份 */
  sub?: string | null;
}

export interface KGEdge {
  id: string;
  source: string;
  target: string;
  rel: string;
}

export interface KGSubgraph {
  center: string | null;
  nodes: KGNode[];
  edges: KGEdge[];
}

export interface KGSchema {
  tables: { label: KGLabel; count: number }[];
  edge_count: number;
  /** 悬空边（端点未建节点）数量，不参与可视化 */
  dangling: number;
  relations: { rel: string; count: number }[];
}
