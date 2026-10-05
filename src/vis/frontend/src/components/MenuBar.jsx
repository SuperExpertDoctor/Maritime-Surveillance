import { useEffect, useRef, useState } from "react";
import { Check } from "lucide-react";

/**
 * VSCode 风格顶部菜单栏。
 * menus: [{id, label, items:[{id,label,shortcut?,checked?,disabled?,danger?,onClick}|"sep"]}]
 */
export default function MenuBar({ menus }) {
  const [openMenu, setOpenMenu] = useState(null);
  const barRef = useRef(null);

  useEffect(() => {
    if (!openMenu) return undefined;
    const onPointerDown = (event) => {
      if (barRef.current && !barRef.current.contains(event.target)) {
        setOpenMenu(null);
      }
    };
    const onKey = (event) => {
      if (event.key === "Escape") setOpenMenu(null);
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [openMenu]);

  return (
    <div className="menu-bar" ref={barRef} role="menubar" aria-label="主菜单">
      <span className="menu-bar-logo" aria-hidden="true">侦</span>
      {menus.map((menu) => (
        <div className="menu-entry" key={menu.id}>
          <button
            type="button"
            className={`menu-item ${openMenu === menu.id ? "open" : ""}`}
            onClick={() => setOpenMenu(openMenu === menu.id ? null : menu.id)}
            onMouseEnter={() => { if (openMenu) setOpenMenu(menu.id); }}
            aria-expanded={openMenu === menu.id}
            aria-haspopup="menu"
          >
            {menu.label}
          </button>
          {openMenu === menu.id && (
            <div className="menu-dropdown" role="menu">
              {menu.items.map((item, index) =>
                item === "sep" ? (
                  <div className="menu-sep" key={`sep-${index}`} />
                ) : (
                  <button
                    type="button"
                    role="menuitem"
                    key={item.id}
                    className={`menu-dd-item ${item.danger ? "danger" : ""}`}
                    disabled={item.disabled}
                    onClick={() => { setOpenMenu(null); item.onClick?.(); }}
                  >
                    <span className="menu-dd-check" aria-hidden="true">
                      {item.checked ? <Check size={13} /> : null}
                    </span>
                    <span className="menu-dd-label">{item.label}</span>
                    {item.shortcut && <span className="menu-dd-key">{item.shortcut}</span>}
                  </button>
                ),
              )}
            </div>
          )}
        </div>
      ))}
      <span className="menu-bar-spacer" />
      <span className="menu-bar-win" aria-hidden="true">—</span>
      <span className="menu-bar-win" aria-hidden="true">▢</span>
      <span className="menu-bar-win" aria-hidden="true">✕</span>
    </div>
  );
}
