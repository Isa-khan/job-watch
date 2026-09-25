#!/usr/bin/env python3
"""Watch company job boards for early-career SWE roles open to Canada.

Run: python3 job_watch.py            -> prints matches, alerts on new ones
     python3 job_watch.py --all      -> prints every current match, not just new
State lives in state/job_watch_state.json; history in state/job_watch_log.csv.
On GitHub Actions, new roles are filed as one GitHub issue per run (which emails you).
"""
import csv, hashlib, html, json, os, re, subprocess, sys, urllib.request
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE / "state" / "job_watch_state.json"
LOG = HERE / "state" / "job_watch_log.csv"

# (company, ats, slug, list) — list: usd = USD-in-Canada audit, general = wishlist
BOARDS = [
    ("Coinbase", "greenhouse", "coinbase", "usd"),
    ("Vercel", "greenhouse", "vercel", "usd"),
    ("Mercury", "greenhouse", "mercury", "usd"),
    ("Tailscale", "greenhouse", "tailscale", "usd"),
    ("Postscript", "greenhouse", "postscript", "usd"),
    ("Customer.io", "greenhouse", "customerio", "usd"),
    ("Hiro Systems", "greenhouse", "hiro", "usd"),
    ("Warp", "greenhouse", "warp", "usd"),
    ("ServiceNow", "smartrecruiters", "ServiceNow", "usd"),
    ("Buffer", "ashby", "buffer", "usd"),
    ("RevenueCat", "ashby", "revenuecat", "usd"),
    ("Supabase", "ashby", "supabase", "usd"),
    ("Kraken", "ashby", "kraken.com", "usd"),
    ("Float", "ashby", "float", "usd"),
    ("Kit", "ashby", "kit", "usd"),
    ("Expensify", "ashby", "expensify", "usd"),
    ("Help Scout", "ashby", "helpscout", "usd"),
    ("Sticker Mule", "ashby", "stickermule", "usd"),
    ("Wealthsimple", "ashby", "wealthsimple", "general"),
    ("Cohere", "ashby", "cohere", "general"),
    ("Cerebras Systems", "ashby", "cerebras", "general"),
    ("Waabi", "lever", "waabi", "general"),
    ("Shakudo", "lever", "shakudo", "general"),
    ("Stripe", "greenhouse", "stripe", "general"),
    ("Robinhood", "greenhouse", "robinhood", "general"),
    ("Faire", "greenhouse", "faire", "general"),
    ("DoorDash", "greenhouse", "doordashusa", "general"),
    ("Lyft", "greenhouse", "lyft", "general"),
    ("Point72", "greenhouse", "point72", "general"),
    ("Instacart", "greenhouse", "instacart", "general"),
]

# Self-hosted careers pages with no JSON API: alert when engineering lines change.
PAGES = [
    ("37signals", "https://37signals.com/jobs"),
    ("Fly.io", "https://fly.io/jobs/"),
    ("Oxide Computer Company", "https://oxide.computer/careers"),
    ("DuckDuckGo", "https://duckduckgo.com/hiring"),
    ("Automattic", "https://automattic.com/work-with-us/"),
    ("Cal.com", "https://cal.com/jobs"),
    ("Daily", "https://www.daily.co/company/careers/"),
    ("Whimsical", "https://whimsical.com/careers"),
]

SIMPLIFY = "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json"

EARLY = re.compile(r"new ?grad|graduate|entry|junior|early.career|university|campus|"
                   r"associate (software|engineer|developer)|engineer,? i\b|engineer 1\b|"
                   r"developer i\b|\bl1\b|\bic1\b|2027|apprentice|residen", re.I)
INTERN = re.compile(r"\bintern(ship)?\b|co-?op", re.I)
ENG = re.compile(r"engineer|developer|programmer|software|swe\b|sde\b", re.I)
CANADA = re.compile(r"canada|toronto|vancouver|montr[eé]al|ottawa|waterloo|calgary|"
                    r"\bon\b|\bbc\b|remote|anywhere|americas|north america|worldwide|global", re.I)
