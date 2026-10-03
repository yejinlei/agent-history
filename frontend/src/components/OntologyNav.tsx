import { useMemo, useState } from "react";
import type { EntityKind, OntologyIndex } from "../types";
import { fmtYear } from "../utils";

type Cat = "dynasty" | "event" | "person" | "place" | "book"
  | "institution" | "artifact" | "concept" | "rel";

/** 后端 /api/entity 仅支持 event/person/place：这三类供浏览，点击不跳转实体卡 */
const BROWSE_ONLY: Cat[] = ["institution", "artifact", "concept"];

const CATS: { id: Cat; label: string; icon: string }[] = [
  { id: "dynasty", label: "政权", icon: "🏛" },
  { id: "event", label: "事件", icon: "⚔" },
  { id: "person", label: "人物", icon: "👤" },
  { id: "place", label: "地点", icon: "📍" },
  { id: "book", label: "书源", icon: "📚" },
  { id: "institution", label: "机构", icon: "🏯" },
  { id: "artifact", label: "文物", icon: "🏺" },
  { id: "concept", label: "观念", icon: "💡" },
  { id: "rel", label: "关系", icon: "🕸" },
];

interface Props {
  onto: OntologyIndex | null;
  selectedId: string | null;
  onPick: (kind: EntityKind, id: string) => void;
  onPickDynasty: (id: string, year: number) => void;
  onPickBook: (id: string) => void;
}

export default function OntologyNav(p: Props) {
  const [cat, setCat] = useState<Cat>("dynasty");
  const [filter, setFilter] = useState("");

  const counts = p.onto?.stats;
  const list = useMemo(() => {
    if (!p.onto) return [];
    const q = filter.trim();
    const match = (s: string) => !q || s.toLowerCase().includes(q.toLowerCase());
    switch (cat) {
      case "dynasty":
        return p.onto.dynasties
          .filter((d) => match(d.name))
          .map((d) => ({
            id: d.id, name: d.name, sub: `${fmtYear(d.span[0])}—${fmtYear(d.span[1])}`,
            lane: d.lane, badge: null as string | null,
          }));
      case "event":
        return p.onto.events
          .filter((e) => match(e.name))
          .map((e) => ({
            id: e.id, name: e.name, sub: fmtYear(e.year), lane: 0,
            badge: e.from_book ? "挂载" : null,
          }));
      case "person":
        return p.onto.people
          .filter((x) => match(x.name))
          .map((x) => ({
            id: x.id, name: x.name, sub: x.years.join("—"), lane: 0,
            badge: x.from_book ? "挂载" : null,
          }));
      case "place":
        return p.onto.places
          .filter((x) => match(x.name) || match(x.ancient))
          .map((x) => ({ id: x.id, name: x.name, sub: x.ancient, lane: 0, badge: null }));
      case "book":
        return p.onto.books
          .filter((b) => match(b.title))
          .map((b) => ({
            id: b.id, name: `《${b.title}》`, sub: b.mounted ? "已挂载" : "未挂载",
            lane: 0, badge: b.mounted ? "挂载" : null,
          }));
      case "institution":
        return p.onto.institutions
          .filter((x) => match(x.name))
          .map((x) => ({
            id: x.id, name: x.name, sub: x.kind || "机构", lane: 0,
            badge: null as string | null,
          }));
      case "artifact":
        return p.onto.artifacts
          .filter((x) => match(x.name))
          .map((x) => ({
            id: x.id, name: x.name,
            sub: [x.kind, x.year != null ? String(x.year) : "纪年无考", x.museum]
              .filter(Boolean).join(" · "),
            lane: 0, badge: null as string | null,
          }));
      case "concept":
        return p.onto.concepts
          .filter((x) => match(x.name))
          .map((x) => ({
            id: x.id, name: x.name, sub: x.kind || "观念", lane: 0,
            badge: null as string | null,
          }));
      case "rel":
        return p.onto.relations.map((r) => ({
          id: r.rel, name: r.rel, sub: `${r.count} 条边`, lane: 0, badge: null,
        }));
    }
  }, [p.onto, cat, filter]);

  const onClick = (id: string) => {
    if (cat === "event" || cat === "person" || cat === "place") {
      p.onPick(cat, id);
    } else if (cat === "dynasty") {
      const d = p.onto?.dynasties.find((x) => x.id === id);
      if (d) p.onPickDynasty(id, Math.max(d.span[0], Math.min(d.span[1], d.span[0] + 1)));
    } else if (cat === "book") {
      p.onPickBook(id);
    }
  };

  return (
    <aside className="onto-nav">
      <section className="card onto-card">
        <div className="onto-head">
          <div className="onto-title">本体导航</div>
          <input
            className="onto-flt"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="过滤…"
          />
        </div>
        <div className="onto-cats">
          {CATS.map((c) => (
            <button
              key={c.id}
              className={`onto-cat ${cat === c.id ? "on" : ""}`}
              onClick={() => setCat(c.id)}
            >
              <span className="oi">{c.icon}</span>
              <span className="ol">{c.label}</span>
              {counts && (
                <span className="oc">
                  {c.id === "rel" ? counts.edge : counts[c.id as keyof typeof counts] ?? 0}
                </span>
              )}
            </button>
          ))}
        </div>
        <div className="onto-list">
          {!p.onto && <div className="empty" style={{ padding: 16 }}>加载中…</div>}
          {p.onto && list.length === 0 && (
            <div className="empty" style={{ padding: 16 }}>无匹配项</div>
          )}
          {list.map((it) => (
            <div
              key={it.id}
              className={`onto-item ${p.selectedId === it.id ? "on" : ""} ${it.lane > 0 ? "parallel" : ""}`}
              onClick={() => onClick(it.id)}
            >
              <div className="n">{it.name}</div>
              <div className="s">
                {it.sub}
                {it.badge && <span className="mbadge">{it.badge}</span>}
              </div>
            </div>
          ))}
        </div>
        {p.onto && (
          <div className="onto-foot">
            {BROWSE_ONLY.includes(cat)
              ? "该分类暂无实体卡，可在图谱浏览中查看其关系"
              : `共 ${counts?.edge ?? 0} 条关系边 · 数据源本体论 JSON`}
          </div>
        )}
      </section>
    </aside>
  );
}
