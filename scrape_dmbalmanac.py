#!/usr/bin/env python3
"""
DMB Almanac Scraper — v2
Correctly scrapes dmbalmanac.com by:
  1. Fetching the tour list (all tour IDs) from any page's sidebar
  2. For each tour, fetching TourShowInfo.aspx?tid=XXX (table of shows)
  3. Parsing date, venue, city, and setlist URL from each row

Output: dmb_almanac.json  keyed by ISO date (YYYY-MM-DD)

Usage:   python3 scrape_dmbalmanac.py
Deps:    pip3 install requests beautifulsoup4
Runtime: ~20–30 min (150+ tour pages, 1s delay each)
Resumable: re-run after interruption — skips already-saved tours
"""

import re, json, time, sys
from pathlib import Path

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("pip3 install requests beautifulsoup4")
    sys.exit(1)

BASE    = "https://dmbalmanac.com"
DELAY   = 1.0
HEADERS = {"User-Agent": "DMB-Collection-Scraper/2.0 (personal hobby project)"}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# ── Helpers ───────────────────────────────────────────────────────────────────

def fetch(url, retries=3):
    for i in range(retries):
        try:
            r = SESSION.get(url, timeout=20)
            r.raise_for_status()
            return r.text
        except Exception as e:
            if i < retries - 1:
                time.sleep(2 ** i)
            else:
                print(f"  ✗ {url}: {e}")
                return None

def parse_date(raw):
    """'06.05.26' → '2026-06-05'"""
    m = re.match(r"(\d{1,2})\.(\d{2})\.(\d{2,4})$", raw.strip())
    if not m:
        return None
    mo, day, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if yr < 100:
        yr = 2000 + yr if yr < 50 else 1900 + yr
    return f"{yr:04d}-{mo:02d}-{day:02d}"

# ── Step 1: get all tour IDs from the sidebar ────────────────────────────────

def get_all_tours(html):
    """
    Every page's sidebar has a <select> or list of tours like:
      <option value="25">1997 Summer</option>
    We also check for links like TourShowInfo.aspx?tid=25&where=1997
    Returns list of (tid, label) tuples.
    """
    soup = BeautifulSoup(html, "html.parser")
    tours = []
    seen  = set()

    # Method A: <option> tags with numeric values (the tour dropdown)
    for opt in soup.find_all("option"):
        val = opt.get("value", "")
        if re.match(r"^\d+$", val) and int(val) > 0:
            tid   = int(val)
            label = opt.get_text(strip=True)
            if tid not in seen and label:
                seen.add(tid)
                tours.append((tid, label))

    # Method B: links to TourShowInfo.aspx?tid=X
    for a in soup.find_all("a", href=re.compile(r"TourShowInfo\.aspx\?tid=(\d+)", re.I)):
        m = re.search(r"tid=(\d+)", a["href"])
        if m:
            tid = int(m.group(1))
            if tid not in seen:
                seen.add(tid)
                tours.append((tid, a.get_text(strip=True)))

    return tours

# ── Step 2: parse a TourShowInfo page ────────────────────────────────────────

DATE_RE = re.compile(r"\b(\d{2}\.\d{2}\.\d{2,4})\b")

