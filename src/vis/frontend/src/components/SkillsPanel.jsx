import { useCallback, useEffect, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { BookMarked, Check, Pencil, RefreshCw, Sparkles, X } from "lucide-react";

// Skills 栏：接管地图区——左侧凝练出的 skill 选项卡，右侧 SKILL.md
// Markdown 渲染，可人工编辑并保存回写 harness/agent/skills/<name>/SKILL.md。
export default function SkillsPanel({ readOnly }) {
  const [skills, setSkills] = useState([]);
  const [selected, setSelected] = useState(null);
  const [content, setContent] = useState("");
  const [draft, setDraft] = useState("");
  const [editing, setEditing] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [savedTick, setSavedTick] = useState(0);
  const [error, setError] = useState("");
  const [refining, setRefining] = useState(false);
  const [refineNote, setRefineNote] = useState("");

  const loadList = useCallback(async () => {
    try {
      const res = await fetch("/api/skills");
      const data = await res.json();
      setSkills(data.skills || []);
    } catch {
      setError("技能列表加载失败");
    }
  }, []);

  const loadSkill = useCallback(async (name) => {
    setEditing(false);
    setDirty(false);
    setError("");
    try {
      const res = await fetch(`/api/skills/${encodeURIComponent(name)}`);
      const data = await res.json();
      setSelected(name);
      setContent(data.content || "");
      setDraft(data.content || "");
    } catch {
      setError("技能内容加载失败");
    }
  }, []);

  useEffect(() => { loadList(); }, [loadList, savedTick]);
  useEffect(() => {
    if (!selected && skills.length) loadSkill(skills[0].name);
  }, [skills, selected, loadSkill]);

  const refine = async () => {
    setRefining(true);
    setRefineNote("");
    setError("");
    try {
      const res = await fetch("/api/skills/refine", { method: "POST" });
      const data = await res.json();
      const written = data.written || [];
      setRefineNote(
        written.length
          ? `已凝练 ${written.length} 个技能：${written.join("、")}`
          : "本轮没有符合阈值的成功轨迹，技能未变化",
      );
      setSavedTick((v) => v + 1);
      if (selected) loadSkill(selected);
    } catch {
      setError("凝练失败");
    } finally {
      setRefining(false);
    }
  };

  const save = async () => {
    try {
      const res = await fetch(`/api/skills/${encodeURIComponent(selected)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: draft }),
      });
      if (!res.ok) throw new Error(await res.text());
      setContent(draft);
      setEditing(false);
      setDirty(false);
      setSavedTick((v) => v + 1);
    } catch {
      setError("保存失败");
    }
  };

  return (
    <div className="skills-view">
      <aside className="skills-list" aria-label="技能列表">
        <header>
          <BookMarked size={14} />
          <strong>凝练技能</strong>
          <button
            type="button"
            className="icon-btn"
            onClick={refine}
            disabled={refining || readOnly}
            title="立即凝练——对积累的决策证据运行 extractor"
          >
            <Sparkles size={13} />
          </button>
          <button type="button" className="icon-btn" onClick={loadList} title="刷新"><RefreshCw size={13} /></button>
        </header>
        <ul>
          {skills.map((skill) => (
            <li key={skill.name}>
              <button
                type="button"
                className={`skills-tab${selected === skill.name ? " active" : ""}`}
                onClick={() => loadSkill(skill.name)}
              >
                <strong>{skill.name}</strong>
                <small>{skill.description || "（无描述）"}</small>
                <span className="skills-badges">
                  {skill.verified != null ? (
                    <i className={`skills-badge${skill.stale ? " stale" : ""}`}>
                      已验证 {skill.verified} 回合{skill.confidence != null ? ` · 置信 ${skill.confidence}` : ""}
                    </i>
                  ) : null}
                  {skill.conflicted ? <i className="skills-badge warn" title="同局面存在备选策略，需人工裁决">⚠ 待裁决</i> : null}
                  {skill.human_reviewed ? <i className="skills-badge ok" title="人工审定，自动凝练不覆盖正文">人工审定</i> : null}
                </span>
              </button>
            </li>
          ))}
          {!skills.length ? <li className="skills-empty">尚未凝练出技能 —— 成功决策轨迹将由 extractor 生成</li> : null}
        </ul>
        {refineNote ? <p className="skills-refine-note">{refineNote}</p> : null}
      </aside>
      <section className="skills-doc">
        <header>
          <strong>{selected || "技能详情"}</strong>
          <span className="skills-actions">
            {editing ? (
              <>
                <button type="button" className="btn" onClick={save}><Check size={13} /> 保存</button>
                <button type="button" className="btn" onClick={() => { setEditing(false); setDraft(content); setDirty(false); }}><X size={13} /> 取消</button>
              </>
            ) : (
              <button type="button" className="btn" onClick={() => setEditing(true)} disabled={readOnly || !selected}>
                <Pencil size={13} /> 编辑
              </button>
            )}
          </span>
        </header>
        {error ? <p className="panel-error">{error}</p> : null}
        {editing ? (
          <textarea
            className="skills-editor"
            value={draft}
            onChange={(e) => { setDraft(e.target.value); setDirty(true); }}
            spellCheck={false}
          />
        ) : (
          <div className="skills-markdown markdown-body">
            <Markdown remarkPlugins={[remarkGfm]}>{content}</Markdown>
          </div>
        )}
        {dirty ? <small className="skills-dirty-hint">未保存修改</small> : null}
      </section>
    </div>
  );
}
