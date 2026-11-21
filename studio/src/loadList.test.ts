import { describe, expect, it } from "vitest";
import type { RunItem } from "./api/types";
import { loadListCsv, loadListHtml, loadRows, tapeLabel } from "./loadList";
import type { PartNeed } from "./loadList";

function need(part_id: string, slot_ids: string[], extra: Partial<PartNeed> = {}): PartNeed {
  return {
    part_id,
    count: 2,
    slot_ids,
    problem: null,
    refs: ["R1", "R2"],
    value: "10k",
    description: "",
    manufacturer: "",
    footprint_id: "R_0603",
    tape: {
      id: "8mm-paper-p4-r90",
      name: "8 mm paper, 4 mm pitch, 90°",
      kind: "paper",
      width: 8,
      pitch: 4,
      rotation: 90,
      basis: "unknown",
      source: "",
      notes: "",
    },
    ...extra,
  } as PartNeed;
}

function item(part_id: string, status: RunItem["status"]): RunItem {
  return { part_id, status } as RunItem;
}

describe("the load list", () => {
  it("puts what is missing first, then by slot number, strips and trays last", () => {
    const rows = loadRows([
      need("T-STRIP", ["T1"]),
      need("SLOT-12", ["12"]),
      need("MISSING", [], { problem: "no feeder holds MISSING" }),
      need("SLOT-3", ["3"]),
    ]);
    expect(rows.map((r) => r.need.part_id)).toEqual(["MISSING", "SLOT-3", "SLOT-12", "T-STRIP"]);
    expect(rows.map((r) => r.loaded)).toEqual([false, true, true, true]);
  });

  it("counts what is left to place in a run", () => {
    const rows = loadRows(
      [need("A", ["1"]), need("B", ["2"])],
      [item("A", "placed"), item("A", "pending"), item("B", "placed"), item("B", "skipped")],
    );
    expect(rows.map((r) => [r.need.part_id, r.left])).toEqual([
      ["A", 1],
      ["B", 0],
    ]);
  });

  it("names a tape by width, kind and pitch", () => {
    expect(tapeLabel(need("A", ["1"]).tape)).toBe("8 mm paper, 4 mm pitch");
    expect(tapeLabel(null)).toBe("");
  });

  it("writes CSV with quoted fields", () => {
    const csv = loadListCsv(loadRows([need("A", ["1"], { description: 'RES 10k, 1%, "thick"' })]));
    const [header, line] = csv.trim().split("\n");
    expect(header?.startsWith("Status,Part number,Value")).toBe(true);
    expect(line).toBe(
      'loaded,A,10k,"RES 10k, 1%, ""thick""",,R_0603,"8 mm paper, 4 mm pitch",2,R1 R2,slot 1',
    );
  });

  it("escapes the printed page", () => {
    const html = loadListHtml("<board>", loadRows([need("A&B", [])]), "Made now.");
    expect(html).toContain("Load list: &lt;board&gt;");
    expect(html).toContain("<td>A&amp;B</td>");
    expect(html).toContain("1 parts, 1 to load.");
  });
});
