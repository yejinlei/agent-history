import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ForceGraph2D, { type ForceGraphMethods } from "react-force-graph-2d";
import { api } from "../api";
import type { EntityKind, KGEdge, KGLabel, KGNode } from "../types";

/* 节点配色（与全站紫/蓝/金/绿一致） */
const COLORS: Record<KGLabel, string> = {
  Dynasty: "#8b5cf6",
  Event: "#2f6fed",
  Person: "#d4a017",
  Place: "#16a34a",
  Book: "#e0563a",
  Institution: "#0891b2",
  Artifact: "#b45309",
  Concept: "#db2777",
};
const LABEL_CN: Record<KGLabel, string> = {
  Dynasty: "政权", Event: "事件", Person: "人物", Place: "地点", Book: "书源",
  Institution: "机构", Artifact: "文物", Concept: "观念",
};
const LABELS: KGLabel[] = [
  "Dynasty", "Event", "Person", "Place", "Institution", "Artifact", "Concept", "Book",
];

interface GNode extends KGNode {
  x?: number; y?: number; vx?: number; vy?: number; fx?: number; fy?: number;
}
interface GLink {
  id: string;
  source: string | GNode;
  target: string | GNode;
  rel: string;
}

const idOf = (x: string | GNode) => (typeof x === "string" ? x : x.id);

interface Props {
  /** 初始中心节点（当前选中实体）；优先级最高 */
  centerId?: string | null;
  /** 兜底中心：未选中实体时，展示当前朝代的子图（顺着知识图谱联动） */
  dynastyId?: string | null;
  /** 内嵌卡片模式：画布高度填满卡片剩余空间（无固定 380px） */
  embedded?: boolean;
  onSelect: (k: EntityKind, id: string) => void;
  onPickDynasty: (id: string, yr: number) => void;
}

