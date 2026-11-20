"""rv-priya21 ordinary-query corpus (OQ-1 classes and sizes, own word choices): 24,400 queries, 22 classes.
iter_cases() -> (meta {c, i}, q). Seed 'rv21oq|class'."""
# Ported into the permanent conformance suite (priya-spec/2 FG-3, CAL-2) from review rv-priya21, file h/g21oq.py
# (sha256 94370e0699f436bb8b9ab00ca4ab8aa9e3a6e6c026b093ab1a415847bb1cdb2b): the reviewer's generator, seeded; synthetic values only, made at run time. Changes: non-ASCII
# literals written as escapes, imports from the package, the command-line driver and environment overrides
# removed (N / PER fixed at the review's defaults). The random call order is unchanged, so the corpus is the
# review's own (sha256 pinned in data/manifest.json).
import random
import string

SIZES = [("plain", 2500), ("multilingual", 2500), ("question", 2000), ("local", 1500), ("price", 1200),
         ("size", 1200), ("tire", 900), ("model", 1500), ("date", 1000), ("time", 600), ("year", 1000),
         ("zip", 1000), ("zip_year", 800), ("version", 1000), ("flight", 900), ("product_code", 900),
         ("part_number", 1500), ("business_address", 500), ("quantity", 1000), ("date_time", 900)]
# 20 classes listed in OQ-1 (business address counted once); total 24,400
NOUNS = ["hiking boots", "standing desk", "air fryer", "yoga mat", "espresso grinder", "dog harness", "rain jacket",
         "phone case", "bike helmet", "garden hose", "wool socks", "cat tree", "lunch box", "desk lamp", "duvet cover",
         "kitchen scale", "pressure washer", "tent stakes", "shower head", "baby monitor"]
ADJ = ["best", "cheap", "waterproof", "lightweight", "quiet", "durable", "vegan", "organic", "portable", "kids"]
MULTI = ["mejor cafetera barata", "chaussures de randonnee femme", "beste kaffeemaschine test", "migliore pizza napoli",
         "melhor tenis corrida", "beste fiets kopen", "basta dammsugaren", "najlepszy odkurzacz", "en iyi kahve makinesi",
         "\u043a\u0443\u043f\u0438\u0442\u044c \u0447\u0430\u0439\u043d\u0438\u043a",
         "\u0623\u0641\u0636\u0644 \u0647\u0627\u062a\u0641", "\u0938\u0938\u094d\u0924\u093e \u092b\u094b\u0928",
         "\u5b89\u3044 \u30b9\u30cb\u30fc\u30ab\u30fc", "\u4fbf\u5b9c \u5496\u5561\u673a", "\uc800\ub834\ud55c \ub178\ud2b8\ubd81",
         "\u0e23\u0e49\u0e32\u0e19\u0e01\u0e32\u0e41\u0e1f", "quan ca phe ngon", "sepatu lari murah",
         "\u03ba\u03b1\u03bb\u03cc \u03ba\u03b1\u03c6\u03ad"]
QW = ["how do i", "why does my", "what is the best way to", "can you", "is it safe to", "when should i", "where can i"]
VERBS = ["clean", "fix", "store", "wash", "replace", "paint", "assemble", "return"]
CITIES = ["denver", "tampa", "boise", "fresno", "akron", "reno", "tulsa", "omaha", "provo", "salem"]
MONTHS = ["jan", "feb", "march", "april", "may", "june", "jul", "aug", "sept", "october", "nov", "december"]
SOFT = ["python", "node", "android", "ios", "windows", "ubuntu", "chrome", "excel", "java", "blender"]
AIR = ["ua", "aa", "dl", "wn", "b6", "as", "ba", "lh", "af", "nk"]
UNITS = ["oz", "ml", "lb", "kg", "pack", "count", "pcs", "rolls", "pairs", "gallons"]


def _upc(r):
    d = [r.randrange(10) for _ in range(11)]
    s = 3 * sum(d[0::2]) + sum(d[1::2])
    return "".join(map(str, d)) + str((10 - s % 10) % 10)


def _isbn(r):
    d = [9, 7, r.choice([8, 9])] + [r.randrange(10) for _ in range(9)]
    s = sum(x * (1 if i % 2 == 0 else 3) for i, x in enumerate(d))
    return "".join(map(str, d)) + str((10 - s % 10) % 10)


