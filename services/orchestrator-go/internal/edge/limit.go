package edge

import (
	"container/list"
	"math"
	"net/netip"
	"sync"
	"time"
)

// DefaultMaxKeys bounds every limiter's table. When a table is full the least recently used entry is evicted to
// make room (AEGIS edge review H-1): a table full of an attacker's addresses must never refuse a client it has not
// seen yet. Evicting a bucket gives that client a fresh burst; evicting a failure record forgets its failures. Both
// are safe because the bearer token, not these tables, is the access control.
const DefaultMaxKeys = 16384

// lru is a bounded map from client key to value, ordered by last use (front = most recent).
type lru[V any] struct {
	max   int
	ll    *list.List
	items map[netip.Prefix]*list.Element
}

type lruEntry[V any] struct {
	key netip.Prefix
	val V
}

func newLRU[V any](max int) *lru[V] {
	return &lru[V]{max: max, ll: list.New(), items: make(map[netip.Prefix]*list.Element)}
}

// get returns the value for k and marks it most recently used.
func (c *lru[V]) get(k netip.Prefix) (V, bool) {
	if e, ok := c.items[k]; ok {
		c.ll.MoveToFront(e)
		return e.Value.(*lruEntry[V]).val, true
	}
	var zero V
	return zero, false
}

// put inserts k (most recently used), evicting the least recently used entry when full.
func (c *lru[V]) put(k netip.Prefix, v V) {
	if e, ok := c.items[k]; ok {
		e.Value.(*lruEntry[V]).val = v
		c.ll.MoveToFront(e)
		return
	}
	if c.ll.Len() >= c.max {
		if old := c.ll.Back(); old != nil {
			c.ll.Remove(old)
			delete(c.items, old.Value.(*lruEntry[V]).key)
		}
	}
	c.items[k] = c.ll.PushFront(&lruEntry[V]{key: k, val: v})
}

func (c *lru[V]) remove(k netip.Prefix) {
	if e, ok := c.items[k]; ok {
		c.ll.Remove(e)
		delete(c.items, k)
	}
}

func (c *lru[V]) len() int { return c.ll.Len() }

// Limiter is a per-client token bucket: each client (an IPv4 address or an IPv6 /64) may make `burst` requests at
// once and `perMinute` requests a minute after that. In memory: correct for the one replica every public service
// runs (.railway/railway.ts numReplicas: 1); a restart forgets every bucket.
type Limiter struct {
	mu        sync.Mutex
	perSecond float64
	burst     float64
	buckets   *lru[*bucket]
	now       func() time.Time
}

type bucket struct {
	tokens float64
	last   time.Time
}

// NewLimiter: perMinute and burst must be >= 1 (the callers' env parsing guarantees it).
func NewLimiter(perMinute, burst, maxKeys int) *Limiter {
	return &Limiter{perSecond: float64(perMinute) / 60, burst: float64(burst), buckets: newLRU[*bucket](maxKeys),
		now: time.Now}
}

// Allow takes one token for ip. When none is left it answers false and how long until one is.
func (l *Limiter) Allow(ip netip.Addr) (bool, time.Duration) {
	k := key(ip)
	l.mu.Lock()
	defer l.mu.Unlock()
	now := l.now()
	b, ok := l.buckets.get(k)
	if !ok {
		b = &bucket{tokens: l.burst, last: now}
		l.buckets.put(k, b)
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

// FailureLimiter counts failed authentications per client in a fixed window that starts at the client's first
// failure: once a client has `max` failures inside `window`, its UNAUTHENTICATED requests are refused until the
// window ends. A request carrying the valid token is never refused by it (Guard.Trusted; AEGIS edge review M-1).
type FailureLimiter struct {
	mu      sync.Mutex
	max     int
	window  time.Duration
	entries *lru[*failures]
	now     func() time.Time
}

type failures struct {
	count int
	start time.Time
}

// NewFailureLimiter: max >= 1 and window > 0 (the callers' env parsing guarantees it).
func NewFailureLimiter(max int, window time.Duration, maxKeys int) *FailureLimiter {
	return &FailureLimiter{max: max, window: window, entries: newLRU[*failures](maxKeys), now: time.Now}
}

// Blocked reports whether ip has used up its failures, and for how long it stays blocked. An untracked client is
// never blocked.
func (f *FailureLimiter) Blocked(ip netip.Addr) (bool, time.Duration) {
	k := key(ip)
	f.mu.Lock()
	defer f.mu.Unlock()
	e, ok := f.entries.get(k)
	if !ok {
		return false, 0
	}
	left := e.start.Add(f.window).Sub(f.now())
	if left <= 0 {
		f.entries.remove(k)
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
	e, ok := f.entries.get(k)
	if !ok || now.Sub(e.start) >= f.window {
		e = &failures{start: now}
		f.entries.put(k, e)
	}
	e.count++
}
