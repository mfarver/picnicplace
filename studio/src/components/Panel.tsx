import { ChevronDown, ChevronRight } from "lucide-react";
import { type ReactNode, useState } from "react";

interface Props {
  title: string;
  children: ReactNode;
  /** Give an ID to make the panel collapsible. Its state is remembered per browser. */
  collapseId?: string;
}

const storageKey = (id: string) => `picnicplace.panel.${id}.collapsed`;

function loadCollapsed(id: string | undefined): boolean {
  if (!id) return false;
  try {
    return localStorage.getItem(storageKey(id)) === "1";
  } catch {
    return false;
  }
}

function saveCollapsed(id: string, collapsed: boolean): void {
  try {
    if (collapsed) localStorage.setItem(storageKey(id), "1");
    else localStorage.removeItem(storageKey(id));
  } catch {
    // Not remembered; fine.
  }
}

export function Panel({ title, children, collapseId }: Props) {
  const [collapsed, setCollapsed] = useState(() => loadCollapsed(collapseId));

  if (!collapseId) {
    return (
      <section className="panel">
        <h2 className="panel-title">{title}</h2>
        <div className="panel-body">{children}</div>
      </section>
    );
  }

  const toggle = () => {
    setCollapsed(!collapsed);
    saveCollapsed(collapseId, !collapsed);
  };
  const Chevron = collapsed ? ChevronRight : ChevronDown;
  return (
    <section className={`panel${collapsed ? " panel-collapsed" : ""}`}>
      <h2 className="panel-title">
        <button type="button" className="panel-toggle" aria-expanded={!collapsed} onClick={toggle}>
          <Chevron size={14} aria-hidden />
          {title}
        </button>
      </h2>
      {!collapsed && <div className="panel-body">{children}</div>}
    </section>
  );
}
