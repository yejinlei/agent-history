import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type {
  ActionEntry, Book, MergePayload, Proposal,
} from "../types";

interface Props {
  books: Book[];
  pipelineStages: string[];
  onChanged: () => void;
  onView: (book: Book) => void;
}

// 提案动辄上万条（含关系时达 8 万），整表渲染会卡死浏览器；
// 只渲染前若干条供抽查，勾选位图仍按全量提交
const PREVIEW_LIMIT = 300;

interface UIState {
  phase: "running" | "proposal";
  stage: number;
  threadId: string;
  proposals: Proposal[];
  accepted: boolean[];
  error?: string;
}

export default function BookMount({ books, pipelineStages, onChanged, onView }: Props) {
  const [ui, setUi] = useState<Record<string, UIState>>({});
  const timers = useRef<Record<string, number>>({});

  useEffect(() => {
    // 组件卸载时清掉所有演示用 setInterval，避免泄漏与卸载后 setState
    const held = timers.current;
    return () => {
      Object.values(held).forEach((t) => window.clearInterval(t));
    };
  }, []);

  // 上传状态
  const [file, setFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadMsg, setUploadMsg] = useState("");
  const [uploadErr, setUploadErr] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);

  // 治理数据
  const [gov, setGov] = useState<MergePayload | null>(null);
  const [acts, setActs] = useState<ActionEntry[]>([]);
  const [confirmDel, setConfirmDel] = useState<string | null>(null);
  const [opErr, setOpErr] = useState<Record<string, string>>({});

  const flag = (bid: string, e: unknown) =>
    setOpErr((m) => ({ ...m, [bid]: (e as any)?.message || String(e) }));

  const loadGov = () => {
    api.merges().then(setGov).catch(() => {});
    api.actions().then((r) => setActs(r.entries)).catch(() => {});
  };
  useEffect(loadGov, [books.length]);

  const doUpload = async () => {
    if (!file || uploading) return;
    setUploading(true);
    setUploadErr("");
    setUploadMsg("");
    try {
      const r = await api.uploadBook(file);
      setUploadMsg(`《${r.book.title}》已入库：${r.chunks} 个切块、${r.proposals} 项提炼提案（${r.extraction_mode}）。可在下方「提炼并挂载」。`);
      setFile(null);
      if (fileRef.current) fileRef.current.value = "";
      onChanged();
    } catch (e) {
      setUploadErr(String(e));
    } finally {
      setUploading(false);
    }
  };

  const startExtract = async (book: Book) => {
    const bid = book.id;
    // 前端演示流水线五阶段进展（真实系统由后端事件/SSE 推进）
    let stage = 0;
    setUi((m) => ({ ...m, [bid]: { phase: "running", stage, threadId: "", proposals: [], accepted: [] } }));
    timers.current[bid] = window.setInterval(async () => {
      stage += 1;
      if (stage < pipelineStages.length) {
        setUi((m) => ({ ...m, [bid]: { ...m[bid], stage } }));
        return;
      }
      window.clearInterval(timers.current[bid]);
      try {
        const r = await api.mountStart(bid);
        setUi((m) => ({
          ...m,
          [bid]: {
            phase: "proposal", stage, threadId: r.thread_id,
            proposals: r.proposals, accepted: r.proposals.map((p) => p.default_accepted),
          },
        }));
      } catch (e) {
        setUi((m) => ({ ...m, [bid]: { ...m[bid], error: String(e) } }));
      }
    }, 480);
  };

  const commit = async (book: Book) => {
    const st = ui[book.id];
    try {
      await api.mountCommit(st.threadId, st.accepted);
      setUi((m) => {
        const c = { ...m };
        delete c[book.id];
        return c;
      });
      onChanged();
      loadGov();
    } catch (e) {
      flag(book.id, e);
    }
  };

  const unmount = async (book: Book) => {
    try {
      await api.unmount(book.id);
      onChanged();
      loadGov();
    } catch (e) {
      flag(book.id, e);
    }
  };

  const remove = async (book: Book) => {
    if (confirmDel !== book.id) {
      setConfirmDel(book.id);
      return;
    }
    try {
      await api.deleteBook(book.id);
      setConfirmDel(null);
      onChanged();
      loadGov();
    } catch (e) {
      flag(book.id, e);
    }
  };

  const undo = async (seq: number) => {
    try {
      await api.undoAction(seq);
      onChanged();
      loadGov();
    } catch (e) {
      setOpErr((m) => ({ ...m, undo: (e as any)?.message || String(e) }));
    }
  };

  const toggle = (bid: string, i: number) =>
    setUi((m) => {
      const s = m[bid];
      const accepted = s.accepted.slice();
      accepted[i] = !accepted[i];
      return { ...m, [bid]: { ...s, accepted } };
    });

  const mountedN = books.filter((b) => b.mounted).length;
  // 最近一条未撤销动作是否可撤销
  const lastUndoable = [...acts].reverse().find((e) => !e.undone && e.action !== "action.undo");

  return (
    <div>
      <div className="sec-t" style={{ marginTop: 0 }}>
        动态本体挂载 · 书库（{mountedN}/{books.length} 已挂载到历史主线）
      </div>
      <div
        style={{
          fontSize: 12, color: "var(--text-2)", background: "var(--surface-2)",
          border: "1px solid var(--border)", borderRadius: 6, padding: "8px 10px",
          marginBottom: 12, lineHeight: 1.75,
        }}
      >
        任何一本书——古籍点校本、学术专著、历史小说、科普读物——经
        <b> 解析 → Agent 抽取 → 时空归一化 → 实体对齐 → 置信度评估 </b>
        后生成「挂载提案」；确认即写入本体，时间轴 / 地图 / 图谱立即联动。
      </div>

      {/* 上传新书 */}
      <div className="up-card">
        <div className="up-title">＋ 上传新书（PDF / TXT / EPUB / MD，≤50MB）</div>
        <div className="up-row">
          <input
            ref={fileRef}
            type="file"
            accept=".pdf,.txt,.epub,.md"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          />
          <button className="btn" disabled={!file || uploading} onClick={doUpload}>
            {uploading ? "解析抽取中…" : "上传并提炼"}
          </button>
        </div>
        {uploadMsg && <div className="up-ok">{uploadMsg}</div>}
        {uploadErr && <div className="dispute">{uploadErr}</div>}
      </div>

      {books.map((b) => {
        const st = ui[b.id];
        const lvlName = ["", "一级史料", "二级·研究", "三级·文学/科普"][b.level];
        const isUpload = b.origin === "upload";
        return (
          <div key={b.id} className={`book ${b.mounted ? "mounted" : ""}`}>
            <div className="book-top">
              <div className="book-sp" style={{ background: b.color }}>{b.title.slice(0, 1)}</div>
              <div className="book-info">
                <h4>
                  {b.title}
                  <span className={`lvl lvl-${b.level}`}>{lvlName}</span>
                  {b.mounted && <span className="mbadge">已挂载</span>}
                  {isUpload && <span className="lvl lvl-up">上传书</span>}
                </h4>
                <div className="au">
                  {b.author} · {b.year} · 覆盖
                </div>
                <div className="book-tags">
                  {b.tags.map((t) => <span key={t} className="t">#{t}</span>)}
                </div>
                <div className="book-desc">{b.description}</div>

                {!st && !b.mounted && (
                  <div className="book-actions">
                    <button className="btn" onClick={() => startExtract(b)}>提炼并挂载</button>
                    {isUpload && (
                      <button className="btn ghost danger" onClick={() => remove(b)}>
                        {confirmDel === b.id ? "再点确认删除" : "删除"}
                      </button>
                    )}
                  </div>
                )}

                {st && st.phase === "running" && (
                  <>
                    <div className="steps">
                      {pipelineStages.map((s, i) => (
                        <div key={i} className={`step ${i < st.stage ? "done" : i === st.stage ? "on" : ""}`}>
                          <span className="n">{i + 1}.</span>{s}
                        </div>
                      ))}
                    </div>
                    <div className="step-note">Agent 工作中：{pipelineStages[Math.min(st.stage, pipelineStages.length - 1)]} …</div>
                  </>
                )}

                {st && st.phase === "proposal" && (
                  <>
                    {st.error && <div className="dispute">{st.error}</div>}
                    {st.proposals.length > PREVIEW_LIMIT && (
                      <div className="pdesc" style={{ marginBottom: 6 }}>
                        共 {st.proposals.length} 项提案，默认已勾选置信度 ≥40% 的项；
                        下方仅预览前 {PREVIEW_LIMIT} 项供抽查，确认挂载仍按全部 {st.proposals.length} 项提交。
                      </div>
                    )}
                    {st.proposals.slice(0, PREVIEW_LIMIT).map((p, i) => (
                      <div key={i} className={`prop ${p.confidence < 0.5 ? "low" : ""}`}>
                        <input type="checkbox" checked={st.accepted[i]} onChange={() => toggle(b.id, i)} />
                        <span
                          className="kind-ic"
                          style={{
                            background:
                              p.kind === "person" ? "var(--gold)"
                              : p.kind === "event" ? "var(--brand)"
                              : p.kind === "relation" ? "#0e7490" : "var(--purple)",
                          }}
                        >
                          {p.kind === "person" ? "人"
                            : p.kind === "event" ? "事"
                            : p.kind === "relation" ? "关" : "挂"}
                        </span>
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <div className="pname">{p.kind === "relation" ? `${p.rel ?? "相关"}：${p.name}` : p.name}</div>
                          <div className="pdesc">{p.summary}</div>
                          <div className="pmeta">
                            {p.kind === "link" ? "对齐到主干实体"
                              : p.kind === "relation" ? "建立实体间关系边"
                              : `新建实体（${p.kind === "person" ? "人物" : "事件"}）`}
                            {p.confidence < 0.5 ? " · 低置信，建议核对上游史料" : ""}
                          </div>
                        </div>
                        <span className="pconf">{(p.confidence * 100).toFixed(0)}%</span>
                      </div>
                    ))}
                    <div className="book-actions" style={{ marginTop: 8 }}>
                      <button className="btn" onClick={() => commit(b)}>
                        确认挂载（{st.accepted.filter(Boolean).length} 项）
                      </button>
                    </div>
                  </>
                )}

                {b.mounted && (
                  <>
                    <div className="mount-stats">
                      ✓ 已写入本体（按接受位图）：
                      {b.extracts.filter((x, i) => x.kind === "person"
                        && (b.accepted?.[i] ?? true)).length} 人物 ·
                      {b.extracts.filter((x, i) => x.kind === "event"
                        && (b.accepted?.[i] ?? true)).length} 事件 ·
                      {b.extracts.filter((x, i) => x.kind === "link"
                        && (b.accepted?.[i] ?? true)).length} 观点挂载点 ·{" "}
                      {b.extracts.filter((x, i) => x.kind === "relation"
                        && (b.accepted?.[i] ?? true)).length} 条实体间关系
                    </div>
                    {opErr[b.id] && <div className="dispute">{opErr[b.id]}</div>}
                    <div className="book-actions">
                      <button className="btn ghost" onClick={() => onView(b)}>前往该书年代查看</button>
                      <button className="btn gray" onClick={() => unmount(b)}>卸载</button>
                    </div>
                  </>
                )}
              </div>
            </div>
          </div>
        );
      })}

      {/* 数据治理：疑似重复 + 操作日志（单步撤销） */}
      <div className="sec-t">数据治理</div>
      {gov && gov.proposals.length > 0 ? (
        gov.proposals.map((p, i) => (
          <div key={i} className="gov-card merge">
            <b>疑似重复（{p.entity_kind === "person" ? "人物" : "地点"}）</b>
            <span className="gov-norm">{p.norm_name}</span>
            <div className="gov-cands">
              {p.candidates.map((c) => (
                <span key={c.id} className="cand">{c.name} <i>{c.id}</i></span>
              ))}
            </div>
          </div>
        ))
      ) : (
        <div className="gov-empty">暂无疑似重复实体（同名人物/地点归一化检测）。</div>
      )}

      <div className="gov-log">
        {acts.slice(-6).reverse().map((e) => (
          <div key={e.seq} className={`gov-line ${e.undone ? "undone" : ""}`}>
            <span className="seq">#{e.seq}</span>
            <span className="gact">{e.action}</span>
            <span className="gparams">
              {String(e.params.book_id ?? e.params.filename ?? e.params.undo_seq ?? "")}
            </span>
            {e.undone && <span className="gtag">已撤销</span>}
            {lastUndoable?.seq === e.seq && (
              <button className="btn mini" onClick={() => undo(e.seq)}>撤销</button>
            )}
          </div>
        ))}
        {acts.length === 0 && <div className="gov-empty">暂无操作日志。上传、挂载、删除均会记录并支持单步撤销。</div>}
      </div>
    </div>
  );
}
