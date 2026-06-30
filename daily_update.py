#!/usr/bin/env python3
"""
DMB Daily Update
Scans the current-year recording folder and scrapes the current-year tours
from dmbalmanac.com, then patches dmb_collection.html in place with any
new entries and updated dashboard stats.

Usage:   python3 daily_update.py
Deps:    pip3 install requests beautifulsoup4 mutagen
"""

import re, json, time, sys, os, subprocess
from pathlib import Path
from datetime import datetime
from calendar import month_name

# ── Config ───────────────────────────────────────────────────────────────────
COLLECTION_ROOT = Path("/Volumes/DMB Archive/DMB Fan and Broadcast Library")
DASHBOARD_DIR   = Path("/Volumes/DMB Archive/Dashboard Page")
HTML_FILE       = DASHBOARD_DIR / "dmb_collection.html"

# Automatically advances to the new year on January 1
CURRENT_YEAR    = str(datetime.now().year)

BASE    = "https://dmbalmanac.com"
DELAY   = 1.0
HEADERS = {"User-Agent": "DMB-Collection-Scanner/2.0 (personal hobby project)"}

# ── Plex config ───────────────────────────────────────────────────────────────
# Token is read from ~/.dmb_plex_token — never hardcoded here.
_PLEX_TOKEN_FILE = Path.home() / ".dmb_plex_token"
PLEX_TOKEN       = _PLEX_TOKEN_FILE.read_text().strip() if _PLEX_TOKEN_FILE.exists() else ""
PLEX_URL         = "http://localhost:32400"
PLEX_LIBRARY_ID  = "2"
PLEX_MACHINE_ID  = "341172425e0e6f0ff15189aa5ab8d1e0a2acd625"

# ── Attended shows ────────────────────────────────────────────────────────────
MEMBER_SHOWS_URL = "https://dmbalmanac.com/myalmanac/MyShows.aspx?number=1551"

# ── Optional deps ─────────────────────────────────────────────────────────────
try:
    import requests
    from bs4 import BeautifulSoup
    HAS_NET = True
except ImportError:
    print("⚠  requests/beautifulsoup4 not found — almanac scrape will be skipped.")
    print("   Install with: pip3 install requests beautifulsoup4\n")
    HAS_NET = False

try:
    from mutagen import File as MutagenFile
    HAS_MUTAGEN = True
except ImportError:
    HAS_MUTAGEN = False

AUDIO_EXTENSIONS = {'.flac', '.shn', '.mp3', '.mp2', '.wav', '.ape', '.ogg'}
TEXT_EXTENSIONS  = {'.txt', '.nfo', '.md', '.log'}

# ── Folder-name parser (mirrors scan_dmb_collection.py) ──────────────────────
DATE_PATTERNS = [
    re.compile(
        r'^(?:DMB|dmb|dm)[- _]?'
        r'(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})'
        r'(?:\.(?P<rest>.+))?$', re.IGNORECASE
    ),
    re.compile(
        r'^DMB-(?P<month>\d{2})\.(?P<day>\d{2})\.(?P<year2>\d{2})'
        r'(?:-(?P<rest>.+))?$', re.IGNORECASE
    ),
]
FORMAT_TOKENS = {'flac16','flac24','flac','shnf','shn','mp3','mp2','wav','ape'}
SOURCE_HINTS  = {
    'aud':'Audience','daud':'Digital Audience','sbd':'Soundboard',
    'dsbd':'Digital Soundboard','matrix':'Matrix','fm':'FM Broadcast',
    'siriusxm':'SiriusXM','xm':'SiriusXM','web':'Web/Stream Download',
    'webdl':'Web/Stream Download',
}

