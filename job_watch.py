#!/usr/bin/env python3
"""Early-career job radar: new grad + internship postings, as fast as public data allows.

Sources (polled every run):
  1. Community trackers — SimplifyJobs + vanshb03 (new grad & Summer 2027 intern JSON),
     speedyapply + negarprh (Canada) README tables.
  2. Direct ATS polling — every Greenhouse / Ashby / Lever / SmartRecruiters / Workday board that
     appears in the trackers (auto-discovered, ~3,000 boards), plus our hand-picked lists.
     This catches drops before the trackers do.
  3. Amazon's own search API.
  4. Careers pages with no API (diffed).

Outputs:
  state/feed/YYYY-MM.csv  every new early-career posting (intern + new grad), timestamped
  state/seen.txt          hashes of everything already seen
  GitHub issue            ONLY for new grad roles you can take: Canada/remote, or US at a watchlist company.

Run locally: python3 job_watch.py   (add --dry to skip writing state)
"""
import csv, hashlib, html, json, os, re, ssl, sys, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE / "state"
SEEN = STATE / "seen.txt"
BOARDS_SEEN = STATE / "boards.txt"
PAGES_STATE = STATE / "pages.json"
NOW = datetime.now(timezone.utc)
DRY = "--dry" in sys.argv
FORCE_ALL = "--all" in sys.argv or not (Path(__file__).resolve().parent / "state" / "seen.txt").exists()

RAW = "https://raw.githubusercontent.com/{}/HEAD/{}"
JSON_TRACKERS = [  # (repo, kind)
    ("SimplifyJobs/New-Grad-Positions", "newgrad"),
    ("SimplifyJobs/Summer2027-Internships", "intern"),
    ("vanshb03/New-Grad-2027", "newgrad"),
    ("vanshb03/Summer2027-Internships", "intern"),
]
README_TRACKERS = [  # (repo/file, kind)
    ("speedyapply/2027-SWE-College-Jobs/README.md", "intern"),
    ("speedyapply/2027-SWE-College-Jobs/INTERN_INTL.md", "intern"),
    ("speedyapply/2027-SWE-College-Jobs/NEW_GRAD_USA.md", "newgrad"),
    ("speedyapply/2027-SWE-College-Jobs/NEW_GRAD_INTL.md", "newgrad"),
    ("negarprh/Canadian-Tech-Internships-2027/README.md", "intern"),
]

# Hand-picked boards (USD-in-Canada audit + general wishlist). Always polled.
PINNED = [
    ("greenhouse", "coinbase"), ("greenhouse", "vercel"), ("greenhouse", "mercury"), ("greenhouse", "tailscale"),
    ("greenhouse", "postscript"), ("greenhouse", "customerio"), ("greenhouse", "hiro"), ("greenhouse", "warp"),
    ("smartrecruiters", "ServiceNow"), ("ashby", "buffer"), ("ashby", "revenuecat"), ("ashby", "supabase"),
    ("ashby", "kraken.com"), ("ashby", "float"), ("ashby", "kit"), ("ashby", "expensify"), ("ashby", "helpscout"),
    ("ashby", "stickermule"), ("ashby", "wealthsimple"), ("ashby", "cohere"), ("ashby", "cerebras"),
    ("lever", "waabi"), ("lever", "shakudo"), ("greenhouse", "stripe"), ("greenhouse", "robinhood"),
    ("greenhouse", "faire"), ("greenhouse", "doordashusa"), ("greenhouse", "lyft"), ("greenhouse", "point72"),
    ("greenhouse", "instacart"), ("ashby", "ramp"), ("ashby", "notion"), ("ashby", "snowflake"),
    ("greenhouse", "databricks"), ("greenhouse", "figma"), ("greenhouse", "airbnb"), ("greenhouse", "pinterest"),
    ("greenhouse", "discord"), ("greenhouse", "anthropic"), ("ashby", "openai"), ("greenhouse", "scaleai"),
    ("greenhouse", "twitch"), ("greenhouse", "reddit"), ("greenhouse", "brex"), ("greenhouse", "plaid"),
    ("greenhouse", "cloudflare"), ("greenhouse", "datadog"), ("greenhouse", "roblox"), ("greenhouse", "dropbox"),
    ("lever", "palantir"), ("greenhouse", "janestreet"), ("greenhouse", "hudsonrivertrading"),
    ("greenhouse", "imc"), ("greenhouse", "optiver"), ("greenhouse", "sig"), ("greenhouse", "citadel"),
]
PAGES = [
    ("37signals", "https://37signals.com/jobs"), ("Fly.io", "https://fly.io/jobs/"),
    ("Oxide", "https://oxide.computer/careers"), ("DuckDuckGo", "https://duckduckgo.com/hiring"),
    ("Automattic", "https://automattic.com/work-with-us/"), ("Cal.com", "https://cal.com/jobs"),
    ("Daily", "https://www.daily.co/company/careers/"), ("Whimsical", "https://whimsical.com/careers"),
]
AMAZON_QUERIES = ["2027", "intern", "early career", "university graduate"]
WORKDAY_QUERIES = ["intern", "graduate", "2027"]
WORKDAY_EVERY_RUN = NOW.minute < 15  # Workday is slow (~2s/board): full sweep once an hour

