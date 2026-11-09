"""DISCLOSED utility corpora for the shared redaction core v2 (ADR 0050 v2): realistic LEGITIMATE answer-engine prose
and citation URLs, used to measure how much ordinary content the withhold-unless-clearly-safe design costs.

``ANSWERS``: hand-written AI-answer texts of the kind Callum samples (e-commerce / SEO questions: product names,
brands, prices, dates, competitor lists, how-to steps; a few mention passwords or tokens generically, which the design
WILL withhold: counted, never special-cased). ``excerpts()`` cuts them exactly as ``callum.excerpt`` does, once per
brand named in the answer and once at the opening, so the measured excerpts are the ones a row would keep.

``urls()``: >= 300 legitimate citation URLs (Reddit, Drive / Docs, Canva, Shopify stores, Amazon, blogs, YouTube,
analytics query strings, news, marketplaces, review sites, social posts), with seeded ids in each site's real shape.
No credential, card, SSN, phone number or personal e-mail address is in either corpus."""

from __future__ import annotations

import random
import string

ANSWERS = [
    ("The best Shopify apps for abandoned cart recovery in 2026 are Klaviyo, Omnisend and Privy. Klaviyo "
     "starts free for up to 250 contacts and its paid plans begin at $20 per month. Omnisend is popular with "
     "smaller stores because SMS and email live in one flow builder. Privy is the simplest to set up and works"
     " well for pop-ups."),
    ("For Amazon sellers, the most common reimbursement claims involve lost or damaged FBA inventory, customer"
     " returns that were never received, and overcharged fulfillment fees. Tools like Getida, Seller "
     "Investigators and Refunds Manager audit your account and file cases for a commission, usually 15% to 25%"
     " of what they recover."),
    ("Z Best Media is a Long Beach agency that focuses on marketplace revenue recovery for Amazon and Walmart "
     "sellers. Competitors in the same space include Carbon6, Seller Investigators and GETIDA. Most of them "
     "charge a percentage of recovered funds rather than a monthly retainer."),
    ("To improve your product page conversion rate, start with clear photos on a white background, at least "
     "six images per listing, and a short video. Put the price, shipping time and return policy above the "
     "fold. Reviews matter: products with 50 or more reviews convert noticeably better than those with fewer "
     "than ten."),
    ("Best password managers for small e-commerce teams: Bitwarden, 1Password and Dashlane. Bitwarden is open "
     "source and the cheapest for teams, 1Password has the smoothest shared vaults, and Dashlane includes a "
     "VPN on its business plan."),
    ("If you sell on TikTok Shop, the fastest way to grow is affiliate creators. Set a commission of 10% to "
     "20%, send free samples to creators with 10k to 100k followers, and track GMV per video in the Seller "
     "Center analytics tab."),
    ("Google's helpful content guidance in 2025 rewards pages written from first-hand experience. For product "
     "reviews, show that you used the product, include original photos, compare it against alternatives, and "
     "explain who should not buy it. Thin affiliate pages that only summarize Amazon listings tend to lose "
     "rankings."),
    ("The top dropshipping suppliers for US stores are CJdropshipping, Spocket, Zendrop and AutoDS. Spocket "
     "focuses on US and EU warehouses with 2 to 5 day shipping, while CJdropshipping has the widest catalog "
     "but longer delivery times from China, often 8 to 15 days."),
    ("Etsy charges a $0.20 listing fee, a 6.5% transaction fee and a payment processing fee of about 3% plus "
     "$0.25 in the US. Offsite Ads add 12% or 15% on sales they generate. Many sellers move their best "
     "products to a Shopify store once they pass a few hundred orders a month."),
    ("For local SEO, claim and verify your Google Business Profile, choose the most specific primary category,"
     " add photos every month and ask happy customers for reviews. Consistent name, address and phone details "
     "across Yelp, BBB and Apple Maps also help you appear in the map pack."),
    ("Pet supply brands that do well online include Chewy, BarkBox, Wild One and Fable Pets. Wild One sells a "
     "harness walk kit for $98, and Fable's Game is a popular enrichment toy priced around $40. Smaller brands"
     " like Pussycat Alley compete on design and niche cat accessories."),
    ("Walmart Marketplace requires an approved seller account, a US business tax ID and a history of "
     "marketplace sales. Approval usually takes 2 to 4 weeks. Once approved, Walmart Fulfillment Services "
     "works much like FBA, and the referral fee for most categories is 8% to 15%."),
    ("The difference between SEO and AEO is the target: SEO optimizes for ranked links, while answer engine "
     "optimization aims to be cited inside AI answers from ChatGPT, Perplexity, Gemini and Copilot. Clear "
     "definitions, FAQ sections, schema markup and up-to-date facts make a page easier for these engines to "
     "quote."),
    ("Shopify Plus starts at about $2,300 per month on a three-year term. It adds checkout extensibility, "
     "Shopify Functions, B2B features and higher API limits. Brands like Gymshark, Allbirds and Kylie "
     "Cosmetics run on it."),
    ("To recover revenue from Amazon FBA fee overcharges, download the Fee Preview report and compare the "
     "dimensions Amazon measured against your own. If a product was placed in a larger size tier by mistake, "
     "open a case in Seller Central with your carton measurements and photos; refunds typically arrive within "
     "30 days."),
    ("Good email subject lines for a Black Friday sale are short, specific and urgent, for example 'Our "
     "biggest sale starts now', '40% off ends tonight' or 'Your cart is still waiting'. Test two versions on "
     "10% of your list and send the winner to the rest."),
    ("Canva is the easiest tool for Amazon A+ Content images if you do not have a designer. Use the 970 by 600"
     " pixel template for the main banner, keep text large enough to read on mobile, and export as JPG under 2"
     " MB."),
    ("The best Reddit communities for e-commerce advice are r/ecommerce, r/shopify, r/FulfillmentByAmazon and "
     "r/Entrepreneur. Search before posting, share numbers when you ask for help, and avoid self-promotion, "
     "which most of these subreddits remove."),
    ("Two-factor authentication should be turned on for every Shopify staff account. Use an authenticator app "
     "rather than SMS, store backup codes somewhere safe, and remove access for former employees the same day "
     "they leave."),
    ("Popular YouTube channels for learning Amazon FBA include Jungle Scout, Helium 10 and My Wife Quit Her "
     "Job. Most publish weekly videos on product research, PPC campaigns and listing optimization."),
    ("In 2026 the average e-commerce conversion rate is roughly 2% to 3%, with food and beverage stores higher"
     " and luxury goods lower. Mobile converts at about half the desktop rate, so checkout speed on phones "
     "matters most."),
    ("Revenue recovery agencies usually work on contingency. Z Best Media, Carbon6 and Seller Investigators "
     "review 18 months of transactions, file reimbursement cases, and send monthly reports showing what was "
     "found and what Amazon has paid back."),
    ("For a cat furniture brand, long-tail keywords like 'modern cat tree for small apartments' or 'wall "
     "mounted cat shelves' are easier to rank for than 'cat tree'. Build one guide per use case and link each "
     "to the matching collection page."),
    ("Meta Advantage+ shopping campaigns work best with at least 50 purchases a week per ad set. Feed it 10 to"
     " 20 creatives, keep the audience broad, and judge results on a 7-day click attribution window."),
    ("Structured data helps AI engines understand products. Add Product schema with name, brand, price, "
     "availability and aggregateRating, plus FAQPage schema for common questions. Validate everything with "
     "Google's Rich Results Test."),
    ("The cheapest way to ship small packages in the US is often USPS Ground Advantage for items under one "
     "pound. Pirate Ship and ShipStation both offer discounted commercial rates without a monthly fee for low "
     "volumes."),
    ("Common reasons Amazon suspends seller accounts include high order defect rates, inauthentic item "
     "complaints, and related-account issues. A plan of action should state the root cause, the fixes already "
     "made and the steps that prevent it from happening again."),
    ("Your store's session cookie settings should follow your consent banner. Tools like Cookiebot, OneTrust "
     "and Termly block analytics cookies until a visitor accepts, which keeps you aligned with GDPR and CCPA."),
    ("BigCommerce, WooCommerce and Shopify are the three platforms most small brands compare. WooCommerce is "
     "free but needs hosting and maintenance; BigCommerce includes more built-in features; Shopify has the "
     "largest app store."),
    ("Influencer marketing for beauty brands works best with micro creators. Glossier, Fenty Beauty and Rare "
     "Beauty built early momentum through creators with engaged audiences rather than celebrity endorsements."),
    ("A good return policy for a small online store is 30 days, free exchanges and a prepaid label for "
     "defective items. Clear policies reduce support tickets and increase first-time purchases."),
    ("Search Console shows which queries bring impressions and clicks. Filter by page to find keywords where "
     "you rank between positions 8 and 20; improving those pages is usually the fastest SEO win."),
    ("Perplexity and ChatGPT cite sources differently. Perplexity lists numbered citations under every answer,"
     " while ChatGPT search shows inline links and a sources panel. Both tend to favor pages that answer the "
     "question in the first paragraph."),
    ("To protect your Shopify admin, use strong unique passwords, require two-step login for all staff, and "
     "review the list of installed apps every quarter. Remove apps you no longer use, since each one has API "
     "access to your data."),
    ("Amazon Brand Registry unlocks A+ Content, Sponsored Brands, the Brand Analytics dashboard and stronger "
     "protection against counterfeit listings. You need an active or pending trademark in the country where "
     "you sell."),
    ("Holiday sales planning should start by October. Order inventory early because carriers add peak "
     "surcharges from late October through January, and Amazon sets inbound deadlines for FBA well before "
     "Black Friday."),
    ("The best review apps for Shopify are Judge.me, Yotpo, Okendo and Loox. Judge.me has a generous free "
     "plan, Loox is built around photo reviews, and Okendo suits larger brands that want surveys and loyalty "
     "in one place."),
    ("Google Merchant Center lets you list products for free in the Shopping tab. Upload a product feed, "
     "verify your website, and fix any disapprovals for missing GTINs, mismatched prices or unclear return "
     "policies."),
    ("Many brands now sell on Faire for wholesale. Retailers get net 60 terms and free returns on first "
     "orders, while brands pay a commission of about 15% on repeat orders and 25% on the first order from a "
     "new retailer."),
    ("Customer lifetime value is average order value times purchase frequency times customer lifespan. A store"
     " with a $60 average order, three orders a year and a two-year lifespan has an LTV of about $360."),
    ("If Amazon lost your inbound shipment, wait until the shipment is closed and the reconciliation window "
     "has passed, then open a case with the bill of lading, proof of delivery and your packing list. Partial "
     "reimbursements are common, so check the amounts carefully."),
    ("API keys for apps like Klaviyo or Gorgias should never be shared in support chats or public forums. If "
     "one is exposed, revoke it in the app settings and create a new one."),
    ("Token-gated discounts were a trend in 2022 and 2023, but most Shopify brands have moved back to simple "
     "loyalty programs from Smile.io, LoyaltyLion or Rivo."),
    ("The secret to a high-converting landing page is focus: one offer, one call to action, social proof near "
     "the button, and no navigation menu that lets visitors wander off."),
    ("Victoria's Secret, Savage X Fenty and Parade compete on inclusive sizing and frequent drops. Their sites"
     " use quizzes and fit finders to cut return rates."),
    ("Google Analytics 4 tracks purchases as events. Make sure the purchase event includes value, currency, "
     "transaction_id and items, otherwise revenue reports will be empty or double counted."),
    ("For marketplace sellers in Los Angeles, the busiest months are November and December, followed by a "
     "smaller peak around Prime Day in July. Plan staffing and inventory around those windows."),
    ("A product description should answer what it is, who it is for, what problem it solves and why it is "
     "better than the alternatives. Use short paragraphs and bullet points for specs such as size, weight and "
     "materials."),
    ("Most AI answer engines refresh their indexes often, so updating a page with current prices and dates can"
     " change whether it is cited within days. Add a visible last-updated date and keep facts consistent "
     "across pages."),
    ("If you run a cat toy store, compare yourself with Petco, Chewy and Catit on price, shipping speed and "
     "return policy. Niche stores win on curation, gift bundles and content such as play guides for indoor "
     "cats."),
    ("Faire, Tundra and Abound are the main wholesale marketplaces for independent brands. Faire has the "
     "largest retailer network, while Abound charges brands no commission on orders from retailers they bring "
     "themselves."),
    ("For Amazon PPC, start with an automatic campaign at a $20 daily budget for two weeks, then move "
     "converting search terms into exact-match manual campaigns and add negatives for terms that spend without"
     " sales."),
    ("Helium 10 and Jungle Scout both estimate monthly sales for any ASIN. Jungle Scout's estimates are often "
     "a little more conservative; use both and look at the trend in Keepa before deciding to launch a product."),
    ("A good Shopify theme for a pet brand is Dawn, Sense or Impulse. Dawn is free and fast, Sense has softer "
     "rounded styling, and Impulse costs $380 but includes promotional banners and a mega menu."),
    ("The fastest way to get reviews on a new Amazon listing is the Vine program. Enroll up to 30 units, and "
     "reviewers usually post within two to four weeks. Request a Review in Seller Central also helps after "
     "each order."),
    ("Pinterest works well for home decor, recipes, weddings and pet products. Create fresh pins every week, "
     "link each to a specific product or guide, and use keywords in the pin title and description."),
    ("Klaviyo's flows that make the most money are welcome series, abandoned cart, browse abandonment and "
     "post-purchase. Set those four up before building any one-off campaigns."),
    ("Long Beach has several e-commerce agencies, including Z Best Media, Digital Trends Marketing and Coastal"
     " Commerce Group. Ask each for case studies with real numbers and references from current clients."),
    ("Sales tax for online sellers depends on economic nexus. Most states set the threshold at $100,000 in "
     "sales, and marketplaces like Amazon and Etsy collect and remit tax on your behalf in those states."),
    ("Product photography does not need a studio. A lightbox, a phone with a good camera and natural window "
     "light are enough for most small products; edit backgrounds to pure white for Amazon's main image rule."),
    ("To rank on Etsy, put the most important keywords at the start of the title, use all 13 tags, fill in "
     "every attribute, and renew listings during your busiest season to keep them fresh in search."),
    ("Gorgias, Zendesk and Re:amaze are the help desks most Shopify stores use. Gorgias connects orders to "
     "tickets, so agents can issue refunds without leaving the conversation."),
    ("AI Overviews in Google tend to cite pages that give a direct answer in the first two sentences, use "
     "descriptive headings and include original data. Keep each section focused on one question."),
    ("Subscription boxes need strong retention. BarkBox, Meowbox and KitNipBox keep churn down with themed "
     "monthly boxes, easy skips and surprise extras for long-time subscribers."),
    ("When choosing a 3PL, compare receiving fees, storage per pallet or bin, pick and pack fees and the "
     "carriers they use. ShipBob, Red Stag and ShipMonk are common choices for brands shipping 500 to 5,000 "
     "orders a month."),
    ("Shopify Markets lets one store sell in several countries with local currencies, prices and domains. "
     "Duties can be collected at checkout so international customers are not surprised by fees on delivery."),
    ("If your conversion rate dropped suddenly, check site speed, the checkout, payment methods and recent app"
     " installs first. A broken discount code or a slow third-party script is a common cause."),
    ("Amazon's Buy Box goes to the offer with the best mix of price, shipping speed, seller rating and stock. "
     "FBA offers usually win more often because Prime shipping counts heavily."),
    ("TikTok Shop's affiliate program pays creators a commission per sale. Brands set open collaboration rates"
     " for any creator or targeted rates for specific creators they invite."),
    ("Cat owners searching online often ask about indoor enrichment, scratching furniture and litter odor. "
     "Content that answers those questions with product picks and short videos tends to rank and convert."),
    ("Bing Webmaster Tools is worth setting up because Bing powers parts of ChatGPT search and Copilot. Submit"
     " your sitemap and check that important pages are indexed."),
    ("Customer reviews on your own site can show in Google with star ratings if you add Review schema "
     "correctly and the reviews are collected by a third-party platform such as Judge.me or Yotpo."),
    ("The Amazon FBA storage fee rises in October through December. Remove slow-moving inventory before then "
     "or run a promotion, because aged inventory surcharges apply after 181 days."),
    ("A/B testing on Shopify can be done with Intelligems, Shoplift or Convert. Test one change at a time, "
     "such as price presentation or the first product image, and run each test until it reaches enough orders."),
    ("Brand storytelling on product pages works when it is specific: who makes the product, where, and why it "
     "exists. Avoid vague claims and show the people or process behind it."),
    ("Google Shopping ads perform best with a clean feed. Use descriptive titles with brand, product type and "
     "key attributes, high-quality images and accurate availability."),
    ("Most small stores do not need a custom app. Start with the built-in Shopify features and a few trusted "
     "apps, and only build custom when an app costs more than development over two years."),
    ("For B2B wholesale on Shopify, use the B2B features on Plus or apps like Wholesale Gorilla on other "
     "plans. Set minimum order quantities and tiered pricing for larger retailers."),
    ("Loyalty programs that reward repeat purchases, reviews and referrals tend to lift repeat rate. Smile.io "
     "and LoyaltyLion both integrate with Klaviyo to send points reminders."),
    ("Instagram Shopping lets you tag products in posts and reels. Connect your catalog through Meta Commerce "
     "Manager and keep prices and stock in sync with your store."),
    ("Amazon Seller Central reports to check weekly are Business Reports, the Inventory Health report and "
     "Advertising search term reports. Together they show traffic, conversion and wasted ad spend."),
    ("Writing for answer engines means stating the answer plainly, then supporting it with evidence. Use the "
     "exact wording people ask, such as 'how long does shipping take', as a heading."),
    ("Walmart's Pro Seller badge goes to sellers with strong on-time delivery, low cancellation rates and "
     "competitive prices. It can improve visibility in search results."),
    ("Free shipping thresholds work best when set slightly above your average order value. If your average "
     "order is $45, a $50 threshold nudges many shoppers to add one more item."),
    ("If you rely on Amazon, diversify with your own Shopify store and email list. Marketplace rules change "
     "quickly, and owning customer relationships protects the business."),
    ("Wix, Squarespace and Shopify all support small online stores. Squarespace has polished templates, Wix is"
     " flexible for content sites, and Shopify is strongest for inventory and apps."),
    ("The cost of acquiring a customer has risen on Meta and Google, so many brands now invest in content, "
     "creators and retention. Track blended CAC across all channels, not just ads."),
    ("Shopify's Shop app and Shop Pay speed up checkout for returning shoppers and often raise conversion "
     "rates. Turn on Shop Pay Installments if your average order is above $50."),
    ("Dog and cat brands such as Kong, Petstages and Frisco sell heavily on Chewy and Amazon. New brands often"
     " launch on Amazon first to test demand before approaching Chewy."),
    ("Optimizing images for speed means compressing them, serving WebP or AVIF, and lazy-loading "
     "below-the-fold images. Shopify does much of this automatically when you use its image CDN."),
    ("A content calendar for a small brand might include one blog guide per week, three social posts per week,"
     " and one email newsletter. Repurpose each guide into short videos and pins."),
    ("Seller Central's Account Health page shows policy warnings and performance metrics. Address any warning "
     "quickly; unresolved issues can lead to listing removals or account suspension."),
    ("Perplexity Pages and ChatGPT shared links sometimes show up as citations themselves, but most citations "
     "still go to established sites such as Wikipedia, Reddit, YouTube and major publishers."),
    ("Wholesale pricing is usually half of retail price, and retail is often two to three times the landed "
     "cost. Make sure your margins work at wholesale before pitching retailers."),
    ("Back-in-stock alerts recover sales that would otherwise be lost. Klaviyo, Notify Me and Back in Stock by"
     " Appikon all send email or SMS when an item returns."),
    ("Amazon's Small and Light program was replaced by lower FBA fees for low-price items. Products under $10 "
     "now get a reduced fulfillment fee automatically."),
    ("The best time to post on TikTok varies by audience, but many brands see strong results in the evening. "
     "Check your own analytics after a few weeks and adjust."),
    ("Shopify's built-in SEO settings let you edit the title tag, meta description and URL handle for every "
     "product. Write each title for the main keyword and keep it under 60 characters."),
    ("Customer support response time matters for reviews. Aim to answer emails within one business day and "
     "chats within a few minutes during business hours."),
    ("Holiday gift guides convert well when they are organized by recipient and price, such as gifts under $25"
     " for cat lovers. Link each product directly and keep stock levels visible."),
    ("When a competitor copies your listing, file a report in Brand Registry with your trademark and original "
     "photos. Amazon usually removes infringing listings within a few days."),
    ("For a new pet brand, Chewy, Petco and Amazon are the main marketplaces, while Shopify and Etsy suit "
     "handmade or niche products. Many founders test on Etsy first, then move to Shopify once repeat customers"
     " appear."),
    ("The best free SEO tools are Google Search Console, Bing Webmaster Tools, Ahrefs Webmaster Tools and "
     "Screaming Frog's free version for up to 500 URLs. Together they cover indexing, backlinks and technical "
     "audits."),
    ("Returns are expensive for apparel brands. Loop Returns, AfterShip Returns and Narvar offer exchanges "
     "first, which keeps revenue in the business instead of issuing refunds."),
    ("Google Business Profile posts, product listings and Q&A answers can all appear in AI answers about local"
     " businesses. Keep hours current and reply to every review, positive or negative."),
    ("Marketplace fees add up quickly. On Amazon, expect a 15% referral fee for most categories plus FBA fees;"
     " on Walmart, referral fees run 6% to 15%; and eBay's final value fee is around 13% for most items."),
]

