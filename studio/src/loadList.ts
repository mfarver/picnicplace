import type { JobPlan, RunItem, Tape } from "./api/types";

export type PartNeed = JobPlan["parts"][number];

/** One line of the load list: a part the job uses, and whether it is on the machine. */
export interface LoadRow {
  need: PartNeed;
  /** Loaded on a feeder, strip, or tray the run can use. */
  loaded: boolean;
  /** Placements of it still to do, in a run; else every placement it has. */
  left: number;
}

/** "8 mm embossed, 4 mm pitch"; "" when the tape is not known. */
export function tapeLabel(tape: Tape | null | undefined): string {
  if (!tape || tape.width == null) return "";
  const kind = tape.kind && tape.kind !== "unknown" ? ` ${tape.kind}` : "";
  const pitch = tape.pitch != null ? `, ${tape.pitch} mm pitch` : "";
  return `${tape.width} mm${kind}${pitch}`;
}

/** "slots 3, 7 and T1": feeder slots by number, strips and trays by ID. */
export function sources(ids: string[]): string {
  const slots = ids.filter((id) => /^\d+$/.test(id));
  const fixed = ids.filter((id) => !/^\d+$/.test(id));
  const parts = [
    ...(slots.length ? [`slot${slots.length > 1 ? "s" : ""} ${slots.join(", ")}`] : []),
    ...fixed,
  ];
  return parts.join(", ");
}

/**
 * The load list: what is missing first, then by where it is loaded; in a run, how many of
 * each part are left to place (``items``, the run's placements).
 */
export function loadRows(parts: PartNeed[], items?: RunItem[]): LoadRow[] {
  const left = new Map<string | null, number>();
  if (items) {
    for (const i of items) {
      if (i.status === "pending" || i.status === "blocked") {
        left.set(i.part_id, (left.get(i.part_id) ?? 0) + 1);
      }
    }
  }
  const rows = parts.map((need) => ({
    need,
    loaded: !need.problem && need.slot_ids.length > 0,
    left: items ? (left.get(need.part_id) ?? 0) : need.count,
  }));
  const where = (r: LoadRow) => {
    const first = r.need.slot_ids[0];
    if (first === undefined) return [0, ""] as const;
    return /^\d+$/.test(first) ? ([1, first.padStart(4, "0")] as const) : ([2, first] as const);
  };
  return rows.sort((a, b) => {
    if (a.loaded !== b.loaded) return a.loaded ? 1 : -1;
    const [ka, sa] = where(a);
    const [kb, sb] = where(b);
    if (ka !== kb) return ka - kb;
    if (sa !== sb) return sa < sb ? -1 : 1;
    return (a.need.part_id ?? "").localeCompare(b.need.part_id ?? "");
  });
}

const COLUMNS = [
  "Status",
  "Part number",
  "Value",
  "Description",
  "Manufacturer",
  "Footprint",
  "Tape",
  "Quantity",
  "References",
  "Where",
] as const;

function cells(r: LoadRow): string[] {
  const n = r.need;
  return [
    r.loaded ? "loaded" : (n.problem ?? "not loaded"),
    n.part_id ?? "(no part number)",
    n.value ?? "",
    n.description ?? "",
    n.manufacturer ?? "",
    n.footprint_id ?? "",
    tapeLabel(n.tape),
    String(r.left),
    (n.refs ?? []).join(" "),
    sources(n.slot_ids),
  ];
}

function csvField(value: string): string {
  return /[",\n]/.test(value) ? `"${value.replace(/"/g, '""')}"` : value;
}

/** The load list as CSV, for a spreadsheet or a purchasing system. */
export function loadListCsv(rows: LoadRow[]): string {
  const lines = [COLUMNS.join(","), ...rows.map((r) => cells(r).map(csvField).join(","))];
  return lines.join("\n") + "\n";
}

function escape(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/** The load list as a page to print (or save as PDF): what is missing first. */
export function loadListHtml(title: string, rows: LoadRow[], when: string): string {
  const missing = rows.filter((r) => !r.loaded).length;
  const head = COLUMNS.map((c) => `<th>${c}</th>`).join("");
  const body = rows
    .map((r) => {
      const tds = cells(r)
        .map((c, i) => `<td${i === 0 ? ' class="status"' : ""}>${escape(c)}</td>`)
        .join("");
      return `<tr class="${r.loaded ? "loaded" : "missing"}">${tds}</tr>`;
    })
    .join("\n");
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Load list: ${escape(title)}</title>
<style>
  body { font: 11px system-ui, sans-serif; color: #111; margin: 16px; }
  h1 { font-size: 16px; margin: 0 0 4px; }
  p { margin: 0 0 12px; color: #555; }
  table { border-collapse: collapse; width: 100%; }
  th, td { border-bottom: 1px solid #ccc; padding: 3px 6px; text-align: left; vertical-align: top; }
  th { background: #eee; }
  tr.missing td.status { color: #b00020; font-weight: 600; }
  tr.loaded { color: #666; }
  @media print { body { margin: 0; } tr { break-inside: avoid; } }
</style>
</head>
<body>
<h1>Load list: ${escape(title)}</h1>
<p>${rows.length} parts, ${missing} to load. ${escape(when)}</p>
<table>
<thead><tr>${head}</tr></thead>
<tbody>
${body}
</tbody>
</table>
</body>
</html>
`;
}
