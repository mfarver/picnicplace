import { Download, Send } from "lucide-react";
import { useState } from "react";
import { api, mediaUrl } from "../../api/client";
import { useAction } from "../../api/useAction";
import type { RevisionSummary } from "../../api/types";
import { Field } from "../Form";

const AUTHOR_KEY = "picnicplace.author";

function loadAuthor(): string {
  try {
    return localStorage.getItem(AUTHOR_KEY) ?? "";
  } catch {
    return "";
  }
}

function saveAuthor(name: string): void {
  try {
    localStorage.setItem(AUTHOR_KEY, name);
  } catch {
    // Remembering the name is only a convenience.
  }
}

/** A revision's job file, to save or take to another machine. */
function revisionFileUrl(jobId: string, number: number): string {
  return mediaUrl(`/api/v1/jobs/${encodeURIComponent(jobId)}/revisions/${number}/file`);
}

/**
 * A job's published revisions, and publishing the saved job as the next one. A revision
 * never changes, holds a copy of the parts, footprints, and tapes the job uses, and is
 * what a machine runs (spec §5.4).
 */
export function JobRevisions({
  jobId,
  revisions,
  unsaved,
  onPublished,
}: {
  jobId: string;
  revisions: RevisionSummary[];
  /** The job has changes not yet saved: a revision is made from the saved job. */
  unsaved: boolean;
  onPublished: () => void;
}) {
  const { run, pending, error } = useAction();
  const [author, setAuthor] = useState(loadAuthor);
  const [reason, setReason] = useState("");
  const [missing, setMissing] = useState<string[]>([]);

  const publish = async () => {
    saveAuthor(author.trim());
    const published = await run(() =>
      api.POST("/api/v1/jobs/{job_id}/revisions", {
        params: { path: { job_id: jobId } },
        body: { author: author.trim(), reason: reason.trim() },
      }),
    );
    if (!published) return;
    setReason("");
    setMissing(published.design.missing);
    onPublished();
  };

  return (
    <>
      {revisions.length === 0 ? (
        <p className="text-muted">Not published yet: a machine runs a published revision.</p>
      ) : (
        <ul className="revision-list">
          {[...revisions].reverse().map((r) => (
            <li key={r.number}>
              <div>
                <span className="item-id">r{r.number}</span>
                <span className="text-muted">
                  {" "}
                  {r.author} · {new Date(r.created).toLocaleString()}
                </span>
                {r.reason && <div className="revision-reason">{r.reason}</div>}
              </div>
              <a
                className="icon-button"
                href={revisionFileUrl(jobId, r.number)}
                download={`${jobId}-r${String(r.number).padStart(4, "0")}.json`}
                title={`Download revision ${r.number}'s job file`}
              >
                <Download size={14} aria-label={`Download revision ${r.number}`} />
              </a>
            </li>
          ))}
        </ul>
      )}
      <Field label="Your name">
        <input className="input" value={author} onChange={(e) => setAuthor(e.target.value)} />
      </Field>
      <Field label="What changed">
        <input
          className="input"
          value={reason}
          placeholder={revisions.length ? "Such as: turned U3 to match its tape" : "First run"}
          onChange={(e) => setReason(e.target.value)}
        />
      </Field>
      <div className="button-row">
        <button
          type="button"
          className="button primary"
          disabled={pending || unsaved || !author.trim()}
          onClick={() => void publish()}
        >
          <Send size={16} aria-hidden />
          Publish r{(revisions.at(-1)?.number ?? 0) + 1}
        </button>
      </div>
      {unsaved && <p className="note text-muted">Save the job first: it is published as saved.</p>}
      {missing.length > 0 && (
        <p className="note note-warn">
          Not in the library, so not in the revision: {missing.join(", ")}. Add them to the
          library and publish again.
        </p>
      )}
      {error && <p className="note note-danger">{error}</p>}
    </>
  );
}
