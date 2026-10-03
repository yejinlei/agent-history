import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api";
import type {
  Book, Dynasty, EntityDetail, EntityKind, EventItem, OntologyIndex, TlEvent, UIAction,
} from "./types";
import MapView from "./components/MapView";
import Timeline from "./components/Timeline";
import RightDock, { DockId } from "./components/RightDock";
import SplitBar from "./components/SplitBar";
import GraphExplorer from "./components/GraphExplorer";
import { MapGallery } from "./components/MapGallery";
import { emperorOf, fmtYear, ganzhi } from "./utils";

/* ---------- 可调布局常量（宽度范围 / 默认值 / 持久化） ---------- */
const LS_KEY = "chronosino:layout";
const LAYOUT_DEF = { leftW: 380, dockW: 420, kgW: 0 };
const LIMITS = {
  left: [260, 600],
  dock: [300, 680],
  kg: [300, 900],
};
const clamp = (v: number, [lo, hi]: number[]) => Math.max(lo, Math.min(hi, v));

function loadLayout() {
  try {
    return { ...LAYOUT_DEF, ...JSON.parse(localStorage.getItem(LS_KEY) || "{}") };
  } catch {
    return { ...LAYOUT_DEF };
  }
}

export default function App() {
  // 基础数据
  const [dynasties, setDynasties] = useState<Dynasty[]>([]);
  const [books, setBooks] = useState<Book[]>([]);
  const [pipelineStages, setPipelineStages] = useState<string[]>([]);
  const [onto, setOnto] = useState<OntologyIndex | null>(null);

  // 时间与事件
  const [year, setYear] = useState(-221);
  const [dynasty, setDynasty] = useState<Dynasty | null>(null);
  const [parallel, setParallel] = useState<Dynasty[]>([]);
  const [events, setEvents] = useState<EventItem[]>([]);
  const [allEvents, setAllEvents] = useState<TlEvent[]>([]);
  // 显式选中的政权（点击车道时设置；年份刻度/拖动会清空，回到主线自动定位）
  const [selectedDynastyId, setSelectedDynastyId] = useState<string | null>(null);

  // 疆域地图图库（当前朝代的全部可用历史地图）
  const [mapList, setMapList] = useState<{ tile: string; title: string; primary?: boolean }[]>([]);
  const [tileSel, setTileSel] = useState<string | null>(null);

  // 右侧面板：active = 当前显示；pinned = 吸附固定（鼠标离开后保留）
  const [dock, setDock] = useState<DockId | null>(null);
  const [pinned, setPinned] = useState<DockId | null>(null);
  const [entity, setEntity] = useState<EntityDetail | null>(null);
  const [sourceFilter, setSourceFilter] = useState<string>("all");

  /* 可调宽度（拖拽分隔条；持久化到 localStorage） */
  const initLayout = useRef(loadLayout());
  const [leftW, setLeftW] = useState(initLayout.current.leftW);
  const [dockW, setDockW] = useState(initLayout.current.dockW);
  const [kgW, setKgW] = useState(initLayout.current.kgW);
  const belowRef = useRef<HTMLDivElement>(null);
  const dragBase = useRef({ left: 0, dock: 0, kg: 0 });

  useEffect(() => {
    localStorage.setItem(LS_KEY, JSON.stringify({ leftW, dockW, kgW }));
  }, [leftW, dockW, kgW]);

  /* 拖拽基准同步为当前宽度 */
  const baseOf = (k: "left" | "dock" | "kg") => () => {
    dragBase.current[k] = k === "left" ? leftW : k === "dock" ? dockW : kgW;
  };

  /* 地图下方图谱列：首次有尺寸时按 54% 初始化 */
  useEffect(() => {
    if (kgW || !belowRef.current) return;
    const w = belowRef.current.clientWidth;
    if (w > 0) setKgW(clamp(Math.round(w * 0.54), LIMITS.kg));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dynasties]);

  // 地图路径动画（选中事件带 route 时显示）
  const [routeEvent, setRouteEvent] = useState<EventItem | null>(null);
  // Agent 驱动的地图视野跳转
  const [flyTarget, setFlyTarget] = useState<{ lon: number; lat: number; zoom: number } | null>(null);
  // 人物地图标记（选中人物且有坐标时）
  const [personFocus, setPersonFocus] = useState<{ id: string; name: string; lon: number; lat: number } | null>(null);
  // 动态效果库状态
  const [pulsePoint, setPulsePoint] = useState<{ lon: number; lat: number; label?: string } | null>(null);
  const [highlightIds, setHighlightIds] = useState<string[]>([]);
  const [region, setRegion] = useState<{ lon: number; lat: number; km: number; label?: string } | null>(null);
  const [dimOthers, setDimOthers] = useState(false);
  const [shakeKey, setShakeKey] = useState(0);
  // 时间轴播放
  const [playing, setPlaying] = useState(false);
  const playTimer = useRef<number>(0);

  // 图层
  const [showBase, setShowBase] = useState(true);
  const [showOverlay, setShowOverlay] = useState(true);

  // 搜索
  const [term, setTerm] = useState("");
  const [suggest, setSuggest] = useState<
    { kind: EntityKind; id: string; name: string }[]
  >([]);
  const suggestTimer = useRef<number>(0);

  /* ---------- 初始加载 ---------- */
  useEffect(() => {
    api.dynasties().then(setDynasties);
    api.allEvents().then(setAllEvents);
    api.ontology().then(setOnto).catch(() => setOnto(null));
    api.books().then((r) => {
      setBooks(r.books);
      setPipelineStages(r.pipeline_stages);
    });
  }, []);

  /* ---------- 年份/选中政权变化：取时间切片 ---------- */
  useEffect(() => {
    api.timeline(year, selectedDynastyId ?? undefined).then((r) => {
      setDynasty(r.dynasty);
      setParallel(r.parallel ?? []);
      setEvents(r.events);
    });
  }, [year, selectedDynastyId]);

  /* ---------- 朝代变化：加载疆域地图图库 ---------- */
  useEffect(() => {
    if (!dynasty) return;
    setTileSel(dynasty.map_tile);
    api.dynastyMaps(dynasty.id).then(setMapList).catch(() => setMapList([]));
  }, [dynasty?.id]);

  /* 图库缩略图取当前朝代都城所在的 z4 瓦片（区域图也能取到内容） */
  const thumbCell = (() => {
    const c = dynasty?.capital;
    if (!c) return "4/12/6";
    const z = 4, n = 2 ** z;
    const x = Math.min(n - 1, Math.max(0, Math.floor(((c.lon + 180) / 360) * n)));
    const la = (c.lat * Math.PI) / 180;
    const y = Math.min(n - 1, Math.max(0, Math.floor(((1 - Math.log(Math.tan(la) + 1 / Math.cos(la)) / Math.PI) / 2) * n)));
    return `${z}/${x}/${y}`;
  })();

  /* ---------- 实体选择（打开并吸附详情面板） ---------- */
  const selectById = useCallback(async (kind: EntityKind, id: string) => {
    const e = await api.entity(kind, id);
    setEntity(e);
    setDock("detail");
    setPinned("detail");
    // 联动地图：人物→人物标记；地点→飞至该地点
    if (e.kind === "person" && e.lon != null && e.lat != null) {
      setPersonFocus({ id: e.id, name: e.name, lon: e.lon, lat: e.lat });
      setRouteEvent(null);
    } else if (e.kind === "place") {
      setFlyTarget({ lon: e.lon, lat: e.lat, zoom: 9 });
      setPersonFocus(null);
      setRouteEvent(null);
    }
  }, []);

  const selectEvent = useCallback(
    async (e: EventItem, openDock = true) => {
      const full = await api.entity("event", e.id);
      setEntity(full);
      if (openDock) {
        setDock("detail");
        setPinned("detail");
      }
      setRouteEvent(full.kind === "event" && full.route ? full : null);
      setPersonFocus(null);
      // 联动：时间轴跳转到事件年份、地图飞至事件地点
      setYear(e.year);
      if (e.lon != null && e.lat != null) {
        setFlyTarget({ lon: e.lon, lat: e.lat, zoom: 7 });
      }
    },
    [],
  );

  /* ---------- 首次进入：自动选中首个事件（不弹出抽屉） ----------
      显式选中某政权时不自动选事件，让图谱先展示该政权的知识子图 */
  useEffect(() => {
    if (!entity && !selectedDynastyId && events.length > 0) selectEvent(events[0], false);
  }, [events, entity, selectedDynastyId, selectEvent]);

  /* ---------- 书库 ---------- */
  const refreshBooks = useCallback(async () => {
    const r = await api.books();
    setBooks(r.books);
    api.ontology().then(setOnto).catch(() => {});
    // 刷新当前时间切片（挂载事件在地图/横条上出现）
    const t = await api.timeline(year, selectedDynastyId ?? undefined);
    api.allEvents().then(setAllEvents);
    setDynasty(t.dynasty);
    setParallel(t.parallel ?? []);
    setEvents(t.events);
    if (entity) {
      const e = await api.entity(entity.kind, entity.id).catch(() => null);
      setEntity(e);
    }
  }, [year, entity, selectedDynastyId]);

  const onViewBook = useCallback((b: Book) => {
    setSourceFilter(b.id);
    // 跳到书覆盖区间，回到主线自动定位
    setSelectedDynastyId(null);
    setYear(Math.round((b.span[0] + b.span[1]) / 2));
  }, []);

  /* ---------- 来源过滤 ---------- */
  const coveredIds = useMemo(() => {
    if (sourceFilter === "all") return null;
    const b = books.find((x) => x.id === sourceFilter);
    const s = new Set<string>();
    b?.extracts.forEach((x) => {
      if (x.target_id) s.add(x.target_id);
      if (x.new_id) s.add(x.new_id);
    });
    return s;
  }, [sourceFilter, books]);

  const shownEvents = useMemo(
    () => (coveredIds ? events.filter((e) => coveredIds.has(e.id)) : events),
    [events, coveredIds],
  );

  /* ---------- 搜索 ---------- */
  useEffect(() => {
    window.clearTimeout(suggestTimer.current);
    const q = term.trim();
    if (!q) {
      setSuggest([]);
      return;
    }
    suggestTimer.current = window.setTimeout(async () => {
      const r = await api.search(q);
      setSuggest(r.map((x) => ({ kind: x.kind, id: x.id, name: x.name })));
    }, 200);
  }, [term]);

  const overlayTile = tileSel ?? dynasty?.map_tile ?? null;
  const emperorText = dynasty ? emperorOf(dynasty, year) : "";

  /* ---------- 本体导航回调 ---------- */
  const onPickDynasty = useCallback((id: string, yr: number) => {
    // 切换政权：清空当前实体（避免详情卡停留在别的朝代），强制选中该政权
    setSelectedDynastyId(id);
    setEntity(null);
    setYear(yr);
  }, []);
  const onPickBook = useCallback(
    (id: string) => {
      const b = books.find((x) => x.id === id);
      if (b) {
        setDock("books");
        setPinned("books");
        onViewBook(b);
      }
    },
    [books, onViewBook],
  );

  /* ---------- 面板吸附切换 ---------- */
  const togglePin = useCallback(
    (id: DockId) => {
      setPinned((cur) => (cur === id ? null : id));
      setDock(id);
    },
    [],
  );

  /* ---------- 时间轴播放 ---------- */
  const stopPlay = useCallback(() => {
    window.clearInterval(playTimer.current);
    playTimer.current = 0;
    setPlaying(false);
  }, []);
  const togglePlay = useCallback(() => {
    if (playTimer.current) {
      stopPlay();
      return;
    }
    setPlaying(true);
    playTimer.current = window.setInterval(() => {
      setSelectedDynastyId(null); // 播放时回到按年份自动定位
      setYear((y) => {
        const nx = y + 1;
        if (nx > 1912) {
          stopPlay();
          return y;
        }
        return nx;
      });
    }, 320);
  }, [stopPlay]);
  useEffect(() => () => stopPlay(), [stopPlay]);

  /* ---------- Agent 动作执行器 ---------- */
  const runActions = useCallback(
    async (actions?: UIAction[]) => {
      if (!actions?.length) return;
      for (const a of actions) {
        if (a.type === "set_year") setYear(a.year);
        else if (a.type === "fly_to") setFlyTarget({ lon: a.lon, lat: a.lat, zoom: a.zoom ?? 6 });
        else if (a.type === "select_entity") {
          const full = await api.entity(a.kind, a.id);
          setEntity(full);
          setDock("detail");
          setPinned("detail");
          setRouteEvent(full.kind === "event" && full.route ? full : null);
          if (full.kind === "person" && full.lon != null && full.lat != null) {
            setPersonFocus({ id: full.id, name: full.name, lon: full.lon, lat: full.lat });
          } else if (full.kind !== "person") {
            setPersonFocus(null);
          }
        } else if (a.type === "show_route") {
          const full = await api.entity("event", a.id);
          if (full.kind === "event" && full.route) setRouteEvent(full);
        } else if (a.type === "pulse") {
          setPulsePoint({ lon: a.lon, lat: a.lat, label: a.label });
        } else if (a.type === "highlight_events") {
          setHighlightIds(a.ids);
        } else if (a.type === "region") {
          setRegion({ lon: a.lon, lat: a.lat, km: a.km, label: a.label });
        } else if (a.type === "dim") {
          setDimOthers(a.on);
        } else if (a.type === "shake") {
          setShakeKey((k) => k + 1);
        } else if (a.type === "clear_effects") {
          setPulsePoint(null);
          setHighlightIds([]);
          setRegion(null);
          setRouteEvent(null);
          setDimOthers(false);
        } else if (a.type === "play") {
          setYear(a.from);
          if (!playTimer.current) togglePlay();
        }
      }
    },
    [togglePlay],
  );

  return (
    <div className="app">
      <header>
        <div className="logo">
          华夏时空 · ChronoSino
          <small>Agent 驱动的中国历史学习与研究平台</small>
        </div>
        <div className="search">
          <svg className="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}>
            <circle cx="11" cy="11" r="7" />
            <path d="M21 21l-4.3-4.3" />
          </svg>
          <input
            value={term}
            onChange={(e) => setTerm(e.target.value)}
            placeholder="搜索人物 / 事件 / 地点"
          />
          {suggest.length > 0 && (
            <div className="suggest">
              {suggest.map((s) => (
                <div
                  key={s.kind + s.id}
                  className="sg-item"
                  onClick={() => {
                    selectById(s.kind, s.id);
                    setTerm("");
                    setSuggest([]);
                  }}
                >
                  {s.name}
                  <span className="sg-type">{{ event: "事件", person: "人物", place: "地点" }[s.kind]}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </header>

      <div className="app-body">
        {/* 左：竖排时间轴（宽度可拖） */}
        <div className="pane-left" style={{ width: leftW }}>
          <Timeline
            dynasties={dynasties}
            books={books}
            events={allEvents}
            year={year}
            emperorText={emperorText}
            playing={playing}
            onTogglePlay={togglePlay}
            onYear={(y) => {
              // 拖动/点击刻度：回到按年份自动定位主线朝代
              setSelectedDynastyId(null);
              setYear(y);
            }}
            selectedDynastyId={selectedDynastyId}
            onPickDynasty={onPickDynasty}
            onPickEvent={(id, y) => {
              setSelectedDynastyId(null);
              setYear(y);
              selectById("event", id);
            }}
          />
        </div>

        <SplitBar
          axis="x"
          title="拖动调整时间轴宽度"
          onStart={baseOf("left")}
          onDrag={(d) => setLeftW(clamp(dragBase.current.left + d, LIMITS.left))}
        />

        {/* 中：地图 + 下方一行（关系图谱 | 本时期大事） */}
        <main>
          <section className="card map-card">
            <div className="card-head">
              <div className="card-title">
                历史地理视图
              </div>
              <div className="layers">
                <span className={`chk ${showBase ? "on" : ""}`} onClick={() => setShowBase(!showBase)}>
                  现代底图
                </span>
                <span className={`chk ${showOverlay ? "on" : ""}`} onClick={() => setShowOverlay(!showOverlay)}>
                  历史地图
                </span>
              </div>
            </div>
            <MapView
              overlayTile={overlayTile}
              events={shownEvents}
              selectedId={entity?.kind === "event" ? entity.id : null}
              showBase={showBase}
              showOverlay={showOverlay}
              routeEvent={routeEvent}
              flyTarget={flyTarget}
              personFocus={personFocus}
              pulsePoint={pulsePoint}
              highlightIds={highlightIds}
              region={region}
              dimOthers={dimOthers}
              shakeKey={shakeKey}
              onSelect={selectEvent}
            />
            <MapGallery
              items={mapList}
              active={overlayTile}
              thumbCell={thumbCell}
              onSelect={setTileSel}
            />
            <div className="map-legend">
              <span><i style={{ background: "var(--brand)" }} />重大事件（可点击）</span>
              <span><i style={{ background: "var(--gold)" }} />人物节点（图谱中）</span>
              <span style={{ fontSize: 11 }}>
                {overlayTile
                  ? `当前历史图层：${dynasty?.name}${parallel.length ? "（并立：" + parallel.map((d) => d.name).join("、") + "）" : ""}（${ganzhi(year)}）`
                  : "该朝代暂缺可叠加地图图层，事件标记仍可查看"}
              </span>
            </div>
          </section>

          {/* 地图下方：关系图谱 | 本时期大事（宽度可拖） */}
          <div className="below-row" ref={belowRef}>
            <section className="card kg-card" style={{ width: kgW || "54%" }}>
              <div className="card-head kg-card-head">
                <div className="card-title">
                  关系图谱
                </div>
              </div>
              <GraphExplorer
                embedded
                centerId={entity?.id ?? null}
                dynastyId={dynasty?.id ?? null}
                onSelect={selectById}
                onPickDynasty={onPickDynasty}
              />
            </section>

            <SplitBar
              axis="x"
              title="拖动调整图谱与大事宽度"
              onStart={baseOf("kg")}
              onDrag={(d) => setKgW(clamp(dragBase.current.kg + d, LIMITS.kg))}
            />

            <section className="card events-card">
              <div className="card-head" style={{ marginBottom: 8 }}>
                <div className="card-title">
                  本时期大事
                  <span className="tag">
                    {dynasty?.name} · {dynasty ? fmtYear(dynasty.span[0]) : ""}—
                    {dynasty ? fmtYear(dynasty.span[1]) : ""}
                  </span>
                </div>
                <div className="src-flt">
                  <span className={`f ${sourceFilter === "all" ? "on" : ""}`} onClick={() => setSourceFilter("all")}>
                    全部
                  </span>
                  {books.filter((b) => b.mounted).map((b) => (
                    <span
                      key={b.id}
                      className={`f ${sourceFilter === b.id ? "on" : ""}`}
                      onClick={() => setSourceFilter(b.id)}
                    >
                      《{b.title}》
                    </span>
                  ))}
                </div>
              </div>
              <div className="ev-strip ev-strip-v">
                {shownEvents.length ? (
                  shownEvents.map((e) => (
                    <div key={e.id} className="ev-chip" onClick={() => selectEvent(e)}>
                      <div className="y">
                        {fmtYear(e.year)} · {ganzhi(e.year)}
                        {e.from_book && <span className="mbadge">挂载</span>}
                      </div>
                      <div className="n">{e.name}</div>
                      <div className="p">{e.place}</div>
                    </div>
                  ))
                ) : (
                  <div style={{ color: "var(--text-3)", fontSize: 12.5, padding: 8 }}>
                    {sourceFilter === "all" ? "本时期事件数据建设中" : "本书在本时期暂无挂载点"}
                  </div>
                )}
              </div>
            </section>
          </div>
        </main>

        {/* 右：常驻标签栏 + 可吸附面板（面板宽度可拖） */}
        <RightDock
          active={dock}
          pinned={pinned}
          panelW={dockW}
          onPanelDrag={(d) => setDockW(clamp(dragBase.current.dock - d, LIMITS.dock))}
          onPanelDragStart={baseOf("dock")}
          onHover={setDock}
          onRestore={(id) => setDock(id)}
          onTogglePin={togglePin}
          onClose={() => { setDock(null); setPinned(null); }}
          onto={onto}
          selectedId={entity?.id ?? null}
          entity={entity}
          dynasties={dynasties}
          books={books}
          pipelineStages={pipelineStages}
          selectById={selectById}
          refreshBooks={refreshBooks}
          onViewBook={onViewBook}
          onPickDynasty={onPickDynasty}
          onPickBook={onPickBook}
          onActions={runActions}
        />
      </div>
    </div>
  );
}