def _parse_rest(rest, result):
    parts = rest.replace('-','.').split('.')
    source_parts, tag_parts, venue_parts = [], [], []
    for part in parts:
        pl = part.lower().strip()
        if pl in ('flac16','flac24'):
            result['format'] = 'FLAC'
            result['bit_depth'] = 16 if '16' in pl else 24
            continue
        if pl in FORMAT_TOKENS:
            result['format'] = pl.upper().replace('SHNF','SHN')
            continue
        if pl in ('16','24'):
            result['bit_depth'] = int(pl)
            continue
        matched = False
        for key, label in SOURCE_HINTS.items():
            if pl == key or pl.startswith(key):
                result['source_type'] = label
                source_parts.append(part)
                matched = True
                break
        if matched:
            continue
        if pl in ('virgin','letterman','trl','reconvert','de-edited','remaster','remastered','webdl','web-dl'):
            tag_parts.append(part)
            continue
        if ' ' in part or ',' in part or (len(part) > 3 and part[0].isupper()):
            venue_parts.append(part)
            continue
        source_parts.append(part)
    if source_parts: result['source'] = '.'.join(source_parts)
    if tag_parts:    result['tags']   = tag_parts
    if venue_parts:  result['venue_hint'] = ' '.join(venue_parts)

def parse_folder_name(name):
    result = {'date':None,'year':None,'month':None,'day':None,
              'source':None,'source_type':None,'format':None,'bit_depth':None,
              'venue_hint':None,'tags':[],'raw_name':name}
    m = DATE_PATTERNS[0].match(name)
    if m:
        result['year']  = int(m.group('year'))
        result['month'] = int(m.group('month'))
        result['day']   = int(m.group('day'))
        result['date']  = f"{m.group('year')}-{m.group('month')}-{m.group('day')}"
        _parse_rest(m.group('rest') or '', result)
        return result
    m = DATE_PATTERNS[1].match(name)
    if m:
        yy = int(m.group('year2'))
        result['year']  = 2000 + yy if yy < 50 else 1900 + yy
        result['month'] = int(m.group('month'))
        result['day']   = int(m.group('day'))
        result['date']  = f"{result['year']}-{result['month']:02d}-{result['day']:02d}"
        _parse_rest(m.group('rest') or '', result)
        return result
    return result

def inspect_folder(folder_path):
    audio_files, text_files = [], []
    total_bytes = 0
    for entry in os.scandir(folder_path):
        if entry.is_file():
            ext = Path(entry.name).suffix.lower()
            sz  = entry.stat().st_size
            total_bytes += sz
            if ext in AUDIO_EXTENSIONS: audio_files.append(entry.path)
            elif ext in TEXT_EXTENSIONS: text_files.append(entry.path)
    duration_sec = None
    if HAS_MUTAGEN and audio_files:
        duration_sec = 0
        for af in audio_files:
            try:
                a = MutagenFile(af)
                if a and a.info: duration_sec += a.info.length
            except Exception: pass
    notes = None
    if text_files:
        try:
            with open(text_files[0], 'r', errors='replace') as f:
                notes = f.read(500).strip().replace('\n',' | ')
        except Exception: pass
    return {
        'audio_file_count': len(audio_files),
        'total_size_mb': round(total_bytes / (1024*1024), 1),
        'duration_minutes': round(duration_sec/60, 1) if duration_sec else 0,
        'has_notes': len(text_files) > 0,
        'notes_snippet': notes or '',
    }

# ── Part 1: Extract data from existing HTML ───────────────────────────────────

def load_html():
    if not HTML_FILE.exists():
        print(f"ERROR: HTML file not found: {HTML_FILE}")
        sys.exit(1)
    return HTML_FILE.read_text(encoding='utf-8')

def extract_shows(html):
    """Slice out the SHOWS array by string position (avoids regex on 1.5 MB)."""
    marker = 'const SHOWS='
    start  = html.find(marker) + len(marker)
    end    = html.find(';\nconst ALMANAC=', start)
    return json.loads(html[start:end])

def extract_almanac(html):
    """Slice out the ALMANAC object by string position."""
    marker = 'const ALMANAC='
    start  = html.find(marker) + len(marker)
    end = html.find(';\nconst PLEX_MAP=', start)
    if end == -1:
        end = html.find(';\n\nconst dateCounts=', start)
    return json.loads(html[start:end])

# ── Part 2: Scan CURRENT_YEAR recording folder ────────────────────────────────

