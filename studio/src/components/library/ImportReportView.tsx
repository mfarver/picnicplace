import type { ImportReport } from "../../api/types";

const show = (value: unknown) =>
  value === null || value === undefined
    ? "none"
    : typeof value === "object"
      ? JSON.stringify(value)
      : String(value);

/** What an import did to the library: added, filled in, and values kept despite a difference. */
export function ImportReportView({ report }: { report: ImportReport }) {
  const filled = report.filled.reduce((n, f) => n + f.fields.length, 0);
  return (
    <div className="import-report">
      <p className="note">
        Added {report.added.length}, filled in {filled} value{filled === 1 ? "" : "s"}
        {report.replaced.length ? `, replaced ${report.replaced.length}` : ""}, unchanged{" "}
        {report.unchanged}.
      </p>
      {report.errors.map((e) => (
        <p key={e} className="note note-danger">
          {e}
        </p>
      ))}
      {report.conflicts.length > 0 && (
        <details className="report-details">
          <summary className="note note-warn">
            {report.conflicts.length} value{report.conflicts.length === 1 ? "" : "s"} differ; the
            library's were kept
          </summary>
          <ul className="report-list">
            {report.conflicts.map((c) => (
              <li key={`${c.kind}:${c.id}:${c.field}`}>
                <strong>{c.id}</strong> {c.field}: kept {show(c.current)}, import has{" "}
                {show(c.incoming)}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}
