export const YEAR_MIN = -2200;
export const YEAR_MAX = 1912;

export function yearPct(y: number): number {
  return ((y - YEAR_MIN) / (YEAR_MAX - YEAR_MIN)) * 100;
}

export function fmtYear(y: number): string {
  return y < 0 ? `公元前${-y} 年` : `公元 ${y} 年`;
}

export function ganzhi(y: number): string {
  const astro = y < 0 ? y + 1 : y;
  const n = ((((astro - 4) % 60) + 60) % 60) as number;
  const g = "甲乙丙丁戊己庚辛壬癸";
  const z = "子丑寅卯辰巳午未申酉戌亥";
  return g[n % 10] + z[n % 12];
}

export function cnNum(n: number): string {
  if (n > 40) return String(n);
  const d = ["", "一", "二", "三", "四", "五", "六", "七", "八", "九"];
  if (n < 10) return d[n];
  if (n === 10) return "十";
  if (n < 20) return "十" + d[n - 10];
  const t = Math.floor(n / 10);
  const o = n % 10;
  return d[t] + "十" + (o ? d[o] : "");
}

export function emperorOf(dynasty: { emperors: { n: string; s: number; e: number; era?: string; es?: number }[] }, year: number): string {
  for (const e of dynasty.emperors) {
    if (year >= e.s && year <= e.e) {
      let t = "在位：" + e.n;
      if (e.era) {
        const n = year - (e.es ?? e.s) + 1;
        t += " · " + e.era + (n > 0 ? cnNum(n) + "年" : "");
      }
      return t;
    }
  }
  return "";
}

/* ---------- WGS84 → GCJ-02（高德底图坐标系适配，境外不转换） ---------- */
const GCJ_A = 6378245.0;
const GCJ_EE = 0.00669342162296594323;

function _outOfChina(lon: number, lat: number): boolean {
  return lon < 72.004 || lon > 137.8347 || lat < 0.8293 || lat > 55.8271;
}
function _tLat(x: number, y: number): number {
  let r = -100 + 2 * x + 3 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * Math.sqrt(Math.abs(x));
  r += (20 * Math.sin(6 * x * Math.PI) + 20 * Math.sin(2 * x * Math.PI)) * 2 / 3;
  r += (20 * Math.sin(y * Math.PI) + 40 * Math.sin(y / 3 * Math.PI)) * 2 / 3;
  r += (160 * Math.sin(y / 12 * Math.PI) + 320 * Math.sin(y * Math.PI / 30)) * 2 / 3;
  return r;
}
function _tLon(x: number, y: number): number {
  let r = 300 + x + 2 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * Math.sqrt(Math.abs(x));
  r += (20 * Math.sin(6 * x * Math.PI) + 20 * Math.sin(2 * x * Math.PI)) * 2 / 3;
  r += (20 * Math.sin(x * Math.PI) + 40 * Math.sin(x / 3 * Math.PI)) * 2 / 3;
  r += (150 * Math.sin(x / 12 * Math.PI) + 300 * Math.sin(x / 30 * Math.PI)) * 2 / 3;
  return r;
}
/** WGS84 → GCJ-02；境外坐标原样返回 */
export function wgs2gcj(lon: number, lat: number): [number, number] {
  if (_outOfChina(lon, lat)) return [lon, lat];
  let dLat = _tLat(lon - 105, lat - 35);
  let dLon = _tLon(lon - 105, lat - 35);
  const radLat = lat / 180 * Math.PI;
  let magic = Math.sin(radLat);
  magic = 1 - GCJ_EE * magic * magic;
  const sqrtMagic = Math.sqrt(magic);
  dLat = dLat * 180 / (GCJ_A * (1 - GCJ_EE) / (magic * sqrtMagic) * Math.PI);
  dLon = dLon * 180 / (GCJ_A / sqrtMagic * Math.cos(radLat) * Math.PI);
  return [lon + dLon, lat + dLat];
}