def scan_year(year_str, existing_folders):
    """Return new SHOWS-format entries for recording folders not yet in the HTML."""
    year_dir = COLLECTION_ROOT / year_str
    if not year_dir.exists():
        print(f"  ⚠  Year folder not found: {year_dir}")
        return []

    new_entries = []
    for show_dir in sorted(year_dir.iterdir()):
        if not show_dir.is_dir() or show_dir.name in existing_folders:
            continue
        parsed    = parse_folder_name(show_dir.name)
        file_info = inspect_folder(show_dir)
        entry = [
            parsed['date']  or '',
            str(parsed['year'] or year_str),
            parsed['format'] or '',
            str(parsed['bit_depth'] or ''),
            parsed['source_type'] or '',
            parsed['source'] or '',
            file_info['audio_file_count'],
            file_info['total_size_mb'],
            int(file_info['duration_minutes'] or 0),
            1 if file_info['has_notes'] else 0,
            file_info['notes_snippet'],
            show_dir.name,
            0,
            ', '.join(parsed['tags']) if parsed['tags'] else '',
        ]
        new_entries.append(entry)
        print(f"  + {show_dir.name}")
    return new_entries

# ── Part 3: Scrape CURRENT_YEAR from dmbalmanac.com ──────────────────────────

SESSION = None

def get_session():
    global SESSION
    if SESSION is None:
        SESSION = requests.Session()
        SESSION.headers.update(HEADERS)
    return SESSION

def fetch(url, retries=3):
    s = get_session()
    for i in range(retries):
        try:
            r = s.get(url, timeout=20)
            r.raise_for_status()
            return r.text
        except Exception as e:
            if i < retries - 1:
                time.sleep(2 ** i)
            else:
                print(f"  ✗ {url}: {e}")
    return None

def parse_date_str(raw):
    m = re.match(r"(\d{1,2})\.(\d{2})\.(\d{2,4})$", raw.strip())
    if not m: return None
    mo, day, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if yr < 100: yr = 2000 + yr if yr < 50 else 1900 + yr
    return f"{yr:04d}-{mo:02d}-{day:02d}"

DATE_RE = re.compile(r"\b(\d{2}\.\d{2}\.\d{2,4})\b")

def get_year_tours(year_str):
    """Fetch the tour list sidebar and return only tours labelled with year_str."""
    html = fetch(f"{BASE}/TourShowInfo.aspx?tid=8188&where={year_str}")
    if not html: return []
    soup  = BeautifulSoup(html, "html.parser")
    tours = []
    seen  = set()
    for opt in soup.find_all("option"):
        val = opt.get("value","")
        if re.match(r"^\d+$", val) and int(val) > 0:
            tid   = int(val)
            label = opt.get_text(strip=True)
            if tid not in seen and year_str in label:
                seen.add(tid)
                tours.append((tid, label))
    for a in soup.find_all("a", href=re.compile(r"TourShowInfo\.aspx\?tid=(\d+)", re.I)):
        m = re.search(r"tid=(\d+)", a["href"])
        if m:
            tid   = int(m.group(1))
            label = a.get_text(strip=True)
            if tid not in seen and year_str in label:
                seen.add(tid)
                tours.append((tid, label))
    return tours

def parse_tour_page(html, tid, label):
    soup  = BeautifulSoup(html, "html.parser")
    shows = {}
    for a in soup.find_all("a", href=re.compile(r"TourShowSet\.aspx\?id=\d+", re.I)):
        text      = a.get_text(strip=True)
        show_date = None
        for d in DATE_RE.findall(text):
            show_date = parse_date_str(d)
            if show_date: break
        if not show_date: continue
        href = a["href"]
        if not href.startswith("http"): href = BASE + "/" + href.lstrip("./")
        row        = a.find_parent("tr")
        venue_name = ""
        city       = ""
        if row:
            va = row.find("a", href=re.compile(r"VenueStats", re.I))
            if va:
                venue_name = va.get_text(strip=True)
                vtd = va.find_parent("td")
                if vtd:
                    ntd = vtd.find_next_sibling("td")
                    if ntd: city = ntd.get_text(strip=True)
            if not city:
                tds = row.find_all("td")
                if tds:
                    lines = [l.strip() for l in tds[-1].get_text("\n",strip=True).split("\n") if l.strip()]
                    if len(lines) >= 2: city = lines[-1]
        if show_date not in shows:
            shows[show_date] = {"venue":venue_name,"city":city,"url":href,"tour":label}
    return shows

