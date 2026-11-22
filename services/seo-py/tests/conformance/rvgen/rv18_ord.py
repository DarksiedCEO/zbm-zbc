"""P4: ordinary, realistic search queries (no personal data by construction), multilingual, by class."""
# Ported into the permanent conformance suite (priya-spec/1.1 C-5) from review rv-priya18, file rv/build_ord.py: the reviewer's
# generator, seeded; synthetic values only, made at run time. Changes: non-ASCII literals written as escapes,
# imports from the package, the command-line driver replaced by a function. The random call order is unchanged,
# so the corpus is the review's own (sha256 pinned in data/manifest.json).
import random


def cases():
    r = random.Random(18_004)
    rows = []

    PRODUCTS = ["running shoes", "rug cleaner", "air fryer", "standing desk", "office chair", "led bulbs", "dog food",
                "coffee maker", "winter jacket", "yoga mat", "car battery", "garden hose", "baby stroller", "laptop bag",
                "water heater", "roof repair", "carpet cleaning", "plumber", "electrician", "hvac service", "pest control",
                "wedding dress", "hiking boots", "bike helmet", "phone case", "tv mount", "mattress", "sofa bed"]
    CITIES = ["austin", "denver", "chicago", "seattle", "boston", "miami", "phoenix", "dallas", "atlanta", "portland",
              "london", "madrid", "paris", "berlin", "toronto", "sydney", "mexico city", "lisbon", "rome", "tokyo"]
    ADJ = ["best", "cheap", "top rated", "affordable", "professional", "eco friendly", "used", "new", "vintage", "small"]
    ML = ["mejor {p} barato", "zapatillas para correr mujer", "comprar {p} online", "meilleur {p} pas cher",
          "acheter {p} en ligne", "beste {p} test", "{p} kaufen g\u00fcnstig", "miglior {p} prezzo", "melhor {p} pre\u00e7o",
          "\u6771\u4eac \u30db\u30c6\u30eb \u5b89\u3044", "\u5927\u962a \u30e9\u30fc\u30e1\u30f3 \u304a\u3059\u3059\u3081", "\u5317\u4eac \u5929\u6c14", "\u4e0a\u6d77 \u9152\u5e97 \u63a8\u8350", "\uc11c\uc6b8 \ub9db\uc9d1 \ucd94\ucc9c", "\ubd80\uc0b0 \ud638\ud154",
          "\u043b\u0443\u0447\u0448\u0438\u0439 {p} \u0446\u0435\u043d\u0430", "\u043a\u0443\u043f\u0438\u0442\u044c {p} \u043c\u043e\u0441\u043a\u0432\u0430", "\u0623\u0641\u0636\u0644 \u0645\u0637\u0639\u0645 \u0641\u064a \u062f\u0628\u064a", "\u0633\u0639\u0631 {p}", "\u0938\u092c\u0938\u0947 \u0905\u091a\u094d\u091b\u093e {p}", "\u0926\u093f\u0932\u094d\u0932\u0940 \u0939\u094b\u091f\u0932",
          "en iyi {p} fiyat", "najlepszy {p} cena", "beste {p} kopen", "b\u00e4sta {p} pris", "{p} gratis env\u00edo",
          "caf\u00e9 pr\u00e8s de moi", "p\u00e2tisserie paris 11e", "stra\u00dfe reinigung k\u00f6ln", "\u03b5\u03bb\u03bb\u03b7\u03bd\u03b9\u03ba\u03cc \u03b5\u03c3\u03c4\u03b9\u03b1\u03c4\u03cc\u03c1\u03b9\u03bf \u03b1\u03b8\u03ae\u03bd\u03b1",
          "\u0e23\u0e35\u0e27\u0e34\u0e27 {p}", "gi\u00e1 {p} r\u1ebb", "harga {p} murah", "\u0e23\u0e32\u0e04\u0e32 {p}"]
    Q = ["how to clean {p}", "what is the best {p}", "how much does {p} cost", "why is my {p} leaking",
         "can i return {p}", "is {p} worth it", "where to buy {p}", "which {p} should i buy", "how long does {p} last",
         "do i need a {p}", "what size {p} do i need", "c\u00f3mo limpiar {p}", "comment choisir {p}", "wie viel kostet {p}"]
    LOCAL = ["{p} near me", "{p} in {c}", "{p} open now", "{p} {c} downtown", "directions to {c} airport",
             "closest {p} store", "{p} nearby", "emergency {p} {c}", "24/7 {p} {c}"]
    CURR = ["$", "\u20ac", "\u00a3", "usd ", "eur ", ""]


    def gen_class(cls):
        p, c = r.choice(PRODUCTS), r.choice(CITIES)
        if cls == "plain_text":
            return r.choice(["{a} {p}", "{p} reviews", "{p} for sale", "{a} {p} brands", "{p} vs {p2}", "{p} ideas",
                             "{p} deals", "diy {p}"]).format(a=r.choice(ADJ), p=p, p2=r.choice(PRODUCTS))
        if cls == "multilingual":
            return r.choice(ML).format(p=p)
        if cls == "question":
            return r.choice(Q).format(p=p)
        if cls == "local":
            return r.choice(LOCAL).format(p=p, c=c)
        if cls == "price":
            v = r.choice([str(r.randint(5, 999)), f"{r.randint(1, 4999)}.{r.randint(0, 99):02d}", f"{r.randint(1, 99)},{r.randint(0, 999):03d}",
                          f"{r.randint(1, 9)}k"])
            return r.choice(["{p} under {cu}{v}", "{p} {cu}{v}", "{p} for {v} dollars", "{p} precio {v} euros",
                             "{p} between {cu}{v} and {cu}{v2}", "{p} {v} usd price"]).format(
                p=p, cu=r.choice(CURR), v=v, v2=str(r.randint(100, 9999)))
        if cls == "size":
            return r.choice(["{p} size {n}", "{n}x{m} rug", "{n} x {m} frame", "{n}x{m}x{k} storage box", "{n} inch tv",
                             "{n}mm drill bit", "queen size {p}", "{n}l backpack", "{n}cm x {m}cm photo frame",
                             "shoes size {n}.5 us", "{n}/{m} plywood", "{n} sq ft {p}", "{n}ft x {m}ft shed",
                             "{n} gallon water heater", "{n}.{m} cu ft fridge"]).format(
                p=p, n=r.randint(2, 99), m=r.randint(2, 99), k=r.randint(2, 99))
        if cls == "tire_size":
            w, a, rim = r.choice(range(155, 335, 10)), r.choice(range(30, 85, 5)), r.choice(range(13, 23))
            load, spd = r.randint(70, 125), r.choice("HVWYTQSRZ")
            return r.choice([f"{w}/{a}r{rim} tires", f"{w}/{a}r{rim} {load}{spd}", f"{w} {a} {rim} tires",
                             f"{w}/{a}/{rim} all season", f"{w}/{a}zr{rim} michelin", f"lt{w}/{a}r{rim} {load}/{load - 3}{spd}",
                             f"{w}/{a}r{rim} {load}{spd} price", f"neumaticos {w}/{a} r{rim}"])
        if cls == "model_number":
            return r.choice([
                f"ddr{r.choice([4, 5])} {r.choice([3200, 3600, 4800, 5600, 6000, 6400, 7200])} {r.choice([8, 16, 32, 64])}gb",
                f"ddr{r.choice([4, 5])}-{r.choice([3200, 5600, 6000])} cl{r.choice([16, 30, 36])} {r.choice([16, 32])}gb kit",
                f"rtx {r.choice([3060, 3070, 3080, 4060, 4070, 4080, 4090, 5070, 5080, 5090])} {r.choice(['ti', 'super', ''])} {r.choice([8, 12, 16, 24])}gb",
                f"rx {r.choice([6600, 6700, 7800, 7900, 9070])} xt", f"iphone {r.randint(11, 17)} pro max {r.choice([128, 256, 512])}gb",
                f"galaxy s{r.randint(20, 25)} ultra {r.choice([256, 512])}gb", f"pixel {r.randint(6, 10)} pro case",
                f"i{r.choice([5, 7, 9])}-{r.choice([12400, 12700, 13600, 13700, 14700, 14900])}k",
                f"ryzen {r.choice([5, 7, 9])} {r.choice([5600, 5800, 7600, 7800, 7950, 9700, 9950])}x3d",
                f"sm-s{r.randint(900, 999)}u firmware", f"model {r.randint(1000, 99999)} replacement filter",
                f"{r.choice(['wd', 'st', 'mx'])}{r.randint(1000, 99999)} drive", f"lg oled{r.randint(42, 83)}c{r.randint(1, 4)}",
                f"samsung qn{r.randint(43, 85)}q{r.randint(60, 90)}c", f"canon eos r{r.randint(5, 8)} {r.randint(24, 105)}mm",
                f"bosch {r.randint(1000, 9999)}-{r.randint(10, 99)} parts", f"hp {r.randint(100, 999)}xl ink",
                f"part number {r.randint(10, 99)}-{r.randint(1000, 9999)}-{r.randint(10, 99)}"])
        if cls == "date":
            y, m, d = r.randint(2000, 2027), r.randint(1, 12), r.randint(1, 28)
            return r.choice([f"events {y}-{m:02d}-{d:02d}", f"{m}/{d}/{y} hotel", f"{d}.{m}.{y} konzert",
                             f"concerts {m}/{d}", f"{y}/{m:02d}/{d:02d} schedule", f"flights {d} {['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'][m - 1]} {y}",
                             f"weather {y}-{m:02d}-{d:02d}", f"{d}/{m}/{y} public holiday"])
        if cls == "time":
            return r.choice([f"open at {r.randint(1, 12)}pm", f"{r.randint(0, 23):02d}:{r.choice(['00', '15', '30', '45'])} train",
                             f"what time is {r.randint(1, 12)}am est in pst", f"store hours {r.randint(6, 11)}am to {r.randint(5, 11)}pm"])
        if cls == "date_time":
            y, m, d = r.randint(2015, 2027), r.randint(1, 12), r.randint(1, 28)
            t = r.choice([f"{r.randint(1, 12)}pm", f"{r.randint(1, 12)}am", f"{r.randint(0, 23):02d}:{r.choice(['00', '30'])}",
                          f"{r.randint(1, 12)}:{r.choice(['00', '15', '30'])} pm"])
            return r.choice([f"{y}-{m:02d}-{d:02d} {t} meeting", f"{m}/{d}/{y} {t} game", f"concert {d}.{m}.{y} {t}",
                             f"flight {m}/{d} {t}", f"{y}-{m:02d}-{d:02d}t{r.randint(0, 23):02d}:{r.choice(['00', '30'])}"])
        if cls == "year":
            y = r.randint(1950, 2027)
            return r.choice([f"{y} honda civic", f"best movies {y}", f"{y} {y + 1} nba season", f"tax brackets {y}",
                             f"{y}-{y + 1} school calendar", f"{y} {y + 1} {y + 2} toyota camry", f"world cup {y}",
                             f"{r.choice([1960, 1970, 1980, 1990])}s music"])
        if cls == "zip":
            z = f"{r.randint(1001, 99950):05d}"
            return r.choice([f"plumber {z}", f"{z} weather", f"homes for sale {z}", f"{c} {z}", f"zip code {z}",
                             f"pizza delivery {z}"])
        if cls == "zip_year":
            z = f"{r.randint(1001, 99950):05d}"
            y = r.randint(2015, 2027)
            return r.choice([f"{z} {y} property tax", f"{z} crime rate {y}", f"home prices {z} {y}", f"{z} {y}"])
        if cls == "version":
            return r.choice([f"python {r.randint(2, 3)}.{r.randint(0, 13)}.{r.randint(0, 20)}",
                             f"chrome {r.randint(100, 135)}.0.{r.randint(5000, 7000)}.{r.randint(10, 200)}",
                             f"ios {r.randint(14, 19)}.{r.randint(0, 6)}.{r.randint(0, 3)} update",
                             f"windows 10 version {r.choice([1903, 1909, 2004, 21, 22])}h{r.randint(1, 2)}",
                             f"build 10.0.{r.choice([19041, 19045, 22621, 22631, 26100])}.{r.randint(100, 5000)}",
                             f"node {r.randint(14, 23)}.{r.randint(0, 20)}.{r.randint(0, 9)}", f"v{r.randint(1, 9)}.{r.randint(0, 30)}.{r.randint(0, 99)} release notes",
                             f"kernel {r.randint(4, 6)}.{r.randint(0, 19)}.{r.randint(0, 150)}",
                             f"firmware {r.randint(1, 9)}.{r.randint(0, 99)}.{r.randint(0, 999)}",
                             f"minecraft {r.randint(1, 1)}.{r.randint(12, 21)}.{r.randint(0, 5)} seeds",
                             f"java {r.randint(8, 23)}.0.{r.randint(1, 15)} download",
                             f"android {r.randint(10, 16)} qpr{r.randint(1, 3)}"])
        if cls == "flight":
            return r.choice([f"{r.choice(['ua', 'aa', 'dl', 'ba', 'lh', 'af', 'ib', 'ek', 'wn', 'b6'])}{r.randint(1, 9999)} status",
                             f"flight {r.choice(['ua', 'aa', 'dl', 'ba'])} {r.randint(1, 9999)} delay",
                             f"vuelo {r.choice(['ib', 'ux', 'vy'])}{r.randint(100, 9999)}"])
        if cls == "product_code":
            return r.choice([f"sku {r.randint(100000, 999999)}", f"order {r.choice(['a', 'b', 'x'])}{r.randint(10000, 99999)} tracking",
                             f"item {r.randint(1000, 99999)}", f"ref {r.randint(100, 999)}-{r.randint(100, 999)}",
                             f"upc {r.randint(10 ** 11, 10 ** 12 - 1)}", f"isbn {r.choice(['978', '979'])}{r.randint(10 ** 9, 10 ** 10 - 1)}",
                             f"asin b0{r.randint(10 ** 7, 10 ** 8 - 1)}", f"lego {r.randint(10000, 79999)}", f"part {r.randint(10, 99)}-{r.randint(100, 999)}"])
        if cls == "business_address":
            return r.choice([f"{r.choice(['starbucks', 'walmart', 'target', 'costco', 'home depot'])} {r.randint(1, 9999)} {r.choice(['main', 'broadway', 'market', 'elm'])} {r.choice(['st', 'street', 'ave'])}",
                             f"{r.choice(['apple store', 'ikea', 'dmv', 'post office'])} on {r.choice(['main', '5th', 'market'])} street",
                             f"restaurants on {r.choice(['broadway', 'sunset blvd', 'bourbon street'])}"])
        if cls == "quantity":
            return r.choice([f"{r.randint(2, 500)} pack {p}", f"{r.randint(1, 50)} lbs {p}", f"{r.randint(1, 20)} kg {p}",
                             f"{r.randint(2, 12)} person tent", f"{r.randint(1, 5)} bedroom apartment {c}",
                             f"{r.randint(10, 1000)} count vitamin d", f"{r.randint(1, 100)} gallon fish tank",
                             f"{r.randint(1, 9)}000 btu air conditioner", f"set of {r.randint(2, 12)} {p}",
                             f"{r.randint(100, 3000)} watt generator", f"{r.randint(1, 9)}0 percent off {p}"])
        raise KeyError(cls)


    WEIGHTS = {"plain_text": 2500, "multilingual": 2500, "question": 2000, "local": 1500, "price": 1200, "size": 1200,
               "tire_size": 900, "model_number": 1500, "date": 1000, "time": 600, "date_time": 900, "year": 1000,
               "zip": 1000, "zip_year": 800, "version": 1000, "flight": 600, "product_code": 900,
               "business_address": 500, "quantity": 1000}
    for cls, n in WEIGHTS.items():
        for _ in range(n):
            rows.append({"c": cls, "d": "", "p": "", "q": gen_class(cls), "x": ""})
    return rows
