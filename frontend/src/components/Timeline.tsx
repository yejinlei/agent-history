import { useEffect, useMemo, useRef, useState } from "react";
import type { Book, Dynasty, TlEvent } from "../types";
import { fmtYear, ganzhi } from "../utils";

interface Props {
  dynasties: Dynasty[];
  books: Book[];
  events: TlEvent[];
  year: number;
  emperorText: string;
  playing: boolean;
  onTogglePlay: () => void;
  onYear: (y: number) => void;
  onPickEvent: (id: string, y: number) => void;
  /** 点击某政权车道（含并立政权）：选中该政权，联动事件/地图/图谱 */
  onPickDynasty?: (id: string, year: number) => void;
  /** 当前选中的政权 id（车道高亮） */
  selectedDynastyId?: string | null;
}

const YMIN = -8000;
const YMAX = 1912;
const FULL = YMAX - YMIN;
const MIN_SPAN = 0.08; // ≈ 一个月，最深放大到「日」级

const MONTHS_CN = ["正月", "二月", "三月", "四月", "五月", "六月",
  "七月", "八月", "九月", "十月", "冬月", "腊月"];
const YEAR_STEPS = [1000, 500, 200, 100, 50, 20, 10, 5, 2, 1];

interface Tick { v: number; label: string; major: boolean; vert?: boolean }

/** 事件在时间轴上的小数年位置（含月日精度） */
function evPos(e: TlEvent): number {
  if (e.month && e.day) return e.year + (e.month - 1 + Math.min(e.day, 30) / 30) / 12;
  if (e.month) return e.year + (e.month - 0.5) / 12;
  return e.year + 0.5;
}

function niceYearStep(span: number): number {
  for (const s of YEAR_STEPS.slice().reverse()) {
    if (span / s >= 7) return s;
  }
  return 1;
}

function buildTicks(c: number, s: number): Tick[] {
  const from = c - s / 2;
  const to = c + s / 2;
  const out: Tick[] = [];
  if (s > 2.4) {
    const step = niceYearStep(s);
    for (let y = Math.ceil(from / step) * step; y <= to; y += step) {
      out.push({ v: y, label: y < 0 ? `前${-y}` : `${y}`, major: true });
    }
  } else if (s > 0.4) {
    // 月级刻度
    for (let y = Math.floor(from); y <= Math.ceil(to); y++) {
      for (let m = 0; m < 12; m++) {
        const v = y + m / 12;
        if (v < from || v > to) continue;
        out.push(
          m === 0
            ? { v, label: y < 0 ? `前${-y}年` : `${y}年`, major: true }
            : { v, label: s < 1.4 ? MONTHS_CN[m] : "", major: false },
        );
      }
    }
  } else {
    // 日级刻度（30天/月近似）；主刻度标签竖排避免横向溢出
    const dayStep = s < 0.16 ? 1 : 5;
    for (let y = Math.floor(from); y <= Math.ceil(to); y++) {
      for (let m = 0; m < 12; m++) {
        for (let d = 0; d < 30; d += dayStep) {
          const v = y + m / 12 + d / 360;
          if (v < from || v > to) continue;
          if (d === 0) {
            out.push({ v, label: `${y < 0 ? "前" + -y : y}年${MONTHS_CN[m]}`, major: true, vert: true });
          } else {
            out.push({ v, label: `${d + 1}`, major: false });
          }
        }
      }
    }
  }
  return out;
}

function scaleName(s: number): string {
  if (s > 800) return "通史概览";
  if (s > 60) return "百年";
  if (s > 8) return "年代";
  if (s > 2.4) return "年度";
  if (s > 0.4) return "月度";
  return "日级";
}

/** 把 [a,b] 百分比区间裁剪到 [0,100]，返回 [top, height] */
function clamp01(a: number, b: number): [number, number] {
  const t = Math.max(a, 0);
  return [t, Math.max(Math.min(b, 100) - t, 0)];
}