def scrape_year_almanac(year_str):
    """Return {date: {venue,city,url,tour}} for all shows in year_str."""
    if not HAS_NET:
        return {}
    print(f"  Fetching {year_str} tour list from dmbalmanac.com…")
    tours = get_year_tours(year_str)
    if not tours:
        print(f"  ⚠  No {year_str} tours found in sidebar.")
        return {}
    print(f"  Found {len(tours)} {year_str} tour(s): {[t[1] for t in tours]}")

    all_shows = {}
    for i, (tid, label) in enumerate(tours):
        url = f"{BASE}/TourShowInfo.aspx?tid={tid}"
        print(f"  [{i+1}/{len(tours)}] {label}… ", end="", flush=True)
        html = fetch(url)
        if not html:
            print("SKIP")
            time.sleep(DELAY)
            continue
        shows = parse_tour_page(html, tid, label)
        print(f"{len(shows)} shows")
        all_shows.update(shows)
        time.sleep(DELAY)
    return all_shows

# ── Part 4: Recalculate dashboard stats ──────────────────────────────────────

def calc_stats(shows, almanac):
    coll_dates = {s[0] for s in shows if s[0]}
    alm_dates  = set(almanac.keys())
    covered    = len(coll_dates & alm_dates)
    total_alm  = len(alm_dates)
    total_mb   = sum(s[7] for s in shows)
    total_tb   = total_mb / 1024 / 1024

    # Most recent dated show
    recent_date = max((s[0] for s in shows if s[0] and len(s[0]) == 10), default='')
    if recent_date:
        y, mo, d = recent_date.split('-')
        recent_label = f"{month_name[int(mo)]} {int(d)}, {y}"
    else:
        recent_label = ''

    # Year chart — count shows per 4-digit year only (skip special folders)
    year_counts = {}
    for s in shows:
        yr = s[1]
        if yr and re.match(r'^\d{4}$', yr):
            year_counts[yr] = year_counts.get(yr, 0) + 1
    year_labels  = sorted(year_counts.keys())
    year_vals    = [year_counts[y] for y in year_labels]
    year_short   = [y[-2:] for y in year_labels]

    # Multiple-recordings-per-date buckets
    from collections import Counter
    date_counts = Counter(s[0] for s in shows if s[0])
    bucket1 = sum(1 for c in date_counts.values() if c == 1)
    bucket2 = sum(1 for c in date_counts.values() if 2 <= c <= 3)
    bucket3 = sum(1 for c in date_counts.values() if 4 <= c <= 6)
    bucket4 = sum(1 for c in date_counts.values() if c >= 7)
    b_max   = max(bucket1, bucket2, bucket3, bucket4, 1)

    return {
        'total_recordings': len(shows),
        'unique_dates':     len(coll_dates),
        'total_tb':         total_tb,
        'covered':          covered,
        'total_alm':        total_alm,
        'coverage_pct':     round(covered/total_alm*100, 1) if total_alm else 0,
        'recent_label':     recent_label,
        'year_labels':      year_labels,
        'year_vals':        year_vals,
        'year_short':       year_short,
        'bucket1': bucket1, 'bucket2': bucket2,
        'bucket3': bucket3, 'bucket4': bucket4,
        'b_max':   b_max,
    }

# ── Part 5: Patch HTML ────────────────────────────────────────────────────────

def _tb_label(tb):
    return f"{tb:.1f} TB" if tb >= 1 else f"{round(tb*1024)} GB"

