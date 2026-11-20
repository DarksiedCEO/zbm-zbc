"""Reviewer's own ordinary corpus (OQ-1 classes and sizes, 24,400): python g7.py OUT.jsonl SEED"""
# Ported into the permanent conformance suite (priya-spec/1.1 C-5) from review rv-priya19, file h/g7.py: the reviewer's
# generator, seeded; synthetic values only, made at run time. Changes: non-ASCII literals written as escapes,
# imports from the package, the command-line driver replaced by a function. The random call order is unchanged,
# so the corpus is the review's own (sha256 pinned in data/manifest.json).
import random

TOPIC = ["rug cleaner", "running shoes", "coffee maker", "garden hose", "winter jacket", "standing desk", "dog food",
         "yoga mat", "air fryer", "electric bike", "baby stroller", "office chair", "led bulbs", "wireless earbuds",
         "camping tent", "kitchen knife", "water filter", "phone case", "hiking boots", "tea kettle"]
ADJ = ["best", "cheap", "organic", "waterproof", "small", "large", "portable", "quiet", "durable", "vintage"]
MULTI = ["mejor cafetera", "zapatillas para correr", "meilleur aspirateur", "chaussures de course", "beste kaffeemaschine",
         "migliore friggitrice", "melhor cadeira", "beste stofzuiger", "b\u00e4sta kaffebryggare", "najlepszy odkurzacz",
         "en iyi kahve makinesi", "\u043b\u0443\u0447\u0448\u0438\u0439 \u043f\u044b\u043b\u0435\u0441\u043e\u0441",
         "\u0623\u0641\u0636\u0644 \u0645\u0643\u0646\u0633\u0629", "\u0938\u092c\u0938\u0947 \u0905\u091a\u094d\u091b\u093e \u092b\u094b\u0928",
         "\u6700\u9ad8\u306e\u30b3\u30fc\u30d2\u30fc\u30e1\u30fc\u30ab\u30fc", "\u6700\u597d\u7684\u5438\u5c18\u5668",
         "\ucd5c\uace0\uc758 \uccad\uc18c\uae30", "\u0e40\u0e04\u0e23\u0e37\u0e48\u0e2d\u0e07\u0e14\u0e39\u0e14\u0e1d\u0e38\u0e48\u0e19",
         "m\u00e1y h\u00fat b\u1ee5i t\u1ed1t nh\u1ea5t", "penyedot debu terbaik",
         "\u03ba\u03b1\u03bb\u03cd\u03c4\u03b5\u03c1\u03b7 \u03c3\u03ba\u03bf\u03cd\u03c0\u03b1"]
QW = ["how to", "what is the best", "why does my", "can i use", "where to buy", "is it safe to", "how long does",
      "which", "should i buy", "how do i clean"]
LOCAL = ["near me", "open now", "nearby", "directions", "closest"]
CITIES = ["seattle", "austin", "denver", "boston", "chicago", "portland", "miami", "atlanta"]
SW = ["windows", "android", "ios", "chrome", "firefox", "python", "node", "ubuntu", "macos", "java"]
GPU = ["rtx", "gtx", "rx"]
PHONE = ["iphone", "galaxy s", "pixel", "galaxy a", "moto g", "redmi note"]
CARRIERS = ["ua", "dl", "aa", "ba", "lh", "af", "wn", "b6", "as", "ek"]
UNITS = ["kg", "lb", "oz", "ml", "liters", "pack", "count", "pcs", "gallons", "feet"]


def _gtin_check(body):
    s = sum(int(c) * (3 if i % 2 == 0 else 1) for i, c in enumerate(reversed(body)))
    return body + str((10 - s % 10) % 10)


