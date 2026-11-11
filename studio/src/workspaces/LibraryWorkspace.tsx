import { Download, FileUp, Library, Search } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api/client";
import { useCore } from "../api/core";
import { useAction } from "../api/useAction";
import type { Footprint, ImportReport, LibraryDocument, Part, Tape } from "../api/types";
import { FootprintDrawing } from "../components/library/FootprintShape";
import { ImportReportView } from "../components/library/ImportReportView";
import { PartEditor } from "../components/library/PartEditor";
import { TapeEditor, TapeTable } from "../components/library/TapeEditor";
import { Panel } from "../components/Panel";
import { Placeholder } from "../components/Placeholder";

type Mode = "parts" | "tapes" | "footprints";

const MODE_LABEL: Record<Mode, string> = {
  parts: "Parts",
  tapes: "Tapes",
  footprints: "Footprints",
};

/** The parts library: parts, the tapes they come in, their footprints, and imports. */
export function LibraryWorkspace() {
  const { status } = useCore();
  const { run, pending, error } = useAction();
  const [parts, setParts] = useState<Part[]>([]);
  const [footprints, setFootprints] = useState<Footprint[]>([]);
  const [tapes, setTapes] = useState<Tape[]>([]);
  const [mode, setMode] = useState<Mode>("parts");
  const [filter, setFilter] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [report, setReport] = useState<ImportReport | null>(null);
  const openpnpInput = useRef<HTMLInputElement>(null);
  const libraryInput = useRef<HTMLInputElement>(null);

  const reload = useCallback(async () => {
    const [p, f, t] = await Promise.all([
      api.GET("/api/v1/library/parts"),
      api.GET("/api/v1/library/footprints"),
      api.GET("/api/v1/library/tapes"),
    ]);
    if (p.data) setParts(p.data);
    if (f.data) setFootprints(f.data);
    if (t.data) setTapes(t.data);
  }, []);

  useEffect(() => {
    if (status !== "open") return;
    let cancelled = false;
    void Promise.all([
      api.GET("/api/v1/library/parts"),
      api.GET("/api/v1/library/footprints"),
      api.GET("/api/v1/library/tapes"),
    ]).then(([p, f, t]) => {
      if (cancelled) return;
      if (p.data) setParts(p.data);
      if (f.data) setFootprints(f.data);
      if (t.data) setTapes(t.data);
    });
    return () => {
      cancelled = true;
    };
  }, [status]);

  const counts = { parts: parts.length, tapes: tapes.length, footprints: footprints.length };
  const used = useCallback((id: string) => parts.filter((p) => p.tape_id === id).length, [parts]);

  const items = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    const list =
      mode === "parts"
        ? parts.map((p) => ({
            id: p.id,
            detail: [p.name, p.footprint_id].filter(Boolean).join(" · "),
            missing:
              p.height == null || !tapes.some((t) => t.id === p.tape_id && t.rotation !== null),
          }))
        : mode === "tapes"
          ? tapes.map((t) => ({
              id: t.id,
              detail: `${t.name || t.id} · ${used(t.id)} parts`,
              missing: t.rotation === null || t.width === null || t.pitch === null,
            }))
          : footprints.map((f) => ({
              id: f.id,
              detail: `${f.pads.length} pads`,
              missing: f.pads.length > 0 && !f.pads.some((p) => p.pin1),
            }));
    return needle
      ? list.filter(
          (i) => i.id.toLowerCase().includes(needle) || i.detail.toLowerCase().includes(needle),
        )
      : list;
  }, [mode, parts, footprints, tapes, filter, used]);

  const part = mode === "parts" ? parts.find((p) => p.id === selected) : undefined;
  const footprint = mode === "footprints" ? footprints.find((f) => f.id === selected) : undefined;
  const tape = mode === "tapes" ? tapes.find((t) => t.id === selected) : undefined;
  const open = (m: Mode, id: string) => {
    setMode(m);
    setFilter("");
    setSelected(id);
  };

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

  const saveTape = async (updated: Tape) => {
    const saved = await run(() =>
      api.PUT("/api/v1/library/tapes/{tape_id}", {
        params: { path: { tape_id: updated.id } },
        body: updated,
      }),
    );
    if (saved) {
      setTapes((list) =>
        list.some((t) => t.id === saved.id)
          ? list.map((t) => (t.id === saved.id ? saved : t))
          : [...list, saved].sort((a, b) => a.id.localeCompare(b.id)),
      );
    }
    return saved;
  };

  /** A tape made from values in the part editor: the library's own if it has that one. */
  const createTape = async (made: Tape) =>
    tapes.find((t) => t.id === made.id) ?? (await saveTape(made));

  const deleteTape = async (id: string) => {
    const result = await api.DELETE("/api/v1/library/tapes/{tape_id}", {
      params: { path: { tape_id: id } },
    });
    if (!result.error) {
      setTapes((list) => list.filter((t) => t.id !== id));
      setSelected(null);
    }
  };

  /** Mark a pad as pin 1 (or clear it, if it was). Drawing only: vision ignores it. */
  const togglePin1 = async (f: Footprint, index: number) => {
    const pads = f.pads.map((pad, i) => ({
      ...pad,
      pin1: i === index ? !pad.pin1 : false,
    }));
    const saved = await run(() =>
      api.PUT("/api/v1/library/footprints/{footprint_id}", {
        params: { path: { footprint_id: f.id } },
        body: { ...f, pads },
      }),
    );
    if (saved) setFootprints((list) => list.map((x) => (x.id === saved.id ? saved : x)));
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
            {(["parts", "tapes", "footprints"] as const).map((m) => (
              <button
                key={m}
                type="button"
                className={mode === m ? "segment active" : "segment"}
                onClick={() => {
                  setMode(m);
                  setSelected(null);
                }}
              >
                {MODE_LABEL[m]} <span className="segment-count">{counts[m]}</span>
              </button>
            ))}
          </div>
          <label className="search">
            <Search size={14} aria-hidden />
            <input
              className="input"
              placeholder={
                { parts: "Part number, value, footprint", tapes: "Tape", footprints: "Footprint" }[
                  mode
                ]
              }
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
            parts={parts}
            footprints={footprints}
            tapes={tapes}
            busy={pending}
            onSave={(p) => void savePart(p)}
            onDelete={() => void deletePart(part.id)}
            onOpenTape={(id) => open("tapes", id)}
            onCreateTape={createTape}
          />
        ) : mode === "tapes" && tapes.length > 0 ? (
          <div className="tape-pane">
            <TapeTable
              tapes={tapes}
              parts={parts}
              selected={selected}
              busy={pending}
              onSelect={setSelected}
              onSave={(t) => void saveTape(t)}
            />
            {tape ? (
              <TapeEditor
                key={`${tape.id}:${JSON.stringify(tape)}`}
                tape={tape}
                parts={parts}
                footprints={footprints}
                busy={pending}
                onSave={(t) => void saveTape(t)}
                onDelete={() => void deleteTape(tape.id)}
                onOpenPart={(id) => open("parts", id)}
              />
            ) : (
              <p className="text-muted note">Pick a tape to see its parts in it.</p>
            )}
          </div>
        ) : footprint ? (
          <div className="editor">
            <header className="editor-header">
              <div>
                <h2 className="editor-title">{footprint.id}</h2>
                <p className="text-muted">{footprint.description || footprint.source}</p>
              </div>
            </header>
            <div className="drawing-box drawing-large">
              <FootprintDrawing
                footprint={footprint}
                onPad={pending ? undefined : (i) => void togglePin1(footprint, i)}
              />
              <p className="caption">
                Click a pad to mark it as pin 1 (red), or click it again to clear it; the mark is
                only drawn, never used by vision. Seen looking down on the part, at 0°.{" "}
                {footprint.pads.length} pads
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