def patch_html(html, shows, almanac, st):
    # 1) SHOWS — slice replacement
    shows_marker = 'const SHOWS='
    s_start = html.find(shows_marker) + len(shows_marker)
    s_end   = html.find(';\nconst ALMANAC=', s_start)
    shows_json = json.dumps(shows, ensure_ascii=False, separators=(',',':'))
    html = html[:s_start] + shows_json + html[s_end:]

    # 2) ALMANAC — slice replacement (recalculate positions after SHOWS replacement)
    alm_marker = 'const ALMANAC='
    a_start = html.find(alm_marker) + len(alm_marker)
    a_end = html.find(';\nconst PLEX_MAP=', a_start)
    if a_end == -1:
        a_end = html.find(';\n\nconst dateCounts=', a_start)
    alm_json = json.dumps(almanac, ensure_ascii=False, separators=(',',':'))
    html = html[:a_start] + alm_json + html[a_end:]

    # 3) Metric HTML values — replace using surrounding context
    #    Recordings count
    html = re.sub(
        r'(<div class="metric-label">Recordings in collection</div><div class="metric-value">)[^<]*(</div>)',
        lambda m: m.group(1) + f"{st['total_recordings']:,}" + m.group(2),
        html
    )
    #    Unique show dates
    html = re.sub(
        r'(<div class="metric-label">Unique show dates</div><div class="metric-value">)[^<]*(</div>)',
        lambda m: m.group(1) + f"{st['unique_dates']:,}" + m.group(2),
        html
    )
    #    Collection size
    html = re.sub(
        r'(<div class="metric-label">Collection size</div><div class="metric-value">)[^<]*(</div>)',
        lambda m: m.group(1) + _tb_label(st['total_tb']) + m.group(2),
        html
    )
    #    Catalog coverage
    html = re.sub(
        r'(<div class="metric-label">Catalog coverage</div><div class="metric-value">)[^<]*(</div>)',
        lambda m: m.group(1) + f"{st['coverage_pct']}%" + m.group(2),
        html
    )
    #    Coverage sub-text
    html = re.sub(
        r'(<div class="metric-label">Catalog coverage</div>.*?<div class="metric-sub">)[^<]*(</div>)',
        lambda m: m.group(1) + f"{st['covered']:,} of {st['total_alm']:,} known shows" + m.group(2),
        html, flags=re.DOTALL
    )
    #    Most recent recording
    html = re.sub(
        r'(<div class="metric-label">Most recent recording</div><div class="metric-value"[^>]*>)[^<]*(</div>)',
        lambda m: m.group(1) + st['recent_label'] + m.group(2),
        html
    )
    #    Unique dates sub-text range
    if st['year_labels']:
        yr_range = f"{st['year_labels'][0]} – {st['year_labels'][-1]}"
        html = re.sub(
            r'(<div class="metric-label">Unique show dates</div>.*?<div class="metric-sub">)[^<]*(</div>)',
            lambda m: m.group(1) + yr_range + m.group(2),
            html, flags=re.DOTALL
        )

    # 4) Year chart arrays
    html = re.sub(
        r'const yearFullLabels=\[.*?\]',
        f"const yearFullLabels={json.dumps(st['year_labels'])}",
        html, flags=re.DOTALL
    )
    html = re.sub(
        r'const yearShortLabels=\[.*?\]',
        f"const yearShortLabels={json.dumps(st['year_short'])}",
        html, flags=re.DOTALL
    )
    html = re.sub(
        r'const yearVals=\[[^\]]*\]',
        f"const yearVals={json.dumps(st['year_vals'])}",
        html
    )

    # 5) Multiple-recordings-per-date bar widths and values
    b_max = st['b_max']
    def bar_row(label, count, color):
        pct = round(count / b_max * 100)
        return (
            f'<div class="bar-row"><span class="bar-label">{label}</span>'
            f'<div class="bar-track"><div class="bar-fill" style="width:{pct}%;background:{color}"></div></div>'
            f'<span class="bar-val">{count}</span></div>'
        )
    new_bars = (
        bar_row('1 recording',      st['bucket1'], '#185FA5') +
        bar_row('2–3 recordings',   st['bucket2'], '#1D9E75') +
        bar_row('4–6 recordings',   st['bucket3'], '#BA7517') +
        bar_row('7+ recordings',    st['bucket4'], '#A32D2D')
    )
    html = re.sub(
        r'(<div class="bar-row">.*?</span></div>\s*){4}',
        new_bars,
        html, count=1, flags=re.DOTALL
    )

    return html

