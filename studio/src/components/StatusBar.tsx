import { type ConnectionStatus, useCore } from "../api/core";
import { formatAxis } from "../format";

const CONNECTION_LABEL: Record<ConnectionStatus, string> = {
  connecting: "Connecting",
  open: "Connected",
  reconnecting: "Reconnecting",
  unauthorized: "Token required",
};

export function StatusBar() {
  const { status, machine, presence, serverVersion } = useCore();
  const live = status === "open" && machine !== undefined;

  return (
    <footer className="statusbar">
      <span className="status-item">
        <span className={`dot dot-${status === "open" ? "ok" : "warn"}`} aria-hidden />
        {CONNECTION_LABEL[status]}
      </span>
      {live && (
        <>
          <span className="status-item">
            Motion{" "}
            <strong className={machine.motion_enabled ? "text-ok" : "text-muted"}>
              {machine.motion_enabled ? "enabled" : "disabled"}
            </strong>
          </span>
          <span className="status-item">{machine.homed ? "Homed" : "Not homed"}</span>
          {machine.estop && <span className="status-item badge-danger">E-STOP LATCHED</span>}
          <span className="status-item mono">
            {machine.axes.map((axis) => (
              <span key={axis} className="status-axis">
                {axis} {formatAxis(axis, machine.position[axis])}
              </span>
            ))}
          </span>
        </>
      )}
      <span className="spacer" />
      {presence && (
        <span className="status-item text-muted">
          {presence.clients} {presence.clients === 1 ? "client" : "clients"}
        </span>
      )}
      <span className="status-item text-muted">
        {serverVersion ? `Core ${serverVersion}` : "Core —"} · {machine?.driver ?? "no driver"}
      </span>
    </footer>
  );
}
