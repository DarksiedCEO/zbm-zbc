"""rv-priya20 ordinary-query corpus per OQ-1 (class sizes of the spec table; own phrasing). iter_cases() -> (meta, q)."""
# Ported into the permanent conformance suite (priya-spec/1.1 C-5) from review rv-priya20, file h/g20oq.py: the reviewer's
# generator, seeded; synthetic values only, made at run time. Changes: non-ASCII literals written as escapes,
# imports from the package, the command-line driver removed. The random call order is unchanged,
# so the corpus is the review's own (sha256 pinned in data/manifest.json).
import random

SIZES = {"plain_text": 2500, "multilingual": 2500, "question": 2000, "local": 1500, "price": 1200, "size": 1200,
         "tire_size": 900, "model_number": 1500, "date": 1000, "time": 600, "year": 1000, "zip": 1000,
         "zip_year": 800, "version": 1000, "flight": 900, "product_code": 900, "part_number": 1500,
         "business_address": 500, "quantity": 1000, "date_time": 900}
NOUNS = ["hiking boots", "standing desk", "air fryer", "yoga mat", "cast iron skillet", "noise cancelling headphones",
         "dog bed", "rain jacket", "espresso grinder", "office chair", "led strip lights", "sofa cover", "bike helmet",
         "phone case", "garden hose", "baby monitor", "electric kettle", "throw pillow", "wall clock", "tent"]
ADJ = ["best", "cheap", "waterproof", "lightweight", "vintage", "eco friendly", "large", "compact", "quiet", "durable"]
ML = ["mejores zapatillas para correr", "meilleur aspirateur sans fil", "beste kaffeemaschine test",
      "migliori cuffie bluetooth", "melhor colchao de casal", "beste fiets voor woon werk", "basta dammsugaren",
      "najlepszy odkurzacz", "en iyi kahve makinesi", "\u043b\u0443\u0447\u0448\u0438\u0439 \u043f\u044b\u043b\u0435\u0441\u043e\u0441",
      "\u0623\u0641\u0636\u0644 \u0647\u0627\u062a\u0641", "\u0938\u092c\u0938\u0947 \u0905\u091a\u094d\u091b\u093e \u092b\u094b\u0928",
      "\u304a\u3059\u3059\u3081 \u30ce\u30fc\u30c8\u30d1\u30bd\u30b3\u30f3", "\u6700\u597d\u7684\u5438\u5c18\u5668",
      "\ucd94\ucc9c \ub178\ud2b8\ubd81", "\u0e42\u0e17\u0e23\u0e28\u0e31\u0e1e\u0e17\u0e4c\u0e17\u0e35\u0e48\u0e14\u0e35\u0e17\u0e35\u0e48\u0e2a\u0e38\u0e14",
      "may hut bui tot nhat", "sepatu lari terbaik", "\u03ba\u03b1\u03bb\u03cd\u03c4\u03b5\u03c1\u03bf \u03ba\u03b9\u03bd\u03b7\u03c4\u03cc"]
CITIES = ["austin", "denver", "portland", "miami", "boston", "phoenix", "chicago", "san diego", "atlanta", "seattle"]
STATES = [("tx", "787"), ("co", "802"), ("ma", "021"), ("fl", "331"), ("az", "850"), ("il", "606"), ("wa", "981"),
          ("ga", "303"), ("ny", "100"), ("ca", "900")]
CUR = ["${a}", "{a} usd", "\u20ac{a}", "{a} eur", "\u00a3{a}", "{a} yen"]
MONTHS = ["jan", "feb", "mar", "april", "june", "july", "sept", "oct", "nov", "december"]
SOFT = ["python", "node", "android", "ios", "chrome", "firefox", "ubuntu", "windows", "kotlin", "postgres"]
AIR = ["ua", "dl", "aa", "ba", "lh", "af", "nh", "qf", "ek", "wn"]


def gtin_check(body):
    s = sum(int(c) * (3 if i % 2 == 0 else 1) for i, c in enumerate(body[::-1]))
    return str(-s % 10)


def isbn13_check(body):
    s = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(body))
    return str(-s % 10)


