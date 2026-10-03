import { useEffect, useState } from "react";
import { api } from "../api";
import type { AgentDef, AgentTool } from "../types";

interface Props {
  onChanged?: () => void;
}

export default function AgentManager({ onChanged }: Props) {
  const [agents, setAgents] = useState<AgentDef[]>([]);
  const [tools, setTools] = useState<AgentTool[]>([]);
  const [editing, setEditing] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [err, setErr] = useState("");
  const [confirmId, setConfirmId] = useState<string | null>(null);

  const load = () => {
    api.agents().then(setAgents).catch(() => {});
    api.agentTools().then(setTools).catch(() => {});
  };
  useEffect(load, []);

  const remove = async (a: AgentDef) => {
    if (confirmId !== a.id) {
      setConfirmId(a.id);
      return;
    }
    await api.deleteAgent(a.id);
    setConfirmId(null);
    load();
    onChanged?.();
  };

  return (
    <div>
      <div className="sec-t" style={{ marginTop: 0 }}>
        Agent 注册表（{agents.length}）
      </div>
      <div
        style={{
          fontSize: 12, color: "var(--text-2)", background: "var(--surface-2)",
          border: "1px solid var(--border)", borderRadius: 6, padding: "8px 10px",
          marginBottom: 12, lineHeight: 1.75,
        }}
      >
        内置 <b>领航员</b>（系统操纵）与 <b>太史</b>（历史考据）；可自定义 Agent：
        名称 + 系统提示词 + 工具勾选。读工具自由执行；<b>写工具只能生成确认卡</b>，
        经用户批准后才落地。
      </div>

      {err && <div className="dispute">{err}</div>}

      {agents.map((a) => (
        <div key={a.id} className="agent-card">
          <div className="agent-top">
            <span className="agent-dot" style={{ background: a.color }} />
            <b>{a.name}</b>
            {a.builtin
              ? <span className="lvl lvl-up">内置</span>
              : <span className="lvl">自定义</span>}
            <span className="agent-id">{a.id}</span>
            {!a.builtin && (
              <span className="agent-btns">
                <button className="btn mini" onClick={() => setEditing(editing === a.id ? null : a.id)}>
                  {editing === a.id ? "收起" : "编辑"}
                </button>
                <button className="btn mini danger" onClick={() => remove(a)}>
                  {confirmId === a.id ? "再点确认" : "删除"}
                </button>
              </span>
            )}
          </div>
          <div className="agent-tools">
            {a.tools.map((t) => (
              <span key={t} className={`atool ${tools.find((x) => x.id === t)?.write ? "w" : ""}`}>
                {tools.find((x) => x.id === t)?.label ?? t}
              </span>
            ))}
          </div>
          {!a.builtin && editing === a.id && (
            <AgentEditor
              key={a.id + editing}
              tools={tools}
              agent={a}
              onDone={() => { setEditing(null); setErr(""); load(); onChanged?.(); }}
              onError={setErr}
            />
          )}
        </div>
      ))}

      {creating ? (
        <AgentEditor
          tools={tools}
          onDone={() => { setCreating(false); setErr(""); load(); onChanged?.(); }}
          onError={setErr}
        />
      ) : (
        <button className="btn" style={{ marginTop: 4 }} onClick={() => setCreating(true)}>
          ＋ 新建自定义 Agent
        </button>
      )}
    </div>
  );
}

/* ---------- 新建 / 编辑表单 ---------- */
function AgentEditor({ tools, agent, onDone, onError }: {
  tools: AgentTool[];
  agent?: AgentDef;
  onDone: () => void;
  onError: (s: string) => void;
}) {
  const [name, setName] = useState(agent?.name ?? "");
  const [prompt, setPrompt] = useState(agent?.system_prompt ?? "");
  const [picked, setPicked] = useState<Set<string>>(
    new Set(agent?.tools ?? ["kg_browse", "rag_search"]));
  const [busy, setBusy] = useState(false);

  const toggleTool = (id: string) =>
    setPicked((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });

  const save = async () => {
    if (!name.trim()) {
      onError("Agent 名称不能为空");
      return;
    }
    setBusy(true);
    try {
      if (agent) {
        await api.updateAgent(agent.id, {
          name, system_prompt: prompt, tools: [...picked] });
      } else {
        await api.createAgent(name, prompt, [...picked]);
      }
      onDone();
    } catch (e) {
      onError(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="agent-edit">
      <input
        value={name}
        onChange={(e) => setName(e.target.value)}
        placeholder="Agent 名称（如：水利小史）"
        maxLength={20}
      />
      <textarea
        value={prompt}
        onChange={(e) => setPrompt(e.target.value)}
        placeholder="系统提示词：角色定位、回答纪律、风格要求……"
        rows={6}
        maxLength={4000}
      />
      <div className="tool-grid">
        {tools.map((t) => (
          <label key={t.id} className={`tg-item ${picked.has(t.id) ? "on" : ""}`}>
            <input
              type="checkbox"
              checked={picked.has(t.id)}
              onChange={() => toggleTool(t.id)}
            />
            <span className="tg-label">
              {t.label}
              {t.write && <i className="tg-w">写·需确认</i>}
            </span>
            <span className="tg-desc">{t.desc}</span>
          </label>
        ))}
      </div>
      <div className="agent-edit-btns">
        <button className="btn" disabled={busy} onClick={save}>
          {busy ? "保存中…" : agent ? "保存修改" : "创建"}
        </button>
        <button className="btn gray" onClick={onDone}>取消</button>
      </div>
    </div>
  );
}