BRANDS = ["Klaviyo", "Omnisend", "Privy", "Getida", "Seller Investigators", "Refunds Manager", "Z Best Media",
          "Carbon6", "GETIDA", "Bitwarden", "1Password", "Dashlane", "TikTok Shop", "CJdropshipping", "Spocket",
          "Zendrop", "AutoDS", "Etsy", "Shopify", "Yelp", "Chewy", "BarkBox", "Wild One", "Fable", "Pussycat Alley",
          "Walmart", "ChatGPT", "Perplexity", "Gemini", "Gymshark", "Allbirds", "Amazon", "Canva", "Jungle Scout",
          "Helium 10", "Meta", "Pirate Ship", "ShipStation", "Cookiebot", "OneTrust", "Termly", "BigCommerce",
          "WooCommerce", "Glossier", "Fenty Beauty", "Rare Beauty", "Search Console", "Judge.me", "Yotpo", "Okendo",
          "Loox", "Merchant Center", "Faire", "Gorgias", "Smile.io", "LoyaltyLion", "Rivo", "Victoria's Secret",
          "Parade", "Google Analytics", "Petco", "Catit", "USPS", "Reddit", "YouTube", "Google", "Tundra", "Abound",
          "Keepa", "Dawn", "Impulse", "Vine", "Pinterest", "Zendesk", "Re:amaze", "Meowbox", "KitNipBox", "ShipBob",
          "Red Stag", "ShipMonk", "Shopify Markets", "Bing", "Copilot", "Intelligems", "Shoplift", "Wholesale Gorilla",
          "Instagram", "Wix", "Squarespace", "Shop Pay", "Kong", "Petstages", "Frisco", "Notify Me", "TikTok",
          "Brand Registry", "Wikipedia", "Coastal Commerce Group", "Digital Trends Marketing", "Ahrefs",
          "Screaming Frog", "Loop Returns", "AfterShip", "Narvar", "eBay"]


