#!/usr/bin/env python3
"""Weekly refresh of the Ad Product Scorer.

Pulls 30-day sales and landing traffic from Shopify, real stock from Vinculum (via the
stock-lookup database), pairs tops with bottoms, scores every product, adds Meta ad
exposure, and saves the result as the "products" document of the Ads Control dashboard.

Environment:
  SHOPIFY_ACCESS_TOKEN  Admin API token (needs read_products and read_reports), or instead:
  SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET  credentials of an app made in the Shopify Dev Dashboard
  DASH_INGEST_TOKEN     write token for the dashboard database
  SHOPIFY_STORE         optional, defaults to shopmulmul.myshopify.com

Run with --fixtures DIR to compute from saved input files instead of the live APIs
(used for testing), and --out FILE to write the result to a file instead of saving it.
"""
import argparse, collections, datetime, difflib, json, math, os, re, statistics, sys, time, urllib.error, urllib.parse, urllib.request

STORE = os.environ.get("SHOPIFY_STORE", "shopmulmul.myshopify.com")
API_VERSION = "2026-10"
STOCK_URL = "https://idhtjxirqvaensqhozdc.supabase.co"
STOCK_KEY = "sb_publishable_AQumcF8aqgNNdrOtSCp4Ig_fBJWSnEK"      # public read key of the stock lookup app
DASH_URL = "https://zcnthnhdwdtxalijrcik.supabase.co"
DASH_KEY = "sb_publishable_FWivcWTl1QYFEXl5A6akMA_V2hbQcSD"       # public key; writes also need DASH_INGEST_TOKEN
DASHBOARD = "ads-control"
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
TODAY = datetime.datetime.now(IST).date()


# ---------------------------------------------------------------- fetching

def http_json(url, body=None, headers=None, tries=4):
    data = None if body is None else json.dumps(body).encode()
    for attempt in range(tries):
        req = urllib.request.Request(url, data=data, headers=dict({"Content-Type": "application/json"}, **(headers or {})))
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:500]
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                time.sleep(3 * (attempt + 1)); continue
            raise SystemExit(f"Request to {url.split('?')[0]} failed with HTTP {e.code}: {detail}")
        except urllib.error.URLError as e:
            if attempt < tries - 1:
                time.sleep(3 * (attempt + 1)); continue
            raise SystemExit(f"Could not reach {url.split('?')[0]}: {e}")


_token = None