# Email only for new grad roles at these (when US-located); Canada/remote new grad roles always email.
WATCHLIST = re.compile(r"\b(" + "|".join([
    "google", "alphabet", "meta", "apple", "amazon", "aws", "microsoft", "netflix", "nvidia", "stripe", "ramp",
    "coinbase", "robinhood", "databricks", "snowflake", "figma", "notion", "openai", "anthropic", "palantir",
    "airbnb", "uber", "lyft", "doordash", "instacart", "pinterest", "reddit", "discord", "cloudflare", "datadog",
    "roblox", "dropbox", "shopify", "wealthsimple", "cohere", "cerebras", "mercury", "vercel", "supabase",
    "tailscale", "plaid", "brex", "scale ai", "jane street", "citadel", "hudson river", "two sigma", "de shaw",
    "d\\. e\\. shaw", "optiver", "imc", "jump trading", "sig", "susquehanna", "point72", "bloomberg", "tiktok",
    "bytedance", "spacex", "anduril", "tesla", "salesforce", "linkedin", "adobe", "oracle", "intuit", "atlassian",
    "servicenow", "workday", "snap", "spotify", "faire", "waabi", "37signals", "automattic", "fly\\.io", "oxide",
    "kraken", "revenuecat", "duckduckgo", "zipline", "rippling", "retool", "linear", "vanta", "hebbia",
]) + r")\b", re.I)

ENG = re.compile(r"engineer|developer|programmer|software|swe\b|sde\b|data|machine learning|\bml\b|\bai\b|"
                 r"quant|infrastructure|security|platform|backend|frontend|full.?stack|mobile|ios|android", re.I)
SENIOR = re.compile(r"senior|\bsr\.?\b|staff|principal|lead\b|manager|director|head of|\bii\b|\biii\b|phd", re.I)
INTERN = re.compile(r"\bintern(ship)?s?\b|co-?op|\bpey\b|apprentice|placement", re.I)
NEWGRAD = re.compile(r"new ?grad|graduate|entry.level|early.career|university|campus|junior|"
                     r"(engineer|developer),? (i|1)\b|\b(sde|swe) ?(i|1)\b|associate (software|engineer)|2027|"
                     r"residen(t|cy)|rotational", re.I)
CANADA = re.compile(r"canada|toronto|vancouver|montr[eé]al|ottawa|waterloo|calgary|edmonton|kitchener|"
                    r"mississauga|qu[eé]bec|, (on|bc|qc|ab|ns|mb)\b", re.I)
