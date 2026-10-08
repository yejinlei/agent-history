import { useEffect, useState } from "react";
import type { AgentDef, Book, Dynasty, EntityDetail, EntityKind, OntologyIndex, PendingOp, UIAction } from "../types";
import { api } from "../api";
import OntologyNav from "./OntologyNav";
import BookMount from "./BookMount";
import AgentManager from "./AgentManager";
import { ArchView, DetailView, SourceView } from "./SidePanel";
import ClaimsView from "./ClaimsView";
import SplitBar from "./SplitBar";

export type DockId =
  | "onto" | "detail" | "source" | "books" | "agents" | "arch" | "claims" | "ask";

interface Props {
  /** 当前显示的面板（hover 临时或 pinned） */
  active: DockId | null;
  /** 吸附固定的面板 */
  pinned: DockId | null;
  panelW: number;
  onPanelDrag: (delta: number) => void;
  onPanelDragStart: () => void;
  onHover: (id: DockId | null) => void;
  /** 鼠标离开后恢复到吸附面板 */
  onRestore: (id: DockId | null) => void;
  onTogglePin: (id: DockId) => void;
  onClose: () => void;
  onto: OntologyIndex | null;
  selectedId: string | null;
  entity: EntityDetail | null;
  dynasties: Dynasty[];
  books: Book[];
  pipelineStages: string[];
  selectById: (k: EntityKind, id: string) => void;
  refreshBooks: () => void;
  onViewBook: (b: Book) => void;
  onPickDynasty: (id: string, yr: number) => void;
  onPickBook: (id: string) => void;
  onActions?: (actions?: UIAction[]) => void;
}

const ITEMS: { id: DockId; label: string; icon: string }[] = [
  { id: "onto", label: "本体导航", icon: "🏛" },
  { id: "detail", label: "实体详情", icon: "🏯" },
  { id: "source", label: "史料溯源", icon: "📜" },
  { id: "books", label: "挂载书库", icon: "📚" },
  { id: "agents", label: "Agent 管理", icon: "🤖" },
  { id: "claims", label: "多视角", icon: "⚖" },
  { id: "arch", label: "技术架构", icon: "⚙" },
];

/**
 * 右侧栏：标签条常驻最右；悬停标签临时预览面板，点击标签吸附固定（再点取消）。
 * 面板在标签条左侧，宽度可拖拽。
 */
export default function RightDock(p: Props) {
  const all = [...ITEMS, { id: "ask" as DockId, label: "智能问答", icon: "💬" }];
  const title = all.find((i) => i.id === p.active)?.label;

  return (
    <div className="dock-wrap" onMouseLeave={() => p.onRestore(p.pinned)}>
      {/* 吸附面板（含左缘拖拽条） */}
      {p.active && (
        <>
          <SplitBar
            axis="x"
            title="拖动调整面板宽度"
            onStart={p.onPanelDragStart}
            onDrag={p.onPanelDrag}
          />
          <aside className="dock-panel2" style={{ width: p.panelW }}>
            <div className="dock-head">
              <span>{title}</span>
              <span className="dock-hbtns">
                <button
                  className={`dock-pin ${p.pinned === p.active ? "on" : ""}`}
                  title={p.pinned === p.active ? "取消吸附" : "吸附到右边"}
                  onClick={() => p.onTogglePin(p.active!)}
                >
                  {p.pinned === p.active ? "📌 已吸附" : "📍 吸附"}
                </button>
                <button className="dock-x" title="关闭" onClick={p.onClose}>×</button>
              </span>
            </div>
            <div className={`dock-body ${p.active === "onto" ? "flush" : ""}`}>
              {p.active === "onto" && (
                <OntologyNav
                  onto={p.onto}
                  selectedId={p.selectedId}
                  onPick={p.selectById}
                  onPickDynasty={p.onPickDynasty}
                  onPickBook={p.onPickBook}
                />
              )}
              {p.active === "detail" && <DetailView entity={p.entity} selectById={p.selectById} />}
              {p.active === "source" && <SourceView entity={p.entity} />}
              {p.active === "books" && (
                <BookMount books={p.books} pipelineStages={p.pipelineStages} onChanged={p.refreshBooks} onView={p.onViewBook} />
              )}
              {p.active === "arch" && <ArchView />}
              {p.active === "agents" && <AgentManager />}
              {p.active === "claims" && <ClaimsView selectById={p.selectById} />}
              {p.active === "ask" && (
                <AskPanel
                  selectById={p.selectById}
                  onActions={p.onActions}
                  onChanged={p.refreshBooks}
                />
              )}
            </div>
          </aside>
        </>
      )}

      {/* 常驻标签条 */}
      <div className="dock-labels">
        {ITEMS.map((it) => (
          <div
            key={it.id}
            className={`dock-label ${p.active === it.id ? "on" : ""} ${p.pinned === it.id ? "pinned" : ""}`}
            onMouseEnter={() => p.onHover(it.id)}
            onClick={() => p.onTogglePin(it.id)}
            title={p.pinned === it.id ? "点击取消吸附" : "点击吸附到右边"}
          >
            <i>{it.icon}</i>
            {it.label}
          </div>
        ))}
        <div
          className={`dock-label ask ${p.active === "ask" ? "on" : ""} ${p.pinned === "ask" ? "pinned" : ""}`}
          onMouseEnter={() => p.onHover("ask")}
          onClick={() => p.onTogglePin("ask")}
        >
          <i>💬</i>
          智能问答
        </div>
      </div>
    </div>
  );
}

