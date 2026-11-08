import { CircuitBoard, Cog, Eye, Film, Layers, Play } from "lucide-react";
import { type ReactNode, useState } from "react";
import { useCore } from "./api/core";
import { Placeholder } from "./components/Placeholder";
import { Splash } from "./components/Splash";
import { StatusBar } from "./components/StatusBar";
import { TokenPrompt } from "./components/TokenPrompt";
import { TopBar, type WorkspaceTab } from "./components/TopBar";
import { useTheme } from "./theme";
import { FeedersWorkspace } from "./workspaces/FeedersWorkspace";
import { LibraryWorkspace } from "./workspaces/LibraryWorkspace";
import { MachineWorkspace } from "./workspaces/MachineWorkspace";
import { PrepareWorkspace } from "./workspaces/PrepareWorkspace";

interface Workspace extends WorkspaceTab {
  render: () => ReactNode;
}

const WORKSPACES: Workspace[] = [
  {
    id: "prepare",
    label: "Prepare",
    icon: CircuitBoard,
    render: () => <PrepareWorkspace />,
  },
  {
    id: "parts",
    label: "Parts",
    icon: Layers,
    render: () => <LibraryWorkspace />,
  },
  {
    id: "feeders",
    label: "Feeders",
    icon: Film,
    render: () => <FeedersWorkspace />,
  },
  {
    id: "run",
    label: "Run",
    icon: Play,
    render: () => (
      <Placeholder
        icon={Play}
        title="Run"
        description="Job progress, live cameras, and placement status."
        milestone="M4"
      />
    ),
  },
  { id: "machine", label: "Machine", icon: Cog, render: () => <MachineWorkspace /> },
  {
    id: "vision",
    label: "Vision",
    icon: Eye,
    render: () => (
      <Placeholder
        icon={Eye}
        title="Vision"
        description="Build and tune vision pipelines with a live preview of each stage."
        milestone="M2"
      />
    ),
  },
];

export function App() {
  const [active, setActive] = useState("machine");
  const [theme, toggleTheme] = useTheme();
  const { status } = useCore();
  const workspace = WORKSPACES.find((w) => w.id === active) ?? WORKSPACES[0]!;

  return (
    <div className="app">
      <TopBar
        tabs={WORKSPACES}
        active={workspace.id}
        onSelect={setActive}
        theme={theme}
        onToggleTheme={toggleTheme}
      />
      <main className="main">{workspace.render()}</main>
      <StatusBar />
      {status === "unauthorized" && <TokenPrompt />}
      <Splash />
    </div>
  );
}
