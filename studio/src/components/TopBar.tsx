import type { LucideIcon } from "lucide-react";
import { Moon, Sun } from "lucide-react";
import type { Theme } from "../theme";
import { EStopButton } from "./EStopButton";

export interface WorkspaceTab {
  id: string;
  label: string;
  icon: LucideIcon;
}

interface Props {
  tabs: WorkspaceTab[];
  active: string;
  onSelect: (id: string) => void;
  theme: Theme;
  onToggleTheme: () => void;
}

export function TopBar({ tabs, active, onSelect, theme, onToggleTheme }: Props) {
  return (
    <header className="topbar">
      <div className="brand">
        <img src="/favicon.svg" alt="" width={24} height={24} />
        <span>PicnicPlace</span>
      </div>
      <nav className="tabs" role="tablist" aria-label="Workspaces">
        {tabs.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            type="button"
            role="tab"
            aria-selected={id === active}
            className="tab"
            onClick={() => onSelect(id)}
          >
            <Icon size={16} aria-hidden />
            <span>{label}</span>
          </button>
        ))}
      </nav>
      <div className="topbar-actions">
        <button
          type="button"
          className="icon-button"
          onClick={onToggleTheme}
          title={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
          aria-label="Toggle theme"
        >
          {theme === "dark" ? <Sun size={16} /> : <Moon size={16} />}
        </button>
        <EStopButton />
      </div>
    </header>
  );
}
