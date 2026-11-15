import { Cpu, MonitorPlay, RefreshCw, RotateCw, Search } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api } from "../../api/client";
import { useAction } from "../../api/useAction";

import type {
  CameraDevice,
  MachineProfile,
  ProbeResult,
  SerialPort,
  SetupState,
} from "../../api/types";

type Saved = SetupState["saved"];
type CameraChoice = Saved["cameras"][string];

interface PageProps {
  setup: SetupState;
  onSetup: (s: SetupState) => void;
  /** This client may change the setup. */
  canChange: boolean;
}

/** Save the wizard's choices, changing some of them. */
function useSave({ setup, onSetup }: PageProps) {
  const { run, pending, error } = useAction();
  const save = useCallback(
    async (change: Partial<Omit<Saved, "format" | "version" | "probe">>) => {
      const s = setup.saved;
      const body = {
        profile: s.profile,
        driver: s.driver,
        serial_port: s.serial_port,
        cameras: s.cameras,
        ...change,
      };
      const updated = await run(() => api.PUT("/api/v1/setup", { body }));
      if (updated) onSetup(updated);
    },
    [setup, onSetup, run],
  );
  return { save, pending, error };
}

function Pinned({ setup, keys }: { setup: SetupState; keys: string[] }) {
  if (!keys.some((k) => setup.pinned.includes(k))) return null;
  return (
    <p className="note note-warn">
      Set in the config file (picnicd.toml), which wins over this page: change it there.
    </p>
  );
}

/** Page 2: which machine this is, or the simulator. */
export function MachinePage(props: PageProps) {
  const { setup, canChange } = props;
  const { save, pending, error } = useSave(props);
  const chosen = setup.saved.profile ?? setup.running.profile;
  const driver = setup.saved.driver ?? setup.running.driver;
  return (
    <section>
      <Pinned setup={setup} keys={["machine.profile", "machine.driver"]} />
      <div className="wizard-cards">
        {setup.machines.map((m) => (
          <button
            key={m.id}
            type="button"
            className={`wizard-card${chosen === m.id ? " active" : ""}`}
            disabled={!canChange || pending}
            onClick={() => void save({ profile: m.id, driver: setup.saved.driver })}
          >
            <Cpu size={18} aria-hidden />
            {m.name}
          </button>
        ))}
      </div>
      <p className="text-muted">
        Not listed? Interviewing for an unlisted machine comes in a later version: for now,
        write a profile and name it in the config.
      </p>
      <div className="segmented">
        <button
          type="button"
          className={driver === "marlin" ? "segment active" : "segment"}
          disabled={!canChange || pending}
          onClick={() => void save({ profile: chosen, driver: "marlin" })}
        >
          My machine, on a serial port
        </button>
        <button
          type="button"
          className={driver !== "marlin" ? "segment active" : "segment"}
          disabled={!canChange || pending}
          onClick={() => void save({ profile: chosen, driver: "simulator" })}
        >
          Try it in the simulator
        </button>
      </div>
      {error && <p className="note note-danger">{error}</p>}
      <RestartBox {...props} />
    </section>
  );
}

/** Page 3: the serial port, and what its firmware says. */
export function ConnectPage(props: PageProps) {
  const { setup, onSetup, canChange } = props;
  const { save, pending, error } = useSave(props);
  const probe = useAction();
  const [ports, setPorts] = useState<SerialPort[] | null>(null);
  const [typed, setTyped] = useState("");
  const port = setup.saved.serial_port ?? setup.running.serial_port ?? "";

  const refresh = useCallback(() => {
    void api.GET("/api/v1/setup/serial-ports").then(({ data }) => setPorts(data ?? []));
  }, []);
  useEffect(refresh, [refresh]);

  const runProbe = async () => {
    const result = await probe.run(() =>
      api.POST("/api/v1/setup/probe", { body: { serial_port: port } }),
    );
    if (result) onSetup({ ...setup, saved: { ...setup.saved, probe: result } });
  };
  const result = setup.saved.probe?.serial_port === port ? setup.saved.probe : null;

  return (
    <section>
      <Pinned setup={setup} keys={["machine.marlin.port"]} />
      <div className="bringup-row">
        <select
          className="input"
          value={port}
          disabled={!canChange || pending}
          onChange={(e) => void save({ serial_port: e.target.value || null })}
        >
          <option value="">Choose a port</option>
          {port && !ports?.some((p) => p.device === port) && <option value={port}>{port}</option>}
          {ports?.map((p) => (
            <option key={p.device} value={p.device}>
              {p.label}
            </option>
          ))}
        </select>
        <button type="button" className="icon-button" aria-label="Look again" onClick={refresh}>
          <RefreshCw size={14} aria-hidden />
        </button>
      </div>
      {ports?.length === 0 && (
        <p className="text-muted">
          No serial ports found. Plug the machine in and turn it on, then look again.
        </p>
      )}
      <div className="bringup-row">
        <input
          className="input"
          placeholder="Or type a port, such as /dev/ttyACM0"
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
        />
        <button
          type="button"
          className="button"
          disabled={!canChange || !typed.trim()}
          onClick={() => void save({ serial_port: typed.trim() })}
        >
          Use
        </button>
      </div>
      <button
        type="button"
        className="button"
        disabled={!canChange || !port || probe.pending}
        onClick={() => void runProbe()}
        title="Ask the firmware what it is. Nothing moves or switches on."
      >
        <Search size={16} aria-hidden />
        {probe.pending ? "Probing…" : "Probe"}
      </button>
      {result && <ProbeSummary result={result} />}
      {(error || probe.error) && <p className="note note-danger">{error ?? probe.error}</p>}
      <RestartBox {...props} />
    </section>
  );
}

