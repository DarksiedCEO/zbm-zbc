// Bug sweep E, F-5: login rate limiting. Failed password attempts are counted
// per client key (the first X-Forwarded-For / X-Real-IP address, which the
// host sets on Vercel) AND across all clients, in a sliding window, per
// process. While either budget is spent every attempt is refused with 429 —
// the right password included — before any scrypt work is done. A spoofable
// per-client key (a dashboard exposed without a proxy that sets the header)
// is still bounded by the global budget. The clock is injected (monotonic
// milliseconds) so tests never depend on wall-clock time.

export type LimiterOptions = { windowMs: number; perKey: number; global: number; maxKeys: number };
export const LOGIN_LIMITS: LimiterOptions = { windowMs: 15 * 60_000, perKey: 5, global: 50, maxKeys: 10_000 };

export class FailureLimiter {
  private readonly byKey = new Map<string, number[]>();
  private all: number[] = [];
  private readonly opts: LimiterOptions;
  private readonly now: () => number;

  // (no TypeScript parameter properties: `node --test` runs this file with type stripping only)
  constructor(opts: LimiterOptions = LOGIN_LIMITS, now: () => number = () => performance.now()) {
    this.opts = opts;
    this.now = now;
  }

  private prune(list: number[], t: number): number[] {
    const cutoff = t - this.opts.windowMs;
    let i = 0;
    while (i < list.length && list[i] <= cutoff) i++;
    return i === 0 ? list : list.slice(i);
  }

  /** null when an attempt may proceed, else the seconds until one may. */
  retryAfterS(key: string): number | null {
    const t = this.now();
    this.all = this.prune(this.all, t);
    const mine = this.prune(this.byKey.get(key) ?? [], t);
    if (mine.length) this.byKey.set(key, mine);
    else this.byKey.delete(key);
    const blockers: number[] = [];
    if (this.all.length >= this.opts.global) blockers.push(this.all[this.all.length - this.opts.global]);
    if (mine.length >= this.opts.perKey) blockers.push(mine[mine.length - this.opts.perKey]);
    if (!blockers.length) return null;
    const until = Math.max(...blockers) + this.opts.windowMs;
    return Math.max(1, Math.ceil((until - t) / 1000));
  }

  fail(key: string): void {
    const t = this.now();
    this.all.push(t);
    const mine = this.byKey.get(key) ?? [];
    mine.push(t);
    this.byKey.delete(key); // re-insert: Map order = least recently failed first
    this.byKey.set(key, mine);
    while (this.byKey.size > this.opts.maxKeys) {
      const oldest = this.byKey.keys().next().value as string;
      this.byKey.delete(oldest);
    }
  }
}

export function clientKey(headers: Headers): string {
  const xff = headers.get("x-forwarded-for");
  const first = xff ? xff.split(",")[0].trim() : "";
  const ip = first || (headers.get("x-real-ip") ?? "").trim();
  return ip ? ip.slice(0, 64) : "unknown";
}
