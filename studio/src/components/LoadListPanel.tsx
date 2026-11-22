import { Download, Printer } from "lucide-react";
import type { RunItem } from "../api/types";
import { loadListCsv, loadListHtml, loadRows, sources, tapeLabel } from "../loadList";
import type { PartNeed } from "../loadList";
import { Panel } from "./Panel";

const SHOWN_REFS = 8;

function fileName(title: string, ext: string): string {
  const safe = title.replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "") || "job";
  return `load-list-${safe}.${ext}`;
}

/**
 * The parts a job needs on the machine: what each is, its tape, its references, and where
 * it is loaded, with what is missing first. Printable, and as CSV. In a run, how many of
 * each are left to place.
 */
export function LoadListPanel({
  title,
  parts,
  items,
}: {
  title: string;
  parts: PartNeed[];
  /** The run's placements, when the list is for a run under way. */
  items?: RunItem[];
}) {
  const rows = loadRows(parts, items);
  const missing = rows.filter((r) => !r.loaded).length;

  const download = () => {
    const blob = new Blob([loadListCsv(rows)], { type: "text/csv" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = fileName(title, "csv");
    link.click();
    URL.revokeObjectURL(link.href);
  };

  const print = () => {
    const when = `Made ${new Date().toLocaleString()}${items ? ", during a run" : ""}.`;
    const page = window.open("", "_blank");
    if (!page) return;
    page.document.write(loadListHtml(title, rows, when));
    page.document.close();
    page.focus();
    page.print();
  };

  return (
    <Panel
      title={missing ? `Load list (${missing} to load)` : "Load list"}
      collapseId="run-parts"
    >
      <div className="button-row">
        <button type="button" className="button" onClick={print}>
          <Printer size={16} aria-hidden />
          Print
        </button>
        <button type="button" className="button" onClick={download}>
          <Download size={16} aria-hidden />
          CSV
        </button>
      </div>
      <ul className="item-list load-list">
        {rows.map(({ need, loaded, left }) => {
          const refs = need.refs ?? [];
          const shown =
            refs.slice(0, SHOWN_REFS).join(", ") +
            (refs.length > SHOWN_REFS ? `, +${refs.length - SHOWN_REFS}` : "");
          const about = [need.value, need.footprint_id, tapeLabel(need.tape) || "tape not known"]
            .filter(Boolean)
            .join(" · ");
          return (
            <li key={need.part_id ?? "-"} className="load-row">
              <div className="run-row">
                <span
                  className={`run-dot ${loaded ? "run-placed" : "run-blocked"}`}
                  aria-label={loaded ? "loaded" : "to load"}
                />
                <span className="item-id">{need.part_id ?? "no part number"}</span>
                <span className="text-muted">
                  ×{left}
                  {items && left !== need.count ? ` of ${need.count}` : ""}
                </span>
                <span className={loaded ? "text-muted run-where" : "text-danger run-where"}>
                  {loaded ? sources(need.slot_ids) : (need.problem ?? "no feeder")}
                </span>
              </div>
              <div className="load-about text-muted" title={refs.join(", ")}>
                {about}
                {shown ? ` · ${shown}` : ""}
              </div>
            </li>
          );
        })}
      </ul>
    </Panel>
  );
}