def make(r, cls):
    t = r.choice(TOPIC)
    if cls == "plain_text":
        return r.choice(ADJ) + " " + t + r.choice(["", " for kids", " reviews", " sale", " online"])
    if cls == "multilingual":
        return r.choice(MULTI) + r.choice(["", " " + r.choice(["online", "precio", "prix", "preis", "2"])[:0]])
    if cls == "question":
        return r.choice(QW) + " " + t + r.choice(["", "?", " at home", " safely"])
    if cls == "local":
        return t + " " + r.choice(LOCAL + ["in " + c for c in CITIES])
    if cls == "price":
        a = r.randint(5, 2500)
        forms = ["$%d" % a, "%d usd" % a, "\u20ac%d" % a, "%d eur" % a, "\u00a3%d.99" % a, "%d dollars" % a]
        return t + " under " + r.choice(forms)
    if cls == "size":
        k = r.randint(0, 3)
        if k == 0:
            return t + " %dx%d" % (r.randint(2, 12), r.randint(2, 16))
        if k == 1:
            return "%d x %d x %d inch box" % (r.randint(4, 40), r.randint(4, 40), r.randint(2, 30))
        if k == 2:
            return "rug %d ft by %d ft" % (r.randint(2, 12), r.randint(3, 15))
        return "%d cm %s" % (r.randint(10, 300), t)
    if cls == "tire_size":
        w, a, d = r.choice(range(155, 335, 10)), r.choice(range(30, 85, 5)), r.randint(13, 24)
        k = r.randint(0, 3)
        if k == 0:
            return "%d/%dr%d tires" % (w, a, d)
        if k == 1:
            return "%d %d %d" % (w, a, d) + r.choice(["", " tires", " all terrain"])
        if k == 2:
            return "%d/%dr%d %d%s" % (w, a, d, r.randint(80, 125), r.choice("hstv"))
        return "lt%d/%dr%d load range e" % (w, a, d)
    if cls == "model_number":
        k = r.randint(0, 2)
        if k == 0:
            return "ddr%d %d %dgb" % (r.choice([4, 5]), r.choice([3200, 4800, 5600, 6000, 6400]), r.choice([8, 16, 32, 64]))
        if k == 1:
            return "%s %d%s" % (r.choice(GPU), r.choice([3060, 3070, 4070, 4080, 4090, 6700, 7900]), r.choice(["", " ti", " xt", " super"]))
        return "%s %d %s" % (r.choice(PHONE), r.randint(5, 16), r.choice(["pro", "case", "128gb", "ultra", "plus"]))
    if cls == "date":
        y, m, d = r.randint(1990, 2030), r.randint(1, 12), r.randint(1, 28)
        f = r.choice(["%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%d.%m.%Y", "%B %d %Y", "%d %B %Y", "%m-%d-%Y"])
        import datetime
        return r.choice(["events ", "holiday ", "", "what happened on "]) + datetime.date(y, m, d).strftime(f).lower()
    if cls == "time":
        return r.choice(["sunset", "store hours", "train", "webinar"]) + " %d:%02d %s" % (r.randint(1, 12), r.choice([0, 15, 30, 45]), r.choice(["am", "pm", ""]))
    if cls == "year":
        return "%s %d" % (r.choice(["best movies", "tax brackets", "honda civic", "calendar", "world cup"]), r.randint(1950, 2030))
    if cls == "zip":
        return "%s %05d" % (r.choice(["plumber", "weather", "pizza delivery", "dentist"]), r.randint(1001, 99950))
    if cls == "zip_year":
        return "%05d %d" % (r.randint(1001, 99950), r.randint(1990, 2030)) + r.choice(["", " population", " election"])
    if cls == "version":
        v = ".".join(str(r.randint(0, 30)) for _ in range(r.randint(2, 4)))
        return (r.choice(SW) + " " + v) if r.random() < 0.5 else (r.choice(["version ", "update ", "release "]) + v + r.choice(["", " notes", " download"]))
    if cls == "flight":
        f = r.choice(CARRIERS) + str(r.randint(10, 9999))
        k = r.randint(0, 2)
        if k == 0:
            return "flight " + f + " status"
        if k == 1:
            return f + " %d/%d %d:%02d" % (r.randint(1, 12), r.randint(1, 28), r.randint(0, 23), r.choice([0, 15, 30, 45]))
        return f + " arrival"
    if cls == "product_code":
        k = r.randint(0, 2)
        if k == 0:
            return "upc " + _gtin_check("".join(str(r.randint(0, 9)) for _ in range(11))) + " " + t
        if k == 1:
            return "isbn " + _gtin_check(r.choice(["978", "979"]) + "".join(str(r.randint(0, 9)) for _ in range(9)))
        return "sku " + "".join(r.choice("abcdefghjk") for _ in range(2)) + str(r.randint(1000, 99999)) + " " + t
    if cls == "part_number":
        return "part number %02d-%04d-%02d" % (r.randint(0, 99), r.randint(0, 9999), r.randint(0, 99)) + r.choice(["", " " + t, " gasket"])
    if cls == "business_address":
        return "%d %s %s %s" % (r.randint(1, 9999), r.choice(["main", "market", "broad", "elm", "oak", "king"]),
                                r.choice(["st", "ave", "street", "road", "blvd"]), r.choice(["starbucks", "target", "walgreens", "hours", "pharmacy"]))
    if cls == "quantity":
        return "%d %s %s" % (r.randint(1, 500), r.choice(UNITS), t)
    if cls == "date_time":
        return "%d/%d/%d %d:%02d" % (r.randint(1, 12), r.randint(1, 28), r.randint(2015, 2030), r.randint(0, 23), r.choice([0, 15, 30, 45])) + r.choice(["", " meeting", " pm"])
    raise ValueError(cls)


SIZES = [("plain_text", 2500), ("multilingual", 2500), ("question", 2000), ("local", 1500), ("price", 1200),
         ("size", 1200), ("tire_size", 900), ("model_number", 1500), ("date", 1000), ("time", 600), ("year", 1000),
         ("zip", 1000), ("zip_year", 800), ("version", 1000), ("flight", 900), ("product_code", 900),
         ("part_number", 1500), ("business_address", 500), ("quantity", 1000), ("date_time", 900)]

def cases(seed):
    """The review's ordinary corpus: the OQ-1 classes and sizes, one RNG."""
    r = random.Random(seed)
    for cls, n in SIZES:
        for _ in range(n):
            yield {"c": cls, "q": make(r, cls)}