REMOTE = re.compile(r"remote|anywhere|north america|americas|worldwide|global", re.I)
US = re.compile(r"\b(us|usa|united states)\b|, [A-Z]{2}\b|new york|san francisco|seattle|boston|austin|chicago", re.I)
US_ONLY_REMOTE = re.compile(r"remote.{0,8}\b(us|usa|united states)\b|\bus.only|\(us\)", re.I)


CTX = ssl.create_default_context()  # built once; per-request CA loading dominated CPU


def get(url, data=None, timeout=25):
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": "Mozilla/5.0 (job-watch; personal new-grad alerts)",
        **({"Content-Type": "application/json"} if data else {})})
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        return r.read().decode("utf-8", "replace")


def item(source, company, title, loc, url, kind=None):
    return {"source": source, "company": (company or "").strip(), "title": (title or "").strip(),
            "loc": (loc or "").strip(), "url": url, "kind": kind}


# ---------- sources ----------

def json_tracker(repo, kind):
    out = []
    for j in json.loads(get(RAW.format(repo, ".github/scripts/listings.json"), timeout=90)):
        if j.get("active") and j.get("is_visible", True):
            out.append(item(repo.split("/")[0], j["company_name"], j["title"], "; ".join(j.get("locations", [])),
                            j["url"], kind))
    return out


def readme_tracker(path, kind):
    repo, file = path.rsplit("/", 1)
    out, company = [], ""
    for line in get(RAW.format(repo, file), timeout=60).splitlines():
        if not line.startswith("|") or "---" in line:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        links = [u for u in re.findall(r'\((https?://[^)\s]+)\)|href="(https?://[^"]+)"', line) for u in u if u]
        links = [u for u in links if "shields.io" not in u and "speedyapply.com/api" not in u and "imgur" not in u]
        if len(cells) < 3 or not links:
            continue
        name = re.sub(r"\[([^\]]*)\]\([^)]*\)|<[^>]+>|\*", lambda m: m.group(1) or "", cells[0]).strip()
        company = company if name in ("↳", "") else name
        title = re.sub(r"<[^>]+>|\[|\]\([^)]*\)|\*", "", cells[1])
        out.append(item(repo.split("/")[0], company, title, re.sub(r"<[^>]+>", "; ", cells[2]), links[-1], kind))
    return out