def parse_tour_page(html, tid):
    """
    Parse TourShowInfo.aspx?tid=XXX — returns list of show dicts.
    The page has a table; each data row contains:
      col 0: date link  (TourShowSet.aspx?id=…)
      col 4: venue link (VenueStats.aspx?vid=…)
      col 5: city text
    """
    soup  = BeautifulSoup(html, "html.parser")
    shows = []

    # Find all rows that contain a TourShowSet link
    for a in soup.find_all("a", href=re.compile(r"TourShowSet\.aspx\?id=\d+", re.I)):
        # Date from link text
        link_text = a.get_text(strip=True)
        dates = DATE_RE.findall(link_text)
        show_date = None
        for d in dates:
            show_date = parse_date(d)
            if show_date:
                break
        if not show_date:
            continue

        # Setlist URL
        href = a["href"]
        if not href.startswith("http"):
            href = BASE + "/" + href.lstrip("./")

        # Walk up to the row, then get venue + city from siblings
        row = a.find_parent("tr")
        venue_name = ""
        city       = ""
        if row:
            venue_a = row.find("a", href=re.compile(r"VenueStats", re.I))
            if venue_a:
                venue_name = venue_a.get_text(strip=True)
                # City is usually the next <td> after the venue <td>
                venue_td = venue_a.find_parent("td")
                if venue_td:
                    next_td = venue_td.find_next_sibling("td")
                    if next_td:
                        city = next_td.get_text(strip=True)
            # If city still empty, try the Details cell (last td) which repeats "Venue\nCity"
            if not city:
                tds = row.find_all("td")
                if tds:
                    last = tds[-1].get_text("\n", strip=True)
                    lines = [l.strip() for l in last.split("\n") if l.strip()]
                    if len(lines) >= 2:
                        city = lines[-1]

        shows.append({
            "date":  show_date,
            "venue": venue_name,
            "city":  city,
            "url":   href,
            "tid":   tid,
        })

    return shows

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    script_dir    = Path(__file__).parent
    output_path   = script_dir / "dmb_almanac.json"
    partial_path  = script_dir / "dmb_almanac_partial.json"
    done_tids_path = script_dir / "dmb_almanac_done_tids.json"

    # Load existing data
    all_shows = {}
    done_tids = set()
    if partial_path.exists():
        with open(partial_path) as f:
            all_shows = json.load(f)
        print(f"Resuming — {len(all_shows)} dates already saved")
    if done_tids_path.exists():
        with open(done_tids_path) as f:
            done_tids = set(json.load(f))
        print(f"Already processed {len(done_tids)} tours")

    # Step 1: get tour list from the current page (has all tours in sidebar)
    print("\nFetching tour list…")
    index_html = fetch(f"{BASE}/TourShowInfo.aspx?tid=8188&where=2026")
    if not index_html:
        print("Could not fetch tour index. Check your internet connection.")
        sys.exit(1)

    tours = get_all_tours(index_html)
    # Filter to only DMB-year tours (exclude tid=0 or junk)
    tours = [(tid, label) for tid, label in tours if tid > 0]
    print(f"Found {len(tours)} tours in sidebar\n")

    if not tours:
        print("ERROR: No tours found. The site structure may have changed.")
        sys.exit(1)

    # Step 2: fetch each tour's show list
    for i, (tid, label) in enumerate(tours):
        if tid in done_tids:
            print(f"  [{i+1}/{len(tours)}] {label} (tid={tid}): already done, skipping")
            continue

        url = f"{BASE}/TourShowInfo.aspx?tid={tid}"
        print(f"  [{i+1}/{len(tours)}] {label} (tid={tid}): ", end="", flush=True)

        html = fetch(url)
        if not html:
            print("SKIP")
            time.sleep(DELAY)
            continue

        shows = parse_tour_page(html, tid)
        print(f"{len(shows)} shows")

        for s in shows:
            date = s["date"]
            if date not in all_shows:
                all_shows[date] = []
            entry = {"venue": s["venue"], "city": s["city"], "url": s["url"], "tour": label}
            # Avoid duplicate URLs
            if not any(e["url"] == entry["url"] for e in all_shows[date]):
                all_shows[date].append(entry)

        done_tids.add(tid)

        # Save progress after every tour
        with open(partial_path, "w") as f:
            json.dump(all_shows, f)
        with open(done_tids_path, "w") as f:
            json.dump(list(done_tids), f)

        time.sleep(DELAY)

    # Final output
    with open(output_path, "w") as f:
        json.dump(all_shows, f, indent=2)

    # Clean up temp files
    for p in [partial_path, done_tids_path]:
        if p.exists():
            p.unlink()

    total_dates   = len(all_shows)
    total_entries = sum(len(v) for v in all_shows.values())
    yr_range      = sorted({d[:4] for d in all_shows})

    print(f"\n✅  Done!")
    print(f"    {total_dates} unique show dates")
    print(f"    {total_entries} total show entries")
    if yr_range:
        print(f"    {yr_range[0]}–{yr_range[-1]}")
    print(f"    → {output_path}")
    print(f"\nPlace dmb_almanac.json alongside dmb_collection.html and reload.")

if __name__ == "__main__":
    main()