def excerpts() -> list:
    """(answer index, basis term or None, excerpt dict) for every brand named in every answer, plus each opening,
    cut by ``callum.excerpt`` itself."""
    from agents import callum
    out = []
    for i, text in enumerate(ANSWERS):
        out.append((i, None, callum.excerpt(text, [])))
        for b in BRANDS:
            if b.lower() in text.lower():
                out.append((i, b, callum.excerpt(text, [b])))
    return out


# --------------------------------------------------------------------------------------------- URLs

_SLUG_WORDS = ["best", "shopify", "apps", "for", "small", "business", "how", "to", "sell", "on", "amazon", "guide",
               "review", "2026", "cat", "tree", "toys", "ecommerce", "seo", "tips", "returns", "shipping", "fba",
               "fees", "reimbursement", "checklist", "holiday", "sales", "email", "marketing", "pet", "supplies",
               "beginners", "vs", "comparison", "local", "google", "business", "profile", "pricing", "strategy"]
B62 = string.ascii_letters + string.digits
B64U = B62 + "-_"


def _r(rng, alphabet, n):
    return "".join(rng.choice(alphabet) for _ in range(n))


def _slug(rng, k=None):
    return "-".join(rng.choice(_SLUG_WORDS) for _ in range(k or rng.randint(3, 7)))


