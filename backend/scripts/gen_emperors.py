# -*- coding: utf-8 -*-
"""用 LLM 补齐 5 个政权的帝王世系缺口，覆盖写入 emperors.json 对应键（其他朝代不动）。

用法: python backend/scripts/gen_emperors.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import time

import httpx

BACKEND = Path(__file__).resolve().parents[1]
EMP_JSON = BACKEND / "app" / "data" / "emperors.json"

API_URL = "https://token.sensenova.cn/v1/chat/completions"
API_KEY = "sk-aFHCHZsKiHbNyQYhZqXoRRHyqz1figmY"
MODEL = "sensenova-6.8-flash-lite"

# 锚定名单（史记/正史通行世系），LLM 只负责配年份，避免名单幻觉与推理失控
ANCHORS: dict[str, tuple[str, list[str], str]] = {
    "xia": ("夏朝（约前2070—前1600，夏商周断代工程通行纪年）",
            ["禹", "启", "太康", "仲康", "相", "少康", "杼", "槐", "芒", "泄",
             "不降", "扃", "廑", "孔甲", "皋", "发", "桀"], "公元前用负整数"),
    "shang": ("商朝（约前1600—前1046，夏商周断代工程通行纪年）",
              ["汤", "外丙", "仲壬", "太甲", "沃丁", "太庚", "小甲", "雍己", "太戊", "仲丁",
               "外壬", "河亶甲", "祖乙", "祖辛", "沃甲", "祖丁", "南庚", "阳甲", "盘庚", "小辛",
               "小乙", "武丁", "祖庚", "祖甲", "廪辛", "康丁", "武乙", "文丁", "帝乙", "帝辛"], "公元前用负整数"),
    "xiongnu": ("匈奴单于世系（前209年冒顿建国—93年北匈奴西迁；含郅支与呼韩邪并立时期）",
                ["冒顿单于", "老上单于", "军臣单于", "伊稚斜单于", "乌维单于", "乌师庐单于（儿单于）",
                 "呴犁湖单于", "且鞮侯单于", "狐鹿姑单于", "壶衍鞮单于", "虚闾权渠单于", "握衍朐鞮单于",
                 "呼韩邪单于", "郅支单于", "复株累单于", "搜谐单于", "车牙单于", "乌珠留单于",
                 "乌累单于", "呼都而尸道皋单于（舆）", "乌达鞮侯单于", "蒲奴单于", "优留单于", "北单于"],
                "公元前用负整数，郅支与呼韩邪并立时段分别取各自在位年"),
    "beiyuan": ("北元蒙古大汗世系（1368年元廷退居漠北—1635年降后金，不含元惠宗）",
                ["元昭宗 爱猷识理达腊", "天元帝 脱古思帖木儿", "也速迭儿（卓里克图汗）", "额勒伯克汗",
                 "坤帖木儿汗", "鬼力赤", "本雅失里", "阿台汗", "脱脱不花", "也先",
                 "乌珂克图汗（马可古儿吉思）", "摩伦汗", "满都鲁", "达延汗（巴图蒙克）", "博迪汗",
                 "打来孙汗", "图们汗（扎萨克图）", "布延汗（彻辰）", "林丹汗"], "公元正整数"),
    "qing": ("清朝（1616年努尔哈赤建后金—1912年宣统退位）",
             ["清太祖 努尔哈赤", "清太宗 皇太极", "清世祖 顺治", "清圣祖 康熙", "清世宗 雍正",
              "清高宗 乾隆", "清仁宗 嘉庆", "清宣宗 道光", "清文宗 咸丰", "清穆宗 同治",
              "清德宗 光绪", "清宣统 溥仪"], "公元正整数；后金时期从1616年起算"),
}

PROMPT = """{title}的全部统治者共{n}位，依次为：{names}。

