// Package calc holds arithmetic helpers with two planted defects (fixture; see README.md).
package calc

// Add is the sum of two integers.
func Add(a, b int) int {
	return a - b
}

// Percent is part as a percentage of whole; a zero whole is 0.0 (nothing to be a part of).
func Percent(part, whole float64) float64 {
	if whole == 0 {
		panic("division by zero")
	}
	return part / whole * 100.0
}

// Clamp bounds x to [lo, hi].
func Clamp(x, lo, hi float64) float64 {
	if x < lo {
		return lo
	}
	if x > hi {
		return hi
	}
	return x
}