/* ---------------- 智能问答 ---------------- */
function AskPanel({ selectById, onActions, onChanged }: {
  selectById: (k: EntityKind, id: string) => void;
  onActions?: (actions?: UIAction[]) => void;
  onChanged?: () => void;
}) {
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const [agents, setAgents] = useState<AgentDef[]>([]);
  const [agentId, setAgentId] = useState("");
  const [resp, setResp] = useState<{
    answer: string;
    entity: { kind: EntityKind; id: string; name: string } | null;
    actions?: UIAction[];
    operations?: PendingOp[];
  } | null>(null);
  const [ops, setOps] = useState<PendingOp[]>([]);
  const [books, setBooks] = useState<Book[]>([]);
  const [err, setErr] = useState("");

  useEffect(() => {
    api.agents().then((a) => setAgents(a)).catch(() => {});
    api.operations("pending").then(setOps).catch(() => {});
    api.books().then((r) => setBooks(r.books)).catch(() => {});
  }, []);

  const ask = async () => {
    if (!q.trim() || busy) return;
    setBusy(true);
    setErr("");
    try {
      const r = await api.chat(q, agentId || undefined);
      setResp(r);
      onActions?.(r.actions);
      if (r.operations?.length) setOps((m) => [...r.operations!, ...m]);
    } catch (e: any) {
      setErr(e?.message || "请求失败");
    } finally {
      setBusy(false);
    }
  };

  const decide = async (op: PendingOp, ok: boolean) => {
    setErr("");
    try {
      const r = ok ? await api.confirmOp(op.id) : await api.cancelOp(op.id);
      setOps((m) => m.map((o) => (o.id === op.id ? r : o)));
      onChanged?.();
    } catch (e: any) {
      // 授权/目标重校验失败等：把原因直接展示在卡片上方
      setErr(e?.message || "操作失败");
    }
  };

  const targetBook = (op: PendingOp) =>
    books.find((b) => b.id === op.params?.book_id);

  return (
    <div>
      <div className="ask-agent">
        <select value={agentId} onChange={(e) => setAgentId(e.target.value)}>
          <option value="">自由问答（默认）</option>
          {agents.map((a) => (
            <option key={a.id} value={a.id}>{a.name}</option>
          ))}
        </select>
      </div>
      <div className="ask-row">
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && ask()}
          placeholder={agentId
            ? `向「${agents.find((a) => a.id === agentId)?.name}」提问…`
            : "提问，如：秦始皇是谁？安史之乱怎么打的？"}
        />
        <button className="btn" disabled={busy} onClick={ask}>{busy ? "思考中" : "提问"}</button>
      </div>
      {err && <div className="ask-err">⚠️ {err}</div>}

      {/* 确认卡 */}
      {ops.map((op) => (
        <div key={op.id} className={`op-card ${op.status}`}>
          <div className="op-head">
            <span className="op-act">{op.action}</span>
            {op.status === "pending"
              ? <span className="op-st pend">待确认</span>
              : <span className={`op-st ${op.status}`}>
                  {op.status === "confirmed" ? "已执行" : "已取消"}
                </span>}
          </div>
          {/* 真实目标：从 params.book_id 解析书名，不能只信模型生成的 summary */}
          {targetBook(op) && (
            <div className="op-target">📖 {targetBook(op)!.title}</div>
          )}
          {!targetBook(op) && op.params?.book_id && op.status === "pending" && (
            <div className="op-target bad">⚠️ 目标书籍已不存在</div>
          )}
          <div className="op-summary">{op.summary}</div>
          {op.status === "pending" && (
            <div className="op-btns">
              <button className="btn mini" onClick={() => decide(op, true)}>确认执行</button>
              <button className="btn mini gray" onClick={() => decide(op, false)}>取消</button>
            </div>
          )}
        </div>
      ))}

      {resp && (
        <>
          <div className="chat-answer">{resp.answer}</div>
          {resp.entity && (
            <div style={{ marginTop: 8 }}>
              <span className="chips">
                <span className="c" onClick={() => selectById(resp.entity!.kind, resp.entity!.id)}>
                  → 查看「{resp.entity.name}」实体卡
                </span>
              </span>
            </div>
          )}
          {resp.actions && resp.actions.length > 0 && (
            <div style={{ marginTop: 6, fontSize: 11.5, color: "var(--text-3)" }}>
              ⚡ Agent 已执行界面操作：
              {resp.actions.map((a, i) => (
                <span key={i} className="action-tag">
                  {a.type === "set_year" && `跳转 ${a.year < 0 ? "前" + -a.year : a.year} 年`}
                  {a.type === "select_entity" && `选中 ${a.kind === "event" ? "事件" : a.kind === "person" ? "人物" : "地点"}`}
                  {a.type === "show_route" && "绘制行军/行进路线"}
                  {a.type === "fly_to" && "移动地图视野"}
                  {a.type === "play" && "时间轴播放"}
                  {a.type === "pulse" && "地点波纹强调"}
                  {a.type === "highlight_events" && `高亮 ${a.ids.length} 个事件`}
                  {a.type === "region" && "圈出影响范围"}
                  {a.type === "dim" && (a.on ? "聚焦模式" : "取消聚焦")}
                  {a.type === "shake" && "地图震动强调"}
                  {a.type === "clear_effects" && "清除效果"}
                </span>
              ))}
            </div>
          )}
        </>
      )}
      {!resp && ops.length === 0 && (
        <div className="empty"><div className="big">💬</div>选择 Agent 后提问；写操作将以确认卡呈现</div>
      )}
    </div>
  );
}