def board(ats, slug, host=None):
    """All postings on one ATS board."""
    out = []
    if ats == "greenhouse":
        for j in json.loads(get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"))["jobs"]:
            out.append(item(f"gh:{slug}", slug, j["title"], j["location"]["name"], j["absolute_url"]))
    elif ats == "ashby":
        d = json.loads(get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}"))
        for j in d["jobs"]:
            locs = [j.get("location") or ""] + [s.get("location", "") for s in j.get("secondaryLocations", [])]
            if j.get("isRemote"):
                locs.append("Remote")
            out.append(item(f"ashby:{slug}", slug, j["title"], "; ".join(filter(None, locs)), j["jobUrl"]))
    elif ats == "lever":
        for j in json.loads(get(f"https://api.lever.co/v0/postings/{slug}?mode=json")):
            c = j.get("categories", {})
            loc = "; ".join(c.get("allLocations") or [c.get("location", "")])
            out.append(item(f"lever:{slug}", slug, j["text"], f"{loc}; {j.get('workplaceType', '')}", j["hostedUrl"]))
    elif ats == "smartrecruiters":
        d = json.loads(get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100"))
        for j in d["content"]:
            l = j.get("location", {})
            loc = f"{l.get('city', '')}, {l.get('region', '')}, {l.get('country', '')}{'; Remote' if l.get('remote') else ''}"
            out.append(item(f"sr:{slug}", j.get("company", {}).get("name", slug), j["name"], loc,
                            f"https://jobs.smartrecruiters.com/{slug}/{j['id']}"))
    elif ats == "workday":
        tenant, site = slug.split("/")
        for q in WORKDAY_QUERIES:
            body = json.dumps({"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": q}).encode()
            d = json.loads(get(f"https://{tenant}.{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs", body))
            for j in d.get("jobPostings", []):
                if "externalPath" in j:
                    out.append(item(f"wd:{tenant}", tenant, j["title"], j.get("locationsText", ""),
                                    f"https://{tenant}.{host}.myworkdayjobs.com/{site}{j['externalPath']}"))
    return out


def amazon():
    out = []
    for q in AMAZON_QUERIES:
        d = json.loads(get("https://www.amazon.jobs/en/search.json?sort=recent&result_limit=100&base_query="
                           + urllib.parse.quote(q)))
        for j in d.get("jobs", []):
            out.append(item("amazon", "Amazon", j["title"], j.get("normalized_location") or j.get("location", ""),
                            "https://www.amazon.jobs" + j["job_path"]))
    return out


def discover(tracker_items):
    """Every ATS board referenced by a tracker listing = a company that hires early-career."""
    boards = set((a, s) for a, s in PINNED)
    wd = {}
    for it in tracker_items:
        u = it["url"]
        for ats, p in (("greenhouse", r"greenhouse\.io/(?!embed)([\w-]+)"), ("ashby", r"jobs\.ashbyhq\.com/([^/?#]+)"),
                       ("lever", r"jobs\.lever\.co/([^/?#]+)"), ("smartrecruiters", r"smartrecruiters\.com/([\w-]+)")):
            m = re.search(p, u)
            if m:
                boards.add((ats, m.group(1)))
        m = re.search(r"https://([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([^/?#]+)", u)
        if m:
            wd[(m.group(1), m.group(3))] = m.group(2)
    out = [(a, s, None) for a, s in boards]
    return out + ([("workday", f"{t}/{s}", h) for (t, s), h in wd.items()] if WORKDAY_EVERY_RUN or FORCE_ALL else [])


def page_lines(url):
    text = html.unescape(re.sub(r"<[^>]+>", "\n", re.sub(r"(?s)<(script|style).*?</\1>", "", get(url))))
    return sorted({l.strip() for l in text.splitlines() if 3 < len(l.strip()) < 120 and ENG.search(l)})


# ---------- classification ----------

def classify(it):
    t, loc = it["title"], it["loc"]
    if SENIOR.search(t) and not INTERN.search(t):
        return None
    kind = "intern" if INTERN.search(t) else "newgrad" if NEWGRAD.search(t) else it["kind"]
    if not kind or (not it["kind"] and not ENG.search(t)):
        return None  # direct ATS boards: only early-career tech titles
    region = ("canada" if CANADA.search(loc) else "remote" if REMOTE.search(loc) and not US_ONLY_REMOTE.search(loc)
              else "us" if US.search(loc) or US_ONLY_REMOTE.search(loc) else "other")
    return kind, region


def norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def company_name(it):
    c = it["company"]
    return c if " " in c or c[:1].isupper() else c.replace("-", " ").title()


def key(it, region):
    return hashlib.sha1(f"{norm(it['company'])[:12]}|{norm(it['title'])}|{region}".encode()).hexdigest()[:12]


def interesting(it, kind, region):
    if kind != "newgrad":
        return False
    return region in ("canada", "remote") or (region == "us" and bool(WATCHLIST.search(company_name(it))))


# ---------- main ----------

def load(path, default):
    return path.read_text().split() if path.exists() else default


def open_issue(rows):
    names = ", ".join(sorted({r["company"] for r in rows}))[:200]
    body = "\n".join(f"- **{r['company']}** — [{r['title']}]({r['url']}) · {r['loc'][:80]} · `{r['region']}`"
                     for r in rows)
    if os.environ.get("GITHUB_ACTIONS"):
        req = urllib.request.Request(
            f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}/issues",
            data=json.dumps({"title": f"{len(rows)} new grad role(s): {names}", "body": body}).encode(),
            headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json"})
        urllib.request.urlopen(req, timeout=30)
    else:
        print("WOULD EMAIL:\n" + body)


def main():
    t0 = time.time()
    seen = set(load(SEEN, []))
    known_boards = set(load(BOARDS_SEEN, []))
    errors, items = [], []

    with ThreadPoolExecutor(8) as ex:
        futs = [ex.submit(json_tracker, r, k) for r, k in JSON_TRACKERS] + \
               [ex.submit(readme_tracker, r, k) for r, k in README_TRACKERS] + [ex.submit(amazon)]
        names = [r for r, _ in JSON_TRACKERS + README_TRACKERS] + ["amazon"]
        for n, f in zip(names, futs):
            try:
                items += [dict(x, tracker=True) for x in f.result()]
            except Exception as e:
                errors.append(f"{n}: {e}")

    boards = discover(items)
    fresh_boards = set()

    def poll(b):
        ats, slug, host = b
        try:
            return b, board(ats, slug, host), None
        except Exception as e:
            return b, [], e

    with ThreadPoolExecutor(48) as ex:
        for (ats, slug, host), got, err in ex.map(poll, boards):
            bid = f"{ats}:{slug}"
            if err is None:
                if bid not in known_boards:
                    fresh_boards.add(bid)  # first sighting: seed silently
                known_boards.add(bid)
                items += [dict(x, board=bid) for x in got]

    # careers pages without an API
    pages = json.loads(PAGES_STATE.read_text()) if PAGES_STATE.exists() else {}
    for company, url in PAGES:
        try:
            lines = page_lines(url)
            old = pages.get(company)
            if old is not None:
                for l in set(lines) - set(old):
                    items.append(item("page", company, l, "", url, "newgrad" if NEWGRAD.search(l) else None))
            pages[company] = lines
        except Exception as e:
            errors.append(f"{company}: {e}")

    first_run = not seen
    new, alerts = [], []
    for it in items:
        c = classify(it)
        if not c:
            continue
        k = key(it, c[1])
        u = it["url"].lower()
        jid = re.search(r"gh_jid=\d+", u)
        uk = hashlib.sha1((re.sub(r"[?#].*$", "", u) + (jid.group() if jid else "")).encode()).hexdigest()[:12]
        if k in seen or uk in seen:  # same role via tracker + direct board counts once
            continue
        seen.update((k, uk))
        if first_run or it.get("board") in fresh_boards:
            continue
        row = {"found_utc": NOW.strftime("%Y-%m-%d %H:%M"), "kind": c[0], "region": c[1],
               "company": company_name(it), "title": it["title"], "loc": it["loc"][:120], "url": it["url"],
               "source": it["source"]}
        new.append(row)
        if interesting(it, *c):
            alerts.append(row)

    if not DRY:
        STATE.mkdir(exist_ok=True)
        SEEN.write_text("\n".join(sorted(seen)) + "\n")
        BOARDS_SEEN.write_text("\n".join(sorted(known_boards)) + "\n")
        PAGES_STATE.write_text(json.dumps(pages, indent=0))
        if new:
            feed = STATE / "feed" / f"{NOW:%Y-%m}.csv"
            feed.parent.mkdir(exist_ok=True)
            header = not feed.exists()
            with feed.open("a", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(new[0]))
                if header:
                    w.writeheader()
                w.writerows(sorted(new, key=lambda r: (r["kind"], r["company"])))
        if alerts:
            open_issue(alerts)

    print(f"{len(boards)} boards, {len(items)} postings scanned, {len(new)} new "
          f"({len(alerts)} alert-worthy), {len(fresh_boards)} boards seeded, {time.time() - t0:.0f}s"
          + (" [first run: seeded]" if first_run else ""))
    for r in new[:40]:
        print(f"  {'*' if r in alerts else ' '} [{r['kind']:<7}] {r['region']:<6} {r['company'][:22]:<22} {r['title'][:70]}")
    if errors:
        print("errors:", *errors, sep="\n  ")


if __name__ == "__main__":
    main()
