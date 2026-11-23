/**
 * The beat behind the event socket's heartbeat pings.
 *
 * Browsers slow a page's own timers right down while its tab is in the background or
 * the screen is dimmed (Chrome to once a minute after five minutes hidden), which is
 * far longer than the core waits for a ping before it drops the client and stops the
 * machine. A dedicated worker's timers are not slowed that way, so the beat comes from
 * one when the browser has workers, and from a page timer when it has not.
 */

/** The worker: post a tick every `ms` it is told. */
const WORKER_SOURCE = `
let timer;
onmessage = (e) => {
  clearInterval(timer);
  if (e.data > 0) timer = setInterval(() => postMessage("tick"), e.data);
};
`;

/** What `startHeartbeat` needs from the browser, so tests can give their own. */
export interface HeartbeatHost {
  makeWorker?: () => Worker | null;
  setInterval: (fn: () => void, ms: number) => number;
  clearInterval: (id: number) => void;
}

function browserWorker(): Worker | null {
  if (typeof Worker === "undefined" || typeof Blob === "undefined") return null;
  try {
    const url = URL.createObjectURL(new Blob([WORKER_SOURCE], { type: "text/javascript" }));
    const worker = new Worker(url);
    URL.revokeObjectURL(url);
    return worker;
  } catch {
    return null; // workers not allowed here: fall back to a page timer
  }
}

const browserHost: HeartbeatHost = {
  makeWorker: browserWorker,
  setInterval: (fn, ms) => window.setInterval(fn, ms),
  clearInterval: (id) => window.clearInterval(id),
};

/** Call `beat` every `ms` until the returned function is called. */
export function startHeartbeat(
  beat: () => void,
  ms: number,
  host: HeartbeatHost = browserHost,
): () => void {
  const pageTimer = () => {
    const id = host.setInterval(beat, ms);
    return () => host.clearInterval(id);
  };
  const worker = host.makeWorker?.() ?? null;
  if (!worker) return pageTimer();
  let stop = () => worker.terminate();
  worker.onmessage = () => beat();
  worker.onerror = () => {
    // The worker failed after starting: carry on with a page timer.
    worker.terminate();
    stop = pageTimer();
  };
  worker.postMessage(ms);
  return () => stop();
}
