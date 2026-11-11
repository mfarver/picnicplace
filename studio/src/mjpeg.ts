/**
 * Reads an MJPEG stream (multipart/x-mixed-replace, each part with a Content-Length, as
 * the core sends it). Read with fetch rather than an <img>, because aborting a fetch
 * closes its connection, while a browser can keep a multipart image loading after its
 * element is gone; a few of those use up the browser's connections to the core (six in
 * Chrome) and every other request then waits forever.
 */

const HEADER_END = new Uint8Array([13, 10, 13, 10]);
const decoder = new TextDecoder();

function indexOf(data: Uint8Array, find: Uint8Array, from = 0): number {
  outer: for (let i = from; i <= data.length - find.length; i++) {
    for (let j = 0; j < find.length; j++) if (data[i + j] !== find[j]) continue outer;
    return i;
  }
  return -1;
}

/** Splits multipart bytes into frames, as they arrive. */
export class FrameParser {
  private buffer: Uint8Array<ArrayBuffer> = new Uint8Array(0);

  /** Adds bytes; returns the frames now complete. */
  push(chunk: Uint8Array): Uint8Array<ArrayBuffer>[] {
    const joined = new Uint8Array(this.buffer.length + chunk.length);
    joined.set(this.buffer);
    joined.set(chunk, this.buffer.length);
    this.buffer = joined;
    const frames: Uint8Array<ArrayBuffer>[] = [];
    for (;;) {
      const end = indexOf(this.buffer, HEADER_END);
      if (end < 0) break;
      const headers = decoder.decode(this.buffer.subarray(0, end));
      const match = /content-length:\s*(\d+)/i.exec(headers);
      if (!match) {
        // Not a part we can size: drop its headers and look for the next.
        this.buffer = this.buffer.slice(end + HEADER_END.length);
        continue;
      }
      const start = end + HEADER_END.length;
      const length = Number(match[1]);
      if (this.buffer.length < start + length) break;
      frames.push(this.buffer.slice(start, start + length));
      this.buffer = this.buffer.slice(start + length);
    }
    return frames;
  }
}

/**
 * Streams `url`, calling `onFrame` with each JPEG. Resolves when the stream ends; rejects
 * on a network or HTTP error, but not when `signal` aborts it.
 */
export async function readMjpeg(
  url: string,
  signal: AbortSignal,
  onFrame: (jpeg: Blob) => void,
): Promise<void> {
  try {
    const response = await fetch(url, { signal });
    if (!response.ok || !response.body) throw new Error(`HTTP ${response.status}`);
    const reader = response.body.getReader();
    const parser = new FrameParser();
    for (;;) {
      const { done, value } = await reader.read();
      if (done) return;
      const frames = parser.push(value);
      const last = frames.at(-1);
      // Only the newest frame is worth showing.
      if (last) onFrame(new Blob([last], { type: "image/jpeg" }));
    }
  } catch (e) {
    if (signal.aborted) return;
    throw e;
  }
}