def make(r, c):
    n = r.choice(NOUNS)
    if c == "plain_text":
        return r.choice(ADJ) + " " + n + r.choice(["", " review", " for beginners", " sale", " near me"])
    if c == "multilingual":
        return r.choice(ML)
    if c == "question":
        return r.choice(["how to clean a ", "what is the best ", "why does my ", "can you wash a ", "is a "]) + n \
            + r.choice(["", " worth it", " safe", " good"])
    if c == "local":
        return r.choice([n + " store in ", n + " repair ", "open now " + n + " "]) + r.choice(CITIES)
    if c == "price":
        a = r.choice([str(r.randint(5, 999)), "%d.%02d" % (r.randint(1, 499), r.randint(0, 99)), "1,299"])
        return n + " under " + r.choice(CUR).format(a=a)
    if c == "size":
        return n + " " + r.choice(["%dx%d" % (r.randint(10, 96), r.randint(10, 96)), "%d x %d x %d cm" % (
            r.randint(5, 99), r.randint(5, 99), r.randint(5, 99)), "%d inch" % r.randint(10, 85), "%dmm" % r.randint(3, 900)])
    if c == "tire_size":
        w, a, d = r.choice([195, 205, 215, 225, 235, 245, 265, 275, 285]), r.choice([40, 45, 55, 60, 65, 70, 75]), \
            r.choice([15, 16, 17, 18, 20, 22])
        return r.choice(["{w}/{a}r{d} tires", "{w} {a} {d} all season", "{w}/{a}/{d} {li}h", "tires {w}/{a}zr{d} {li}w"]
                        ).format(w=w, a=a, d=d, li=r.randint(85, 121))
    if c == "model_number":
        return r.choice(["ddr5 %d ram" % r.choice([4800, 5600, 6000, 6400]), "rtx %d ti" % r.choice([3060, 4070, 4090]),
                         "galaxy s%d ultra" % r.randint(20, 25), "pixel %d pro" % r.randint(6, 9),
                         "thinkpad x1 gen %d" % r.randint(9, 12), "iphone %d pro max" % r.randint(12, 16)])
    if c == "date":
        y, m, d = r.randint(2019, 2027), r.randint(1, 12), r.randint(1, 28)
        return r.choice(["{y}-{m:02d}-{d:02d}", "{m}/{d}/{y}", "{d}.{m}.{y}", "{d} {M} {y}", "{M} {d} {y}"]).format(
            y=y, m=m, d=d, M=r.choice(MONTHS)) + r.choice([" events", " weather", "", " holiday"])
    if c == "time":
        return r.choice(["%d:%02d pm " % (r.randint(1, 12), r.randint(0, 59)), "%02d:%02d " % (r.randint(0, 23),
                         r.randint(0, 59))]) + r.choice(["in tokyo", "est to pst", "alarm", "sunset"])
    if c == "year":
        return r.choice(["best %s %d", "%s %d models", "%s of %d"]) % (n, r.randint(1990, 2027))
    if c == "zip":
        st, p = r.choice(STATES)
        return n + " near " + p + "%02d" % r.randint(1, 99)
    if c == "zip_year":
        st, p = r.choice(STATES)
        return n + " " + p + "%02d %d" % (r.randint(1, 99), r.randint(2015, 2027))
    if c == "version":
        v = "%d.%d.%d" % (r.randint(1, 20), r.randint(0, 30), r.randint(0, 99))
        return r.choice(["%s %s release notes" % (r.choice(SOFT), v), "%s update" % v, "v%s changelog" % v])
    if c == "flight":
        f = r.choice(AIR) + str(r.randint(10, 9999))
        return r.choice(["flight %s status" % f, "%s %d/%d %d:%02d" % (f, r.randint(1, 12), r.randint(1, 28),
                         r.randint(0, 23), r.randint(0, 59)), "%s delayed" % f])
    if c == "product_code":
        k = r.randrange(3)
        if k == 0:
            b = "0" + "".join(str(r.randint(0, 9)) for _ in range(10))
            return "upc " + b + gtin_check(b) + " " + n
        if k == 1:
            b = "978" + "".join(str(r.randint(0, 9)) for _ in range(9))
            return "isbn " + b + isbn13_check(b) + " paperback"
        return "sku " + r.choice(["ab", "kx", "zt"]) + "-" + str(r.randint(1000, 99999)) + " " + n
    if c == "part_number":
        return "part number %02d-%04d-%02d %s" % (r.randint(10, 99), r.randint(0, 9999), r.randint(0, 99), n)
    if c == "business_address":
        return "%s %d %s %s %s" % (r.choice(["starbucks", "home depot", "target", "dmv", "post office"]),
                                    r.randint(10, 9999), r.choice(["elm", "maple", "main", "oak", "park"]),
                                    r.choice(["st", "ave", "blvd", "rd"]), r.choice(CITIES))
    if c == "quantity":
        return "%d %s" % (r.randint(2, 500), r.choice(["pack " + n, "count " + n, "lbs dog food", "gallon paint",
                                                         "sheets plywood"]))
    if c == "date_time":
        return "%d/%d %d:%02d %s" % (r.randint(1, 12), r.randint(1, 28), r.randint(1, 12), r.randint(0, 59),
                                      r.choice(["pm concert", "am appointment", "game time", "webinar"]))
    raise ValueError(c)


def iter_cases():
    for c, k in SIZES.items():
        r = random.Random("rv20oq|" + c)
        for i in range(k):
            yield {"c": c, "i": i}, make(r, c)