def shopify_token():
    """Use a stored Admin API token, or swap a Dev Dashboard app's client ID and secret for a 24-hour token."""
    global _token
    if _token:
        return _token
    _token = os.environ.get("SHOPIFY_ACCESS_TOKEN")
    cid, secret = os.environ.get("SHOPIFY_CLIENT_ID"), os.environ.get("SHOPIFY_CLIENT_SECRET")
    if not _token and cid and secret:
        body = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": cid, "client_secret": secret}).encode()
        req = urllib.request.Request(f"https://{STORE}/admin/oauth/access_token", data=body,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                _token = json.loads(r.read().decode()).get("access_token")
        except urllib.error.HTTPError as e:
            raise SystemExit(f"Shopify did not issue a token (HTTP {e.code}): {e.read().decode()[:400]}")
    if not _token:
        raise SystemExit("Set SHOPIFY_ACCESS_TOKEN, or SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET.")
    return _token


def gql(query, variables=None):
    token = shopify_token()
    url = f"https://{STORE}/admin/api/{API_VERSION}/graphql.json"
    for attempt in range(8):
        out = http_json(url, {"query": query, "variables": variables or {}}, {"X-Shopify-Access-Token": token})
        errs = out.get("errors")
        if errs and any((e.get("extensions") or {}).get("code") == "THROTTLED" for e in errs if isinstance(e, dict)):
            time.sleep(2 + attempt); continue
        if errs:
            raise SystemExit(f"Shopify refused the request: {json.dumps(errs)[:600]}")
        return out["data"]
    raise SystemExit("Shopify kept throttling the request.")


def shopifyql(q):
    """Run one ShopifyQL query and return a list of dicts keyed by column name."""
    data = gql("query($q:String!){shopifyqlQuery(query:$q){parseErrors tableData{columns{name} rows}}}", {"q": q})
    res = data["shopifyqlQuery"]
    if res.get("parseErrors"):
        raise SystemExit(f"ShopifyQL rejected the query: {res['parseErrors']}\n{q}")
    if not res.get("tableData"):
        raise SystemExit("ShopifyQL returned no table. The token probably lacks the read_reports permission.")
    cols = [c["name"] for c in res["tableData"]["columns"]]
    rows = res["tableData"]["rows"]
    if isinstance(rows, str):
        rows = json.loads(rows)
    return [r if isinstance(r, dict) else dict(zip(cols, r)) for r in rows]


SALES_Q = ("FROM sales SHOW net_sales, gross_sales, discounts, sales_reversals, net_items_sold, quantity_ordered, orders, "
           "customers, new_customers, returning_customers GROUP BY product_title, product_type SINCE {since} UNTIL today "
           "ORDER BY net_items_sold DESC LIMIT 5000")
LANDING_Q = ("FROM sessions SHOW sessions, sessions_with_cart_additions, sessions_that_completed_checkout "
             "GROUP BY landing_page_path SINCE -30d UNTIL today ORDER BY sessions DESC LIMIT 3000")
UTM_Q = ("FROM sessions SHOW sessions, sessions_with_cart_additions, sessions_that_reached_checkout, sessions_that_completed_checkout "
         "GROUP BY utm_source, utm_medium, utm_campaign, landing_page_path SINCE -7d UNTIL -1d ORDER BY sessions DESC LIMIT 10000")
PRODUCTS_Q = """query($after:String){products(first:100, after:$after, query:"status:active"){
  pageInfo{hasNextPage endCursor}
  nodes{title handle productType createdAt publishedAt totalInventory tags description(truncateAt:200)
    mediaCount{count} media(first:1){nodes{preview{image{url width}}}} priceRangeV2{minVariantPrice{amount}}}}}"""
COLLECTION_Q = """query($q:String!,$after:String){collections(first:1, query:$q){nodes{title handle
  products(first:100, after:$after){pageInfo{hasNextPage endCursor} nodes{title}}}}}"""


def fetch_products():
    out, after = [], None
    while True:
        d = gql(PRODUCTS_Q, {"after": after})["products"]
        out += d["nodes"]
        if not d["pageInfo"]["hasNextPage"]:
            return out
        after = d["pageInfo"]["endCursor"]


def fetch_collection(handle):
    titles, after, title = [], None, handle
    while True:
        nodes = gql(COLLECTION_Q, {"q": f"handle:{handle}", "after": after})["collections"]["nodes"]
        if not nodes or nodes[0]["handle"] != handle:
            return title, titles
        title = nodes[0]["title"]
        p = nodes[0]["products"]
        titles += [n["title"].strip() for n in p["nodes"]]
        if not p["pageInfo"]["hasNextPage"]:
            return title, titles
        after = p["pageInfo"]["endCursor"]


def fetch_stock():
    return http_json(f"{STOCK_URL}/rest/v1/rpc/ads_stock_pairs", {}, {"apikey": STOCK_KEY, "Authorization": f"Bearer {STOCK_KEY}"})


def dash_rpc(fn, args):
    token = os.environ.get("DASH_INGEST_TOKEN")
    if not token:
        raise SystemExit("DASH_INGEST_TOKEN is not set.")
    return http_json(f"{DASH_URL}/rest/v1/rpc/{fn}", dict(args, p_token=token), {"apikey": DASH_KEY, "Authorization": f"Bearer {DASH_KEY}"})


def load_inputs(fixtures):
    """Return everything the scorer needs, from the live APIs or from saved files."""
    if not fixtures:
        return dict(products=fetch_products(), s30=shopifyql(SALES_Q.format(since="-30d")), s7=shopifyql(SALES_Q.format(since="-7d")),
                    landing=shopifyql(LANDING_Q), utm=shopifyql(UTM_Q), stock=fetch_stock(),
                    periods=dash_rpc("dash_read", {"p_dashboard": DASHBOARD, "p_name": "periods"}), collections=None)
    def rd(name):
        return json.load(open(os.path.join(fixtures, name)))
    scols = ["product_title", "product_type", "net_sales", "gross_sales", "discounts", "sales_reversals", "net_items_sold",
             "quantity_ordered", "orders", "customers", "new_customers", "returning_customers"]
    ucols = ["utm_source", "utm_medium", "utm_campaign", "landing_page_path", "sessions", "visitors", "sessions_with_cart_additions",
             "sessions_that_reached_checkout", "sessions_that_completed_checkout"]
    products = rd("prods.json")
    known = {p["title"].strip().lower() for p in products}
    for r in rd("inv_prod.json"):          # older fixtures listed stock for products outside the product pull
        if r[0].strip().lower() not in known:
            products.append({"title": r[0], "totalInventory": int(r[2]), "stub": True})
    return dict(products=products, s30=[dict(zip(scols, r)) for r in rd("s30.json")], s7=[dict(zip(scols, r)) for r in rd("s7.json")],
                landing=[dict(zip(["landing_page_path", "sessions", "sessions_with_cart_additions", "sessions_that_completed_checkout"], r)) for r in rd("sess.json")],
                utm=[dict(zip(ucols, r[:9])) for r in rd("utm.json")],
                stock={"stock": rd("vin.json"), "master": rd("master.json"), "synced_at": None},
                periods=rd("periods.json"), collections=rd("coll.json") if os.path.exists(os.path.join(fixtures, "coll.json")) else None)


# ---------------------------------------------------------------- helpers

def norm(t):
    return re.sub(r"\s+", " ", (t or "").strip().lower())


def clamp(x, a=0, b=100):
    return max(a, min(b, x))


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


EXCLUDED_TITLES = {"mulmul potli bag (colour may vary)", "potli bag (colour may vary)", "gift box", "mulmul perfume", "blouse", "stitching", ""}
COMPONENT_TYPES = {"dupatta", "pajama", "pant", "salwar", "kurta", "charms", "top", "skirt", "shirt", "gift card", "gift cards", "blouse",
                   "sharara", "garara", "jacket", "shawl", "perfume", "box", "carry bag", "fabric", "bom set"}
GENERIC = {"set", "sets", "kurta", "with", "and", "co", "ord", "coord", "mulmul", "pant", "pants", "salwar", "farshi", "short", "long", "printed",
           "print", "sharara", "garara", "kaftan", "saree", "lehenga", "dupatta", "the", "in", "of", "wide", "draped", "dhoti", "strappy", "pre",
           "blouse", "raw", "silk"}


def title_tokens(s):
    s = s.lower().replace("fuschia", "fuchsia").replace("banarasi", "banarsi").replace("&", " ").replace("/", " ")
    return [t for t in re.split(r"[^a-z0-9]+", s) if t]


# ---------------------------------------------------------------- step 1: one row per sellable product

def build_rows(inp):
    prod = {norm(p["title"]): p for p in inp["products"]}
    s30 = {}
    for r in inp["s30"]:
        k = norm(r["product_title"])
        vals = {c: num(r[c]) for c in r if c not in ("product_title", "product_type")}
        if k in s30:
            for c, v in vals.items():
                s30[k][c] = s30[k].get(c, 0) + v
        else:
            s30[k] = dict(vals, title=r["product_title"], ptype=r.get("product_type") or "")
    s7 = collections.Counter()
    for r in inp["s7"]:
        s7[norm(r["product_title"])] += num(r["net_items_sold"])
    landing = {}
    for r in inp["landing"]:
        path = (r["landing_page_path"] or "").split("?")[0]
        if path.startswith("/products/"):
            h = path[len("/products/"):].strip("/")
            cur = landing.setdefault(h, [0, 0, 0])
            cur[0] += int(num(r["sessions"])); cur[1] += int(num(r["sessions_with_cart_additions"])); cur[2] += int(num(r["sessions_that_completed_checkout"]))
    handle_tokens = {h: set(title_tokens(h)) for h in landing}

    def fuzzy_handle(title):
        tt = [t for t in title_tokens(title) if t not in GENERIC]
        if len(tt) < 2:
            return None
        best = None
        for h, ht in handle_tokens.items():
            if all(t in ht for t in tt) and (best is None or landing[h][0] > landing[best][0]):
                best = h
        return best

    rows = []
    for k in set(s30) | set(prod):
        if k in EXCLUDED_TITLES or "gift card" in k:
            continue
        p, s = prod.get(k), s30.get(k)
        full = p if p and not p.get("stub") else None
        title = (p or {}).get("title") or s["title"]
        ptype = (full or {}).get("productType") or (s["ptype"] if s else "")
        pt = ptype.lower().strip()
        if pt in COMPONENT_TYPES:
            continue
        if not pt:
            if k.startswith("mulmul ") and not re.search(r"\b(set|saree|lehenga|poshak)\b", k):
                continue
            if not re.search(r"\b(set|saree|lehenga|co-ord|kaftan|dress|anarkali|jumpsuit|poshak)\b", k):
                continue
            ptype = "Unclassified"
        if k.startswith("mulmul ") and (" with mulmul " in k or "poshak" not in k):
            continue
        if pt in ("gift box", "rakhi"):
            continue
        stock = int((p or {}).get("totalInventory") or 0)
        u30 = s["net_items_sold"] if s else 0
        if not (stock > 0 or u30 > 0):
            continue
        handle = full["handle"] if full else fuzzy_handle(title)
        pub = (full or {}).get("publishedAt") or (full or {}).get("createdAt")
        img = width = None
        if full and full.get("media", {}).get("nodes"):
            im = (full["media"]["nodes"][0].get("preview") or {}).get("image")
            if im:
                img, width = im.get("url"), im.get("width")
        price = num(full["priceRangeV2"]["minVariantPrice"]["amount"]) if full else ((s["net_sales"] / u30) if s and u30 > 0 else None)
        rows.append(dict(
            t=title, ty=ptype, stock=stock, u30=u30, u7=s7.get(k, 0), net=s["net_sales"] if s else 0, gross=s["gross_sales"] if s else 0,
            rev=-s["sales_reversals"] if s else 0, cust=s["customers"] if s else 0, newc=s["new_customers"] if s else 0,
            h=handle, exact=bool(full), s=landing.get(handle) if handle else None,
            age=(TODAY - datetime.date.fromisoformat(pub[:10])).days if pub else None,
            mc=full["mediaCount"]["count"] if full else None, dl=len(full.get("description") or "") if full else None,
            img=img, w=width, price=price))
    return rows


# ---------------------------------------------------------------- step 2: pair tops and bottoms from Vinculum

ALIAS = {"teracotta": "terracotta", "voilet": "violet", "pinkk": "pink", "duaptta": "dupatta", "olive": "", "fuschia": "fuchsia",
         "banarasi": "banarsi", "offwhite": "off white", "grey": "gray", "payjama": "pant", "pyjama": "pant", "pajama": "pant",
         "pants": "pant", "juttie": "jutti", "musturd": "mustard"}
BOTTOMS = {"Pant", "Skirt", "Short"}
DUPATTAS = {"Chunni", "Shawl"}
SKIP_CATEGORIES = {"Fabric", "Box", "Hamper", "Bom Set", "Stitching", "Physical Gift Card", "Carry Bag", "Perfume"}
SIZE_ORDER = ["XXS", "XS", "S", "M", "L", "XL", "2XL", "3XL", "4XL", "5XL", "6XL", "-"]
SET_TYPES = ("Kurta Sets", "Co-ord Set", "Kurta Set", "Kaftan Sets", "Co-ord Sets", "Kids", "Men", "Unclassified")


def piece_tokens(s):
    s = (s or "").lower().replace("off-white", "off white").replace("&", " ").replace("/", " ")
    out = []
    for t in re.split(r"[^a-z0-9]+", s):
        if t:
            out += [x for x in ALIAS.get(t, t).split() if x]
    return out


def pair_stock(rows, stock):
    have = {}
    for v in stock["stock"]:
        for k in ("wh", "st", "pr", "tr"):
            v[k] = int(v[k])
        have[(v["s"], v["n"], v["c"], v["g"])] = v
    pieces = []
    for m in stock["master"]:
        key = (m["s"], m["n"], m["c"], m["g"])
        pieces.append(have.get(key) or dict(s=m["s"], n=m["n"], c=m["c"], g=m["g"], wh=0, st=0, pr=0, tr=0, z={}))
    by_name = collections.defaultdict(list)
    for v in pieces:
        if v["g"] in SKIP_CATEGORIES:
            continue
        nt = piece_tokens(v["n"])
        if not nt:
            continue
        v["nt"], v["ct"] = set(nt), set(piece_tokens(v["c"]))
        by_name[nt[0]].append(v)

    def best(tokens, want=None, exclude=DUPATTAS, strict=True):
        pt, cands = set(tokens), []
        for t in pt:
            for v in by_name.get(t, []):
                if v["g"] in exclude or not v["ct"]:
                    continue
                if want == "bot" and v["g"] not in BOTTOMS:
                    continue
                if want == "top" and v["g"] in BOTTOMS:
                    continue
                hit = sum(1 for c in v["ct"] if c in pt or difflib.get_close_matches(c, pt, 1, 0.84))
                if hit < len(v["ct"]) and (strict or hit == 0):
                    continue
                cands.append((hit * 3 - (len(v["ct"]) - hit) * 2 + len(v["nt"] & pt), v["wh"] + v["st"], v))
        if not cands:
            return best(tokens, want, exclude, False) if strict else None
        cands.sort(key=lambda x: (-x[0], -x[1]))
        return cands[0][2]

    def order(s):
        return SIZE_ORDER.index(s) if s in SIZE_ORDER else 99

    out = {}
    for r in rows:
        h = r["h"] if r["exact"] else None
        tt = piece_tokens(r["t"])
        top = bot = None
        if h and "-with-" in h:
            a, b = h.split("-with-", 1)
            b = b.replace("kurta-with-", "")
            top = best(piece_tokens(a), "top") or best(piece_tokens(a))
            bot = best(piece_tokens(b), "bot") or best(tt, "bot")
            kind = "set"
        elif re.search(r"\b(set|with)\b", r["t"].lower()) and r["ty"] in SET_TYPES:
            top, bot, kind = best(tt, "top"), best(tt, "bot"), "set"
        else:
            top, kind = best(tt, None, exclude=set()), "single"
        if kind == "set" and top and bot and top is not bot:
            sizes = sorted(set(top["z"]) | set(bot["z"]), key=order)
            zt = lambda s: top["z"].get(s, [0, 0])
            zb = lambda s: bot["z"].get(s, [0, 0])
            live = sum(1 for s in ("S", "M", "L", "XL") if min(sum(zt(s)), sum(zb(s))) > 0)
            if not any(s in SIZE_ORDER[:10] for s in sizes):
                live = None
            out[r["t"]] = dict(
                k="set", top=f"{top['n']} · {top['c']}", bot=f"{bot['n']} · {bot['c']}", tu=top["wh"] + top["st"], bu=bot["wh"] + bot["st"],
                pairs=min(top["wh"] + top["st"], bot["wh"] + bot["st"]), wp=min(top["wh"], bot["wh"]),
                sp=sum(min(sum(zt(s)), sum(zb(s))) for s in sizes),
                pr=min(top["pr"], bot["pr"]) if top["pr"] and bot["pr"] else max(top["pr"], bot["pr"]), tr=top["tr"] + bot["tr"], core=live,
                zs=[[s, sum(zt(s)), sum(zb(s)), zt(s)[0], zb(s)[0]] for s in sizes])
        elif top and kind == "single":
            sizes = sorted(top["z"], key=order)
            out[r["t"]] = dict(k="single", top=f"{top['n']} · {top['c']}", bot=None, tu=top["wh"] + top["st"], bu=None,
                               pairs=top["wh"] + top["st"], wp=top["wh"], sp=top["wh"] + top["st"], pr=top["pr"], tr=top["tr"], core=None,
                               zs=[[s, sum(top["z"][s]), None, top["z"][s][0], None] for s in sizes])
    return out


# ---------------------------------------------------------------- step 3: score and verdict

def score(rows, pairs):
    rated = [(r["s"][1] / r["s"][0], r["s"][2] / r["s"][0]) for r in rows if r["s"] and r["s"][0] >= 300]
    med_atc = statistics.median([a for a, _ in rated]) if rated else 0.00955
    med_conv = statistics.median([c for _, c in rated]) if rated else 0.00065
    med_atc, med_conv = med_atc or 0.00955, med_conv or 0.00065
    out = []
    for r in rows:
        pz = pairs.get(r["t"])
        shop, u30, u7 = max(r["stock"], 0), max(r["u30"], 0), max(r["u7"], 0)
        stock = pz["pairs"] if pz else shop
        prod = pz["pr"] if pz else 0
        core = pz["core"] if pz else None
        unit = "pairs" if (pz and pz["k"] == "set") else "units"
        demand = clamp(100 * math.log1p(u30) / math.log1p(60))
        rr30, rr7 = u30 / 30, u7 / 7
        mom = 50 if u30 < 4 else (clamp(50 * (rr7 / rr30)) if rr30 > 0 else 50)
        cover = (stock / rr30) if rr30 > 0 else None
        su = clamp(100 * stock / 60)
        stk = su if cover is None else min(su, clamp(100 * cover / 45))
        s = r["s"]
        ses = atc = conv = cvs = None
        if s and s[0]:
            ses, atc, conv = s[0], s[1] / s[0], s[2] / s[0]
            if ses >= 300:
                cvs = clamp(25 * (atc / med_atc) + 25 * (conv / med_conv))
        age = r["age"]
        fresh = None if age is None else (100 if age <= 30 else 75 if age <= 60 else 45 if age <= 120 else 15)
        mc = r["mc"]
        content = None
        if mc is not None:
            content = 100 if mc >= 5 else 80 if mc == 4 else 50 if mc == 3 else 20
            if r["dl"] is not None and r["dl"] < 120:
                content -= 25
            if r["w"] and r["w"] < 1200:
                content -= 15
            content = clamp(content)
        ret = (r["rev"] / r["gross"]) if r["gross"] > 0 else None
        newshare = (r["newc"] / r["cust"]) if r["cust"] >= 5 else None
        parts = [(demand, .30), (mom, .10), (stk, .25), (cvs, .15), (fresh if fresh is not None else 15, .10), (content, .10)]
        w = sum(wt for v, wt in parts if v is not None)
        sc = sum(v * wt for v, wt in parts if v is not None) / w
        if ret is not None and u30 >= 5 and ret > 0.30:
            sc -= 8
        if newshare is not None and newshare >= 0.6:
            sc += 4
        sc = round(clamp(sc))
        why = []
        new = age is not None and age <= 30
        if not pz and u30 < 1 and not new:
            v = "SKIP"; why.append("no Vinculum match and no sales")
        elif stock < 8 or (cover is not None and cover < 10 and stock < 25):
            if prod >= 30 and u30 >= 5:
                v = "HOLD"; why.append(f"{stock} {unit} on hand, {prod} in production: scale when it lands")
            elif u30 >= 5:
                v = "STOP"; why.append(f"only {stock} {unit} left" + (f", {cover:.0f} days of cover" if cover is not None else "") + f" after {int(u30)} sold in 30 days")
            else:
                v = "SKIP"; why.append(f"low stock ({stock} {unit}) and low demand ({int(u30)} sold)")
        elif u30 <= 2 and stock >= 80 and not new and (age is None or age > 120):
            v = "CLEAR"; why.append(f"{stock} {unit}, {int(u30)} sold in 30 days: clearance candidate, ads will not fix it")
        elif cover is not None and cover < 21:
            v = "HOLD"; why.append(f"selling fast but {cover:.0f} days of cover ({stock} {unit})" + (f", {prod} more in production" if prod else "") + ": replenish before scaling")
        elif core is not None and core <= 1 and u30 >= 4:
            v = "HOLD"; why.append(f"sizes broken: only {core} of S, M, L, XL can be paired ({stock} {unit} in total)")
        elif ret is not None and u30 >= 8 and ret > 0.35:
            v = "HOLD"; why.append(f"{ret * 100:.0f}% of gross sales reversed (returns/cancellations): check fit or quality first")
        elif sc >= 62 and u30 >= 8:
            v = "PUSH"; why.append(f"{int(u30)} sold in 30 days, {stock} {unit}" + (f", {cover:.0f} days cover" if cover else ""))
        elif new and stock >= 30 and (content is None or content >= 75):
            v = "TEST"; why.append(f"launched {age} days ago, {stock} {unit}, {mc} images: give it a test budget")
        elif cvs is not None and ses >= 2000 and cvs < 35:
            v = "FIX"; why.append(f"{ses:,} landing sessions but {conv * 100:.2f}% buy: fix the page or creative before more spend")
        elif sc >= 50 and u30 >= 4:
            v = "HOLD"; why.append(f"steady, not a hero: {int(u30)} sold, {stock} {unit}")
        else:
            v = "SKIP"; why.append(f"weak demand: {int(u30)} sold in 30 days")
        if v in ("PUSH", "TEST") and content is not None and content < 60:
            why.append(f"needs more images ({mc})")
        if v == "PUSH" and core is not None and core <= 2:
            why.append(f"{core} of 4 core sizes pairable")
        if v in ("PUSH", "TEST") and pz and prod and prod >= stock:
            why.append(f"{prod} more in production")
        if not pz and v != "SKIP":
            why.append("not matched in Vinculum, using Shopify stock")
        if v == "PUSH" and mom >= 75:
            why.append("accelerating this week")
        if v == "PUSH" and mom <= 25 and u30 >= 10:
            why.append("slowing this week")
        if v == "PUSH" and newshare is not None and newshare >= 0.6:
            why.append(f"{newshare * 100:.0f}% new customers")
        if v == "PUSH" and ret is not None and ret > 0.25:
            why.append(f"{ret * 100:.0f}% reversed")
        p = None
        if pz:
            p = {k: pz[k] for k in ("k", "top", "bot", "tu", "bu", "wp", "sp", "pr", "tr", "core", "zs")}
        out.append(dict(
            t=r["t"], ty=r["ty"], v=v, sc=sc, why="; ".join(why), u30=int(u30), u7=int(u7), stock=stock, shop=shop, unit=unit,
            cover=None if cover is None else round(cover), ses=ses, atc=None if atc is None else round(atc * 100, 2),
            conv=None if conv is None else round(conv * 100, 3), age=age, mc=mc, ret=None if ret is None else round(ret * 100),
            newp=None if newshare is None else round(newshare * 100), price=round(r["price"]) if r["price"] else None,
            h=r["h"] if r["exact"] else None,
            sub=dict(d=round(demand), m=round(mom), s=round(stk), c=None if cvs is None else round(cvs), f=fresh, k=content),
            p=p, wp=pz["wp"] if pz else None, pr=pz["pr"] if pz else None, _net=r["net"]))
    out.sort(key=lambda x: (-x["sc"], -x["_net"]))
    for o in out:
        del o["_net"]
    return out, med_atc, med_conv


# ---------------------------------------------------------------- step 4: Meta ad exposure

CAMPAIGN_NAMES = {"120256466217730159": "Prospect_Accessories", "120256040992510159": "RC_Opportunity", "120248731981590159": "Prospect_Festive_BAU",
                  "120243700888410159": "Prospect_SmartAd", "120242781595140159": "OMNI_web+store", "120213165680460159": "Engage_Repeat Customer",
                  "120232086387380159": "Awareness_Reach"}


def is_meta(source, medium):
    s, m = (source or "").lower(), (medium or "").lower()
    if s == "adyogi":
        return m in ("ig", "fb", "wa", "th", "an", "msg")
    return s in ("facebook", "fb", "ig", "instagram", "meta") and m != "social"


def campaign_label(c):
    c = c or ""
    m = re.search(r"(\d{15,})", c)
    if m and m.group(1) in CAMPAIGN_NAMES:
        return CAMPAIGN_NAMES[m.group(1)]
    c = re.sub(r"-\d{15,}$", "", c)
    c = re.sub(r"^SHOP_\d+_adyogi_", "", c).replace("Conversions_", "")
    if re.fullmatch(r"\d{12,}", c):
        return "Campaign …" + c[-6:]
    return c or "(no campaign)"


def add_ad_exposure(data, inp):
    by_title = {o["t"]: o for o in data}
    by_handle = {p["handle"]: p["title"] for p in inp["products"] if p.get("handle")}
    core = {t: [x for x in title_tokens(t) if x not in GENERIC] for t in by_title}
    cache = {}

    def product_for(handle):
        if handle not in cache:
            t = by_handle.get(handle)
            if t not in by_title:
                ht, t = set(title_tokens(handle)), None
                for k, v in core.items():
                    if len(v) >= 2 and all(x in ht for x in v) and (t is None or len(v) > len(core[t])):
                        t = k
            cache[handle] = t
        return cache[handle]

    agg = collections.defaultdict(lambda: dict(s=0, a=0, p=0, camps=collections.Counter()))
    coll_sessions = collections.Counter()
    totals = collections.Counter()
    for r in inp["utm"]:
        if not is_meta(r.get("utm_source"), r.get("utm_medium")):
            continue
        ses = int(num(r["sessions"]))
        totals["all"] += ses
        path = (r.get("landing_page_path") or "").split("?")[0]
        if path.startswith("/products/"):
            totals["product_pages"] += ses
            t = product_for(path[len("/products/"):].strip("/"))
            if t:
                a = agg[t]
                a["s"] += ses; a["a"] += int(num(r["sessions_with_cart_additions"])); a["p"] += int(num(r["sessions_that_completed_checkout"]))
                a["camps"][campaign_label(r.get("utm_campaign"))] += ses
        elif path.startswith("/collections/"):
            totals["collection_pages"] += ses
            coll_sessions[path[len("/collections/"):].strip("/")] += ses
    for o in data:
        a = agg.get(o["t"])
        o["ats"], o["ata"], o["atp"] = (a["s"], a["a"], a["p"]) if a and a["s"] else (None, None, None)
        o["atcm"] = [[k, v] for k, v in a["camps"].most_common(3)] if a and a["s"] else None

    # products inside the collections that ads land on
    member = collections.defaultdict(list)
    top = [(h, n) for h, n in coll_sessions.most_common(8) if n >= 1000]
    for handle, sessions in top:
        if inp.get("collections") is not None:
            title, titles = handle.replace("-", " ").title(), inp["collections"].get(handle, [])
        else:
            title, titles = fetch_collection(handle)
        for t in titles:
            if t in by_title:
                member[t].append([title, sessions])
    for o in data:
        o["col"] = member.get(o["t"]) or None

    # single-product ads, from the last 7 days of the dashboard's own ad history
    periods = inp.get("periods") or {}
    ads, rows = periods.get("ads") or {}, periods.get("rows") or []
    if rows:
        last = max(r[1] for r in rows)
        start = (datetime.date.fromisoformat(last) - datetime.timedelta(days=6)).isoformat()
        days = [r for r in rows if r[0] == r[1] and start <= r[0] <= last]
        blocks = [r for r in rows if r[1] == last and r[0] != r[1]]
        use = days if days and not blocks else blocks or days
        ndays = len({r[0] for r in use}) if use is days else (datetime.date.fromisoformat(last) - datetime.date.fromisoformat(use[0][0])).days + 1
        spend = collections.defaultdict(lambda: dict(sp=0.0, pu=0, val=0.0, names=[]))
        for r in use:
            ad = ads.get(str(r[2])) or {}
            if ad.get("p") in by_title:
                a = spend[ad["p"]]
                a["sp"] += num(r[3]); a["pu"] += int(num(r[9])); a["val"] += num(r[10])
                if ad.get("n") and ad["n"] not in a["names"]:
                    a["names"].append(ad["n"])
        for o in data:
            a = spend.get(o["t"])
            if a and a["sp"] > 0:
                o["ad"], o["adp"], o["adn"] = round(a["sp"]), a["pu"], a["names"]
                o["roas"] = round(a["val"] / a["sp"], 2) if a["pu"] else None
                o["why"] += f"; ads: ₹{round(a['sp']):,} spent in {ndays} day{'s' if ndays != 1 else ''}, " + (f"ROAS {o['roas']}" if o["roas"] else "no purchases")
            else:
                o["ad"] = o["roas"] = o["adp"] = o["adn"] = None
    else:
        for o in data:
            o["ad"] = o["roas"] = o["adp"] = o["adn"] = None
    return totals, [[h, n] for h, n in top]


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures")
    ap.add_argument("--out")
    args = ap.parse_args()
    inp = load_inputs(args.fixtures)
    rows = build_rows(inp)
    pairs = pair_stock(rows, inp["stock"])
    data, med_atc, med_conv = score(rows, pairs)
    totals, top_collections = add_ad_exposure(data, inp)
    data = [o for o in data if o["v"] != "SKIP" or o["u30"] > 0 or o["ad"] is not None]   # drop idle products nobody would advertise
    verdicts = collections.Counter(o["v"] for o in data)
    judged = [o for o in data if o["v"] != "SKIP"]
    matched = sum(1 for o in judged if o["p"])
    synced = inp["stock"].get("synced_at")
    doc = dict(
        meta=dict(updated=datetime.datetime.now(IST).isoformat(timespec="seconds"),
                  sales_from=(TODAY - datetime.timedelta(days=30)).isoformat(), sales_to=TODAY.isoformat(), stock_synced=synced,
                  products=len(data), verdicts=dict(verdicts), matched_in_vinculum=matched, judged=len(judged),
                  median_atc=round(med_atc * 100, 3), median_purchase=round(med_conv * 100, 3),
                  ad_sessions=dict(totals), top_collections=top_collections),
        rows=data)
    print(json.dumps(doc["meta"], indent=1))
    if len(data) < 200 or matched < 0.6 * max(len(judged), 1):
        raise SystemExit(f"Refusing to save: only {len(data)} products and {matched} of {len(judged)} matched to Vinculum stock. Something upstream is wrong.")
    if args.out:
        json.dump(doc, open(args.out, "w"), ensure_ascii=False, separators=(",", ":"))
        print("written to", args.out)
        return
    if args.fixtures:
        print("fixture run: nothing saved")
        return
    res = dash_rpc("dash_ingest", {"p_dashboard": DASHBOARD, "p_name": "products", "p_data": doc})
    print("saved:", res)


if __name__ == "__main__":
    main()