/** 内嵌 Kuzu 图库浏览器：力导向布局，单击展开邻域，右键打开实体卡 */
export default function GraphExplorer({ centerId, dynastyId, embedded, onSelect, onPickDynasty }: Props) {
  const [nodes, setNodes] = useState<GNode[]>([]);
  const [links, setLinks] = useState<GLink[]>([]);
  const [hidden, setHidden] = useState<Set<KGLabel>>(new Set());
  const [busy, setBusy] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [full, setFull] = useState(false);
  const [loaded, setLoaded] = useState(false);

  const [searchType, setSearchType] = useState<KGLabel>("Person");
  const [searchQ, setSearchQ] = useState("");
  const [results, setResults] = useState<KGNode[]>([]);

  const hostRef = useRef<HTMLDivElement>(null);
  const fgRef = useRef<ForceGraphMethods<any, any>>();
  const [size, setSize] = useState({ w: 360, h: 380 });

  /* ESC 退出全屏 */
  useEffect(() => {
    if (!full) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setFull(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [full]);

  /* ---------- 合并子图（保留旧节点位置，避免全图重排） ---------- */
  const mergeGraph = useCallback(
    (g: { nodes: KGNode[]; edges: KGEdge[] }, reset = false) => {
      setNodes((prev) => {
        const map = new Map<string, GNode>();
        (reset ? [] : prev).forEach((n) => map.set(n.id, { ...n }));
        g.nodes.forEach((n) => {
          const ex = map.get(n.id);
          if (ex) {
            ex.name = n.name; ex.sub = n.sub; ex.label = n.label;
          } else {
            map.set(n.id, { ...n });
          }
        });
        return [...map.values()];
      });
      setLinks((prev) => {
        const map = new Map<string, GLink>();
        (reset ? [] : prev).forEach((l) =>
          map.set(l.id, { ...l, source: idOf(l.source), target: idOf(l.target) }));
        g.edges.forEach((e) => {
          if (!map.has(e.id)) {
            map.set(e.id, { id: e.id, source: e.source, target: e.target, rel: e.rel });
          }
        });
        return [...map.values()];
      });
    }, []);

  /* ---------- 初始加载 ---------- */
  useEffect(() => {
    setLoaded(false);
    const fitSoon = () =>
      setTimeout(() => fgRef.current?.zoomToFit(500, 44), 250);
    // 优先级：选中实体 > 当前朝代 > 全图
    const focusId = centerId || dynastyId || null;
    if (focusId) {
      setBusy(focusId);
      api.kgSubgraph(focusId)
        .then((g) => { mergeGraph(g, true); setSelected(focusId); fitSoon(); })
        .finally(() => { setBusy(null); setLoaded(true); });
    } else {
      api.kgWhole()
        .then((g) => { mergeGraph(g, true); fitSoon(); })
        .finally(() => setLoaded(true));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [centerId, dynastyId]);

  /* ---------- 画布尺寸（面板/全屏共用 ResizeObserver） ---------- */
  useEffect(() => {
    const el = hostRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => {
      setSize({ w: el.clientWidth, h: el.clientHeight });
    });
    ro.observe(el);
    setSize({ w: el.clientWidth, h: el.clientHeight });
    return () => ro.disconnect();
  }, [full]);

  /* ---------- 搜索（300ms 防抖） ---------- */
  useEffect(() => {
    if (!searchQ.trim()) { setResults([]); return; }
    const t = setTimeout(() => {
      api.kgNodes(searchType, searchQ).then(setResults).catch(() => setResults([]));
    }, 300);
    return () => clearTimeout(t);
  }, [searchQ, searchType]);

  /* ---------- 交互 ---------- */
  const expand = useCallback(async (n: GNode) => {
    if (busy) return;
    setSelected(n.id);
    setBusy(n.id);
    try {
      mergeGraph(await api.kgSubgraph(n.id));
      fgRef.current?.d3ReheatSimulation();
    } finally {
      setBusy(null);
    }
  }, [busy, mergeGraph]);

  const jumpTo = useCallback((n: KGNode) => {
    setSelected(n.id);
    setBusy(n.id);
    api.kgSubgraph(n.id)
      .then((g) => {
        mergeGraph(g, true);
        setTimeout(() => fgRef.current?.zoomToFit(500, 44), 250);
      })
      .finally(() => setBusy(null));
    setResults([]);
    setSearchQ("");
  }, [mergeGraph]);

  const openEntity = useCallback((n: GNode) => {
    if (n.label === "Event" || n.label === "Person" || n.label === "Place") {
      onSelect(n.label.toLowerCase() as EntityKind, n.id);
    } else if (n.label === "Dynasty") {
      const yr = Number((n.sub || "0").split("–")[0]);
      onPickDynasty(n.id, yr);
    }
  }, [onSelect, onPickDynasty]);

  const loadWhole = () => {
    setBusy("whole");
    api.kgWhole()
      .then((g) => { mergeGraph(g, true); setSelected(null); })
      .finally(() => setBusy(null));
  };

  /* ---------- 类型筛选后的可见数据 ---------- */
  const graphData = useMemo(() => {
    const ns = nodes.filter((n) => !hidden.has(n.label));
    const ids = new Set(ns.map((n) => n.id));
    const ls = links.filter(
      (l) => ids.has(idOf(l.source)) && ids.has(idOf(l.target)));
    return { nodes: ns, links: ls };
  }, [nodes, links, hidden]);

  const selectedNode = nodes.find((n) => n.id === selected) || null;

  /* ---------- 节点绘制 ---------- */
  const drawNode = (n: GNode, c: CanvasRenderingContext2D) => {
    const isSel = n.id === selected;
    const r = isSel ? 9 : n.id === busy ? 8 : 6;
    if (n.id === busy) {
      c.beginPath();
      c.arc(n.x ?? 0, n.y ?? 0, r + 4, 0, 2 * Math.PI);
      c.strokeStyle = "rgba(47,111,237,.45)";
      c.lineWidth = 2;
      c.stroke();
    }
    c.beginPath();
    c.arc(n.x ?? 0, n.y ?? 0, r, 0, 2 * Math.PI);
    c.fillStyle = COLORS[n.label];
    c.fill();
    c.strokeStyle = "#fff";
    c.lineWidth = 1.5;
    c.stroke();

    c.font = `${isSel ? "600 " : ""}11px "PingFang SC","Microsoft YaHei",sans-serif`;
    c.textAlign = "center";
    c.textBaseline = "top";
    c.fillStyle = isSel ? "#0f172a" : "#475569";
    const label = n.name.length > 7 ? n.name.slice(0, 7) + "…" : n.name;
    c.fillText(label, n.x ?? 0, (n.y ?? 0) + r + 2);
  };

  return (
    <div className="kg-ex">
      {/* 工具栏 */}
      <div className="kg-toolbar">
        <div className="kg-search">
          <select value={searchType} onChange={(e) => setSearchType(e.target.value as KGLabel)}>
            {LABELS.map((l) => <option key={l} value={l}>{LABEL_CN[l]}</option>)}
          </select>
          <input
            value={searchQ}
            onChange={(e) => setSearchQ(e.target.value)}
            placeholder="搜索节点并定位…"
          />
        </div>
        {results.length > 0 && (
          <div className="kg-sr">
            {results.map((r) => (
              <div key={r.id} className="kg-sr-i" onClick={() => jumpTo(r)}>
                <i style={{ background: COLORS[r.label] }} />
                {r.name}
                <em>{LABEL_CN[r.label]}{r.sub ? ` · ${r.sub}` : ""}</em>
              </div>
            ))}
          </div>
        )}
        <div className="kg-btns">
          <button onClick={loadWhole} title="加载全图">全图</button>
          <button onClick={() => fgRef.current?.zoomToFit(400, 40)} title="适应窗口">适应</button>
          <button onClick={() => setFull((v) => !v)} className="on-full">{full ? "退出全屏" : "全屏"}</button>
        </div>
      </div>

      {/* 画布（全屏时同一容器 fixed 到视口；embedded 时 flex 填满卡片） */}
      <div ref={hostRef} className={`kg-host2 ${embedded ? "adaptive" : ""} ${full ? "fs" : ""}`}>
        {full && (
          <button className="kg-fs-x" onClick={() => setFull(false)}>
            退出全屏 ✕
          </button>
        )}
        {!loaded && <div className="kg-loading">加载中…</div>}
        {loaded && graphData.nodes.length === 0 && (
          <div className="kg-loading">暂无图谱关系</div>
        )}
        <ForceGraph2D
          ref={fgRef}
          width={size.w}
          height={size.h}
          graphData={graphData}
          nodeCanvasObject={drawNode}
          nodePointerAreaPaint={(n: GNode, color, c) => {
            c.fillStyle = color;
            c.beginPath();
            c.arc(n.x ?? 0, n.y ?? 0, 12, 0, 2 * Math.PI);
            c.fill();
          }}
          nodeLabel={(n) => `${LABEL_CN[(n as GNode).label]}·${(n as GNode).name}`}
          onNodeClick={(n) => expand(n as GNode)}
          onNodeRightClick={(n) => openEntity(n as GNode)}
          onNodeDragEnd={(n) => {
            (n as GNode).fx = (n as GNode).x;
            (n as GNode).fy = (n as GNode).y;
          }}
          linkLabel={(l) => (l as GLink).rel}
          linkColor={(l) =>
            selected && (idOf((l as GLink).source) === selected || idOf((l as GLink).target) === selected)
              ? "rgba(47,111,237,.7)" : "rgba(120,130,150,.45)"}
          linkWidth={(l) =>
            selected && (idOf((l as GLink).source) === selected || idOf((l as GLink).target) === selected) ? 2 : 1}
          linkDirectionalArrowLength={5}
          linkDirectionalArrowRelPos={0.82}
          cooldownTicks={120}
          d3VelocityDecay={0.3}
          backgroundColor="#f8fafc"
        />
      </div>

      {/* 图例 / 选中信息 */}
      <div className="kg-legend">
        {LABELS.map((l) => (
          <span
            key={l}
            className={hidden.has(l) ? "off" : ""}
            onClick={() =>
              setHidden((s) => {
                const nx = new Set(s);
                nx.has(l) ? nx.delete(l) : nx.add(l);
                return nx;
              })}
          >
            <i style={{ background: COLORS[l] }} />{LABEL_CN[l]}
          </span>
        ))}
      </div>
      {selectedNode && (
        <div className="kg-sel">
          <b>{selectedNode.name}</b>
          <span className="kg-sel-sub">{LABEL_CN[selectedNode.label]}{selectedNode.sub ? ` · ${selectedNode.sub}` : ""}</span>
          {selectedNode.label !== "Book" && (
            <button onClick={() => openEntity(selectedNode)}>
              {selectedNode.label === "Dynasty" ? "跳转时间线 →" : "打开实体卡 →"}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
