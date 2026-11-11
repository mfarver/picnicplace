import { describe, expect, it } from "vitest";
import { FrameParser } from "./mjpeg";

const encoder = new TextEncoder();

function part(body: string): Uint8Array {
  return encoder.encode(
    `--frame\r\nContent-Type: image/jpeg\r\nContent-Length: ${body.length}\r\n\r\n${body}\r\n`,
  );
}

const text = (frames: Uint8Array[]) => frames.map((f) => new TextDecoder().decode(f));

describe("FrameParser", () => {
  it("splits whole parts", () => {
    const parser = new FrameParser();
    const both = new Uint8Array([...part("one"), ...part("three")]);
    expect(text(parser.push(both))).toEqual(["one", "three"]);
  });

  it("waits for a part that arrives in pieces", () => {
    const parser = new FrameParser();
    const bytes = part("a frame\r\n\r\nwith a blank line");
    expect(parser.push(bytes.subarray(0, 20))).toEqual([]);
    expect(parser.push(bytes.subarray(20, 60))).toEqual([]);
    expect(text(parser.push(bytes.subarray(60)))).toEqual(["a frame\r\n\r\nwith a blank line"]);
  });
});