export default function Timeline({ dynasties, books, events, year, emperorText, playing, onTogglePlay, onYear, onPickEvent, onPickDynasty, selectedDynastyId }: Props) {
  const [view, setView] = useState({ c: (YMIN + YMAX) / 2, s: FULL });
  const bodyRef = useRef<HTMLDivElement>(null);
  const trackRef = useRef<HTMLDivElement>(null);
  const scrubbing = useRef(false);
  const panning = useRef<null | { y0: number; c0: number; moved: boolean }>(null);

  const { c, s } = view;
  const from = c - s / 2;
  const to = c + s / 2;
  const y = (v: number) => ((v - from) / s) * 100; // 小数年 → 纵向百分比（古在上，今在下）
  const inv = (clientY: number, el: HTMLElement) => {
    const r = el.getBoundingClientRect();
    const p = Math.max(0, Math.min(1, (clientY - r.top) / r.height));
    return from + p * s;
  };
  const clampView = (nc: number, ns: number) => {
    ns = Math.max(MIN_SPAN, Math.min(FULL, ns));
    const half = ns / 2;
    nc = Math.max(YMIN + half - (ns >= FULL ? half : 0), Math.min(YMAX - half + (ns >= FULL ? half : 0), nc));
    if (ns >= FULL) nc = (YMIN + YMAX) / 2;
    return { c: nc, s: ns };
  };

  /* 滚轮缩放（以鼠标所在年份为锚点） */
  useEffect(() => {
    const el = bodyRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const r = el.getBoundingClientRect();
      const p = Math.max(0, Math.min(1, (e.clientY - r.top) / r.height));
      const factor = e.deltaY > 0 ? 1.3 : 1 / 1.3;
      setView((v) => {
        const anchor = v.c - v.s / 2 + p * v.s;
        const ns = Math.max(MIN_SPAN, Math.min(FULL, v.s * factor));
        const nc = anchor + (v.c - anchor) * (ns / v.s);
        return clampView(nc, ns);
      });
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []);

  const zoomBtn = (dir: 1 | -1) =>
    setView((v) => clampView(v.c, v.s * (dir > 0 ? 1.6 : 1 / 1.6)));

  /* 政权车道拖动 = 平移视图（纵向） */
  const onLaneDown = (e: React.PointerEvent) => {
    panning.current = { y0: e.clientY, c0: c, moved: false };
    (e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId);
  };
  const onLaneMove = (e: React.PointerEvent) => {
    const p = panning.current;
    if (!p || !bodyRef.current) return;
    const dy = e.clientY - p.y0;
    if (Math.abs(dy) > 3) p.moved = true;
    if (p.moved) {
      const h = bodyRef.current.getBoundingClientRect().height;
      setView((v) => clampView(p.c0 - (dy / h) * v.s, v.s));
    }
  };
  const onLaneUp = () => {
    // 轻微延迟复位，抑制平移后的点击跳转
    setTimeout(() => (panning.current = null), 0);
  };

  /* 左轴拖动 = 选择年份 */
  const yearFromPointer = (e: React.PointerEvent | PointerEvent) =>
    Math.round(inv(e.clientY, trackRef.current!));

  const ticks = useMemo(() => buildTicks(c, s), [c, s]);

  /* 可视范围内的政权车道 */
  const lanes = useMemo(
    () => dynasties.filter((d) => d.span[1] >= from && d.span[0] <= to),
    [dynasties, from, to],
  );
  const maxLane = lanes.reduce((m, d) => Math.max(m, d.lane ?? 0), 0);
  const EMP_W = 22; // 皇帝分段列宽（国号之左）
  const BAR_W = 26;
  /* 分级显示：跨度太大时隐藏皇帝列，避免密密麻麻；文字阈值随跨度提高 */
  const showEmp = s <= 1400;
  const minEmpH = s <= 700 ? 1.0 : 1.6; // 皇帝标签最小高度%
  const minNameH = s > 1500 ? 2.0 : 1.4; // 国号标签最小高度%
  const BLOCK = BAR_W + (showEmp ? EMP_W : 0) + 3;
  const laneLeft = (lane: number) => 2 + lane * BLOCK;
  const lanesW = (maxLane + 1) * BLOCK + 6;

  /* 可视范围内的事件落点 */
  const dots = useMemo(
    () =>
      events
        .map((e) => ({ e, v: evPos(e) }))
        .filter(({ v }) => v >= from && v <= to),
    [events, from, to],
  );
  const dynColor = useMemo(() => {
    const m = new Map<string, string>();
    dynasties.forEach((d) => m.set(d.id, d.color));
    return m;
  }, [dynasties]);

  const mountedBooks = books.filter((b) => b.mounted && b.span[1] >= from && b.span[0] <= to);

  return (
    <section className="card tl-card">
      <div className="tl-now">
        <span className="yr">{fmtYear(year)}</span>
        {ganzhi(year)}年
        <span className="meta">{emperorText}</span>
        <span className="tl-ctl">
          <span className="tl-scale">{scaleName(s)}</span>
          <button onClick={() => zoomBtn(1)} title="缩小">－</button>
          <button onClick={() => zoomBtn(-1)} title="放大">＋</button>
          <button onClick={() => setView({ c: (YMIN + YMAX) / 2, s: FULL })} title="复位">复位</button>
          <button className={playing ? "on" : ""} onClick={onTogglePlay} title="播放/暂停">{playing ? "⏸" : "▶"}</button>
        </span>
      </div>

      <div className="tl-body" ref={bodyRef}>
        {/* 刻度与选年轴（竖） */}
        <div
          className="track"
          ref={trackRef}
          onPointerDown={(e) => {
            scrubbing.current = true;
            (e.target as HTMLElement).setPointerCapture?.(e.pointerId);
            onYear(yearFromPointer(e));
          }}
          onPointerMove={(e) => {
            if (scrubbing.current) onYear(yearFromPointer(e));
          }}
          onPointerUp={() => (scrubbing.current = false)}
          onPointerCancel={() => (scrubbing.current = false)}
        >
          <div className="tl-line" />
          {ticks.map((t, i) => (
            <div
              key={i}
              className={`tl-tick ${t.major ? "major" : "minor"}`}
              style={{ top: `${y(t.v)}%` }}
            >
              {t.label && <span className={t.vert ? "vert" : t.major ? "" : "sub"}>{t.label}</span>}
            </div>
          ))}
          {year + 0.5 >= from && year + 0.5 <= to && (
            <div className="tl-handle" style={{ top: `${y(year + 0.5)}%` }} />
          )}
        </div>

        {/* 事件落点列（按年/月/日精确定位） */}
        <div className="ev-dots">
          {dots.map(({ e, v }) => (
            <div
              key={e.id}
              className="tl-dot"
              style={{
                top: `${y(v)}%`,
                background: dynColor.get(e.dynasty_id) ?? "var(--brand)",
              }}
              title={`${fmtYear(e.year)}${e.month ? `·${MONTHS_CN[e.month - 1]}` : ""}${e.day ? `${e.day}日` : ""} ${e.name}`}
              onClick={() => onPickEvent(e.id, e.year)}
            />
          ))}
        </div>

        {/* 挂载书源覆盖区间列 */}
        {mountedBooks.length > 0 && (
          <div className="mount-col">
            {mountedBooks.map((b) => {
              const [mt, mh] = clamp01(y(b.span[0]), y(b.span[1]));
              return (
                <div
                  key={b.id}
                  className="mt-bar"
                  style={{ top: `${mt}%`, height: `${Math.max(mh, 1.2)}%`, background: b.color }}
                  title={`《${b.title}》覆盖区间`}
                  onClick={() => onYear(Math.round((b.span[0] + b.span[1]) / 2))}
                />
              );
            })}
          </div>
        )}

        {/* 政权车道：上下拖动平移，点击跳转；并立政权过多时可横向滚动 */}
        <div className="tl-scroll">
          <div
            className="lanes"
            style={{ width: lanesW }}
            onPointerDown={onLaneDown}
            onPointerMove={onLaneMove}
            onPointerUp={onLaneUp}
            onPointerCancel={onLaneUp}
          >
            {lanes.map((d) => {
              const [lt, lh0] = clamp01(y(d.span[0]), y(d.span[1]));
              const lh = Math.max(lh0, 0.35);
              const lane = d.lane ?? 0;
              /* 皇帝在位分段（标注在国号之左） */
              const segs = d.emperors
                .map((emp) => {
                  const es = Math.max(emp.s, d.span[0]);
                  const ee = Math.min(emp.e, d.span[1]);
                  if (ee < from || es > to || ee < es) return null;
                  const [st, sh0] = clamp01(y(es), y(ee));
                  return { emp, st, sh: Math.max(sh0, 0.15) };
                })
                .filter((v): v is NonNullable<typeof v> => v !== null);
              return (
                <div key={d.id}>
                  {showEmp && segs.length > 0 && (
                    <div className="emp-col" style={{ left: laneLeft(lane) }}>
                      {segs.map(({ emp, st, sh }, i) => (
                        <div
                          key={i}
                          className="emp-seg"
                          style={{
                            top: `${st}%`,
                            height: `${sh}%`,
                            borderColor: d.color,
                          }}
                          title={`${emp.n}（${fmtYear(emp.s)}—${fmtYear(emp.e)}）${emp.era ? ` · 年号 ${emp.era}` : ""}`}
                          onClick={() => {
                            if (panning.current?.moved) return;
                            const mid = Math.round((emp.s + emp.e) / 2);
                            if (onPickDynasty) onPickDynasty(d.id, mid);
                            else onYear(mid);
                          }}
                        >
                          {sh > minEmpH ? (sh > minEmpH * 2.9 && emp.era ? `${emp.n}·${emp.era}` : emp.n) : ""}
                        </div>
                      ))}
                    </div>
                  )}
                  <div
                    className={`lane${selectedDynastyId === d.id ? " sel" : ""}`}
                    data-lane={lane}
                    style={{
                      top: `${lt}%`,
                      height: `${lh}%`,
                      left: laneLeft(lane) + (showEmp ? EMP_W : 0),
                      background: d.color,
                    }}
                    title={`${d.name}（${fmtYear(d.span[0])}—${fmtYear(d.span[1])}）`}
                    onClick={() => {
                      if (panning.current?.moved) return;
                      const mid = Math.round((d.span[0] + d.span[1]) / 2);
                      if (onPickDynasty) onPickDynasty(d.id, mid);
                      else onYear(mid);
                    }}
                  >
                    {lh > minNameH ? d.name : ""}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </section>
  );
}
