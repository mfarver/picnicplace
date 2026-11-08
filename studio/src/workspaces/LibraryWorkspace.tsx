import { Download, FileUp, Library, Search } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api/client";
import { useCore } from "../api/core";
import { useAction } from "../api/useAction";
import type { Footprint, ImportReport, LibraryDocument, Part } from "../api/types";
import { FootprintDrawing } from "../components/library/FootprintShape";
import { ImportReportView } from "../components/library/ImportReportView";
import { PartEditor } from "../components/library/PartEditor";
import { Panel } from "../components/Panel";
import { Placeholder } from "../components/Placeholder";

type Mode = "parts" | "footprints";

/** The parts library: parts, their footprints and tape details, and imports. */
export function LibraryWorkspace() {
  const { status } = useCore();
  const { run, pending, error } = useAction();
  const [parts, setParts] = useState<Part[]>([]);
  const [footprints, setFootprints] = useState<Footprint[]>([]);
  const [mode, setMode] = useState<Mode>("parts");
  const [filter, setFilter] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [report, setReport] = useState<ImportReport | null>(null);
  const openpnpInput = useRef<HTMLInputElement>(null);
  const libraryInput = useRef<HTMLInputElement>(null);

  const reload = useCallback(async () => {
    const [p, f] = await Promise.all([
      api.GET("/api/v1/library/parts"),
      api.GET("/api/v1/library/footprints"),
    ]);
    if (p.data) setParts(p.data);
    if (f.data) setFootprints(f.data);
  }, []);

  useEffect(() => {
    if (status !== "open") return;
    let cancelled = false;
    void Promise.all([
      api.GET("/api/v1/library/parts"),
      api.GET("/api/v1/library/footprints"),
    ]).then(([p, f]) => {
      if (cancelled) return;
      if (p.data) setParts(p.data);
      if (f.data) setFootprints(f.data);
    });
    return () => {
      cancelled = true;
    };
  }, [status]);

  const items = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    const list =
      mode === "parts"
        ? parts.map((p) => ({
            id: p.id,
            detail: [p.name, p.footprint_id].filter(Boolean).join(" · "),
            missing: p.height == null || !p.tape,
          }))
        : footprints.map((f) => ({ id: f.id, detail: `${f.pads.length} pads`, missing: false }));
    return needle
      ? list.filter(
          (i) => i.id.toLowerCase().includes(needle) || i.detail.toLowerCase().includes(needle),
        )
      : list;
  }, [mode, parts, footprints, filter]);

  const part = mode === "parts" ? parts.find((p) => p.id === selected) : undefined;
  const footprint = mode === "footprints" ? footprints.find((f) => f.id === selected) : undefined;

  const importOpenPnp = async (files: FileList | null) => {
    if (!files?.length) return;
    let packages_xml: string | null = null;
    let parts_xml: string | null = null;
    for (const file of Array.from(files)) {
      const text = await file.text();
      if (text.includes("<openpnp-packages")) packages_xml = text;
      else if (text.includes("<openpnp-parts")) parts_xml = text;
    }
    const result = await run(() =>
      api.POST("/api/v1/library/import/openpnp", {
        body: { packages_xml, parts_xml, overwrite: false },
      }),
    );
    if (result) setReport(result);
    await reload();
  };

  const importLibrary = async (files: FileList | null) => {
    const file = files?.[0];
    if (!file) return;
    let document: LibraryDocument;
    try {
      document = JSON.parse(await file.text()) as LibraryDocument;
    } catch {
      setReport({
        added: [],
        replaced: [],
        filled: [],
        conflicts: [],
        unchanged: 0,
        errors: [`${file.name} is not a library file`],
      });
      return;
    }
    const result = await run(() =>
      api.POST("/api/v1/library/import", { body: { document, overwrite: false } }),
    );
    if (result) setReport(result);
    await reload();
  };

  const exportLibrary = async () => {
    const data = await run(() => api.GET("/api/v1/library/export"));
    if (!data) return;
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = "picnicplace-library.json";
    link.click();
    URL.revokeObjectURL(link.href);
  };

  const savePart = async (updated: Part) => {
    const saved = await run(() =>
      api.PUT("/api/v1/library/parts/{part_id}", {
        params: { path: { part_id: updated.id } },
        body: updated,
      }),
    );
    if (saved) setParts((list) => list.map((p) => (p.id === saved.id ? saved : p)));
  };

  const deletePart = async (id: string) => {
    const result = await api.DELETE("/api/v1/library/parts/{part_id}", {
      params: { path: { part_id: id } },
    });
    if (!result.error) {
      setParts((list) => list.filter((p) => p.id !== id));
      setSelected(null);
    }
  };

  return (
    <div className="workspace">
      <aside className="sidebar">
        <Panel title="Library">
          <div className="segmented segmented-wide">
            {(["parts", "footprints"] as const).map((m) => (
              <button
                key={m}
                type="button"
                className={mode === m ? "segment active" : "segment"}
                onClick={() => {
                  setMode(m);
                  setSelected(null);
                }}
              >
                {m === "parts" ? `Parts (${parts.length})` : `Footprints (${footprints.length})`}
              </button>
            ))}
          </div>
          <label className="search">
            <Search size={14} aria-hidden />
            <input
              className="input"
              placeholder={mode === "parts" ? "Part number, value, footprint" : "Footprint"}
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
            />
          </label>
          <ul className="item-list" aria-label={mode}>
            {items.map((item) => (
              <li key={item.id}>
                <button
                  type="button"
                  className={item.id === selected ? "item active" : "item"}
                  onClick={() => setSelected(item.id)}
                >
                  <span className="item-id">{item.id}</span>
                  <span className="item-detail">
                    {item.detail}
                    {item.missing && <span className="chip chip-warn">to check</span>}
                  </span>
                </button>
              </li>
            ))}
            {items.length === 0 && <li className="text-muted item-empty">Nothing here yet.</li>}
          </ul>
        </Panel>

        <Panel title="Import and export" collapseId="library-import">
          <p className="text-muted">
            Boards bring their own parts: import a KiCad board in Prepare. Here you can add OpenPnP{" "}
            <code>packages.xml</code> and <code>parts.xml</code>, or a library file. Existing values
            are never overwritten.
          </p>
          <div className="button-row">
            <button
              type="button"
              className="button"
              disabled={pending}
              onClick={() => openpnpInput.current?.click()}
            >
              <FileUp size={16} aria-hidden />
              OpenPnP files
            </button>
            <button
              type="button"
              className="button"
              disabled={pending}
              onClick={() => libraryInput.current?.click()}
            >
              <FileUp size={16} aria-hidden />
              Library file
            </button>
            <button
              type="button"
              className="button"
              disabled={pending}
              onClick={() => void exportLibrary()}
            >
              <Download size={16} aria-hidden />
              Export
            </button>
          </div>
          <input
            ref={openpnpInput}
            type="file"
            accept=".xml"
            multiple
            hidden
            onChange={(e) => {
              void importOpenPnp(e.target.files);
              e.target.value = "";
            }}
          />
          <input
            ref={libraryInput}
            type="file"
            accept=".json"
            hidden
            onChange={(e) => {
              void importLibrary(e.target.files);
              e.target.value = "";
            }}
          />
          {error && <p className="note note-danger">{error}</p>}
          {report && <ImportReportView report={report} />}
        </Panel>
      </aside>

      <div className="canvas library-canvas">
        {part ? (
          <PartEditor
            key={part.id}
            part={part}
            footprints={footprints}
            busy={pending}
            onSave={(p) => void savePart(p)}
            onDelete={() => void deletePart(part.id)}
          />
        ) : footprint ? (
          <div className="editor">
            <header className="editor-header">
              <div>
                <h2 className="editor-title">{footprint.id}</h2>
                <p className="text-muted">{footprint.description || footprint.source}</p>
              </div>
            </header>
            <div className="drawing-box drawing-large">
              <FootprintDrawing footprint={footprint} />
              <p className="caption">
                Seen looking down on the part, at 0°. {footprint.pads.length} pads
                {footprint.body
                  ? `; body ${footprint.body.x} × ${footprint.body.y} mm (${footprint.body_basis})`
                  : ""}
                . Used by {parts.filter((p) => p.footprint_id === footprint.id).length} part(s).
              </p>
            </div>
          </div>
        ) : (
          <Placeholder
            icon={Library}
            title="Parts library"
            description={
              parts.length
                ? "Pick a part to see its footprint and tape, and fill in what is missing."
                : "Import a KiCad board in Prepare, or OpenPnP files here, to fill the library."
            }
          />
        )}
      </div>
    </div>
  );
}
