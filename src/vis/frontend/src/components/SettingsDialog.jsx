import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, RotateCcw, X } from "lucide-react";

/** 与后端 settings_schema.py 一致的客户端校验：非法值给出中文文案。 */
function validateField(field, raw, merged) {
  const label = field.label;
  if (field.type === "boolean") {
    return raw === "true" || raw === "false" ? null : `${label}：取值必须是 是/否`;
  }
  if (raw === "" || raw === null || raw === undefined) {
    return `${label}：不能为空`;
  }
  const value = Number(raw);
  if (!Number.isFinite(value)) {
    return `${label}：请输入数字`;
  }
  if (field.type === "integer" && !Number.isInteger(value)) {
    return `${label}：请输入整数`;
  }
  if (field.min !== null && field.min !== undefined && value < field.min) {
    return `${label}：不能小于 ${field.min}`;
  }
  if (field.max !== null && field.max !== undefined && value > field.max) {
    return `${label}：不能大于 ${field.max}`;
  }
  if (field.lt) {
    const other = Number(merged[field.lt]);
    const otherField = field._index?.[field.lt];
    if (Number.isFinite(other) && !(value < other)) {
      return `${label}：必须小于「${otherField?.label || field.lt}」`;
    }
  }
  if (field.gt) {
    const other = Number(merged[field.gt]);
    const otherField = field._index?.[field.gt];
    if (Number.isFinite(other) && !(value > other)) {
      return `${label}：必须大于「${otherField?.label || field.gt}」`;
    }
  }
  if (field.pair_sum) {
    const other = Number(merged[field.pair_sum]);
    const otherField = field._index?.[field.pair_sum];
    if (Number.isFinite(other) && Math.abs(value + other - 1.0) > 1e-6) {
      return `${label}：与「${otherField?.label || field.pair_sum}」之和必须为 1`;
    }
  }
  return null;
}

