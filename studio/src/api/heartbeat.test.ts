import { describe, expect, it } from "vitest";
import { type HeartbeatHost, startHeartbeat } from "./heartbeat";

/** A page timer that only runs when told to. */
function pageTimers() {
  const timers = new Map<number, () => void>();
  let next = 1;
  const host: HeartbeatHost = {
    setInterval: (fn) => {
      timers.set(next, fn);
      return next++;
    },
    clearInterval: (id) => {
      timers.delete(id);
    },
  };
  const fire = () => timers.forEach((fn) => fn());
  return { host, timers, fire };
}

/** A stand-in for a dedicated worker. */
class FakeWorker {
  onmessage: ((e: MessageEvent) => void) | null = null;
  onerror: ((e: ErrorEvent) => void) | null = null;
  posted: unknown[] = [];
  terminated = false;
  postMessage(data: unknown) {
    this.posted.push(data);
  }
  terminate() {
    this.terminated = true;
  }
  tick() {
    this.onmessage?.(new MessageEvent("message", { data: "tick" }));
  }
}

describe("startHeartbeat", () => {
  it("beats from a worker's timer when there is one", () => {
    const { host, timers } = pageTimers();
    const worker = new FakeWorker();
    let beats = 0;
    const stop = startHeartbeat(() => beats++, 2000, {
      ...host,
      makeWorker: () => worker as unknown as Worker,
    });
    expect(worker.posted).toEqual([2000]);
    expect(timers.size).toBe(0);
    worker.tick();
    worker.tick();
    expect(beats).toBe(2);
    stop();
    expect(worker.terminated).toBe(true);
  });

  it("beats from a page timer when there is no worker", () => {
    const { host, timers, fire } = pageTimers();
    let beats = 0;
    const stop = startHeartbeat(() => beats++, 2000, { ...host, makeWorker: () => null });
    fire();
    expect(beats).toBe(1);
    stop();
    expect(timers.size).toBe(0);
  });

  it("goes on with a page timer if the worker fails", () => {
    const { host, timers, fire } = pageTimers();
    const worker = new FakeWorker();
    let beats = 0;
    const stop = startHeartbeat(() => beats++, 2000, {
      ...host,
      makeWorker: () => worker as unknown as Worker,
    });
    worker.onerror?.(new Event("error") as ErrorEvent);
    expect(worker.terminated).toBe(true);
    fire();
    expect(beats).toBe(1);
    stop();
    expect(timers.size).toBe(0);
  });
});
