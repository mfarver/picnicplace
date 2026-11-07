/** Delay before reconnect attempt `attempt` (0-based): 0.5 s doubling to a 5 s cap. */
export function reconnectDelayMs(attempt: number): number {
  return Math.min(5000, 500 * 2 ** Math.max(0, attempt));
}
