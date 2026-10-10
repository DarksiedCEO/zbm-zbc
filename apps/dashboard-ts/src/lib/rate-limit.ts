// Bug sweep E, F-5: login rate limiting, per process, sliding window, with an
// injected monotonic clock (tests never depend on wall-clock time).
//
// Per client (the first X-Forwarded-For / X-Real-IP address, which the host
// sets on Vercel): a HARD limit. AEGIS M-1 (Oct 9 2026): an attempt is
// RESERVED synchronously, before any await (body read, scrypt), so parallel
// requests from one client cannot all pass the check before the first failure
// is recorded; a reservation counts as a failure until the password is
// verified, and only a success gives it back.
//
// Across all clients: a BACKOFF, never a block. AEGIS M-2: a hard global limit
// let 50 bad attempts from anywhere lock the owner out indefinitely. Once the
// global failure budget is spent, password checks are serialised (one scrypt
// at a time, per process), which bounds the guess rate from rotating or
// spoofed client keys while the right password still gets through.
//
// Wave F (dashboard M-3): the serialised queue is BOUNDED. Unbounded, a flood
// queued every guess (each one a real scrypt check) and the owner waited behind
// all of them (25 s seen). Past ``maxQueued`` waiting checks a request is
// answered 429 at once (no password check, not counted as a failure), and
// ``cleanSlots`` further places are kept for clients with ZERO failures and no
// attempt in flight (the owner): they wait in their own lane, which is served
// first, so a flood from clients that already failed can never starve them.
// The per-client hard limit is unchanged. A flood from ever-fresh client keys
// looks clean too: the clean lane is bounded as well, so the worst case for the
// owner is a prompt 429 (Retry-After: 1), never an unbounded wait.

export type LimiterOptions = {
  windowMs: number;
  perKey: number;
  global: number;
  maxKeys: number;
  maxQueued?: number;
  cleanSlots?: number;
};
export const LOGIN_LIMITS: LimiterOptions = {
  windowMs: 15 * 60_000,
  perKey: 5,
  global: 50,
  maxKeys: 10_000,
  maxQueued: 8,
  cleanSlots: 2,
};

/** ``clean``: this client had no failure and no attempt in flight when the reservation was taken. */
export type Reservation = { key: string; at: number; released: boolean; clean: boolean };

/** The serialised queue is full: answer 429 without checking the password. */
export class ThrottleQueueFull extends Error {
  constructor() {
    super("login checks are queued at capacity; try again shortly");
    this.name = "ThrottleQueueFull";
  }
}

type Waiter = { go: () => void };

export class FailureLimiter {
  private readonly byKey = new Map<string, Reservation[]>();
  private all: number[] = [];
  private busy = false;
  private readonly queue: Waiter[] = [];
  private readonly cleanQueue: Waiter[] = [];
  private readonly opts: LimiterOptions;
  private readonly now: () => number;

  // (no TypeScript parameter properties: `node --test` runs this file with type stripping only)
  constructor(opts: LimiterOptions = LOGIN_LIMITS, now: () => number = () => performance.now()) {
    this.opts = opts;
    this.now = now;
  }

  private live(key: string, t: number): Reservation[] {
    const cutoff = t - this.opts.windowMs;
    const mine = (this.byKey.get(key) ?? []).filter((r) => !r.released && r.at > cutoff);
    if (mine.length) this.byKey.set(key, mine);
    else this.byKey.delete(key);
    return mine;
  }

  /** Synchronous: a reservation, or the seconds until this client may try again. */
  reserve(key: string): Reservation | { retryAfterS: number } {
    const t = this.now();
    const mine = this.live(key, t);
    if (mine.length >= this.opts.perKey) {
      const until = mine[mine.length - this.opts.perKey].at + this.opts.windowMs;
      return { retryAfterS: Math.max(1, Math.ceil((until - t) / 1000)) };
    }
    const r: Reservation = { key, at: t, released: false, clean: mine.length === 0 };
    mine.push(r);
    this.byKey.delete(key); // re-insert: Map order = least recently active first
    this.byKey.set(key, mine);
    while (this.byKey.size > this.opts.maxKeys) {
      const oldest = this.byKey.keys().next().value as string;
      this.byKey.delete(oldest);
    }
    return r;
  }

  /** The password was right: the reservation is given back. */
  succeed(r: Reservation): void {
    r.released = true;
    this.live(r.key, this.now());
  }

  /** No password was checked (the queue was full): the reservation is given back and nothing is counted. */
  release(r: Reservation): void {
    this.succeed(r);
  }

  /** Waiting checks: [general queue, clean lane] (tests). */
  queued(): [number, number] {
    return [this.queue.length, this.cleanQueue.length];
  }

  /** The password was wrong: the reservation stays (it is the failure) and the global count grows. */
  fail(): void {
    this.all.push(this.now());
  }

  globalSpent(): boolean {
    const cutoff = this.now() - this.opts.windowMs;
    let i = 0;
    while (i < this.all.length && this.all[i] <= cutoff) i++;
    if (i) this.all = this.all.slice(i);
    return this.all.length >= this.opts.global;
  }

  /** Runs ``check`` directly, or one at a time once the global budget is spent (backoff, never a block). Past
   * the global budget at most ``maxQueued`` checks wait (plus ``cleanSlots`` for clean clients, served first);
   * beyond that it rejects with ``ThrottleQueueFull`` before checking anything (the caller answers 429). */
  async throttled<T>(check: () => Promise<T>, clean = false): Promise<T> {
    if (!this.globalSpent() && !this.busy) return check();
    if (this.busy) {
      const lane = clean && this.cleanQueue.length < (this.opts.cleanSlots ?? 0) ? this.cleanQueue : this.queue;
      if (lane === this.queue && this.queue.length >= (this.opts.maxQueued ?? Infinity)) throw new ThrottleQueueFull();
      await new Promise<void>((resolve) => lane.push({ go: resolve }));
    }
    this.busy = true; // held from here (or handed over by the previous check) until this check settles
    try {
      return await check();
    } finally {
      const next = this.cleanQueue.shift() ?? this.queue.shift();
      if (next) next.go();
      else this.busy = false;
    }
  }
}

export function clientKey(headers: Headers): string {
  const xff = headers.get("x-forwarded-for");
  const first = xff ? xff.split(",")[0].trim() : "";
  const ip = first || (headers.get("x-real-ip") ?? "").trim();
  return ip ? ip.slice(0, 64) : "unknown";
}

/** AEGIS L-1/L-2: a login or logout POST must carry an Origin whose scheme AND host are this site's: the
 * Host header, with the scheme the host's proxy reports (X-Forwarded-Proto on Vercel), else the request's own
 * (Next's ``req.url`` names the server's bind host, not the one the browser used). */
export function sameOrigin(req: Request): boolean {
  const origin = req.headers.get("origin");
  const host = req.headers.get("host");
  if (!origin || origin === "null" || !host) return false;
  try {
    const fwd = (req.headers.get("x-forwarded-proto") ?? "").split(",")[0].trim().toLowerCase();
    const scheme = fwd === "https" || fwd === "http" ? fwd : new URL(req.url).protocol.replace(":", "");
    return new URL(origin).origin === new URL(`${scheme}://${host}`).origin;
  } catch {
    return false;
  }
}
