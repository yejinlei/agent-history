import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import type { PointerEvent as ReactPointerEvent } from "react";

/* 地图图库：卡片可拖拽排序（自动记住），按主题着色，宽度自适应。 */

export interface GalleryItem {
  tile: string;
  title: string;
  primary?: boolean;
}

type ThemeId = "territory" | "admin" | "military" | "ethnic" | "economy" | "site" | "city";

const THEMES: Record<ThemeId, { name: string; color: string }> = {
  territory: { name: "疆域总图", color: "#b91c1c" },
  admin: { name: "政区建制", color: "#2563eb" },
  military: { name: "军事战争", color: "#ea580c" },
  ethnic: { name: "民族分布", color: "#7c3aed" },
  economy: { name: "经济交通", color: "#16a34a" },
  site: { name: "遗址文化", color: "#ca8a04" },
  city: { name: "城邑局部", color: "#0891b2" },
};

const THEME_ORDER: ThemeId[] = ["territory", "admin", "military", "ethnic", "economy", "site", "city"];

/** 从地图标题推断主题（规则按优先级，先命中先返回） */
function themeOf(title: string): ThemeId {
  if (/遗址|原始社会|新石器|旧石器/.test(title)) return "site";
  if (/盐铁|手工业|商业城市|煤与盐/.test(title)) return "economy";
  if (/战争|起义|南侵|侵入|割据|称雄|统一|路线/.test(title)) return "military";
  if (/少数民族|等部|鲜卑|匈奴|柔然|突厥|吐蕃|回鹘|乌护|乌孙|鞑靼|瓦剌|克烈|黑汗|喀喇汗|汗国|民族/.test(title))
    return "ethnic";
  if (/附近|中心区域|逻些城/.test(title)) return "city";
  if (/刺史部|校尉部|都护府|长史府|行省|诸郡|诸州|[州道郡府]|都司/.test(title)) return "admin";
  return "territory";
}

/** 卡片显示名：去掉"中国/在线历史地图"等包装词，截断 */
function shortTitle(title: string): string {
  const t = title
    .replace(/^中国/, "")
    .replace(/在线历史地图$/, "")
    .replace(/历史地图$/, "")
    .replace(/在线地图$/, "")
    .trim();
  return t.length > 14 ? t.slice(0, 14) + "…" : t;
}

/* ---------- 顺序持久化（跨朝代全局 tile 顺序） ---------- */
const ORDER_KEY = "chronosino:maporder:v1";

function loadOrder(): string[] {
  try {
    const v = JSON.parse(localStorage.getItem(ORDER_KEY) ?? "[]");
    return Array.isArray(v) ? v.filter((x) => typeof x === "string") : [];
  } catch {
    return [];
  }
}

function saveOrder(order: string[]) {
  try {
    localStorage.setItem(ORDER_KEY, JSON.stringify(order));
  } catch {
    /* localStorage 不可用时静默 */
  }
}

interface Props {
  items: GalleryItem[];
  active: string | null;
  thumbCell: string;
  onSelect: (tile: string) => void;
}