# ── Attended shows ────────────────────────────────────────────────────────────

def scrape_attended():
    """Return sorted list of YYYY-MM-DD dates James attended (member #1551)."""
    if not HAS_NET:
        return []
    try:
        resp = requests.get(MEMBER_SHOWS_URL, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"⚠  Could not fetch attended shows: {e}")
        return []
    # Dates appear as MM.DD.YY in the page
    seen, result = set(), []
    for mm, dd, yy in re.findall(r'(\d{2})\.(\d{2})\.(\d{2})', resp.text):
        yr = int(yy)
        year = 2000 + yr if yr <= 30 else 1900 + yr
        date_str = f"{year}-{mm}-{dd}"
        if date_str not in seen:
            seen.add(date_str)
            result.append(date_str)
    result.sort()
    print(f"  → Attended: {len(result)} show date(s) scraped")
    return result


# ── Plex map ─────────────────────────────────────────────────────────────────

def get_plex_map():
    """Return {folder_name: plex_web_url} for all albums in Music library.
    Returns {} silently if Plex is unavailable or token not configured."""
    if not PLEX_TOKEN:
        print("⚠  Plex token not found (~/.dmb_plex_token) — skipping Plex map.")
        return {}
    if not HAS_NET:
        return {}
    url = f"{PLEX_URL}/library/sections/{PLEX_LIBRARY_ID}/all?type=9"
    try:
        resp = requests.get(url, headers={"X-Plex-Token": PLEX_TOKEN}, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        print(f"⚠  Plex unreachable: {e}")
        return {}
    plex_map = {}
    try:
        data = resp.json()
        for album in data.get("MediaContainer", {}).get("Metadata", []):
            title      = album.get("title", "")
            rating_key = album.get("ratingKey", "")
            if title and rating_key:
                from urllib.parse import quote
                key_path = f"/library/metadata/{rating_key}"
                web_url  = (
                    f"https://app.plex.tv/desktop/#!/server/{PLEX_MACHINE_ID}"
                    f"/details?key={quote(key_path, safe='')}"
                )
                plex_map[title] = web_url
    except Exception as e:
        print(f"⚠  Plex JSON parse error: {e}")
        return {}
    print(f"  → Plex: {len(plex_map):,} albums indexed")
    return plex_map

# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print(f"🎵 DMB Daily Update — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"   Collection:  {COLLECTION_ROOT}")
    print(f"   Dashboard:   {HTML_FILE}")
    print(f"   Target year: {CURRENT_YEAR}\n")

    # Load existing data
    html    = load_html()
    shows   = extract_shows(html)
    almanac = extract_almanac(html)
    print(f"Loaded: {len(shows)} recordings, {len(almanac)} almanac dates\n")

    # ── Scan new local recordings (all years) ──
    print(f"📂 Scanning all year folders…")
    existing_folders = {s[11] for s in shows}
    new_shows = []
    if COLLECTION_ROOT.exists():
        year_dirs = sorted([d for d in COLLECTION_ROOT.iterdir() if d.is_dir()])
        for year_dir in year_dirs:
            year_new = scan_year(year_dir.name, existing_folders)
            if year_new:
                print(f"  {year_dir.name}: {len(year_new)} new")
                new_shows += year_new
    if new_shows:
        shows += new_shows
        shows.sort(key=lambda s: (s[0], s[11]))
        print(f"  → {len(new_shows)} new recording(s) added total")
    else:
        print("  → No new recordings found")

    # ── Scrape new almanac entries ──
    print(f"\n🌐 Scraping {CURRENT_YEAR} from dmbalmanac.com…")
    new_alm = scrape_year_almanac(CURRENT_YEAR)
    new_alm_count = 0
    for date, info in new_alm.items():
        if date not in almanac:
            almanac[date] = info
            new_alm_count += 1
        else:
            # Refresh existing entry in case venue/city was corrected
            almanac[date] = info
    if new_alm_count:
        print(f"  → {new_alm_count} new show date(s) added to almanac")
    else:
        print(f"  → Almanac up to date (refreshed {len(new_alm)} {CURRENT_YEAR} entries)")

    # ── Recalculate stats ──
    stats = calc_stats(shows, almanac)
    print(f"\n📊 Stats:")
    print(f"   Recordings:  {stats['total_recordings']:,}")
    print(f"   Unique dates: {stats['unique_dates']:,}")
    print(f"   Size:         {_tb_label(stats['total_tb'])}")
    print(f"   Coverage:     {stats['coverage_pct']}% ({stats['covered']:,} of {stats['total_alm']:,})")
    print(f"   Most recent:  {stats['recent_label']}")

    # ── Patch and write HTML ──
    run_ts = datetime.now().strftime('%Y-%m-%d %H:%M')
    data_changed = bool(new_shows or new_alm_count)

    # Always patch stats/charts so year chart stays current even on quiet days
    print(f"\n✍  Patching {HTML_FILE.name}…")
    updated_html = patch_html(html, shows, almanac, stats)

    # Always update the "last run" timestamp in the footer
    if 'last run' in updated_html:
        updated_html = re.sub(
            r'last run \d{4}-\d{2}-\d{2} \d{2}:\d{2}',
            f'last run {run_ts}',
            updated_html, count=1
        )
    else:
        updated_html = updated_html.replace(
            'DMB Collection Dashboard',
            f'DMB Collection Dashboard &nbsp;·&nbsp; last run {run_ts}',
            1
        )

    # ── Inject live Plex map (slice replacement so it refreshes every run) ──
    print(f"\n🎧 Querying Plex…")
    plex_map = get_plex_map()
    pm_marker = 'const PLEX_MAP='
    pm_start = updated_html.find(pm_marker) + len(pm_marker)
    pm_end   = updated_html.find(';\nconst ATTENDED_DATES=', pm_start)
    if pm_end == -1:
        pm_end = updated_html.find(';\n\nconst dateCounts=', pm_start)
    updated_html = updated_html[:pm_start] + json.dumps(plex_map, ensure_ascii=False) + updated_html[pm_end:]

    # ── Inject attended dates (slice replacement) ──────────────────────────────
    print(f"\n🎟  Scraping attended shows…")
    attended = scrape_attended()
    ad_marker = 'const ATTENDED_DATES=new Set('
    ad_start  = updated_html.find(ad_marker) + len(ad_marker)
    ad_end    = updated_html.find(');\n\nconst dateCounts=', ad_start)
    if ad_end != -1:
        updated_html = updated_html[:ad_start] + json.dumps(attended) + updated_html[ad_end:]
    else:
        print("⚠  ATTENDED_DATES end-marker not found — skipping injection")

    HTML_FILE.write_text(updated_html, encoding='utf-8')

    if new_shows:
        print(f"✅ Done — {len(new_shows)} new recording(s), reload to see changes.")
    else:
        print(f"\n✅ Done — footer and charts updated ({run_ts}).")
    git_push(updated_html)

# ── GitHub Pages push ─────────────────────────────────────────────────────────

def git_push(updated_html):
    """Copy dmb_collection.html → index.html and push to GitHub Pages."""
    index_file = DASHBOARD_DIR / "index.html"
    try:
        index_file.write_text(updated_html, encoding='utf-8')
    except Exception as e:
        print(f"⚠  Could not write index.html: {e}")
        return

    today = datetime.now().strftime('%Y-%m-%d')
    cmds = [
        ['git', '-C', str(DASHBOARD_DIR), 'add', 'index.html', 'dmb_collection.html'],
        ['git', '-C', str(DASHBOARD_DIR), 'commit', '-m', f'Daily update {today}'],
        ['git', '-C', str(DASHBOARD_DIR), 'push'],
    ]
    for cmd in cmds:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            # "nothing to commit" is not a real error
            if 'nothing to commit' in result.stdout + result.stderr:
                print("  GitHub: nothing new to commit.")
                return
            print(f"  ⚠  git error ({' '.join(cmd[3:])}):\n{result.stderr.strip()}")
            return
    print("  ✅ Pushed to GitHub Pages.")

if __name__ == '__main__':
    main()
