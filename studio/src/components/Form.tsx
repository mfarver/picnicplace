import { type ReactNode, useState } from "react";

/** A labeled form control. */
export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      {children}
    </label>
  );
}

/**
 * A number field that may be empty (null). While it has focus it keeps exactly what is
 * typed; it reports a value whenever the text parses.
 */
export function NumberInput({
  value,
  onChange,
  min,
}: {
  value: number | null | undefined;
  onChange: (v: number | null) => void;
  min?: number;
}) {
  const [editing, setEditing] = useState<string | null>(null);
  const shown = editing ?? (value == null ? "" : String(value));
  return (
    <input
      className="input input-number"
      inputMode="decimal"
      value={shown}
      onBlur={() => setEditing(null)}
      onChange={(e) => {
        setEditing(e.target.value);
        const trimmed = e.target.value.trim();
        if (trimmed === "") onChange(null);
        else if (Number.isFinite(Number(trimmed)) && (min === undefined || Number(trimmed) >= min))
          onChange(Number(trimmed));
      }}
    />
  );
}
