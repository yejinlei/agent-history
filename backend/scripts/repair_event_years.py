# -*- coding: utf-8 -*-
"""让 LLM 回原文，给「抽到了事件但没抽到年份」的那些补年份。

背景
----
还有约 1.6 万条候选事件没进图，主角就是没年份的事件（明史 6,031 条、
宋史 9,765 条……）。没有年份就不知道该挂在哪一年，挂载时一律不写。
而原始史料里年份几乎总是写着的（年号纪年、干支、"XX年春"），只是首轮抽取
没填出来。

为什么不重跑整块抽取
--------------------
重抽要重新产人物+事件+关系，token 花三倍，还可能把已抽对的改坏。这里只问
一件事：这块里的这几个事件分别发生在哪一年。一块一次调用，覆盖该块全部缺年
事件（宋史 1,236 块就能覆盖 9,765 条事件）。

口径（宁缺毋滥）
----------------
- 只补**原本缺年份**的事件，已有年份的一律不动
- LLM 给不出年份（null）或置信度低于 --min-confidence，就保持缺失
- 年份必须落在该书朝代跨度附近（±150 年）才采信，明显跑偏的直接丢
- 每次改写都在 event_year_repairs 留痕，可按 batch 核账

用法（服务须停止：独占 DuckDB）
------------------------------
    python scripts/repair_event_years.py songshi --dry-run --limit 3
    python scripts/repair_event_years.py songshi --limit 50           # 小批试跑
    python scripts/repair_event_years.py songshi --concurrency 3      # 整本
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config                                       # noqa: E402
from app.ingestion.bookrun import source_path                # noqa: E402
from app.ingestion.extract import _loads_tolerant            # noqa: E402
from app.ingestion.rag import chunk_text                     # noqa: E402
from zhconv import convert as zh_convert                     # noqa: E402

import duckdb                                                # noqa: E402


def _norm(s: str) -> str:
    return zh_convert((s or "").strip(), "zh-cn")

DUCKDB_PATH = Path(config.DATA_DIR) / "warehouse" / "chronosino.duckdb"
EXTRACT_SIZE = 2400          # 与 RunOptions 默认一致，否则对不上 piece_idx
EXTRACT_OVERLAP = 240
MAX_EVENTS_PER_CALL = 24     # 一次问太多，输出 JSON 会被截断

_SYS = "你是中国史纪年判定器。只输出 JSON，不要解释。"
_USER = """下面是《{title}》的一段原文，以及从这段里抽出的若干事件（缺年份）。

请据原文判断每个事件发生的**公元年份**（整数；公元前用负数，如 -221）。

判定步骤（务必按此顺序，别跳步）：
1. 先在原文里找准该事件对应的**年号纪年**，原样抄进 reign 字段（如"太平興國六年"）；
   reign_year 填该年号后的年数（"六年"填 6；元年填 1）。不要自己编造原文没有的年号。
2. 再把年号换成公元年份填进 year。元年 = 该年号起始年；第 N 年 = 起始年 + (N-1)。
   不确定换算就如实降低 confidence，不要猜。

只输出 JSON，不要解释，不要多余文字：
{{"events":[{{"name":"事件名","year":1234,"reign":"太平興國六年","reign_year":6,
"confidence":0.9}}]}}

原文：
{text}

