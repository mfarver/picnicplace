import {
  CircuitBoard,
  FileInput,
  FileUp,
  FolderOpen,
  LocateFixed,
  Save,
  Trash2,
  X,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { useCore } from "../api/core";
import { useAction } from "../api/useAction";
import { useDescription } from "../api/useDescription";
import type { ImportReport, Job, JobLayout, JobSummary } from "../api/types";
import { Field, NumberInput } from "../components/Form";
import { JobOverlay } from "../components/job/JobOverlay";
import { JobRevisions } from "../components/job/JobRevisions";
import { ImportReportView } from "../components/library/ImportReportView";
import { MachineView } from "../components/machine/MachineView";
import { Panel } from "../components/Panel";
import { Placeholder } from "../components/Placeholder";

/** A file-safe job ID from its name, as the core expects. */
function jobId(name: string): string {
  return (
    name
      .replace(/[^A-Za-z0-9._-]+/g, "-")
      .replace(/^[-._]+|[-._]+$/g, "")
      .slice(0, 100) || "job"
  );
}

interface Files {
  board?: File;
  panel?: File;
  bom?: File;
}

/** Prepare a job: import a KiCad board (and panel), place it on the bed, check it, save it. */
export function PrepareWorkspace() {
  const { status } = useCore();
  const description = useDescription();
  const { run, pending, error } = useAction();
  const [jobs, setJobs] = useState<JobSummary[]>([]);
  const [job, setJob] = useState<Job | null>(null);
  const [savedAs, setSavedAs] = useState<string | null>(null);
  // A job loaded from a file has no working copy: only where it sits can be changed.
  const [fromFile, setFromFile] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [checked, setLayout] = useState<JobLayout | null>(null);
  const [files, setFiles] = useState<Files>({});
  const [addToLibrary, setAddToLibrary] = useState(true);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [report, setReport] = useState<ImportReport | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [jobFile, setJobFile] = useState<File>();
  const [loaded, setLoaded] = useState<string | null>(null);

  const reloadJobs = useCallback(async () => {
    const { data } = await api.GET("/api/v1/jobs");
    if (data) setJobs(data);
  }, []);

  useEffect(() => {
    if (status !== "open") return;
    let cancelled = false;
    void api.GET("/api/v1/jobs").then(({ data }) => {
      if (data && !cancelled) setJobs(data);
    });
    return () => {
      cancelled = true;
    };
  }, [status]);

  // Check the job against the machine whenever it changes.
  const latest = useRef(0);
  useEffect(() => {
    if (!job) return;
    const n = ++latest.current;
    const timer = window.setTimeout(() => {
      void api.POST("/api/v1/jobs/layout", { body: job }).then(({ data }) => {
        if (data && n === latest.current) setLayout(data);
      });
    }, 150);
    return () => window.clearTimeout(timer);
  }, [job]);

  const update = (changes: Partial<Job>) => {
    setJob((j) => (j ? { ...j, ...changes } : j));
    setDirty(true);
    setNotice(null);
  };

  const importBoard = async () => {
    if (!files.board) return;
    const name = files.board.name.replace(/\.kicad_pcb$/i, "");
    const result = await run(async () =>
      api.POST("/api/v1/boards/import/kicad", {
        body: {
          kicad_pcb: await files.board!.text(),
          panel_pcb: files.panel ? await files.panel.text() : null,
          bom_csv: files.bom ? await files.bom.text() : null,
          name,
          add_to_library: addToLibrary,
        },
      }),
    );
    if (!result) return;
    setJob(result.job);
    setSavedAs(null);
    setDirty(true);
    setWarnings(result.warnings);
    setReport(result.library_report ?? null);
    setFiles({});
    setNotice(null);
  };

  const openJob = async (id: string) => {
    const summary = jobs.find((j) => j.id === id);
    const latest = summary?.revisions.at(-1);
    const path = { job_id: id };
    const data =
      summary && !summary.draft && latest
        ? await run(async () => {
            const [published, setup] = await Promise.all([
              api.GET("/api/v1/jobs/{job_id}/revisions/{number}", {
                params: { path: { ...path, number: latest.number } },
              }),
              api.GET("/api/v1/jobs/{job_id}/setup", { params: { path } }),
            ]);
            if (!published.data) return { error: published.error };
            if (!setup.data) return { error: setup.error };
            const { name, board, panel, side_up } = published.data.design;
            const job: Job = {
              format: "picnicplace-job",
              version: 1,
              name,
              board,
              panel,
              side_up,
              ...setup.data,
            };
            return { data: job };
          })
        : await run(() => api.GET("/api/v1/jobs/{job_id}", { params: { path } }));
    if (!data) return;
    setJob(data);
    setFromFile(!summary?.draft);
    setSavedAs(id);
    setDirty(false);
    setWarnings([]);
    setReport(null);
    setNotice(null);
  };

  const deleteJob = async (id: string) => {
    await run(() => api.DELETE("/api/v1/jobs/{job_id}", { params: { path: { job_id: id } } }));
    await reloadJobs();
  };

  const saveJob = async () => {
    if (!job) return;
    const id = savedAs ?? jobId(job.name);
    const path = { job_id: id };
    const saved = await run(() =>
      fromFile
        ? api.PUT("/api/v1/jobs/{job_id}/setup", {
            params: { path },
            body: { location: job.location, board_z: job.board_z },
          })
        : api.PUT("/api/v1/jobs/{job_id}", { params: { path }, body: job }),
    );
    if (!saved) return;
    setSavedAs(id);
    setDirty(false);
    setNotice(`Saved as ${id}.`);
    await reloadJobs();
  };

  const loadJobFile = async () => {
    if (!jobFile) return;
    const result = await run(async () =>
      api.POST("/api/v1/jobs/import", { body: { text: await jobFile.text() } }),
    );
    if (!result) return;
    setJobFile(undefined);
    setLoaded(
      result.kind === "working-copy"
        ? `Loaded ${result.job_id}, saved before revisions: open it and publish it.`
        : result.already_kept
          ? `Revision ${result.number} of ${result.job_id} was already here.`
          : `Loaded revision ${result.number} of ${result.job_id}.`,
    );
    await reloadJobs();
  };

  const findPlace = async () => {
    if (!job) return;
    const location = await run(() => api.POST("/api/v1/jobs/find-location", { body: job }));
    if (location) {
      update({ location });
      setNotice("Placed where every nozzle reaches it and the camera sees its fiducials.");
    }
  };

  const close = () => {
    setJob(null);
    setSavedAs(null);
    setFromFile(false);
    setDirty(false);
    setWarnings([]);
    setReport(null);
  };

  // A layout left over from a closed job is not shown.
  const layout = job ? checked : null;
  const errors = layout?.issues.filter((i) => i.severity === "error") ?? [];
  const cautions = layout?.issues.filter((i) => i.severity === "warning") ?? [];
  const parts = layout?.placements.filter((p) => p.kind === "part" && p.enabled).length ?? 0;
  const location = job?.location;

  return (
    <div className="workspace">
      <aside className="sidebar">
        {!job ? (
          <>
            <Panel title="Import a board">
              <p className="text-muted">
                A KiCad board, and optionally the panel made from it (such as by KiKit) and the
                schematic's BOM, whose part numbers win over the board's.
              </p>
              <FilePick
                label="Board (.kicad_pcb)"
                accept=".kicad_pcb"
                file={files.board}
                onPick={(board) => setFiles((f) => ({ ...f, board }))}
              />
              <FilePick
                label="Panel (.kicad_pcb, optional)"
                accept=".kicad_pcb"
                file={files.panel}
                onPick={(panel) => setFiles((f) => ({ ...f, panel }))}
              />
              <FilePick
                label="BOM (.csv, optional)"
                accept=".csv"
                file={files.bom}
                onPick={(bom) => setFiles((f) => ({ ...f, bom }))}
              />
              <label className="check">
                <input
                  type="checkbox"
                  checked={addToLibrary}
                  onChange={(e) => setAddToLibrary(e.target.checked)}
                />
                Add its footprints and parts to the library
              </label>
              <div className="button-row">
                <button
                  type="button"
                  className="button primary"
                  disabled={!files.board || pending}
                  onClick={() => void importBoard()}
                >
                  <FileUp size={16} aria-hidden />
                  Import
                </button>
              </div>
              {error && <p className="note note-danger">{error}</p>}
            </Panel>
            <Panel title="Saved jobs">
              {jobs.length === 0 && <p className="text-muted">No saved jobs yet.</p>}
              <ul className="item-list">
                {jobs.map((j) => {
                  const latest = j.revisions.at(-1);
                  const published = latest ? `r${latest.number}` : "not published";
                  return (
                    <li key={j.id} className="item-with-action">
                      <button
                        type="button"
                        className="item"
                        title={
                          j.draft
                            ? undefined
                            : "Loaded from a job file: it runs as published; set where it sits"
                        }
                        onClick={() => void openJob(j.id)}
                      >
                        <span className="item-id">{j.name}</span>
                        <span className="item-detail">
                          {j.copies > 1 ? `${j.copies} boards · ` : ""}
                          {j.side_up} up · {j.draft ? published : `${published}, from a file`}
                          {j.placed ? "" : " · not placed"}
                        </span>
                      </button>
                      {j.draft && (
                        <button
                          type="button"
                          className="icon-button"
                          title={`Delete ${j.name} (its published revisions are kept)`}
                          onClick={() => void deleteJob(j.id)}
                        >
                          <Trash2 size={14} aria-label={`Delete ${j.name}`} />
                        </button>
                      )}
                    </li>
                  );
                })}
              </ul>
            </Panel>
            <Panel title="Load a job file">
              <p className="text-muted">
                A published revision saved from another PicnicPlace, to run here as it is.
              </p>
              <FilePick
                label="Job file (.json)"
                accept=".json,application/json"
                file={jobFile}
                onPick={(file) => {
                  setJobFile(file);
                  setLoaded(null);
                }}
              />
              <div className="button-row">
                <button
                  type="button"
                  className="button"
                  disabled={!jobFile || pending}
                  onClick={() => void loadJobFile()}
                >
                  <FileInput size={16} aria-hidden />
                  Load
                </button>
              </div>
              {loaded && <p className="note text-muted">{loaded}</p>}
            </Panel>
          </>
        ) : (
          <>
            <Panel title="Job">
              <Field label="Name">
                <input
                  className="input"
                  value={job.name}
                  disabled={fromFile}
                  onChange={(e) => update({ name: e.target.value })}
                />
              </Field>
              <p className="text-muted">
                {job.board.name}: {job.board.size.x} × {job.board.size.y} mm
                {job.panel.copies.length > 1
                  ? `, ${job.panel.copies.length} on a ${job.panel.size.x} × ${job.panel.size.y} mm panel`
                  : ""}
                .
              </p>
              <Field label="Side facing up">
                <div className="segmented">
                  {(["top", "bottom"] as const).map((side) => (
                    <button
                      key={side}
                      type="button"
                      className={job.side_up === side ? "segment active" : "segment"}
                      disabled={fromFile}
                      onClick={() => update({ side_up: side })}
                    >
                      {side === "top" ? "Top" : "Bottom"}
                    </button>
                  ))}
                </div>
              </Field>
              <div className="button-row">
                <button
                  type="button"
                  className="button primary"
                  disabled={pending || (!dirty && !!savedAs)}
                  onClick={() => void saveJob()}
                >
                  <Save size={16} aria-hidden />
                  Save
                </button>
                <button type="button" className="button" onClick={close}>
                  <X size={16} aria-hidden />
                  Close
                </button>
              </div>
              {fromFile && (
                <p className="note text-muted">
                  Loaded from a job file: it runs as published. Where it sits on this machine
                  can be set and saved.
                </p>
              )}
              {notice && <p className="note text-muted">{notice}</p>}
              {error && <p className="note note-danger">{error}</p>}
            </Panel>

            {savedAs && (
              <Panel title="Published revisions">
                <JobRevisions
                  jobId={savedAs}
                  revisions={jobs.find((j) => j.id === savedAs)?.revisions ?? []}
                  unsaved={dirty}
                  publishable={!fromFile}
                  onPublished={() => void reloadJobs()}
                />
              </Panel>
            )}

            <Panel title="On the machine">
              <p className="text-muted">
                Where the {job.panel.copies.length > 1 ? "panel" : "board"}'s front-left corner
                sits, in machine coordinates, and how far it is turned as you look at the bed.
                This machine keeps it, apart from the job: it is in no revision.
              </p>
              <div className="field-row">
                <Field label="X, mm">
                  <NumberInput
                    value={location?.x}
                    onChange={(x) =>
                      update({
                        location: {
                          x: x ?? 0,
                          y: location?.y ?? 0,
                          rotation: location?.rotation ?? 0,
                        },
                      })
                    }
                  />
                </Field>
                <Field label="Y, mm">
                  <NumberInput
                    value={location?.y}
                    onChange={(y) =>
                      update({
                        location: {
                          x: location?.x ?? 0,
                          y: y ?? 0,
                          rotation: location?.rotation ?? 0,
                        },
                      })
                    }
                  />
                </Field>
              </div>
              <Field label="Turn">
                <div className="segmented">
                  {[0, 90, 180, 270].map((turn) => (
                    <button
                      key={turn}
                      type="button"
                      className={location?.rotation === turn ? "segment active" : "segment"}
                      onClick={() =>
                        update({
                          location: { x: location?.x ?? 0, y: location?.y ?? 0, rotation: turn },
                        })
                      }
                    >
                      {turn}°
                    </button>
                  ))}
                </div>
              </Field>
              <div className="button-row">
                <button
                  type="button"
                  className="button"
                  disabled={pending}
                  onClick={() => void findPlace()}
                >
                  <LocateFixed size={16} aria-hidden />
                  Find a place
                </button>
              </div>
            </Panel>

            <Panel title="Checks">
              {!layout ? (
                <p className="text-muted">Checking…</p>
              ) : layout.ok ? (
                <p className="note">
                  Ready: {parts} placement{parts === 1 ? "" : "s"} within reach of every nozzle.
                </p>
              ) : null}
              {errors.slice(0, 12).map((i, n) => (
                <p key={n} className="note note-danger">
                  {i.copy_id ? `Board ${i.copy_id}: ` : ""}
                  {i.message}
                </p>
              ))}
              {errors.length > 12 && (
                <p className="note note-danger">…and {errors.length - 12} more.</p>
              )}
              {cautions.map((i, n) => (
                <p key={n} className="note note-warn">
                  {i.message}
                </p>
              ))}
              {warnings.map((w) => (
                <p key={w} className="note note-warn">
                  {w}
                </p>
              ))}
              {report && <ImportReportView report={report} />}
            </Panel>

            {job.panel.copies.length > 1 && (
              <Panel title="Boards in the panel" collapseId="prepare-copies">
                {job.panel.copies.map((copy, i) => (
                  <label key={copy.id} className="check">
                    <input
                      type="checkbox"
                      checked={copy.enabled}
                      disabled={fromFile}
                      onChange={(e) =>
                        update({
                          panel: {
                            ...job.panel,
                            copies: job.panel.copies.map((c, k) =>
                              k === i ? { ...c, enabled: e.target.checked } : c,
                            ),
                          },
                        })
                      }
                    />
                    Board {copy.id}
                    <span className="text-muted">
                      {" "}
                      at {copy.x}, {copy.y}
                      {copy.rotation ? `, turned ${copy.rotation}°` : ""}
                    </span>
                  </label>
                ))}
              </Panel>
            )}
          </>
        )}
      </aside>

      <div className="canvas machine-canvas">
        {description ? (
          <MachineView
            description={description}
            overlay={layout ? <JobOverlay layout={layout} /> : undefined}
          />
        ) : (
          <Placeholder
            icon={job ? FolderOpen : CircuitBoard}
            title="Prepare"
            description="Waiting for the machine description from the core."
          />
        )}
      </div>
    </div>
  );
}

function FilePick({
  label,
  accept,
  file,
  onPick,
}: {
  label: string;
  accept: string;
  file?: File;
  onPick: (file: File | undefined) => void;
}) {
  return (
    <label className="field file-pick">
      <span className="field-label">{label}</span>
      <input type="file" accept={accept} onChange={(e) => onPick(e.target.files?.[0])} />
      <span className="file-name">{file ? file.name : "No file chosen"}</span>
    </label>
  );
}
