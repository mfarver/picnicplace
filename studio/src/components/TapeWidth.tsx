import type { Part, Tape } from "../api/types";
import { TAPE_WIDTHS } from "../tape";

/** The width of tape a feeder or strip holder takes, if the owner knows it. */
export function WidthSelect({
  value,
  onChange,
}: {
  value: number | null;
  onChange: (width: number | null) => void;
}) {
  return (
    <select
      className="input"
      value={value ?? ""}
      onChange={(e) => onChange(e.target.value ? Number(e.target.value) : null)}
    >
      <option value="">Not known</option>
      {TAPE_WIDTHS.map((w) => (
        <option key={w} value={w}>
          {w}
        </option>
      ))}
    </select>
  );
}

/** A warning when a feeder takes one width of tape and its part comes in another. */
export function WidthWarning({
  where,
  width,
  part,
  tape,
}: {
  where: string;
  width: number | null;
  part?: Part;
  tape?: Tape;
}) {
  if (width === null || !part || !tape || tape.width === null) return null;
  if (Math.abs(width - tape.width) < 0.01) return null;
  return (
    <p className="note note-warn">
      {where} takes {width} mm tape, but {part.id} comes in {tape.width} mm tape. The part may be
      in the wrong feeder, or have the wrong tape in the Parts workspace.
    </p>
  );
}
