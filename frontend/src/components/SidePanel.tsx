import type { EntityDetail, EntityKind, SourceRef } from "../types";
import { fmtYear } from "../utils";

/* 本文件为右侧面板视图集合（由 RightDock 以吸附抽屉形式承载） */

/* ---------------- 详情 ---------------- */
export function DetailView({ entity, selectById }: {
  entity: EntityDetail | null; selectById: (k: EntityKind, id: string) => void;
}) {
  if (!entity) return <div className="empty"><div className="big">🏯</div>点击地图标记 / 事件卡，或搜索任意人、事、地</div>;

  return (
    <div className="detail">
      <h2>{entity.name}</h2>
      <div className="sub">
        <span className={`badge b-${entity.kind === "event" ? "ev" : entity.kind === "person" ? "bc" : "pl"}`}>
          {{ event: "事件", person: "人物", place: "地点" }[entity.kind]}
        </span>
        {entity.kind === "event" && `${fmtYear(entity.year)} · ${entity.place}`}
        {entity.kind === "event" && entity.date_confidence === "disputed" && (
          <span className="dc-badge" title="该年份在学界存在多种说法">纪年存疑</span>
        )}
        {entity.kind === "event" && entity.route && (
          <span className="route-badge" title={entity.route_label ?? ""}>🧭 行军/行进路线已绘制</span>
        )}
        {entity.kind === "person" && entity.years.join("—")}
        {entity.kind === "place" && entity.modern}
        {entity.from_book && <span className="mbadge">挂载</span>}
      </div>

      {entity.kind === "person" && <dl className="kv"><dt>籍贯/地</dt><dd>{entity.home}</dd></dl>}
      {entity.kind === "place" && (
        <dl className="kv"><dt>古称</dt><dd>{entity.ancient}</dd><dt>今属</dt><dd>{entity.modern}</dd></dl>
      )}

      <div className="narr">{entity.narrative.scholar}</div>

      {entity.kind === "event" && entity.actor_names && entity.actor_names.length > 0 && (
        <>
          <div className="sec-t">相关人物</div>
          <div className="chips">
            {entity.actors.map((id, i) => (
              <span key={id} className="c" onClick={() => selectById("person", id)}>
                {entity.actor_names![i].split(" ")[0]}
              </span>
            ))}
          </div>
        </>
      )}
      {entity.kind === "event" && entity.significance && (
        <div className="signif"><b>历史意义：</b>{entity.significance}</div>
      )}
      {entity.dispute && <div className="dispute"><b>⚖ 学术分歧：</b>{entity.dispute}</div>}

      {entity.edges && entity.edges.length > 0 && (
        <>
          <div className="sec-t">知识图谱邻接（{entity.edges.length}）</div>
          <div className="edge-list">
            {entity.edges.map((e, i) => {
              const k = e.node.kind;
              const clickable = k === "event" || k === "person" || k === "place";
              return (
                <div
                  key={i}
                  className={`edge-item ${clickable ? "clk" : ""}`}
                  onClick={() => clickable && selectById(k as EntityKind, e.node.id)}
                >
                  <span className="edge-dir">{e.direction === "out" ? "→" : "←"}</span>
                  <span className="edge-rel">{e.rel}</span>
                  <span className="edge-node">
                    {e.node.name}
                    <span className="edge-kind">
                      {{ event: "事件", person: "人物", place: "地点", book: "书", dynasty: "政权" }[k] ?? k}
                    </span>
                  </span>
                  {e.from_book && <span className="mbadge">挂载</span>}
                </div>
              );
            })}
          </div>
        </>
      )}
    </div>
  );
}

