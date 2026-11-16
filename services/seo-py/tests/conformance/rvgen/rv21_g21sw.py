"""rv-priya21 ordinary short-word queries (round 21 (c) over-drop probe): runs of 1-2 character words. iter_cases()."""
# Ported into the permanent conformance suite (priya-spec/2 FG-3, CAL-2) from review rv-priya21, file h/g21sw.py
# (sha256 4ae18c67a3830055dbd1e97ecfb5d690624c146552c6d69c69c0ed66388bb1d1): the reviewer's generator, seeded; synthetic values only, made at run time. Changes: non-ASCII
# literals written as escapes, imports from the package, the command-line driver and environment overrides
# removed (N / PER fixed at the review's defaults). The random call order is unchanged, so the corpus is the
# review's own (sha256 pinned in data/manifest.json).
import random
W1 = ["i", "a", "is", "it", "ok", "to", "go", "up", "in", "an", "rv", "on", "my", "bf", "gf", "la", "ny", "tv", "pc",
      "of", "or", "be", "do", "if", "me", "we", "us", "so", "no", "at", "by", "as", "ps", "4", "5", "2", "10", "xl", "uk"]
W3 = ["cheap", "near", "best", "how", "wifi", "size", "deal", "trip", "fix", "new", "used", "sale", "plan", "rent"]


def iter_cases():
    r = random.Random("rv21sw")
    for i in range(6000):
        n = r.randint(4, 10)
        ws = [r.choice(W1) for _ in range(n)]
        if r.random() < 0.6:
            ws.insert(r.randrange(len(ws) + 1), r.choice(W3))
        q = " ".join(ws)
        if i % 3 == 0:
            q = q.upper()
        yield {"c": "shortwords", "i": i}, q

