import { useCallback, useEffect, useRef, useState } from "react";
import L from "leaflet";
import type { EventItem } from "../types";
import { wgs2gcj } from "../utils";

interface Props {
  overlayTile: string | null;
  events: EventItem[];
  selectedId: string | null;
  showBase: boolean;
  showOverlay: boolean;
  routeEvent: EventItem | null;
  flyTarget: { lon: number; lat: number; zoom: number } | null;
  personFocus: { id: string; name: string; lon: number; lat: number } | null;
  /** 动态效果库（Agent 动作驱动） */
  pulsePoint: { lon: number; lat: number; label?: string } | null;
  highlightIds: string[];
  region: { lon: number; lat: number; km: number; label?: string } | null;
  dimOthers: boolean;
  shakeKey: number; // 变化即触发地图震动
  onSelect: (e: EventItem) => void;
}

// 走后端瓦片缓存代理（本地 backend/app/data/tiles/ 持久化，可提交 git）
const TILE_HOST = "/api/tiles";

type BaseStyle = "gaode" | "carto";

const BASES: Record<BaseStyle, { name: string; url: string; attr: string; sub: string; gcj: boolean }> = {
  gaode: {
    name: "高德",
    url: "https://wprd0{s}.is.autonavi.com/appmaptile?x={x}&y={y}&z={z}&lang=zh_cn&size=1&scl=1&style=7",
    attr: "© 高德地图 GS(2023)4677号",
    sub: "1234",
    gcj: true,
  },
  carto: {
    name: "全球",
    url: "https://basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png",
    attr: "© OpenStreetMap © CARTO",
    sub: "abcd",
    gcj: false,
  },
};

