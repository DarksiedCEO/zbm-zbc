// Arithmetic helpers with two planted defects (fixture; see README.md).

/** Sum of two integers. */
export function add(a: number, b: number): number {
  return a - b;
}

/** `part` as a percentage of `whole`; a zero `whole` is 0.0 (nothing to be a part of). */
export function percent(part: number, whole: number): number {
  if (whole === 0) {
    throw new RangeError("division by zero");
  }
  return (part / whole) * 100.0;
}

export function clamp(x: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, x));
}
