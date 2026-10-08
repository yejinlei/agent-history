import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import type { Claim, ClaimConflict, EntityKind } from "../types";

/** 立场色：一眼分清「谁赞成、谁反对、谁只是评论」 */
const STANCE_COLOR: Record<string, string> = {
  肯定: "#2f8f4e",
  否定: "#c0392b",
  存疑: "#b45309",
  调和: "#1d4ed8",
  评论: "#6b7280",
};

// 只有这三类能被 /api/entity/{kind}/{id} 打开；concept 等尚未接入，回落 event
const KIND_OF: Record<string, EntityKind> = {
  event: "event",
  person: "person",
  place: "place",
};

interface Props {
  selectById: (k: EntityKind, id: string) => void;
}

/**
 * 多视角：把各方对同一对象的主张**并列**出来，不裁决谁对。
 * 「分歧」页只列互斥立场（肯定 vs 否定），「全部」页可筛任一立场。
 */
export default function ClaimsView({ selectById }: Props) {
  const [tab, setTab] = useState<"conflict" | "all">("conflict");
  const [conflicts, setConflicts] = useState<ClaimConflict[]>([]);
  const [claims, setClaims] = useState<Claim[]>([]);
  const [stance, setStance] = useState<string>("");
  const [err, setErr] = useState<string>("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    setErr("");
    try {
      const [c, a] = await Promise.all([
        api.claimConflicts(50),
        api.claims(undefined, undefined, 300),
      ]);
      setConflicts(c.items);
      setClaims(a.items);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const filtered = useMemo(
    () => (stance ? claims.filter((c) => c.stance === stance) : claims),
    [claims, stance],
  );
  const stances = useMemo(
    () => Array.from(new Set(claims.map((c) => c.stance))).sort(),
    [claims],
  );

  if (err) {
    return <div className="dock-empty">加载失败：{err}</div>;
  }

  return (
    <div style={{ padding: "10px 12px" }}>
      <div style={{ display: "flex", gap: 6, marginBottom: 10 }}>
        <button
          className={tab === "conflict" ? "on" : ""}
          onClick={() => setTab("conflict")}
          title="同一对象上出现互斥立场的主张组"
        >
          分歧 {conflicts.length}
        </button>
        <button
          className={tab === "all" ? "on" : ""}
          onClick={() => setTab("all")}
          title="全部主张"
        >
          全部 {claims.length}
        </button>
        <button onClick={() => void load()} disabled={busy} title="重新加载">
          {busy ? "…" : "⟳"}
        </button>
      </div>

      {tab === "conflict" && (
        <>
          {conflicts.length === 0 && (
            <div className="dock-empty">
              当前图里还没有互斥立场。分歧来自「评论/异议/主张X」这类边
              （占全部边 56% 的「书载观点」只是出处标注，不构成主张）。
            </div>
          )}
          {conflicts.map((g) => (
            <div
              key={g.about_id}
              style={{
                border: "1px solid #d8d2c4",
                borderRadius: 6,
                padding: "8px 10px",
                marginBottom: 10,
                background: "#fffdf7",
              }}
            >
              <div
                style={{ fontWeight: 600, cursor: "pointer", marginBottom: 6 }}
                onClick={() =>
                  selectById(KIND_OF[g.claims[0]?.about_kind] ?? "event", g.about_id)
                }
                title="查看该对象"
              >
                ⚖ {g.about_name}
              </div>
              <div style={{ fontSize: 12, color: "#8a7f6b", marginBottom: 6 }}>
                立场：{g.stances.join(" / ")}｜{g.claims.length} 条主张
              </div>
              {g.claims.map((c) => (
                <ClaimRow key={c.id} c={c} selectById={selectById} />
              ))}
            </div>
          ))}
        </>
      )}

      {tab === "all" && (
        <>
          <div style={{ display: "flex", gap: 4, flexWrap: "wrap", marginBottom: 8 }}>
            <button className={stance === "" ? "on" : ""} onClick={() => setStance("")}>
              全部
            </button>
            {stances.map((s) => (
              <button
                key={s}
                className={stance === s ? "on" : ""}
                onClick={() => setStance(s)}
                style={{ color: STANCE_COLOR[s] }}
              >
                {s}
              </button>
            ))}
          </div>
          {filtered.map((c) => (
            <ClaimRow key={c.id} c={c} selectById={selectById} />
          ))}
          {filtered.length === 0 && <div className="dock-empty">无主张</div>}
        </>
      )}
    </div>
  );
}

function ClaimRow({ c, selectById }: { c: Claim; selectById: Props["selectById"] }) {
  const color = STANCE_COLOR[c.stance] ?? "#6b7280";
  return (
    <div
      style={{
        borderLeft: `3px solid ${color}`,
        padding: "4px 8px",
        marginBottom: 6,
        background: "#faf8f2",
        borderRadius: 3,
      }}
    >
      <div style={{ fontSize: 13 }}>
        <span style={{ color, fontWeight: 600 }}>[{c.stance}]</span>{" "}
        <span
          style={{ cursor: "pointer" }}
          onClick={() => selectById(KIND_OF[c.about_kind] ?? "event", c.about_id)}
          title="查看主张对象"
        >
          {c.about_name}
        </span>
      </div>
      <div style={{ fontSize: 12, color: "#6b6252" }}>
        {c.claimant_name || c.claimant_id}
        {c.from_book ? `（${c.from_book}）` : ""}
        {c.kind ? ` · ${c.kind}` : ""}
        {c.year ? ` · ${c.year} 年` : ""}
      </div>
      {c.text && (
        <div style={{ fontSize: 12, marginTop: 3, color: "#4b453a" }}>{c.text}</div>
      )}
    </div>
  );
}