export default function SettingsDialog({ open, onClose, onRestarting }) {
  const [groups, setGroups] = useState(null);
  const [activeGroup, setActiveGroup] = useState("uav");
  const [values, setValues] = useState({});
  const [initial, setInitial] = useState({});
  const [errors, setErrors] = useState({});
  const [loadError, setLoadError] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [confirmReset, setConfirmReset] = useState(false);

  useEffect(() => {
    if (!open) return undefined;
    let cancelled = false;
    setGroups(null);
    setLoadError("");
    setSaveError("");
    setConfirmReset(false);
    fetch("/api/settings")
      .then((response) => {
        if (!response.ok) throw new Error(`读取配置失败 (${response.status})`);
        return response.json();
      })
      .then((payload) => {
        if (cancelled) return;
        const index = {};
        const vals = {};
        for (const group of payload.groups || []) {
          for (const field of group.fields) {
            index[field.key] = field;
            if (!(field.key in vals)) {
              vals[field.key] = field.type === "boolean"
                ? String(field.value)
                : String(field.value ?? "");
            }
          }
        }
        // 让校验函数能查到关联字段的显示名
        for (const field of Object.values(index)) field._index = index;
        setGroups(payload.groups || []);
        setActiveGroup((payload.groups || [])[0]?.id || "uav");
        setValues(vals);
        setInitial(vals);
        setErrors({});
      })
      .catch((error) => { if (!cancelled) setLoadError(error.message); });
    return () => { cancelled = true; };
  }, [open]);

  const allFields = useMemo(() => {
    const out = {};
    for (const group of groups || []) {
      for (const field of group.fields) {
        if (!out[field.key]) out[field.key] = field;
      }
    }
    return out;
  }, [groups]);

  const revalidate = (nextValues) => {
    const merged = { ...initial };
    for (const key of Object.keys(merged)) merged[key] = Number(merged[key]);
    for (const [key, raw] of Object.entries(nextValues)) {
      if (allFields[key]?.type === "boolean") merged[key] = raw;
      else merged[key] = Number(raw);
    }
    const nextErrors = {};
    for (const [key, field] of Object.entries(allFields)) {
      const message = validateField(field, nextValues[key], merged);
      if (message) nextErrors[key] = message;
    }
    setErrors(nextErrors);
    return nextErrors;
  };

  const handleChange = (key, raw) => {
    const next = { ...values, [key]: raw };
    setValues(next);
    revalidate(next);
  };

  const dirtyKeys = Object.keys(values).filter((key) => values[key] !== initial[key]);
  const errorCount = Object.keys(errors).length;

  const submit = async (reset = false) => {
    setSaving(true);
    setSaveError("");
    try {
      const url = reset ? "/api/settings/reset" : "/api/settings";
      const body = reset
        ? {}
        : {
            values: Object.fromEntries(
              dirtyKeys.map((key) => [
                key,
                allFields[key].type === "boolean"
                  ? values[key] === "true"
                  : allFields[key].type === "integer"
                    ? parseInt(values[key], 10)
                    : Number(values[key]),
              ]),
            ),
          };
      const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) {
        if (result.fields) {
          const mapped = {};
          for (const [key, message] of Object.entries(result.fields)) mapped[key] = message;
          setErrors((prev) => ({ ...prev, ...mapped }));
          setSaveError("仍有参数不合法，请修正后重试");
        } else {
          setSaveError(result.message || result.error_code || "保存失败");
        }
        return;
      }
      onRestarting?.();
    } catch (error) {
      setSaveError("无法连接服务：" + error.message);
    } finally {
      setSaving(false);
    }
  };

  if (!open) return null;
  const active = (groups || []).find((group) => group.id === activeGroup);

  return (
    <div className="settings-overlay" role="presentation" onMouseDown={(e) => {
      if (e.target === e.currentTarget) onClose();
    }}>
      <div className="settings-dialog" role="dialog" aria-modal="true" aria-label="算法参数设置">
        <div className="settings-head">
          <div>
            <h2>设置 · 算法参数</h2>
            <p className="settings-sub">保存后将基于新配置重新启动仿真，当前回合进度不会保留</p>
          </div>
          <button type="button" className="settings-close" onClick={onClose} aria-label="关闭设置">
            <X size={16} />
          </button>
        </div>
        <div className="settings-body">
          <nav className="settings-nav" aria-label="参数分组">
            {(groups || []).map((group) => (
              <button
                type="button"
                key={group.id}
                className={`settings-nav-item ${group.id === activeGroup ? "sel" : ""}`}
                onClick={() => setActiveGroup(group.id)}
              >
                {group.title}
                {group.fields.some((f) => errors[f.key]) && <span className="nav-error-dot" />}
              </button>
            ))}
          </nav>
          <div className="settings-form">
            {loadError && <p className="settings-load-error">{loadError}</p>}
            {active && active.fields.map((field) => {
              const message = errors[field.key];
              return (
                <div className={`settings-row ${message ? "error" : ""}`} key={field.key}>
                  <label htmlFor={`set-${field.key}`}>
                    {field.label}
                    {field.note && <em className="field-note">{field.note}</em>}
                  </label>
                  <div className="settings-field">
                    {field.type === "boolean" ? (
                      <select
                        id={`set-${field.key}`}
                        value={values[field.key] ?? "false"}
                        onChange={(e) => handleChange(field.key, e.target.value)}
                      >
                        <option value="true">允许</option>
                        <option value="false">禁止</option>
                      </select>
                    ) : (
                      <input
                        id={`set-${field.key}`}
                        type="number"
                        step="any"
                        value={values[field.key] ?? ""}
                        onChange={(e) => handleChange(field.key, e.target.value)}
                        aria-invalid={Boolean(message)}
                      />
                    )}
                    {field.unit && <span className="field-unit">{field.unit}</span>}
                    {message && <p className="field-error" role="alert">{message}</p>}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
        <div className="settings-foot">
          <div className="settings-foot-left">
            {confirmReset ? (
              <>
                <span className="reset-warn">恢复全部默认值并重启？</span>
                <button type="button" className="btn danger" disabled={saving}
                  onClick={() => { setConfirmReset(false); submit(true); }}>
                  确认恢复
                </button>
                <button type="button" className="btn ghost" onClick={() => setConfirmReset(false)}>取消</button>
              </>
            ) : (
              <button type="button" className="btn ghost" onClick={() => setConfirmReset(true)} disabled={saving}>
                <RotateCcw size={13} /> 恢复默认值
              </button>
            )}
          </div>
          <div className="settings-foot-right">
            {errorCount > 0 && (
              <span className="settings-warn"><AlertTriangle size={13} /> 有 {errorCount} 项参数不合法，修正后才能保存</span>
            )}
            {saveError && <span className="settings-warn">{saveError}</span>}
            <button type="button" className="btn ghost" onClick={onClose} disabled={saving}>取消</button>
            <button
              type="button"
              className="btn primary"
              disabled={saving || errorCount > 0 || dirtyKeys.length === 0}
              onClick={() => submit(false)}
            >
              {saving ? "保存中…" : "保存并重启仿真"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