export default function MapView({
  overlayTile, events, selectedId, showBase, showOverlay, routeEvent, flyTarget, personFocus,
  pulsePoint, highlightIds, region, dimOthers, shakeKey, onSelect,
}: Props) {
  const hostRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<L.Map | null>(null);
  const baseRef = useRef<L.TileLayer | null>(null);
  const overlayRef = useRef<L.TileLayer | null>(null);
  const markerLayerRef = useRef<L.LayerGroup | null>(null);
  const routeLayerRef = useRef<L.LayerGroup | null>(null);
  const personLayerRef = useRef<L.LayerGroup | null>(null);
  const fxLayerRef = useRef<L.LayerGroup | null>(null);
  const marchTimerRef = useRef<number>(0);
  const [baseStyle, setBaseStyle] = useState<BaseStyle>("gaode");
  const selectCb = useRef(onSelect);
  selectCb.current = onSelect;

  /** 坐标适配：高德底图用 GCJ-02，CartoDB 用 WGS84 */
  const cv = useCallback(
    (lat: number, lon: number): [number, number] => {
      if (!BASES[baseStyle].gcj) return [lat, lon];
      const [glon, glat] = wgs2gcj(lon, lat);
      return [glat, glon];
    },
    [baseStyle],
  );

  // 初始化地图（只一次）
  useEffect(() => {
    if (!hostRef.current || mapRef.current) return;
    const map = L.map(hostRef.current, {
      center: [36.5, 104.5],
      zoom: 4,
      minZoom: 3,
      maxZoom: 14,
      zoomControl: true,
      attributionControl: false,
    });
    // 精简署名：无 Leaflet 前缀，仅保留图源法定信息（随图层自动增减）
    L.control.attribution({ prefix: false }).addTo(map);
    markerLayerRef.current = L.layerGroup().addTo(map);
    routeLayerRef.current = L.layerGroup().addTo(map);
    personLayerRef.current = L.layerGroup().addTo(map);
    fxLayerRef.current = L.layerGroup().addTo(map);
    mapRef.current = map;
    // 容器尺寸变化（窗口拉伸 / 分隔条拖拽 / 布局切换）时同步地图
    const ro = new ResizeObserver(() => map.invalidateSize());
    ro.observe(hostRef.current);
    return () => ro.disconnect();
  }, []);

  // 底图：按 baseStyle 重建瓦片层
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    if (baseRef.current) {
      baseRef.current.remove();
      baseRef.current = null;
    }
    if (!showBase) return;
    const b = BASES[baseStyle];
    baseRef.current = L.tileLayer(b.url, {
      attribution: b.attr,
      subdomains: b.sub,
      opacity: 0.85,
    }).addTo(map);
  }, [baseStyle, showBase]);

  // 历史地图覆盖层（随朝代切换；淡入效果）
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    if (overlayRef.current) {
      overlayRef.current.remove();
      overlayRef.current = null;
    }
    if (showOverlay && overlayTile) {
      const layer = L.tileLayer(
        `${TILE_HOST}/${overlayTile}/{z}/{x}/{y}.png`,
        { attribution: "© OSGeo中国中心", opacity: 0, maxZoom: 14 },
      ).addTo(map);
      overlayRef.current = layer;
      // 淡入
      let op = 0;
      const t = window.setInterval(() => {
        op += 0.15;
        if (op >= 0.9) {
          op = 0.9;
          window.clearInterval(t);
        }
        layer.setOpacity(op);
      }, 60);
      return () => window.clearInterval(t);
    }
  }, [overlayTile, showOverlay]);

  // 事件标记（按缩放级别做网格聚合，密集处合并为数字气泡）
  useEffect(() => {
    const map = mapRef.current;
    const layer = markerLayerRef.current;
    if (!map || !layer) return;

    const render = () => {
      layer.clearLayers();
      // 无坐标事件不渲染：后端缺失坐标为 null（不再用 0.0 占位，
      // 否则所有未知地点会堆在几内亚湾）
      const located = events.filter(
        (e): e is EventItem & { lon: number; lat: number } =>
          e.lon != null && e.lat != null);
      const zoom = map.getZoom();
      const cellPx = zoom <= 4 ? 42 : zoom <= 6 ? 22 : 0;
      if (cellPx > 0) {
        const buckets = new Map<string, EventItem[]>();
        located.forEach((e) => {
          const [la, lo] = cv(e.lat, e.lon);
          const pt = map.latLngToContainerPoint([la, lo]);
          const key = `${Math.floor(pt.x / cellPx)}:${Math.floor(pt.y / cellPx)}`;
          const arr = buckets.get(key) ?? [];
          arr.push(e);
          buckets.set(key, arr);
        });
        buckets.forEach((arr) => {
          if (arr.length === 1) addSingle(arr[0]);
          else {
            const lat = arr.reduce((s, e) => s + e.lat, 0) / arr.length;
            const lon = arr.reduce((s, e) => s + e.lon, 0) / arr.length;
            const [gla, glo] = cv(lat, lon);
            const icon = L.divIcon({
              className: "",
              html: `<div class="ev-cluster">${arr.length}</div>`,
              iconSize: [22, 22],
            });
            const m = L.marker([gla, glo], { icon });
            m.on("click", () => {
              map.setView([gla, glo], Math.min(zoom + 2, 14), { animate: true });
            });
            m.addTo(layer);
          }
        });
      } else {
        located.forEach(addSingle);
      }
    };

    const addSingle = (e: EventItem) => {
      const selected = selectedId === e.id;
      const hl = highlightIds.includes(e.id);
      const cls = `${selected ? "selected" : hl ? "hl" : ""} ${dimOthers && !selected && !hl ? "dim" : ""}`;
      const size = selected ? 12 : hl ? 12 : 10;
      const icon = L.divIcon({
        className: "",
        html: `<div class="ev-marker ${cls}" style="width:${size}px;height:${size}px"></div>`,
        iconSize: [size, size],
      });
      const [gla, glo] = cv(e.lat, e.lon);
      const m = L.marker([gla, glo], { icon, title: e.name });
      m.on("click", () => selectCb.current(e));
      m.addTo(layer);
    };

    render();
    map.on("zoomend", render);
    return () => {
      map.off("zoomend", render);
    };
  }, [events, selectedId, highlightIds, dimOthers, cv]);

  // Agent 驱动的地图视野跳转（flyTo）
  useEffect(() => {
    if (!flyTarget) return;
    const [gla, glo] = cv(flyTarget.lat, flyTarget.lon);
    mapRef.current?.flyTo([gla, glo], flyTarget.zoom, { duration: 1.2 });
  }, [flyTarget, cv]);

  // 地图震动强调（shakeKey 变化即触发）
  useEffect(() => {
    const el = hostRef.current;
    if (!el || !shakeKey) return;
    el.classList.remove("map-shake");
    void el.offsetWidth; // reflow
    el.classList.add("map-shake");
  }, [shakeKey]);

  // 人物聚焦标记（选中人物且有坐标时，金色大头针+名字）
  useEffect(() => {
    const map = mapRef.current;
    const layer = personLayerRef.current;
    if (!map || !layer) return;
    layer.clearLayers();
    if (!personFocus) return;
    const [gla, glo] = cv(personFocus.lat, personFocus.lon);
    const icon = L.divIcon({
      className: "",
      html: `<div class="person-pin"><i>👤</i><b>${personFocus.name}</b></div>`,
      iconSize: [0, 0],
      iconAnchor: [12, 30],
    });
    L.marker([gla, glo], { icon, zIndexOffset: 1000 }).addTo(layer);
    map.flyTo([gla, glo], Math.max(map.getZoom(), 6), { duration: 1.2 });
  }, [personFocus, cv]);

  // 行军/行进路线动画（带 route 的事件）：生长绘制 + 方向箭头 + 行军游标
  useEffect(() => {
    const map = mapRef.current;
    const layer = routeLayerRef.current;
    if (!map || !layer) return;
    layer.clearLayers();
    window.clearInterval(marchTimerRef.current);
    marchTimerRef.current = 0;
    if (!routeEvent?.route || routeEvent.route.length < 2) return;

    const latlngs = routeEvent.route.map(([lon, lat]) => cv(lat, lon));
    // 底衬（深色描边）
    L.polyline(latlngs, { color: "#0f172a", weight: 5, opacity: 0.35 }).addTo(layer);
    // 主线（品牌红）
    const main = L.polyline(latlngs, {
      color: "#b91c1c", weight: 3, opacity: 0.95, className: "route-line",
    }).addTo(layer);
    // 途经节点
    latlngs.forEach((ll, i) => {
      L.circleMarker(ll, {
        radius: i === 0 ? 6 : 4,
        color: "#fff", weight: 1.5,
        fillColor: i === latlngs.length - 1 ? "#7e22ce" : "#b91c1c",
        fillOpacity: 1,
      }).addTo(layer);
    });
    // 方向箭头：每段中点一个，按该段方位角旋转
    for (let i = 0; i < latlngs.length - 1; i++) {
      const [lat1, lon1] = latlngs[i];
      const [lat2, lon2] = latlngs[i + 1];
      const bearing = Math.atan2(lon2 - lon1, lat2 - lat1) * 180 / Math.PI;
      const arrow = L.divIcon({
        className: "",
        html: `<div class="route-arrow" style="transform: rotate(${bearing.toFixed(1)}deg)">➤</div>`,
        iconSize: [0, 0],
        iconAnchor: [8, 8],
      });
      L.marker([(lat1 + lat2) / 2, (lon1 + lon2) / 2], { icon: arrow, interactive: false, zIndexOffset: 900 }).addTo(layer);
    }
    // 视野聚焦路线
    map.fitBounds(L.latLngBounds(latlngs).pad(0.18), { animate: true });

    // 生长动画：先按总长 dash，offset 归零后切换流动模式
    const el = (main as unknown as { _path?: SVGPathElement })._path;
    if (el) {
      const len = el.getTotalLength();
      el.style.strokeDasharray = `${len}`;
      el.style.strokeDashoffset = `${len}`;
      el.getBoundingClientRect(); // reflow
      el.style.transition = "stroke-dashoffset 1.6s ease-in-out";
      el.style.strokeDashoffset = "0";
      window.setTimeout(() => {
        el.style.transition = "";
        el.style.strokeDasharray = "10 8";
        el.classList.add("route-anim");
      }, 1700);
    }

    // 行军游标：兵符图标沿路线缓慢循环移动
    const marchIcon = L.divIcon({
      className: "",
      html: `<div class="march-dot">⚔</div>`,
      iconSize: [0, 0],
      iconAnchor: [10, 10],
    });
    const marcher = L.marker(latlngs[0], { icon: marchIcon, interactive: false, zIndexOffset: 1200 }).addTo(layer);
    const segs: number[] = [];
    let total = 0;
    for (let i = 0; i < latlngs.length - 1; i++) {
      const d = map.distance(latlngs[i], latlngs[i + 1]);
      segs.push(d);
      total += d;
    }
    let prog = 0;
    const SPEED = total / 12000; // 全程约 12 秒
    let last = performance.now();
    const step = (now: number) => {
      prog = (prog + (now - last) * SPEED) % total;
      last = now;
      let acc = 0;
      for (let i = 0; i < segs.length; i++) {
        if (prog <= acc + segs[i]) {
          const t = (prog - acc) / segs[i];
          const [a, b] = [latlngs[i], latlngs[i + 1]];
          marcher.setLatLng([a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]);
          break;
        }
        acc += segs[i];
      }
    };
    marchTimerRef.current = window.setInterval(() => step(performance.now()), 40);

    return () => {
      window.clearInterval(marchTimerRef.current);
      marchTimerRef.current = 0;
    };
  }, [routeEvent, cv]);

  /* ---------- 动态效果层：pulse / region ---------- */
  useEffect(() => {
    const map = mapRef.current;
    const layer = fxLayerRef.current;
    if (!map || !layer) return;
    layer.clearLayers();
    if (pulsePoint) {
      const [gla, glo] = cv(pulsePoint.lat, pulsePoint.lon);
      const icon = L.divIcon({
        className: "",
        html: `<div class="pulse-wrap"><i class="pulse-ring"></i><i class="pulse-ring d2"></i><i class="pulse-core"></i>${pulsePoint.label ? `<b class="pulse-label">${pulsePoint.label}</b>` : ""}</div>`,
        iconSize: [0, 0],
      });
      L.marker([gla, glo], { icon, interactive: false, zIndexOffset: 800 }).addTo(layer);
    }
    if (region) {
      const [gla, glo] = cv(region.lat, region.lon);
      L.circle([gla, glo], {
        radius: region.km * 1000,
        color: "#7e22ce", weight: 2, dashArray: "6 6",
        fillColor: "#7e22ce", fillOpacity: 0.08,
        className: "region-circle",
      }).addTo(layer);
      if (region.label) {
        const icon = L.divIcon({
          className: "",
          html: `<div class="region-label">${region.label}</div>`,
          iconSize: [0, 0],
        });
        // 标签放圈北缘：纬度每度约 111km
        const dLat = region.km / 111;
        L.marker([Math.min(gla + dLat, 85), glo], { icon, interactive: false, zIndexOffset: 810 }).addTo(layer);
      }
    }
  }, [pulsePoint, region, cv]);

  return (
    <div className="map-wrap">
      <div className="map-host" ref={hostRef} />
      <div className="base-switch">
        {(Object.keys(BASES) as BaseStyle[]).map((k) => (
          <button key={k} className={baseStyle === k ? "on" : ""} onClick={() => setBaseStyle(k)}>
            {BASES[k].name}
          </button>
        ))}
      </div>
    </div>
  );
}