请为每位给出即位年 s 与退位/卒年 e（整数公元纪年；{note}；年份有争议时取史学界通行说法，史书无载的按通行估计给出）。
直接输出紧凑 JSON 数组，共 {n} 项，每项 {{"n":"称号","s":年,"e":年}}，n 必须使用我给的称号原文。
不要输出推理过程，不要任何解释或 markdown 代码块。"""

# few-shot 示例：让推理模型跳过反复求证、直接输出（对争议纪年防推理 token 耗尽）
_FEWSHOT_USER = (
    "某虚构政权连续3位君主：甲一、乙二、丙三。给每位即位年s与退位年e"
    "（整数，前一位的e即后一位的s+1，整体落在100~200）。直接输出紧凑 JSON 数组（3项），不要解释。"
)
_FEWSHOT_ASSISTANT = '[{"n":"甲一","s":100,"e":139},{"n":"乙二","s":140,"e":166},{"n":"丙三","s":167,"e":200}]'


def build_prompt(dyn_id: str) -> tuple[str, int]:
    title, names, note = ANCHORS[dyn_id]
    return PROMPT.format(title=title, n=len(names), names="、".join(names), note=note), len(names)


def build_chunk_prompt(dyn_id: str, names: list[str]) -> str:
    """分块提示（大块名单推理 token 超限时分用）：只给一段名单，配年份。"""
    title, _, note = ANCHORS[dyn_id]
    return PROMPT.format(title=title, n=len(names), names="、".join(names), note=note)


def call_llm(client: httpx.Client, prompt: str, expect_n: int) -> list[dict]:
    # 流式请求：长生成时 SSE 持续吐 token，避免读超时（非流式要等全部生成完才返回）
    chunks: list[str] = []
    with client.stream(
        "POST",
        API_URL,
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={
            "model": MODEL,
            "messages": [
                {"role": "user", "content": _FEWSHOT_USER},
                {"role": "assistant", "content": _FEWSHOT_ASSISTANT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "stream": True,
            "max_tokens": 8192,  # 该模型 reasoning 占完成额度，须留足空间否则 content 为空
        },
        timeout=httpx.Timeout(connect=30.0, read=180.0, write=60.0, pool=30.0),
    ) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                delta = json.loads(payload)["choices"][0].get("delta") or {}
            except (json.JSONDecodeError, KeyError, IndexError):
                continue
            piece = delta.get("content")
            if piece:
                chunks.append(piece)
    content = "".join(chunks)
    m = re.search(r"\[.*\]", content, re.S)
    if not m:
        raise ValueError(f"响应中未找到 JSON 数组: {content[:200]!r}")
    data = json.loads(m.group(0))
    assert isinstance(data, list) and all(isinstance(x, dict) for x in data)
    # 规范字段：只保留 n/s/e（若模型给了 era/es 也保留）
    out = []
    for x in data:
        item = {"n": str(x["n"]), "s": int(x["s"]), "e": int(x["e"])}
        for k in ("era", "es"):
            if k in x:
                item[k] = x[k]
        out.append(item)
    if len(out) != expect_n:
        raise ValueError(f"数量为 {len(out)}，应为 {expect_n}")
    return out


def gen_dynasty(client: httpx.Client, dyn_id: str) -> list[dict]:
    """单朝生成：名单 ≤20 位一次调用，否则分块拼接；每块最多重试 3 次。"""
    _, names, _ = ANCHORS[dyn_id]
    blocks: list[tuple[str, int]] = []
    if len(names) <= 20:
        prompt, n = build_prompt(dyn_id)
        blocks.append((prompt, n))
    else:
        # 名单越长推理 token 越多，按小块切分（商后期王年纪年争议大，须更小块）
        size = {"shang": 10, "xiongnu": 12}.get(dyn_id, 12)
        for i in range(0, len(names), size):
            part = names[i:i + size]
            blocks.append((build_chunk_prompt(dyn_id, part), len(part)))
    results: list[dict] = []
    for bi, (prompt, expect_n) in enumerate(blocks):
        last_err: Exception | None = None
        got: list[dict] | None = None
        for attempt in (1, 2, 3, 4, 5):
            try:
                got = call_llm(client, prompt, expect_n)
                break
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                print(f"[{dyn_id}#块{bi + 1}] 第 {attempt} 次调用失败: {exc}", file=sys.stderr)
                time.sleep(60 * attempt)  # 429 限流退避（等 TPM 窗口重置）
        if got is None:
            raise SystemExit(f"[{dyn_id}#块{bi + 1}] 重试后仍失败: {last_err}")
        results.extend(got)
        if len(blocks) > 1 and bi < len(blocks) - 1:
            time.sleep(65)  # 块间限流退避
    if len(results) != len(names):
        raise SystemExit(f"[{dyn_id}] 拼接后数量 {len(results)} != {len(names)}")
    return results


def main() -> None:
    emp: dict[str, list[dict]] = json.loads(EMP_JSON.read_text(encoding="utf-8"))
    targets = {k: v for k, v in ANCHORS.items() if not sys.argv[1:] or k in sys.argv[1:]}
    with httpx.Client(trust_env=True) as client:
        for dyn_id in targets:
            result = gen_dynasty(client, dyn_id)
            old = len(emp.get(dyn_id, []))
            emp[dyn_id] = result
            print(f"[{dyn_id}] {old} -> {len(result)}")
            # 每个朝代成功后立即落盘，避免中途失败丢失进度
            EMP_JSON.write_text(json.dumps(emp, ensure_ascii=False, indent=1), encoding="utf-8")
            time.sleep(65)  # 降低限流概率（TPM 窗口约 1 分钟）
    EMP_JSON.write_text(json.dumps(emp, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已写回 {EMP_JSON}，总帝王数 {sum(len(v) for v in emp.values())}")


if __name__ == "__main__":
    sys.exit(main())