export function MapGallery({ items, active, thumbCell, onSelect }: Props) {
  const [savedOrder, setSavedOrder] = useState<string[]>(loadOrder);
  const [filter, setFilter] = useState<ThemeId | "all">("all");

  /* 默认顺序套用用户偏好：已知 tile 按 savedOrder，其余追加 */
  const ordered = useMemo(() => {
    const byTile = new Map(items.map((m) => [m.tile, m]));
    const known: GalleryItem[] = [];
    for (const t of savedOrder) {
      const m = byTile.get(t);
      if (m) known.push(m);
    }
    const knownIds = new Set(known.map((m) => m.tile));
    const rest = items.filter((m) => !knownIds.has(m.tile));
    return [...known, ...rest];
  }, [items, savedOrder]);

  const isCustom = useMemo(
    () => ordered.some((m, i) => m.tile !== items[i]?.tile),
    [ordered, items],
  );

  /* 拖拽中使用的工作顺序（listRef 始终镜像最新值，供事件回调读取） */
  const [list, setList] = useState<GalleryItem[]>(ordered);
  const listRef = useRef(list);
  useEffect(() => {
    listRef.current = list;
  }, [list]);
  const [dragId, setDragId] = useState<string | null>(null);
  const [ghostX, setGhostX] = useState(0);

  const scrollRef = useRef<HTMLDivElement>(null);
  const itemRefs = useRef(new Map<string, HTMLElement>());
  const drag = useRef<{
    id: string;
    startX: number;
    grabX: number;
    ghostY: number;
    width: number;
    moved: boolean;
  } | null>(null);
  const autoScroll = useRef<{ raf: number; dir: number } | null>(null);

  /* items/偏好变化或拖拽结束时同步工作顺序 */
  useEffect(() => {
    if (!dragId) {
      setList(ordered);
      listRef.current = ordered;
    }
  }, [ordered, dragId]);

  useEffect(() => () => stopAutoScroll(), []);

  function stopAutoScroll() {
    if (autoScroll.current) {
      cancelAnimationFrame(autoScroll.current.raf);
      autoScroll.current = null;
    }
  }

  /* 根据指针 X 计算在其他卡片中的插入位置（id 在调用处显式捕获） */
  const reorderTo = useCallback((clientX: number, id: string) => {
    const cur = listRef.current;
    const others = cur.filter((m) => m.tile !== id);
    let target = others.length;
    for (let i = 0; i < others.length; i++) {
      const el = itemRefs.current.get(others[i].tile);
      if (!el) continue;
      const r = el.getBoundingClientRect();
      if (clientX < r.left + r.width / 2) {
        target = i;
        break;
      }
    }
    const from = cur.findIndex((m) => m.tile === id);
    if (from === -1 || from === target) return;
    const next = cur.filter((m) => m.tile !== id);
    next.splice(target, 0, cur[from]);
    listRef.current = next;
    setList(next);
  }, []);

  const onMove = useCallback(
    (e: PointerEvent) => {
      const d = drag.current;
      if (!d) return;
      if (!d.moved && Math.abs(e.clientX - d.startX) < 5) return;
      if (!d.moved) {
        d.moved = true;
        setDragId(d.id);
        document.body.classList.add("gallery-dragging");
      }
      setGhostX(e.clientX);
      reorderTo(e.clientX, d.id);

      // 边缘自动滚动
      const host = scrollRef.current;
      if (host) {
        const r = host.getBoundingClientRect();
        const dir =
          e.clientX < r.left + 48 ? -1 : e.clientX > r.right - 48 ? 1 : 0;
        if (dir !== 0 && (!autoScroll.current || autoScroll.current.dir !== dir)) {
          stopAutoScroll();
          const tick = () => {
            host.scrollLeft += dir * 14;
            const raf = requestAnimationFrame(tick);
            autoScroll.current = { raf, dir };
          };
          const raf = requestAnimationFrame(tick);
          autoScroll.current = { raf, dir };
        } else if (dir === 0) {
          stopAutoScroll();
        }
      }
    },
    [reorderTo],
  );

  const onUp = useCallback(() => {
    const d = drag.current;
    drag.current = null;
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onUp);
    stopAutoScroll();
    if (!d) return;
    if (d.moved) {
      // 提交：当前朝代顺序在前，其他朝代 tile 保持相对顺序在后
      const current = listRef.current.map((m) => m.tile);
      const others = savedOrder.filter((t) => !current.includes(t));
      const next = [...current, ...others];
      setSavedOrder(next);
      saveOrder(next);
    } else {
      onSelect(d.id);
    }
    setDragId(null);
    document.body.classList.remove("gallery-dragging");
  }, [onMove, onSelect, savedOrder]);

  const startDrag = (item: GalleryItem, e: ReactPointerEvent<HTMLElement>) => {
    if (e.button !== 0) return;
    const el = itemRefs.current.get(item.tile);
    const host = scrollRef.current;
    if (!el || !host) return;
    const r = el.getBoundingClientRect();
    const hr = host.getBoundingClientRect();
    drag.current = {
      id: item.tile,
      startX: e.clientX,
      grabX: e.clientX - r.left,
      ghostY: r.top - hr.top + host.scrollTop,
      width: r.width,
      moved: false,
    };
    setGhostX(e.clientX);
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  };

  const resetDefault = () => {
    setSavedOrder([]);
    try {
      localStorage.removeItem(ORDER_KEY);
    } catch {
      /* ignore */
    }
  };

  const shown = dragId ? list : filter === "all" ? list : list.filter((m) => themeOf(m.title) === filter);
  const ghostItem = dragId ? list.find((m) => m.tile === dragId) ?? null : null;
  const host = scrollRef.current?.getBoundingClientRect();
  const ghostStyle: CSSProperties | undefined =
    ghostItem && host && drag.current
      ? {
          width: drag.current.width,
          left: ghostX - host.left + scrollRef.current!.scrollLeft - drag.current.grabX,
          top: drag.current.ghostY,
        }
      : undefined;

  return (
    <div className="map-gallery">
      <div className="mg-bar">
        <span className="mg-hint">地图图库 · 拖动卡片排序</span>
        <span className="mg-filters">
          <button
            className={filter === "all" ? "on" : ""}
            onClick={() => setFilter("all")}
          >
            全部
          </button>
          {THEME_ORDER.map((t) => (
            <button
              key={t}
              className={filter === t ? "on" : ""}
              onClick={() => setFilter(t)}
              title={THEMES[t].name}
            >
              <i style={{ background: THEMES[t].color }} />
              {THEMES[t].name}
            </button>
          ))}
        </span>
        {isCustom && (
          <button className="mg-reset" onClick={resetDefault}>
            恢复默认
          </button>
        )}
      </div>

      <div className="mg-scroll" ref={scrollRef}>
        {shown.length === 0 && <div className="mg-empty">该主题暂无地图</div>}
        {shown.map((m) =>
          m.tile === dragId ? (
            <span key={m.tile} className="mg-ph" style={{ width: drag.current?.width }} />
          ) : (
            <span
              key={m.tile}
              ref={(el) => {
                if (el) itemRefs.current.set(m.tile, el);
                else itemRefs.current.delete(m.tile);
              }}
              className={`mg-item ${active === m.tile ? "on" : ""}`}
              style={{ "--tc": THEMES[themeOf(m.title)].color } as CSSProperties}
              title={`${m.title}（${THEMES[themeOf(m.title)].name}）`}
              onPointerDown={(e) => startDrag(m, e)}
            >
              <img
                src={`/api/tiles/${m.tile}/${thumbCell}.png`}
                alt={m.title}
                loading="lazy"
                draggable={false}
                onError={(e) => ((e.target as HTMLImageElement).style.visibility = "hidden")}
              />
              <i>{shortTitle(m.title)}</i>
            </span>
          ),
        )}
        {ghostItem && ghostStyle && (
          <span
            className={`mg-item dragging ${active === ghostItem.tile ? "on" : ""}`}
            style={
              {
                ...ghostStyle,
                "--tc": THEMES[themeOf(ghostItem.title)].color,
              } as CSSProperties
            }
          >
            <img
              src={`/api/tiles/${ghostItem.tile}/${thumbCell}.png`}
              alt={ghostItem.title}
              draggable={false}
            />
            <i>{shortTitle(ghostItem.title)}</i>
          </span>
        )}
      </div>
    </div>
  );
}
