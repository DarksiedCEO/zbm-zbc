package edge

import (
	"math"
	"net/netip"
	"sync"
	"time"
)

// DefaultMaxKeys bounds every limiter's table. A flood from more distinct clients than this is refused (fail
// closed) rather than letting the table, and the process's memory, grow without bound.
const DefaultMaxKeys = 16384

// Limiter is a per-client token bucket: each client (an IPv4 address or an IPv6 /64) may make `burst` requests at
// once and `perMinute` requests a minute after that. In memory: correct for the one replica every public service
// runs (.railway/railway.ts numReplicas: 1); a restart forgets every bucket.
type Limiter struct {
	mu        sync.Mutex
	perSecond float64
	burst     float64
	idle      time.Duration // a bucket idle this long is full again, i.e. the same as no entry: it may be dropped
	maxKeys   int
	buckets   map[netip.Prefix]*bucket
	lastSweep time.Time
	now       func() time.Time
}

type bucket struct {
	tokens float64
	last   time.Time
}

// NewLimiter: perMinute and burst must be >= 1 (the callers' env parsing guarantees it).
func NewLimiter(perMinute, burst, maxKeys int) *Limiter {
	ps := float64(perMinute) / 60
	return &Limiter{perSecond: ps, burst: float64(burst), maxKeys: maxKeys,
		idle:    time.Duration(float64(burst)/ps*float64(time.Second)) + time.Second,
		buckets: make(map[netip.Prefix]*bucket), now: time.Now}
}

// Allow takes one token for ip. When none is left it answers false and how long until one is.
func (l *Limiter) Allow(ip netip.Addr) (bool, time.Duration) {
	k := key(ip)
	l.mu.Lock()
	defer l.mu.Unlock()
	now := l.now()
	b := l.buckets[k]
	if b == nil {
		if len(l.buckets) >= l.maxKeys || now.Sub(l.lastSweep) > time.Minute {
			l.sweep(now)
		}
		if len(l.buckets) >= l.maxKeys {
			return false, time.Second // table full: fail closed
		}
		b = &bucket{tokens: l.burst, last: now}
		l.buckets[k] = b
	}
	if el := now.Sub(b.last).Seconds(); el > 0 {
		b.tokens = math.Min(l.burst, b.tokens+el*l.perSecond)
	}
	b.last = now
	if b.tokens >= 1 {
		b.tokens--
		return true, 0
	}
	return false, time.Duration((1 - b.tokens) / l.perSecond * float64(time.Second))
}

func (l *Limiter) sweep(now time.Time) {
	l.lastSweep = now
	for k, b := range l.buckets {
		if now.Sub(b.last) >= l.idle {
			delete(l.buckets, k)
		}
	}
}

// FailureLimiter counts failed authentications per client in a fixed window that starts at the client's first
// failure: once a client has `max` failures inside `window`, every request from it is refused until the window ends
// (a correct token does not reset it early: the point is to stop guessing, not to reward a lucky guess).
type FailureLimiter struct {
	mu      sync.Mutex
	max     int
	window  time.Duration
	maxKeys int
	entries map[netip.Prefix]*failures
	now     func() time.Time
}

type failures struct {
	count int
	start time.Time
}

// NewFailureLimiter: max >= 1 and window > 0 (the callers' env parsing guarantees it).
func NewFailureLimiter(max int, window time.Duration, maxKeys int) *FailureLimiter {
	return &FailureLimiter{max: max, window: window, maxKeys: maxKeys, entries: make(map[netip.Prefix]*failures),
		now: time.Now}
}

// Blocked reports whether ip has used up its failures, and for how long it stays blocked. A full table blocks
// clients it cannot track (fail closed).
func (f *FailureLimiter) Blocked(ip netip.Addr) (bool, time.Duration) {
	k := key(ip)
	f.mu.Lock()
	defer f.mu.Unlock()
	now := f.now()
	e := f.entries[k]
	if e == nil {
		if len(f.entries) >= f.maxKeys {
			f.sweep(now)
			if len(f.entries) >= f.maxKeys {
				return true, time.Second
			}
		}
		return false, 0
	}
	left := e.start.Add(f.window).Sub(now)
	if left <= 0 {
		delete(f.entries, k)
		return false, 0
	}
	if e.count >= f.max {
		return true, left
	}
	return false, 0
}

// Fail records one failed authentication from ip.
func (f *FailureLimiter) Fail(ip netip.Addr) {
	k := key(ip)
	f.mu.Lock()
	defer f.mu.Unlock()
	now := f.now()
	e := f.entries[k]
	if e != nil && now.Sub(e.start) >= f.window {
		e = nil
	}
	if e == nil {
		if len(f.entries) >= f.maxKeys {
			f.sweep(now)
			if len(f.entries) >= f.maxKeys {
				return // Blocked already refuses untracked clients while the table is full
			}
		}
		e = &failures{start: now}
		f.entries[k] = e
	}
	e.count++
}

func (f *FailureLimiter) sweep(now time.Time) {
	for k, e := range f.entries {
		if now.Sub(e.start) >= f.window {
			delete(f.entries, k)
		}
	}
}