function ProbeSummary({ result }: { result: ProbeResult }) {
  const speaks = Object.entries(result.vacuum).find(([, values]) =>
    Object.values(values).every((v) => typeof v === "number"),
  );
  const mismatch =
    result.expected_machine_type && result.machine_type !== result.expected_machine_type;
  return (
    <ul className="wizard-facts">
      <li>Firmware: {result.firmware ?? "no answer"}</li>
      <li className={mismatch ? "text-warn" : undefined}>
        Machine type: {result.machine_type ?? "not said"}
        {mismatch && ` (this machine's profile expects ${result.expected_machine_type})`}
      </li>
      <li className={result.emergency_parser === false ? "text-warn" : undefined}>
        STOP{" "}
        {result.emergency_parser === true
          ? "acts mid-move"
          : result.emergency_parser === false
            ? "waits for the current move to end"
            : "mid-move: not said"}
      </li>
      <li>
        Vacuum sensors:{" "}
        {speaks ? `read in the ${speaks[0]} format` : "no format gave readings"}
      </li>
    </ul>
  );
}

/** Page 4: which camera is which, and how each picture is turned. */
export function CamerasPage(props: PageProps & { description: MachineProfile }) {
  const { setup, canChange, description } = props;
  const { save, pending, error } = useSave(props);
  const [devices, setDevices] = useState<CameraDevice[] | null>(null);
  useEffect(() => {
    void api.GET("/api/v1/setup/cameras").then(({ data }) => setDevices(data ?? []));
  }, []);

  const choice = (id: string): CameraChoice =>
    setup.saved.cameras[id] ?? {
      device: setup.running[`cameras.${id}`] ?? null,
      flip_x: false,
      flip_y: false,
      rotation: 0,
    };
  const change = (id: string, part: Partial<CameraChoice>) =>
    void save({ cameras: { ...setup.saved.cameras, [id]: { ...choice(id), ...part } } });

  return (
    <section>
      {description.cameras.map((camera) => {
        const c = choice(camera.id);
        return (
          <div key={camera.id} className="wizard-camera">
            <h4>
              <MonitorPlay size={14} aria-hidden /> {camera.label}
            </h4>
            <Pinned setup={setup} keys={[`cameras.${camera.id}`]} />
            <select
              className="input"
              value={c.device ?? ""}
              disabled={!canChange || pending}
              onChange={(e) => change(camera.id, { device: e.target.value || null })}
            >
              <option value="">Off</option>
              {c.device && !devices?.some((d) => d.device === c.device) && (
                <option value={c.device}>{c.device}</option>
              )}
              {devices?.map((d) => (
                <option key={d.device} value={d.device}>
                  {d.label}
                </option>
              ))}
            </select>
            <div className="bringup-row">
              <label>
                <input
                  type="checkbox"
                  checked={c.flip_x}
                  disabled={!canChange || pending || !c.device}
                  onChange={(e) => change(camera.id, { flip_x: e.target.checked })}
                />{" "}
                Mirror left to right
              </label>
              <label>
                <input
                  type="checkbox"
                  checked={c.flip_y}
                  disabled={!canChange || pending || !c.device}
                  onChange={(e) => change(camera.id, { flip_y: e.target.checked })}
                />{" "}
                Mirror top to bottom
              </label>
            </div>
          </div>
        );
      })}
      <p className="text-muted">
        The live pictures are at the top of this sidebar. Choose a camera, restart the core, then
        check its picture moves the right way below.
      </p>
      {error && <p className="note note-danger">{error}</p>}
      <RestartBox {...props} />
    </section>
  );
}

/** Restart the core to use the wizard's choices, then reload the Studio when it is back. */
export function RestartBox({ setup, canChange }: PageProps) {
  const { run, pending, error } = useAction();
  const [restarting, setRestarting] = useState(false);
  if (!setup.restart_needed) return null;

  const restart = async () => {
    const accepted = await run(() => api.POST("/api/v1/setup/restart"));
    if (!accepted) return;
    setRestarting(true);
    // Wait for the core to go and come back, then start fresh with its new machine.
    await new Promise((resolve) => setTimeout(resolve, 2000));
    for (let i = 0; i < 60; i++) {
      try {
        const { data } = await api.GET("/api/v1/health");
        if (data) {
          window.location.reload();
          return;
        }
      } catch {
        // Not back yet.
      }
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }
    setRestarting(false);
  };

  return (
    <div className="note note-warn wizard-restart">
      <p>Restart the core to use these choices.</p>
      {setup.can_restart ? (
        <button
          type="button"
          className="button"
          disabled={!canChange || pending || restarting}
          onClick={() => void restart()}
        >
          <RotateCw size={16} aria-hidden />
          {restarting ? "Restarting…" : "Restart the core"}
        </button>
      ) : (
        <p>Stop the core and start it again.</p>
      )}
      {error && <p className="text-danger">{error}</p>}
    </div>
  );
}