def make(r, c):
    n, a = r.choice(NOUNS), r.choice(ADJ)
    if c == "plain":
        return r.choice(["{a} {n}", "{n} reviews", "{n} vs {m}", "{a} {n} for beginners", "how to choose {n}"]).format(
            a=a, n=n, m=r.choice(NOUNS))
    if c == "multilingual":
        return r.choice(MULTI) + r.choice(["", " 2025", " online", ""])
    if c == "question":
        return r.choice(QW) + " " + r.choice(VERBS) + " a " + n + r.choice(["", "?", " at home", " without tools"])
    if c == "local":
        return r.choice(["{n} store near me", "{n} repair {city}", "open now {n} {city}", "{city} {n} delivery"]).format(
            n=n, city=r.choice(CITIES))
    if c == "price":
        v = r.choice(["$%d" % r.randint(5, 2000), "%d usd" % r.randint(5, 999), "\u20ac%d" % r.randint(5, 999),
                      "%d,%02d \u20ac" % (r.randint(1, 99), r.randint(0, 99)), "\u00a3%d.%02d" % (r.randint(1, 500),
                                                                                                 r.randint(0, 99)),
                      "%d dollars" % r.randint(10, 9000)])
        return r.choice(["{n} under {v}", "{n} {v}", "{v} {n} deal"]).format(n=n, v=v)
    if c == "size":
        return r.choice(["{n} {a}x{b}", "{n} {a} x {b} x {h} cm", "{a}x{b} inch {n}", "{n} {a}\" x {b}\""]).format(
            n=n, a=r.randint(4, 120), b=r.randint(4, 120), h=r.randint(2, 90))
    if c == "tire":
        w, ar, rim = r.choice([195, 205, 215, 225, 235, 245, 265, 275, 285, 295]), r.choice([45, 50, 55, 60, 65, 70,
                                                                                            75]), r.randint(15, 22)
        return r.choice(["{w}/{a}r{d} tires", "{w} {a} {d}", "{w}/{a}/{d} all season", "lt{w}/{a}r{d} {li}s",
                         "{w}/{a}zr{d} {li}w"]).format(w=w, a=ar, d=rim, li=r.randint(85, 121))
    if c == "model":
        return r.choice(["ddr{g} {s}00 {c}gb", "rtx {m}0 ti", "galaxy s{k} ultra", "iphone {k} pro max",
                         "thinkpad x{t} gen {k}", "rx {m}00 xt"]).format(g=r.choice([4, 5]), s=r.randint(24, 72),
                                                                          c=r.choice([8, 16, 32]), m=r.randint(30, 79),
                                                                          k=r.randint(8, 25), t=r.randint(1, 13) * 10)
    if c == "date":
        y, m, d = r.randint(1990, 2030), r.randint(1, 12), r.randint(1, 28)
        return r.choice(["{m}/{d}/{y}", "{y}-{m:02d}-{d:02d}", "{d}.{m}.{y}", "{mn} {d} {y}", "{d} {mn} {y}",
                         "{m}-{d}-{y}", "{y}/{m}/{d}"]).format(y=y, m=m, d=d, mn=r.choice(MONTHS)) + r.choice(
            ["", " events", " holiday"])
    if c == "time":
        return r.choice(["{h}:{mi} am", "{h}:{mi}pm", "{H}:{mi}", "{h}{mi} hours", "{H}h{mi}"]).format(
            h=r.randint(1, 12), H=r.randint(0, 23), mi="%02d" % r.randint(0, 59)) + r.choice(["", " eastern", " to pst"])
    if c == "year":
        return r.choice(["{n} {y}", "{y} {n} models", "best {n} of {y}"]).format(n=n, y=r.randint(1950, 2030))
    if c == "zip":
        return r.choice(["{z} weather", "{n} {z}", "zip {z} map"]).format(z="%05d" % r.randint(501, 99950), n=n)
    if c == "zip_year":
        return r.choice(["{z} {y}", "{z} homes {y}", "{y} {z} census"]).format(z="%05d" % r.randint(501, 99950),
                                                                              y=r.randint(1990, 2030))
    if c == "version":
        v = "%d.%d.%d" % (r.randint(0, 20), r.randint(0, 40), r.randint(0, 99))
        return r.choice(["{s} {v}", "{v} changelog", "{s} {v} release notes", "version {v}"]).format(
            s=r.choice(SOFT), v=v if r.random() < 0.7 else v.rsplit(".", 1)[0])
    if c == "flight":
        f = r.choice(AIR) + str(r.randint(1, 9999))
        return r.choice(["{f} status", "flight {f}", "{f} {m}/{d} {h}:{mi}", "{f} gate"]).format(
            f=f, m=r.randint(1, 12), d=r.randint(1, 28), h=r.randint(0, 23), mi="%02d" % r.randint(0, 59))
    if c == "product_code":
        k = r.randrange(3)
        if k == 0:
            return "upc " + _upc(r) + " " + n
        if k == 1:
            return "isbn " + _isbn(r) + r.choice([" paperback", " book", ""])
        return "sku " + "".join(r.choice(string.ascii_uppercase) for _ in range(3)) + "-" + str(r.randint(100, 99999))
    if c == "part_number":
        return r.choice(["part number {a}-{b}-{c}", "oem part {a}-{b}", "{n} part # {a}{b}"]).format(
            a=r.randint(10, 99), b="%04d" % r.randint(0, 9999), c="%02d" % r.randint(0, 99), n=n)
    if c == "business_address":
        return r.choice(["{num} {st} {sfx} {city}", "{name} {num} {st} {sfx}", "{num} {st} {sfx} parking"]).format(
            num=r.randint(1, 9999), st=r.choice(["main", "market", "broad", "union", "maple", "church"]),
            sfx=r.choice(["st", "ave", "blvd", "road", "way", "drive"]), city=r.choice(CITIES),
            name=r.choice(["costco", "target", "city hall", "dmv office", "post office"]))
    if c == "quantity":
        return r.choice(["{n} {q} {u}", "{q} {u} {n}", "{n} set of {q}"]).format(n=n, q=r.randint(2, 500),
                                                                                u=r.choice(UNITS))
    if c == "date_time":
        return r.choice(["{m}/{d} {h}:{mi}", "{y}-{m:02d}-{d:02d} {h}:{mi}", "{mn} {d} at {h}:{mi}pm",
                         "{m}/{d}/{y} {h}:{mi}:{s}"]).format(y=r.randint(2000, 2030), m=r.randint(1, 12),
                                                              d=r.randint(1, 28), h=r.randint(0, 23),
                                                              mi="%02d" % r.randint(0, 59), s="%02d" % r.randint(0, 59),
                                                              mn=r.choice(MONTHS))
    raise ValueError(c)


def iter_cases():
    for c, n in SIZES:
        r = random.Random("rv21oq|" + c)
        for i in range(n):
            yield {"c": c, "i": i}, make(r, c)