US_ONLY = re.compile(r"remote.{0,6}(us|usa|united states)\b|\(us\)|us.only", re.I)


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 job-watch"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def fetch(ats, slug):
    """Yield (id, title, location, url) for every posting on a board."""
    if ats == "greenhouse":
        for j in json.loads(get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"))["jobs"]:
            yield str(j["id"]), j["title"], j["location"]["name"], j["absolute_url"]
    elif ats == "ashby":
        for j in json.loads(get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}"))["jobs"]:
            locs = [j.get("location") or ""] + [s.get("location", "") for s in j.get("secondaryLocations", [])]
            if j.get("isRemote"):
                locs.append("Remote")
            yield j["id"], j["title"], "; ".join(filter(None, locs)), j["jobUrl"]
    elif ats == "lever":
        for j in json.loads(get(f"https://api.lever.co/v0/postings/{slug}?mode=json")):
            c = j.get("categories", {})
            loc = "; ".join(c.get("allLocations") or [c.get("location", "")])
            yield j["id"], j["text"], f"{loc}; {j.get('workplaceType', '')}", j["hostedUrl"]
    elif ats == "smartrecruiters":
        off = 0
        while True:
            d = json.loads(get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100&offset={off}"))
            for j in d["content"]:
                l = j.get("location", {})
                loc = f"{l.get('city', '')}, {l.get('country', '')}{'; Remote' if l.get('remote') else ''}"
                yield j["id"], j["name"], loc, f"https://jobs.smartrecruiters.com/{slug}/{j['id']}"
            off += 100
            if off >= d["totalFound"]:
                break


def classify(title, loc):
    if not ENG.search(title) or re.search(r"senior|staff|principal|lead\b|manager", title, re.I):
        return None
    kind = "intern" if INTERN.search(title) else "early" if EARLY.search(title) else None
    if not kind:
        return None
    canada = "yes" if re.search(r"canada|toronto|vancouver|montr|ottawa|waterloo|calgary", loc, re.I) else \
             "no" if (US_ONLY.search(loc) or not CANADA.search(loc)) else "maybe"
    return kind, canada


def page_lines(url):
    text = html.unescape(re.sub(r"<[^>]+>", "\n", re.sub(r"(?s)<(script|style).*?</\1>", "", get(url))))
    return sorted({l.strip() for l in text.splitlines() if 3 < len(l.strip()) < 120 and ENG.search(l)})


def notify(rows):
    names = ", ".join(sorted({r[1] for r in rows}))
    if os.environ.get("GITHUB_ACTIONS"):
        body = "\n".join(f"- **{r[1]}** — [{r[5]}]({r[7]}) · {r[6]} · canada={r[4]} · {r[3]}" for r in rows)
        req = urllib.request.Request(
            f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}/issues",
            data=json.dumps({"title": f"{len(rows)} new role(s): {names}"[:250], "body": body}).encode(),
            headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                     "Accept": "application/vnd.github+json"})
        urllib.request.urlopen(req, timeout=30)
    else:
        msg = f"{len(rows)} new: {names}"
        subprocess.run(["osascript", "-e", f'display notification {json.dumps(msg)} with title "New grad job watch"'],
                       check=False)


def main():
    show_all = "--all" in sys.argv
    state = json.loads(STATE.read_text()) if STATE.exists() else {"seen": {}, "pages": {}}
    first_run = not state["seen"]
    today, new, errors, current = date.today().isoformat(), [], [], []

    for company, ats, slug, lst in BOARDS:
        try:
            for jid, title, loc, url in fetch(ats, slug):
                c = classify(title, loc)
                if not c:
                    continue
                key = f"{company}:{jid}"
                row = [today, company, lst, c[0], c[1], title, loc, url]
                current.append(row)
                if key not in state["seen"]:
                    state["seen"][key] = today
                    new.append(row)
        except Exception as e:
            errors.append(f"{company}: {e}")

    for company, url in PAGES:
        try:
            lines = page_lines(url)
            h = hashlib.sha1("\n".join(lines).encode()).hexdigest()
            old = state["pages"].get(company)
            if old and old["hash"] != h:
                added = [l for l in lines if l not in old["lines"]]
                if added:
                    new.append([today, company, "usd", "page-change", "?", " | ".join(added[:5]), "", url])
            state["pages"][company] = {"hash": h, "lines": lines}
        except Exception as e:
            errors.append(f"{company}: {e}")

    # Community new-grad tracker: every company, not just ours. Seed silently on first run.
    try:
        seeded = state.get("simplify_seeded", False)
        for j in json.loads(get(SIMPLIFY)):
            locs = "; ".join(j.get("locations", []))
            if not (j.get("active") and j.get("is_visible", True)) or not CANADA.search(locs) or US_ONLY.search(locs):
                continue
            key = f"simplify:{j['id']}"
            if key in state["seen"]:
                continue
            state["seen"][key] = today
            if seeded:
                canada = "yes" if re.search(r"canada|, on\b|, bc\b|, qc\b|, ab\b", locs, re.I) else "maybe"
                new.append([today, j["company_name"], "tracker", "early", canada, j["title"], locs, j["url"]])
        state["simplify_seeded"] = True
    except Exception as e:
        errors.append(f"Simplify tracker: {e}")

    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(state, indent=1))
    if new:
        write_header = not LOG.exists()
        with LOG.open("a", newline="") as f:
            w = csv.writer(f)
            if write_header:
                w.writerow(["found", "company", "list", "kind", "canada", "title", "location", "url"])
            w.writerows(new)

    rows = current if (show_all or first_run) else new
    label = "current matches" if (show_all or first_run) else "NEW since last run"
    print(f"{len(rows)} {label} ({today})")
    for r in sorted(rows, key=lambda r: (r[4] == "no", r[3] != "early", r[1])):
        print(f"  [{r[3]:<11}] canada={r[4]:<5} {r[1]:<16} {r[5]}  —  {r[6]}\n      {r[7]}")
    if errors:
        print("errors:", *errors, sep="\n  ")
    if new and not first_run:
        relevant = [r for r in new if r[4] != "no"]
        if relevant:
            notify(relevant)


if __name__ == "__main__":
    main()
