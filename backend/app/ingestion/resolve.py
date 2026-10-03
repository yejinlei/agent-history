# -*- coding: utf-8 -*-
"""实体融合 / 消歧（模式层之外的数据治理纪律）

- normalize_name：统一归一化（NFKC、去空白标点、去常见称谓）；
- 别名索引：人物/地点的 name + aliases 全部建键；
- resolve：归一化唯一命中 → 自动对齐并写 data/merges.json；
- merge_proposals：同名不同 id 的疑似对 → kind="merge" 提案，走人审。
"""
from __future__ import annotations

import json
import re
import threading
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

_DATA = Path(__file__).resolve().parents[1] / "data"
_MERGE_FILE = _DATA / "merges.json"

_PUNCT = re.compile(r"[\s,，。·:：;；!！?？\"'（）()【】\[\]「」『』、\-—_/\\]+")
# 常见身份后缀（不参与匹配）
_TITLE_SUFFIX = re.compile(r"(先生|皇帝|武帝|文帝|公|王|侯|帝)$")


def normalize_name(s: str) -> str:
    s = unicodedata.normalize("NFKC", str(s))
    s = _PUNCT.sub("", s)
    s = _TITLE_SUFFIX.sub("", s)
    return s


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Resolver:
    def __init__(self, store=None) -> None:
        self._store = store
        self.merges: list[dict] = []
        self._lock = threading.RLock()
        if _MERGE_FILE.exists():
            try:
                self.merges = json.loads(
                    _MERGE_FILE.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.merges = []

    def _persist(self) -> None:
        _MERGE_FILE.write_text(
            json.dumps(self.merges, ensure_ascii=False, indent=1),
            encoding="utf-8")

    def _objects(self, kind: str) -> list:
        assert self._store is not None
        return {"person": self._store.people,
                "place": self._store.places}.get(kind, [])

    @staticmethod
    def _keys(obj) -> list[str]:
        keys = [normalize_name(getattr(obj, "name", ""))]
        for a in getattr(obj, "aliases", []) or []:
            n = normalize_name(a)
            if n and n not in keys:
                keys.append(n)
        return [k for k in keys if k]

    def resolve(self, name: str, kind: str) -> dict | None:
        """归一化/别名唯一命中 → 返回 {id,name,...}；多命中或无命中 → None。"""
        n = normalize_name(name)
        if not n:
            return None
        hits: list = []
        for obj in self._objects(kind):
            if n in self._keys(obj):
                hits.append(obj)
        if len(hits) == 1:
            o = hits[0]
            if normalize_name(getattr(o, "name", "")) != n:
                # 经由别名命中：记录一次融合
                self._record(kind, o.id, n)
            return {"id": o.id, "name": getattr(o, "name", n)}
        return None

    def _record(self, kind: str, kept_id: str, merged_name: str) -> None:
        with self._lock:
            if any(m["kind"] == kind and m["kept_id"] == kept_id
                   and m["merged_name"] == merged_name for m in self.merges):
                return
            self.merges.append({"ts": _now(), "kind": kind,
                                "kept_id": kept_id, "merged_name": merged_name})
            self._persist()

    def record_merge(self, kind: str, kept_id: str,
                     merged_id: str, merged_name: str) -> None:
        """人审确认的合并：记录 kept/merged 对。"""
        with self._lock:
            self.merges.append({"ts": _now(), "kind": kind, "kept_id": kept_id,
                                "merged_id": merged_id, "merged_name": merged_name})
            self._persist()

    def merge_proposals(self) -> list[dict]:
        """疑似重复对：同归一化名、不同 id（人物/地点）。"""
        out: list[dict] = []
        for kind in ("person", "place"):
            buckets: dict[str, list] = {}
            for obj in self._objects(kind):
                for k in {normalize_name(getattr(obj, "name", ""))}:
                    if k:
                        buckets.setdefault(k, []).append(obj)
            for key, objs in buckets.items():
                ids = {o.id for o in objs}
                if len(ids) > 1:
                    out.append({"kind": "merge", "entity_kind": kind,
                                "norm_name": key,
                                "candidates": [{"id": o.id,
                                                "name": getattr(o, "name", key)}
                                               for o in objs]})
        return out

    def payload(self) -> dict:
        return {"merges": self.merges,
                "proposals": self.merge_proposals()}
