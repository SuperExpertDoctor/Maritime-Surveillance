import { useEffect, useState } from "react";
import { X } from "lucide-react";

// Skills 自演进运行时设置（菜单栏 设置 → Skills 自演进…）。
// 走 /api/skills/settings，改即生效不重启仿真；后端持久化到
// outputs/skills_evidence/settings.json。
export default function SkillSettingsDialog({ open, onClose }) {
  const [settings, setSettings] = useState(null);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!open) return;
    setError("");
    fetch("/api/skills/settings")
      .then((res) => res.ok ? res.json() : Promise.reject(new Error()))
      .then(setSettings)
      .catch(() => setError("skills 自演进未启用（需 PI AGENT 在线）"));
  }, [open]);

  if (!open) return null;

  const patch = async (changes) => {
    setSaving(true);
    setError("");
    try {
      const res = await fetch("/api/skills/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(changes),
      });
      if (!res.ok) throw new Error();
      setSettings(await res.json());
    } catch {
      setError("保存失败");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="settings-overlay" onClick={onClose}>
      <div className="settings-dialog skills-settings-dialog" onClick={(e) => e.stopPropagation()}>
        <div className="settings-head">
          <div>
            <h2>Skills 自演进</h2>
            <p className="settings-sub">决策证据按签名凝练成技能，改即生效不重启；人工修改的技能不会被覆盖</p>
          </div>
          <button type="button" className="settings-close" onClick={onClose} aria-label="关闭"><X size={16} /></button>
        </div>
        <div className="skills-settings-body">
          {error ? <p className="panel-error">{error}</p> : null}
          {settings ? (
            <>
              <label className="skills-setting-row">
                <input
                  type="checkbox"
                  checked={settings.auto_refine}
                  disabled={saving}
                  onChange={(e) => patch({ auto_refine: e.target.checked })}
                />
                <span>
                  <strong>自动凝练</strong>
                  <small>每积累 {settings.refine_every} 次成功决策自动运行凝练器；关闭后仍写证据，可用技能栏「立即凝练」手动触发</small>
                </span>
              </label>
              <label className="skills-setting-row">
                <span>
                  <strong>凝练间隔</strong>
                  <small>距下次自动凝练还需 {Math.max(0, settings.refine_every - (settings.since_refine || 0))} 次成功决策</small>
                </span>
                <input
                  type="number" min={1} max={100} value={settings.refine_every}
                  disabled={saving}
                  onChange={(e) => setSettings({ ...settings, refine_every: Number(e.target.value) })}
                  onBlur={(e) => patch({ refine_every: Number(e.target.value) })}
                />
                <em>次成功决策</em>
              </label>
              <label className="skills-setting-row">
                <span>
                  <strong>最低覆盖率阈值</strong>
                  <small>回合累计覆盖率低于该值时，其轨迹不支撑技能（只凝练优质轨迹）</small>
                </span>
                <input
                  type="number" min={0} max={100} value={settings.min_coverage_pct}
                  disabled={saving}
                  onChange={(e) => setSettings({ ...settings, min_coverage_pct: Number(e.target.value) })}
                  onBlur={(e) => patch({ min_coverage_pct: Number(e.target.value) })}
                />
                <em>%</em>
              </label>
            </>
          ) : !error ? <p>加载中…</p> : null}
        </div>
      </div>
    </div>
  );
}
