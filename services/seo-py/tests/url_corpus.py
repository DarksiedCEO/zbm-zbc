"""A deterministic corpus of ORDINARY (legitimate) cited-URL shapes for the secret-redaction precision tests (ADR 0046
B9). Self-built from templates of real URL formats with seeded random ids; no URL here carries a credential. Used to
measure over-redaction: the share of these URLs that ``secret_guard.url_has_secret`` flags."""

from __future__ import annotations

import random
import string

WORDS = ["revenue", "recovery", "agency", "marketplace", "amazon", "seller", "refund", "reimbursement", "guide", "best", "tips", "how", "to", "choose", "small", "business", "audit", "inventory", "fees", "storage", "returns", "long", "beach", "los", "angeles", "report", "2026", "update", "policy", "change", "review", "compare", "pricing", "plans", "case", "study", "growth", "strategy", "brand", "retail", "ecommerce", "shipping"]
B62 = string.ascii_letters + string.digits
B64URL = B62 + "-_"
HEX = "0123456789abcdef"


def _r(rng, alphabet, n):
    return "".join(rng.choice(alphabet) for _ in range(n))


def _slug(rng, k=None):
    return "-".join(rng.choice(WORDS) for _ in range(k or rng.randint(2, 7)))


def _templates():
    return [
        lambda r: f"https://apnews.com/article/{_slug(r)}-{_r(r, HEX, 32)}",
        lambda r: f"https://www.facebook.com/acme/posts/pfbid0{_r(r, B62, 55)}",
        lambda r: f"https://www.facebook.com/permalink.php?story_fbid={r.randint(10**15, 10**17)}&id={r.randint(10**13, 10**15)}",
        lambda r: f"https://static.wixstatic.com/media/{_r(r, HEX, 6)}_{_r(r, HEX, 32)}~mv2.jpg",
        lambda r: f"https://acme.wixsite.com/site/{_slug(r)}",
        lambda r: f"https://images.squarespace-cdn.com/content/v1/{_r(r, HEX, 24)}/{r.randint(10**12, 10**13)}-{_r(r, B62.upper(), 20)}/photo.jpg",
        lambda r: f"https://images.ctfassets.net/{_r(r, string.ascii_lowercase + string.digits, 12)}/{_r(r, B62, 22)}/{_r(r, HEX, 32)}/hero.png",
        lambda r: f"https://gist.github.com/acme/{_r(r, HEX, 32)}",
        lambda r: f"https://huggingface.co/acme/model-{r.randint(1, 99)}/blob/{_r(r, HEX, 40)}/config.json",
        lambda r: f"https://www.loom.com/share/{_r(r, HEX, 32)}",
        lambda r: f"https://www.google.com/maps/place/Acme/@33.77,-118.19,17z/data=!3m1!4b1!4m6!3m5!1s0x{_r(r, HEX, 16)}:0x{_r(r, HEX, 16)}!8m2",
        lambda r: f"https://cdn.jsdelivr.net/npm/acme-widget@{r.randint(0, 9)}.{r.randint(0, 30)}.{r.randint(0, 99)}/dist/acme.min.js",
        lambda r: f"https://unpkg.com/react@18.{r.randint(0, 3)}.{r.randint(0, 9)}/umd/react.production.min.js",
        lambda r: f"https://site.test/_next/static/{_r(r, B64URL, 21)}/_buildManifest.js",
        lambda r: f"https://www.canva.com/design/DAG{_r(r, B64URL, 8)}/{_r(r, B64URL, 22)}/view",
        lambda r: f"https://us02web.zoom.us/j/{r.randint(10**9, 10**11)}",
        lambda r: f"https://www.youtube.com/watch?v={_r(r, B64URL, 11)}",
        lambda r: f"https://youtu.be/{_r(r, B64URL, 11)}",
        lambda r: f"https://drive.google.com/file/d/1{_r(r, B64URL, 32)}/view",
        lambda r: f"https://docs.google.com/document/d/1{_r(r, B64URL, 43)}/edit",
        lambda r: f"https://open.spotify.com/episode/{_r(r, B62, 22)}",
        lambda r: f"https://www.notion.so/acme/{_slug(r, 3).title()}-{_r(r, HEX, 32)}",
        lambda r: f"https://www.figma.com/file/{_r(r, B62, 22)}/{_slug(r, 2).title()}",
        lambda r: f"https://www.indeed.com/viewjob?jk={_r(r, HEX, 16)}",
        lambda r: f"https://fonts.gstatic.com/s/inter/v{r.randint(1, 20)}/{_r(r, B64URL, 40)}.woff2",
        lambda r: f"https://x.com/acme/status/{r.randint(10**18, 10**19)}",
        lambda r: f"https://www.aliexpress.com/item/{r.randint(10**15, 10**16)}.html",
        lambda r: f"https://github.com/acme/site/commit/{_r(r, HEX, 40)}",
        lambda r: f"https://cdn.site.test/static/main.{_r(r, HEX, 16)}.css",
        lambda r: f"https://news.site.test/{r.randint(2015, 2026)}/{r.randint(1, 12):02d}/{r.randint(1, 28):02d}/{_slug(r)}",
        lambda r: f"https://www.ebay.com/itm/{r.randint(10**11, 10**12)}",
        lambda r: f"https://www.walmart.com/ip/{_slug(r, 3)}/{r.randint(10**8, 10**10)}",
        lambda r: f"https://www.etsy.com/listing/{r.randint(10**9, 10**10)}/{_slug(r)}",
        lambda r: f"https://www.amazon.com/{_slug(r, 4).title()}/dp/B0{_r(r, string.ascii_uppercase + string.digits, 8)}",
        lambda r: f"https://stackoverflow.com/questions/{r.randint(10**6, 10**8)}/{_slug(r)}",
        lambda r: f"https://doi.org/10.{r.randint(1000, 9999)}/{_r(r, string.ascii_lowercase + string.digits, 8)}",
        lambda r: f"https://arxiv.org/abs/{r.randint(2001, 2512)}.{r.randint(10000, 99999)}",
        lambda r: f"https://vimeo.com/{r.randint(10**7, 10**9)}",
        lambda r: f"https://www.tiktok.com/@acme/video/{r.randint(10**18, 10**19)}",
        lambda r: f"https://www.pinterest.com/pin/{r.randint(10**17, 10**18)}/",
        lambda r: f"https://en.wikipedia.org/wiki/{'_'.join(w.title() for w in r.sample(WORDS, 3))}",
        lambda r: f"https://www.reddit.com/r/FulfillmentByAmazon/comments/{_r(r, string.ascii_lowercase + string.digits, 7)}/{'_'.join(r.sample(WORDS, 4))}/",
        lambda r: f"https://medium.com/@acme/{_slug(r)}-{_r(r, HEX, 12)}",
        lambda r: f"https://site.test/{_slug(r)}?utm_source=x&utm_medium=y&gclid={_r(r, B64URL, 30)}",
        lambda r: f"https://site.test/blog/{_slug(r)}",
        lambda r: f"https://www.linkedin.com/posts/acme_{_slug(r, 3)}-activity-{r.randint(10**18, 10**19)}-{_r(r, B62, 4)}",
        lambda r: f"https://www.instagram.com/p/{_r(r, B64URL, 11)}/",
        lambda r: f"https://www.yelp.com/biz/{_slug(r, 3)}-long-beach",
        lambda r: f"https://www.bbb.org/us/ca/long-beach/profile/{_slug(r, 2)}/{_slug(r, 3)}-{r.randint(1000, 9999)}-{r.randint(10**8, 10**9)}",
        lambda r: f"https://www.trustpilot.com/review/{_slug(r, 2)}.com",
        lambda r: f"https://site.test/products/{_slug(r, 3)}?variant={r.randint(10**13, 10**14)}",
        lambda r: f"https://shop.site.test/collections/{_slug(r, 2)}/products/{_slug(r, 3)}",
        lambda r: f"https://www.nytimes.com/{r.randint(2015, 2026)}/{r.randint(1, 12):02d}/{r.randint(1, 28):02d}/business/{_slug(r)}.html",
        lambda r: f"https://www.forbes.com/sites/{_slug(r, 2)}/{r.randint(2015, 2026)}/{r.randint(1, 12):02d}/{r.randint(1, 28):02d}/{_slug(r)}/",
        lambda r: f"https://www.bloomberg.com/news/articles/{r.randint(2015, 2026)}-{r.randint(1, 12):02d}-{r.randint(1, 28):02d}/{_slug(r)}",
        lambda r: f"https://sellercentral.amazon.com/help/hub/reference/external/G{r.randint(200000000, 299999999)}",
        lambda r: f"https://www.google.com/search?q={'+'.join(r.sample(WORDS, 3))}",
        lambda r: f"https://site.test/Products/{''.join(w.title() for w in r.sample(WORDS, 2))}/Item{r.randint(1000, 9999)}/Details",
        lambda r: f"https://site.test/en-us/{_slug(r, 2)}/{r.randint(1, 999)}",
    ]


def corpus(n_per_template: int = 20, seed: int = 46) -> list:
    rng = random.Random(seed)
    out = []
    for t in _templates():
        for _ in range(n_per_template):
            out.append(t(rng))
    return out


def random_aws_secrets(n: int = 5000, seed: int = 4046) -> list:
    """Random strings in the documented AWS secret-access-key FORMAT (40 characters of [A-Za-z0-9/+], at least one
    digit, upper and lower case letter): synthetic test values, never real keys."""
    rng = random.Random(seed)
    alphabet = string.ascii_letters + string.digits + "/+"
    out = []
    while len(out) < n:
        k = "".join(rng.choice(alphabet) for _ in range(40))
        if any(c.isdigit() for c in k) and any(c.isupper() for c in k) and any(c.islower() for c in k) \
               :
            out.append(k)
    return out