待判定事件：
{items}
"""


def log(msg: str = "") -> None:
    print(msg, flush=True)


def _year_ok(year: int | None, lo: int, hi: int) -> bool:
    """年份要落在这本书的朝代跨度附近，明显跑偏的直接丢。"""
    if not isinstance(year, int):
        return False
    return lo - 150 <= year <= hi + 150


_CN = {"元": 1, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
       "六": 6, "七": 7, "八": 8, "九": 9}


def _cn2int(s: str) -> int | None:
    """'元'→1、'三'→3、'十二'→12、'二十一'→21。"""
    if s in _CN:
        return _CN[s]
    if "十" in s:
        a, _, b = s.partition("十")
        hi = _CN.get(a, 1) if a else 1
        lo = _CN.get(b, 0) if b else 0
        return hi * 10 + lo
    return None


def _annalistic(seg: str) -> bool:
    """像不像编年体段落：有月份、有干支纪日（本纪/实录的典型写法）。"""
    return bool(re.search(r"[一二三四五六七八九十]+月", seg)
                and re.search(r"[甲乙丙丁戊己庚辛壬癸][子丑寅卯辰巳午未申酉戌亥]",
                              seg))


def _piece_offsets(text: str, pieces: list[str]) -> list[int]:
    """chunk_text 是句末对齐的，步长不固定；用顺序查找反推每块的起始偏移。"""
    offs, pos = [], 0
    for p in pieces:
        i = text.find(p, pos)
        if i < 0:
            i = pos
        offs.append(i)
        pos = i + max(1, len(p) - EXTRACT_OVERLAP)
    return offs


def build_era_hits(text: str, eras: dict[str, list[tuple[int, int]]]
                   ) -> list[tuple[int, str, int, int]]:
    """全篇扫一遍年号出现位置：[(位置, 年号, 第几年, 公元年)]。

    《本纪》按年记事，年号只在章节开头写一次，后面整段都归它管。与其让模型
    去猜，不如直接找「本段之前最近出现的那个年号」。
    """
    names = sorted(eras, key=len, reverse=True)
    pat = re.compile("(" + "|".join(re.escape(n) for n in names) +
                     r")([元一二三四五六七八九十]{1,3})?年")
    hits = []
    for m in pat.finditer(text):
        nm = m.group(1)
        n = _cn2int(m.group(2)) if m.group(2) else 1
        if not n:
            continue
        # 年号可能重名：挑起始年最接近上下文的那条（这里先取第一条，
        # 年份在 era_year 里按同段其它判定再校正）
        fy, ly = eras[nm][0]
        y = fy + n - 1
        if fy <= y <= ly:
            hits.append((m.start(), nm, n, y))
    return hits


def chronicle_year(offs: list[int], hits: list[tuple[int, str, int, int]],
                   pi: int, max_back: int = 3000
                   ) -> tuple[int, str, str] | None:
    """本块之前最近的一个年号 → 该块的纪年（仅用于编年体段落）。"""
    import bisect
    if pi >= len(offs):
        return None
    limit = offs[pi] + 400          # 年号可能就写在本块开头
    j = bisect.bisect_right([h[0] for h in hits], limit) - 1
    if j < 0:
        return None
    pos, nm, n, y = hits[j]
    if limit - pos > max_back:      # 太远就不可信（可能已是上一卷/上一朝）
        return None
    return y, nm, f"{nm}{n}年"


def load_eras() -> dict[str, list[tuple[int, int]]]:
    """CBDB NIAN_HAO：年号 → [(起始年, 终止年)]（同年号可能跨朝代重名）。"""
    import sqlite3
    con = sqlite3.connect(str(config.DATA_DIR / "core" / "raw" / "cbdb"
                              / "CBDB_20240208_sqlite.db"), timeout=30)
    out: dict[str, list[tuple[int, int]]] = {}
    try:
        for nm, fy, ly in con.execute(
                "SELECT c_nianhao_chn, c_firstyear, c_lastyear FROM NIAN_HAO"):
            if not nm or fy is None:
                continue
            out.setdefault(nm.strip(), []).append((int(fy), int(ly or fy)))
    finally:
        con.close()
    return out


def era_year(reign: str, reign_year: int | None, llm_year: int,
             eras: dict[str, list[tuple[int, int]]]
             ) -> tuple[int | None, str]:
    """用年号表把「太平興國六年」换算成公元年；查不到才退回 LLM 的数。

    抽查见过 LLM 把太平興國六年算成 983（实为 981）：年号转公元它会在个别
    年号上系统性偏 2-3 年，但它找年号本身很准。所以换算这部分交给权威表。
    """
    m = re.match(r"^(.+?)([元一二三四五六七八九十]+)年$", (reign or "").strip())
    base = m.group(1) if m else (reign or "").strip()
    n = _cn2int(m.group(2)) if m else reign_year
    if base in eras and n:
        # 年号可能重名（如「建元」），挑起始年与 LLM 结果最接近的那一条
        pick = min(eras[base],
                   key=lambda c: abs(c[0] + n - 1 - (llm_year or c[0])))
        y = pick[0] + n - 1
        if pick[0] <= y <= pick[1]:
            return y, ("era" if y != llm_year else "era_agree")
    return llm_year, "llm"


def ensure_table(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("""CREATE TABLE IF NOT EXISTS event_year_repairs (
            book_id VARCHAR, piece_idx INTEGER, name VARCHAR,
            year INTEGER, llm_year INTEGER, confidence DOUBLE,
            reign VARCHAR, how VARCHAR, batch VARCHAR, ts TIMESTAMP)""")


def run_all(a) -> int:
    """按缺口从大到小，逐本跑完（DuckDB 独占写锁，必须串行）。

    不另起子进程：父进程握着连接时子进程会撞上「File is already open」。
    """
    con = duckdb.connect(str(DUCKDB_PATH))
    cnt: Counter = Counter()
    for bid, payload in con.execute(
            "SELECT book_id, payload FROM extract_raw").fetchall():
        try:
            data = json.loads(payload)
        except Exception:
            continue
        cnt[bid] += sum(1 for e in (data.get("events") or [])
                        if not e.get("year"))
    con.close()
    books = [b for b, n in cnt.most_common() if n]
    log(f"待补 {len(books)} 本，缺年事件合计 {sum(cnt.values()):,}")
    for b in books:
        log(f"\n########## 《{b}》缺年 {cnt[b]:,} 条 ##########")
        rc = repair_book(b, a)
        if rc != 0:
            log(f"[中断] 《{b}》返回 {rc}")
            return rc
    log("\n全部跑完。下一步：逐本 --finalize-only 后重新挂载。")
    return 0


def repair_book(bid: str, a) -> int:
    path = source_path(bid)
    if not path or not path.exists():
        log(f"找不到书源文件：{bid}")
        return 1
    text = path.read_text(encoding="utf-8")
    big = chunk_text(text, size=EXTRACT_SIZE, overlap=EXTRACT_OVERLAP)
    log(f"《{bid}》抽取块共 {len(big)}")
    offs = _piece_offsets(text, big)

    con = duckdb.connect(str(DUCKDB_PATH))          # 读写：改原始抽取结果
    ensure_table(con)
    rows = con.execute(
        "SELECT piece_idx, model, payload FROM extract_raw "
        "WHERE book_id=? ORDER BY piece_idx", [bid]).fetchall()
    # 收集缺年事件（按块聚合）
    plan: list[tuple[int, str, dict]] = []          # (piece_idx, model, payload)
    n_noyear = 0
    for pi, model, payload in rows:
        try:
            data = json.loads(payload)
        except Exception:
            continue
        miss = [e for e in (data.get("events") or []) if not e.get("year")]
        if miss:
            n_noyear += len(miss)
            plan.append((pi, model, data))
    log(f"缺年份事件 {n_noyear:,} 条，分布在 {len(plan):,} 块")

    done_pieces = set()
    if not a.force:
        done_pieces = {r[0] for r in con.execute(
            "SELECT DISTINCT piece_idx FROM event_year_repairs "
            "WHERE book_id=?", [bid]).fetchall()}
    todo = [p for p in plan if p[0] not in done_pieces]
    if a.limit:
        todo = todo[:a.limit]
    log(f"待补 {len(todo):,} 块（已补过 {len(done_pieces):,} 块，幂等跳过）")
    if not todo:
        con.close()
        return 0

    eras = load_eras()
    hits = build_era_hits(text, eras)
    log(f"年号表 {len(eras)} 个（CBDB NIAN_HAO），全篇命中 {len(hits):,} 处")

    from app.llm import get_extract_llm
    llm = get_extract_llm()
    batch = datetime.now().strftime("%Y%m%d-%H%M%S")
    log(f"模型 {getattr(llm, 'name', '?')}  并发 {a.concurrency}")

    result = {"filled": 0, "pieces": 0, "calls": 0, "fail": 0,
              "agree": 0, "disagree": 0}

    def work(item: tuple[int, str, dict]) -> None:
        pi, model, data = item
        miss = [e for e in (data.get("events") or []) if not e.get("year")]
        if not miss or pi >= len(big):
            return
        piece_text = big[pi]
        # 《本纪》是逐日记事，年号常写在**上一块**的开头（本块只剩「二月癸丑朔」
        # 这类干支日）。校验窗口必须带上前后各一块，否则会整块误驳。
        window = "".join(big[max(0, pi - 1):min(len(big), pi + 2)])
        piece_norm = _norm(window)
        got: dict[str, tuple[int, float, str]] = {}
        by_reign: dict[str, set[int]] = {}
        n_bad_reign = 0
        for start in range(0, len(miss), MAX_EVENTS_PER_CALL):
            group = miss[start:start + MAX_EVENTS_PER_CALL]
            items = "\n".join(
                "- %s｜%s" % (e.get("name") or "",
                              (e.get("summary") or "")[:60])
                for e in group)
            # 《本纪》是逐日记事，年号只写在章节开头，本块里往往只剩
            # 「二月癸丑朔」这类干支日。把前一块一并给过去，模型才看得见年号
            # ——只给原文、不给年份，换算仍然由年号表做。
            ctx = (big[pi - 1] + "\n" if pi > 0 else "") + piece_text
            prompt = _USER.format(title=a.book_id, text=ctx, items=items)
            try:
                raw = llm.chat(_SYS, prompt,
                               max_tokens=config.LLM_EXTRACT_MAX_TOKENS)
                result["calls"] += 1
                if a.debug:
                    log(f"  [debug] 块 {pi} 原始返回：{str(raw)[:700]}")
                d = _loads_tolerant(raw) or {}
                for ev in (d.get("events") or []):
                    # LLM 常把「事件名｜摘要」整个塞进 name（我们的 items 就是
                    # 这么给的），取分隔符前的部分才是事件名
                    nm = re.split(r"[|｜]", (ev.get("name") or ""))[0].strip()
                    yr = ev.get("year")
                    if isinstance(yr, str) and yr.lstrip("-").isdigit():
                        yr = int(yr)
                    if not nm or not isinstance(yr, int):
                        continue
                    cf = ev.get("confidence")
                    cf = float(cf) if isinstance(cf, (int, float)) else 0.0
                    if cf < a.min_confidence:
                        continue
                    # 年号必须真的在原文里出现过：抽查时撞见过 LLM 把
                    # 「太平興國六年」在依据里写成「七年」、年份也跟着错两年的情况
                    reign = (ev.get("reign") or "").strip()
                    if reign and _norm(reign) not in piece_norm:
                        n_bad_reign += 1
                        continue
                    y_final, how = era_year(reign, ev.get("reign_year"), yr,
                                            eras)
                    if y_final is None:
                        continue
                    got[_norm(nm)] = (y_final, cf, reign, how, yr)
                    if reign:
                        by_reign.setdefault(reign, set()).add(y_final)
            except Exception as e:
                result["fail"] += 1
                log(f"  块 {pi} 调用失败：{type(e).__name__}: {str(e)[:60]}")
        # 同一年号被换算成好几个年份（相差 >1）→ 这一组整体不采信
        bad = {r for r, ys in by_reign.items()
               if len(ys) > 1 and (max(ys) - min(ys)) > 1}
        if bad:
            got = {k: v for k, v in got.items() if v[2] not in bad}

        # ---- 编年体兜底 ----
        # 《本纪》按年记事，年号只在章节开头写一次。模型看不到就只能瞎编
        # （抽查撞见过「慶元七年」这种根本不存在的年号）。这里按原文往前找
        # 最近的一个年号，换算同样走年号表；顺手跟模型结果对账，看看一致率。
        cy = chronicle_year(offs, hits, pi)
        if cy and _annalistic(piece_text):
            c_year, c_reign, c_label = cy
            llm_ys = [v[0] for v in got.values()]
            if llm_ys:
                modal = Counter(llm_ys).most_common(1)[0][0]
                if abs(modal - c_year) <= 1:
                    result["agree"] += 1
                else:
                    result["disagree"] += 1
                    log(f"  块 {pi}: 年号扫描 {c_year} vs 模型 {modal} 不一致"
                        f"（{c_label}）")
            for e in (data.get("events") or []):
                nm = (e.get("name") or "").strip()
                if e.get("year") or _norm(nm) in got:
                    continue
                got[_norm(nm)] = (c_year, 0.6, c_label, "chronicle", None)

        if not got:
            if n_bad_reign or bad:
                log(f"  块 {pi}: 全部驳回（年号不在原文 {n_bad_reign} 条、"
                    f"同号多解 {len(bad)} 组）")
            return
        filled_here = 0
        for e in (data.get("events") or []):
            nm = (e.get("name") or "").strip()
            if e.get("year") or _norm(nm) not in got:
                continue
            yr, cf, evid, how, llm_yr = got[_norm(nm)]
            e["year"] = yr
            e["year_source"] = "llm_repair"
            con.execute(
                "INSERT INTO event_year_repairs VALUES (?,?,?,?,?,?,?,?,?,?)",
                [bid, pi, nm, yr, llm_yr, cf, evid, how, batch,
                 datetime.now()])
            filled_here += 1
        if filled_here and not a.dry_run:
            con.execute(
                "UPDATE extract_raw SET payload=? "
                "WHERE book_id=? AND piece_idx=? AND model=?",
                [json.dumps(data, ensure_ascii=False), bid, pi, model])
        result["filled"] += filled_here
        result["pieces"] += 1
        log(f"  块 {pi}: 补上 {filled_here} 条年份")

    with ThreadPoolExecutor(max_workers=a.concurrency) as ex:
        list(ex.map(work, todo))

    log(f"\n✓ 完成：处理 {result['pieces']:,} 块 / {result['calls']:,} 次调用，"
        f"补上 {result['filled']:,} 条年份（失败 {result['fail']} 次）"
        f"{'（干跑，未写库）' if a.dry_run else ''}")
    if result["agree"] or result["disagree"]:
        tot = result["agree"] + result["disagree"]
        log(f"  对账：年号扫描与模型判定一致 {result['agree']}/{tot} "
            f"（{100 * result['agree'] / tot:.0f}%）")
    if not a.dry_run:
        log("  下一步：ingest_one.py <book> --finalize-only --max-events 200000"
            " --out <path> → mount_proposals_direct.py <book> --artifact <path>")
    con.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("book_id", help="书 ID；传 ALL 则按缺口从大到小跑完所有书")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少块")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--min-confidence", type=float, default=0.5)
    ap.add_argument("--dry-run", action="store_true", help="只问不写")
    ap.add_argument("--force", action="store_true", help="连已补过的块也重跑")
    ap.add_argument("--debug", action="store_true",
                    help="打印原始返回，用于排查提示词/解析")
    a = ap.parse_args()
    if a.book_id.upper() == "ALL":
        return run_all(a)
    return repair_book(a.book_id, a)


if __name__ == "__main__":
    sys.exit(main())