/* ---------------- 史料 ---------------- */
export function SourceView({ entity }: { entity: EntityDetail | null }) {
  if (!entity) return <div className="empty"><div className="big">📜</div>先选择一个实体</div>;
  const list: SourceRef[] = (entity as { sources?: SourceRef[] }).sources || [];
  if (!list.length) return <div className="empty"><div className="big">📜</div>该实体暂无数字化史料</div>;
  return (
    <div>
      <div className="sec-t" style={{ marginTop: 0 }}>
        RAG 返回：{list.length} 条史料证据
        <span style={{ fontWeight: 400, color: "var(--text-3)" }}>（含出处与置信度）</span>
      </div>
      {list.map((s, i) => (
        <div key={i} className="src">
          <div className="src-top">
            <span className={`lvl lvl-${s.level}`}>{{ 1: "一级·原始史料", 2: "二级·现代史学", 3: "三级·大众/文学" }[s.level]}</span>
            <span className="src-work">{s.work}</span>
            {s.from_book && <span className="mbadge">挂载书源</span>}
          </div>
          <div className="src-text">{s.text}</div>
          <div className="conf">
            <span>{(s.confidence * 100).toFixed(0)}%</span>
            <span className="conf-bar"><i style={{ width: `${s.confidence * 100}%`, background: s.confidence < .5 ? "var(--gold)" : undefined }} /></span>
          </div>
        </div>
      ))}
    </div>
  );
}
/* ---------------- 架构 ---------------- */
export function ArchView() {
  return (
    <div>
      <div className="arch-layer">
        <h4>多 Agent 协作图（LangGraph） <span style={{ background: "var(--brand)" }}>7 个 Agent</span></h4>
        <p>
          对话路由 → 实体解析（KG 对齐）→ RAG 召回 → 考据（置信度校验）→ 叙事（senseNova 生成）
          → <b>校验（独立质检 Agent：引用完整性 / 三级史料标注 / 空草稿拦截）</b> → 审阅（无证据拦截，合并质检结果）。
          挂载书库另有独立流水线 Agent 图：解析 → 抽取 → 时空归一化 → 实体对齐 → 置信度评估，interrupt 暂停等待人工审核。
        </p>
      </div>
      <div className="arch-arrow">▼</div>
      <div className="arch-layer">
        <h4>① 本体论 Ontology 构建 <span style={{ background: "var(--purple)" }}>Schema 层</span></h4>
        <p>
          Pydantic 定义 Object Types：政权（含并立车道 lane/时期组 group）/ 事件 / 人物 / 地点 / 史料 / 书源；
          Link Types：参与 / 发生于 / 师承 / 书载观点。构建过程：schema/ontology.py 建模 → seed.py 种子知识
          → 首次启动冷引导写入本体库。
        </p>
      </div>
      <div className="arch-arrow">▼</div>
      <div className="arch-layer">
        <h4>② 知识图谱 KG 构建 <span style={{ background: "var(--gold)" }}>事实层</span></h4>
        <p>
          实体（政权/人物/事件/地点/书源）与关系（参与 / 发生于 / 辅佐 / 师承…）存储于 Kuzu 嵌入式图库，
          统一以 Cypher 查询；挂载新书时自动追加「书 → 节点」类型化边。地图下方「关系图谱」为子图探索视图，支持单击节点扩展邻域。
        </p>
      </div>
      <div className="arch-arrow">▼</div>
      <div className="arch-layer">
        <h4>③ RAG 史料检索构建 <span style={{ background: "var(--blue)" }}>证据层</span></h4>
        <p>
          史料分级：一级·原始史料 / 二级·现代史学 / 三级·大众文学，每条带原文 + 译文 + 置信度；
          按事件 id 索引于 sources.json。生产环境：《史记》《资治通鉴》全文切块向量化（pgvector）+ BM25 混合检索。
        </p>
      </div>
      <div className="arch-arrow">▼</div>
      <div className="arch-layer">
        <h4>④ 数据持久化（历史如何保存） <span style={{ background: "var(--green)" }}>本地图库</span></h4>
        <p>
          本体主数据保存在 <b>Kuzu 嵌入式图库</b> backend/app/data/kg/kuzu_db（节点表 + KGREL 关系表，单文件）；
          冷启动从 seed 引导写入，挂载 / 卸载书源即时生效，重启不丢——新增书籍即增量叠加到历史主线。
          少量尚未对齐实体的关系边记录于 dangling_edges.json 待补解析。
        </p>
      </div>
      <div className="arch-arrow">▼</div>
      <div className="arch-layer">
        <h4>⑤ 动态本体挂载 <span style={{ background: "var(--purple)" }}>写操作闭环</span></h4>
        <p>LangGraph human-in-the-loop：新书 prepare 后 interrupt 暂停，人工勾选 → resume → commit 写入本体并持久化；卸载可追溯移除（from_book 血缘）。</p>
      </div>
      <div className="arch-layer" style={{ background: "#f0fdf4" }}>
        <h4 style={{ color: "var(--green)" }}>运行栈</h4>
        <p>前端 React+Vite+Leaflet（OSGeo 中国瓦片）；后端 FastAPI+LangGraph+senseNova；知识图谱 Kuzu（生产可扩展为独立图库服务），RAG 生产环境 Postgres+pgvector。</p>
      </div>
    </div>
  );
}