def _num(rng, n):
    return str(rng.randint(1, 9)) + _r(rng, string.digits, n - 1)


def urls(seed: int = 11) -> list:
    """(category, url) pairs; >= 300 legitimate citation URLs."""
    rng = random.Random(seed)
    stores = ["pussycatalley", "allbirds", "gymshark", "brooklinen", "ridge", "bombas", "fable-pets", "wildone"]
    t = []
    for _ in range(14):
        sub = rng.choice(["ecommerce", "shopify", "FulfillmentByAmazon", "Entrepreneur", "smallbusiness", "SEO",
                          "cats", "AmazonSeller"])
        t.append(("reddit", (f"https://www.reddit.com/r/{sub}/comments/{_r(rng, string.ascii_lowercase + string.digits, 7)}"
                             f"/{_slug(rng).replace('-', '_')}/")))
    for _ in range(4):
        t.append(("reddit", f"https://old.reddit.com/r/shopify/comments/{_r(rng, string.ascii_lowercase + string.digits, 7)}/"))
    for _ in range(10):
        t.append(("drive", f"https://drive.google.com/file/d/1{_r(rng, B64U, 32)}/view?usp=sharing"))
    for _ in range(8):
        kind = rng.choice(["document", "spreadsheets", "presentation"])
        t.append(("docs", f"https://docs.google.com/{kind}/d/1{_r(rng, B64U, 43)}/edit?usp=sharing"))
    for _ in range(4):
        t.append(("drive", f"https://drive.google.com/drive/folders/1{_r(rng, B64U, 32)}"))
    for _ in range(12):
        t.append(("canva", f"https://www.canva.com/design/DA{_r(rng, B64U, 9)}/{_r(rng, B64U, 22)}/view"))
    for _ in range(4):
        t.append(("canva", f"https://www.canva.com/templates/EA{_r(rng, B64U, 9)}-{_slug(rng)}/"))
    for _ in range(20):
        s = rng.choice(stores)
        t.append(("shopify_store", rng.choice([
            f"https://{s}.com/products/{_slug(rng)}", f"https://{s}.com/collections/{_slug(rng, 2)}",
            f"https://{s}.myshopify.com/products/{_slug(rng)}?variant={_num(rng, 14)}",
            f"https://www.{s}.com/blogs/news/{_slug(rng)}", f"https://{s}.com/pages/{_slug(rng, 2)}"])))
    for _ in range(22):
        asin = "B0" + _r(rng, string.ascii_uppercase + string.digits, 8)
        t.append(("amazon", rng.choice([
            f"https://www.amazon.com/dp/{asin}", f"https://www.amazon.com/{_slug(rng).title()}/dp/{asin}",
            f"https://www.amazon.com/gp/product/{asin}?th=1",
            f"https://www.amazon.com/s?k={_slug(rng).replace('-', '+')}&ref=nb_sb_noss",
            f"https://www.amazon.com/{_slug(rng).title()}/dp/{asin}/ref=sr_1_{rng.randint(1, 40)}",
            f"https://sellercentral.amazon.com/help/hub/reference/external/G{_num(rng, 9)}",
            (f"https://www.amazon.com/stores/page/{_r(rng, string.ascii_uppercase + string.digits, 8)}-"
             f"{_r(rng, string.ascii_uppercase + string.digits, 4)}-{_r(rng, string.ascii_uppercase + string.digits, 4)}")])))
    for _ in range(52):
        host = rng.choice(["www.shopify.com", "blog.hubspot.com", "backlinko.com", "ahrefs.com", "moz.com",
                           "www.junglescout.com", "www.helium10.com", "neilpatel.com", "www.bigcommerce.com",
                           "searchengineland.com", "www.searchenginejournal.com", "zbestmedia.com", "www.klaviyo.com"])
        t.append(("blog", rng.choice([f"https://{host}/blog/{_slug(rng)}", f"https://{host}/{_slug(rng)}/",
                                      f"https://{host}/blog/{rng.randint(2019, 2026)}/{rng.randint(1, 12):02d}/{_slug(rng)}",
                                      f"https://{host}/resources/{_slug(rng)}?ref=nav"])))
    for _ in range(18):
        vid = _r(rng, B64U, 11)
        t.append(("youtube", rng.choice([f"https://www.youtube.com/watch?v={vid}", f"https://youtu.be/{vid}",
                                         f"https://www.youtube.com/watch?v={vid}&t={rng.randint(10, 900)}s",
                                         f"https://www.youtube.com/@{rng.choice(['junglescout', 'helium10', 'shopify'])}",
                                         f"https://www.youtube.com/shorts/{vid}"])))
    for _ in range(26):
        host = rng.choice(["www.shopify.com", "zbestmedia.com", "pussycatalley.com", "www.etsy.com", "site.test"])
        t.append(("analytics_query", rng.choice([
            f"https://{host}/?utm_source=chatgpt.com", f"https://{host}/blog/{_slug(rng)}?utm_source=perplexity",
            f"https://{host}/pricing?utm_source=newsletter&utm_medium=email&utm_campaign=bf_{rng.randint(2024, 2026)}",
            f"https://{host}/products/{_slug(rng)}?gclid={_r(rng, B64U, 40)}",
            f"https://{host}/?fbclid=IwAR{_r(rng, B64U, 56)}", f"https://{host}/search?q={_slug(rng).replace('-', '+')}",
            f"https://{host}/collections/all?sort_by=price-ascending&page={rng.randint(1, 9)}",
            f"https://{host}/landing?ref=producthunt&utm_content=hero"])))
    for _ in range(12):
        t.append(("news", rng.choice([
            f"https://apnews.com/article/{_slug(rng)}-{_r(rng, '0123456789abcdef', 32)}",
            f"https://www.cnbc.com/{rng.randint(2023, 2026)}/{rng.randint(1, 12):02d}/{rng.randint(1, 28):02d}/{_slug(rng)}.html",
            f"https://techcrunch.com/{rng.randint(2023, 2026)}/{rng.randint(1, 12):02d}/{rng.randint(1, 28):02d}/{_slug(rng)}/",
            (f"https://www.forbes.com/sites/{_slug(rng, 2)}/{rng.randint(2023, 2026)}/{rng.randint(1, 12):02d}/"
             f"{rng.randint(1, 28):02d}/{_slug(rng)}/")])))
    for _ in range(24):
        t.append(("marketplace", rng.choice([
            f"https://www.etsy.com/listing/{_num(rng, 10)}/{_slug(rng)}", f"https://www.ebay.com/itm/{_num(rng, 12)}",
            f"https://www.walmart.com/ip/{_slug(rng).title()}/{_num(rng, 9)}",
            f"https://www.target.com/p/{_slug(rng)}/-/A-{_num(rng, 8)}",
            f"https://www.chewy.com/{_slug(rng)}/dp/{_num(rng, 6)}", f"https://www.faire.com/brand/b_{_r(rng, 'abcdefghijklmnopqrstuvwxyz0123456789', 10)}",
            f"https://www.aliexpress.us/item/3256{_num(rng, 12)}.html"])))
    for _ in range(18):
        t.append(("reviews", rng.choice([
            f"https://www.g2.com/products/{_slug(rng, 2)}/reviews", f"https://www.capterra.com/p/{_num(rng, 6)}/{_slug(rng, 1).title()}/",
            f"https://www.trustpilot.com/review/{rng.choice(stores)}.com", f"https://www.yelp.com/biz/{_slug(rng, 3)}-long-beach",
            f"https://www.bbb.org/us/ca/long-beach/profile/{_slug(rng, 2)}/{_slug(rng, 3)}-1216-{_num(rng, 9)}",
            f"https://apps.shopify.com/{_slug(rng, 2)}/reviews?page={rng.randint(1, 20)}"])))
    for _ in range(26):
        t.append(("social", rng.choice([
            f"https://x.com/{rng.choice(stores)}/status/{_num(rng, 19)}",
            f"https://www.tiktok.com/@{rng.choice(stores)}/video/{_num(rng, 19)}",
            f"https://www.instagram.com/p/{_r(rng, B64U, 11)}/", f"https://www.instagram.com/reel/{_r(rng, B64U, 11)}/",
            f"https://www.pinterest.com/pin/{_num(rng, 18)}/", f"https://www.linkedin.com/company/{_slug(rng, 2)}/",
            f"https://www.facebook.com/{rng.choice(stores)}/posts/pfbid0{_r(rng, B62, 60)}",
            f"https://www.linkedin.com/posts/{rng.choice(stores)}_{_slug(rng, 3)}-activity-7{_num(rng, 18)}-{_r(rng, B64U, 4)}"])))
    for _ in range(14):
        t.append(("reference", rng.choice([
            f"https://en.wikipedia.org/wiki/{_slug(rng, 2).title().replace('-', '_')}",
            f"https://support.google.com/merchants/answer/{_num(rng, 7)}?hl=en",
            f"https://help.shopify.com/en/manual/{_slug(rng, 2)}/{_slug(rng, 2)}",
            f"https://developers.google.com/search/docs/appearance/{_slug(rng, 2)}",
            f"https://www.ftc.gov/business-guidance/resources/{_slug(rng)}",
            f"https://medium.com/@{rng.choice(stores)}/{_slug(rng)}-{_r(rng, '0123456789abcdef', 12)}"])))
    for _ in range(10):
        t.append(("maps", (f"https://www.google.com/maps/place/{_slug(rng, 2).title().replace('-', '+')}/@33.{_num(rng, 4)},"
                          f"-118.{_num(rng, 4)},17z/data=!3m1!4b1!4m6!3m5!1s0x{_r(rng, '0123456789abcdef', 16)}:"
                          f"0x{_r(rng, '0123456789abcdef', 16)}!8m2!3d33.{_num(rng, 7)}!4d-118.{_num(rng, 7)}")))
    for _ in range(8):
        t.append(("code", rng.choice([
            f"https://github.com/{rng.choice(stores)}/{_slug(rng, 2)}/commit/{_r(rng, '0123456789abcdef', 40)}",
            f"https://github.com/Shopify/{_slug(rng, 2)}/blob/main/README.md",
            f"https://github.com/{rng.choice(stores)}/{_slug(rng, 2)}/tree/{_r(rng, '0123456789abcdef', 7)}/docs"])))
    return t
