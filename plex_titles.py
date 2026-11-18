#!/usr/bin/env python3
"""
DMB Plex Titles — fix untitled tracks/albums in Plex using each recording's info file.

Only Plex's database is changed (titles are written through the Plex API and locked).
Your audio files are never opened for writing, so .md5/.ffp checksums stay valid.

Usage (run on the iMac that hosts Plex and the DMB Archive drive):
  python3 plex_titles.py --scan
        Count DMB albums in Plex whose tracks still have file-name titles.
  python3 plex_titles.py --dates 2005-06-08 --sample 10
        DRY RUN: show what would change for those dates + 10 random untitled albums.
  python3 plex_titles.py --dates 2005-06-08 --sample 10 --seed 7 --apply
        Same selection (keep --seed the same as the dry run), but write the changes
        to Plex. Saves an undo file next to this script.
  python3 plex_titles.py --undo plex_titles_undo_20261118-201500.json
        Put back exactly what an --apply run changed.

Deps: pip3 install requests
"""

import argparse, json, random, re, sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("requests not installed — run: pip3 install requests")

# ── Config (matches daily_update.py) ─────────────────────────────────────────
COLLECTION_ROOT = "/Volumes/DMB Archive/DMB Fan and Broadcast Library"
PLEX_URL        = "http://localhost:32400"
PLEX_LIBRARY_ID = "2"
TOKEN_FILE      = Path.home() / ".dmb_plex_token"
HERE            = Path(__file__).resolve().parent
ALMANAC_JSON    = HERE / "dmb_almanac.json"

TEXT_EXT = {".txt", ".nfo", ".md", ".log"}
SKIP_TEXT = re.compile(r"ffp|md5|st5|sha|checksum|fingerprint", re.I)


# ── Plex helpers ─────────────────────────────────────────────────────────────
def token():
    if not TOKEN_FILE.exists():
        sys.exit("Plex token not found (~/.dmb_plex_token) — run plex_diagnose.py first.")
    return TOKEN_FILE.read_text().strip()

def plex_get(path, params=None, timeout=180):
    p = {"X-Plex-Token": TOKEN}
    p.update(params or {})
    r = requests.get(PLEX_URL + path, params=p, headers={"Accept": "application/json"}, timeout=timeout)
    r.raise_for_status()
    return r.json().get("MediaContainer", {})

def plex_put(rating_key, params):
    p = {"X-Plex-Token": TOKEN}
    p.update(params)
    r = requests.put(f"{PLEX_URL}/library/metadata/{rating_key}", params=p, timeout=60)
    r.raise_for_status()

def all_tracks():
    """Every track in the music library, fetched in pages (one big request times out)."""
    out, start, PAGE = [], 0, 2000
    while True:
        mc = plex_get(f"/library/sections/{PLEX_LIBRARY_ID}/all",
                      {"type": 10, "X-Plex-Container-Start": start, "X-Plex-Container-Size": PAGE})
        batch = mc.get("Metadata", []) or []
        out.extend(batch)
        start += len(batch)
        total = int(mc.get("totalSize", 0) or 0)
        print(f"  …{start:,}{f' of {total:,}' if total else ''} tracks", end="\r", flush=True)
        if not batch or len(batch) < PAGE or (total and start >= total):
            break
    print()
    return out


# ── Grouping tracks into albums (one album = one recording folder) ───────────
def track_file(t):
    try:
        return t["Media"][0]["Part"][0]["file"]
    except (KeyError, IndexError):
        return ""

def natural_key(s):
    return [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", s)]

def build_albums(tracks):
    albums = defaultdict(lambda: {"tracks": []})
    for t in tracks:
        f = track_file(t)
        if not f.startswith(COLLECTION_ROOT):
            continue                              # not part of the DMB archive
        a = albums[t.get("parentRatingKey")]
        a["key"] = t.get("parentRatingKey")
        a["album_title"] = t.get("parentTitle", "")
        a["tracks"].append({"key": t["ratingKey"], "title": t.get("title", ""),
                            "index": t.get("index"), "file": f})
    for a in albums.values():
        a["tracks"].sort(key=lambda x: natural_key(x["file"]))
        dirs = {str(Path(x["file"]).parent) for x in a["tracks"]}
        folder = Path(dirs.pop()) if len(dirs) == 1 else Path(_common_dir(dirs | {str(Path(a["tracks"][0]["file"]).parent)}))
        a["disc"] = None
        if DISC_DIR.match(folder.name):                     # .../dmb2009-04-14.xyz/d1
            a["disc"] = int(re.search(r"\d+", folder.name).group())
            folder = folder.parent
        a["folder"] = folder
        a["date"] = folder_date(folder.name) or folder_date(str(folder))
    return list(albums.values())

DISC_DIR = re.compile(r"^\s*(?:d|cd|disc|disk)\s*[-_ ]?\d{1,2}\s*$", re.I)

def _common_dir(paths):
    parts = [Path(p).parts for p in paths]
    common = []
    for seg in zip(*parts):
        if len(set(seg)) == 1:
            common.append(seg[0])
        else:
            break
    return str(Path(*common))

def folder_date(name):
    m = re.search(r"((?:19|20)\d{2})[-.](\d{2})[-.](\d{2})", name)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None

def untitled(track):
    title = (track["title"] or "").strip()
    stem = Path(track["file"]).stem
    return (not title
            or title.lower() == stem.lower()
            or re.fullmatch(r"(track\s*)?\d{1,3}", title, re.I) is not None
            or re.search(r"d\d+\s*t\d+", title, re.I) is not None
            or re.search(r"(19|20)\d{2}[-.]\d{2}[-.]\d{2}", title) is not None)

def album_untitled(a):
    n = sum(untitled(t) for t in a["tracks"])
    return n > 0 and n >= len(a["tracks"]) / 2


# ── Reading the setlist out of the info file ─────────────────────────────────
DISC_HDR = re.compile(r"^\s*(?:disc|disk|cd|set)\s*[#:]?\s*(\d+|one|two|three|four|i{1,3}|iv)\b", re.I)
SONG_LINE = re.compile(
    r"^\s*(?:(?:d|cd|disc)\s*(\d)\s*[-_.]?\s*)?(?:t(?:rack)?\s*)?(\d{1,2})"
    r"(?:\s*[.):,\-]\s*|\s+)(.+?)\s*$", re.I)
DURATION = re.compile(r"\s*[\(\[]?\d{1,2}:\d{2}(?:[:.]\d{2})?[\)\]]?\s*$")
JUNK = re.compile(r"\.(flac|shn|wav|mp3|ffp|md5)\b|[0-9a-f]{32}|\bkbps\b|\bkhz\b|\bhz\b|source:|lineage|taper|transfer|"
                  r"conversion|seeded|recorded by|\bmics?\b|\bdat\b|>\s*cd\b|sbd\s*>|aud\s*>", re.I)
WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4, "i": 1, "ii": 2, "iii": 3, "iv": 4}

FOOTNOTE = re.compile(r"\s*[*~$#+&^%@!]+\s*$")
SEGUE    = re.compile(r"\s*(?:-+>|=+>|>+)\s*$")

def clean_title(t):
    t = t.replace("\t", " ")
    t = DURATION.sub("", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" |")
    t = re.sub(r"^(?:e|encore)\s*[:\-]\s*", "", t, flags=re.I)      # "E: Christmas Song"
    segue = False
    for _ in range(3):                                            # "What Would You Say > &", "Seek Up>>"
        t2 = FOOTNOTE.sub("", t)
        if SEGUE.search(t2):
            segue, t2 = True, SEGUE.sub("", t2)
        if t2 == t:
            break
        t = t2
    t = t.strip(" -–—|")
    return (t + " >") if (segue and t) else t

def title_from_filename(path):
    """'15 - encore break (1999-06-22).flac' → 'encore break'"""
    s = Path(path).stem
    s = re.sub(r"\((?:19|20)\d{2}[-.]\d{2}[-.]\d{2}\)", "", s)
    s = re.sub(r"^\s*(?:d\d+\s*)?t?\d{1,3}\s*[-._)]?\s*", "", s, flags=re.I)
    s = s.strip(" -_.")
    return s if re.search(r"[a-z]{3}", s, re.I) and not re.search(r"(19|20)\d{2}[-.]\d{2}", s) else ""

def parse_setlist(text):
    """Return [(disc, num, title)] in file order."""
    out, disc, last_num = [], 1, 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = DISC_HDR.match(line)
        if m and len(line) < 30:
            v = m.group(1).lower()
            disc = int(v) if v.isdigit() else WORDNUM.get(v, disc)
            last_num = 0
            continue
        m = SONG_LINE.match(line)
        if not m:
            continue
        d, num, title = m.group(1), int(m.group(2)), clean_title(m.group(3))
        if len(title) > 90 or JUNK.search(title) or re.fullmatch(r"[\d:.\s]*", title):
            continue
        if not re.search(r"[a-z0-9#]", title, re.I):
            title = ""                             # e.g. "==========" encore-break line
        if d:
            disc = int(d)
        elif num == 1 and last_num >= 1:
            disc += 1                              # numbering restarted → next disc
        out.append((disc, num, title))
        last_num = num
    return out

def setlists_for_folder(folder):
    found = []
    try:
        files = [p for p in Path(folder).rglob("*") if p.is_file() and p.suffix.lower() in TEXT_EXT
                 and not SKIP_TEXT.search(p.name)]
    except OSError:
        return found
    for p in sorted(files):
        try:
            entries = parse_setlist(p.read_text(errors="replace"))
        except OSError:
            continue
        if len(entries) >= 3:
            found.append((p.name, entries))
    return found

def file_pos(path):
    """(disc, track) from names like dmb2005-06-08d1t01.flac or ...t07.flac."""
    stem = Path(path).stem
    m = re.search(r"d(\d+)\s*t(\d+)$", stem, re.I) or re.search(r"(?:cd|disc)\s*(\d+).*?(\d+)$", stem, re.I)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"(\d+)$", stem)
    return (None, int(m.group(1))) if m else (None, None)


# ── Matching tracks to titles ────────────────────────────────────────────────
def propose(album):
    tracks = album["tracks"]
    best = None
    for fname, entries in setlists_for_folder(album["folder"]):
        # 1) by disc/track numbers in the file names
        by_dn = {(d, n): t for d, n, t in entries}
        by_n = {}
        for d, n, t in entries:
            by_n.setdefault(n, t)
        for label, lookup in (("disc+track", lambda d, n: by_dn.get((d, n)) if d is not None else None),
                              ("track #", lambda d, n: by_n.get(n) if d in (None, 1) and not album.get("disc", 0) not in (None, 1) else None)):
            titles = [lookup(*_pos(album, t)) for t in tracks]
            if all(x is not None for x in titles):
                titles = _fill_blanks(titles, tracks)
                if titles:
                    return {"method": f"numbers ({label})", "source": fname, "titles": titles}
        # 2) same number of songs as tracks → take them in order
        if len(entries) == len(tracks):
            titles = _fill_blanks([t for _, _, t in entries], tracks)
            if titles:
                best = best or {"method": "same count, in order", "source": fname, "titles": titles}
        elif best is None or best.get("method") == "mismatch":
            best = {"method": "mismatch", "source": fname,
                    "detail": f"info file lists {len(entries)} songs, folder has {len(tracks)} tracks"}
    return best or {"method": "none", "detail": "no setlist found in the folder's info files"}

def _pos(album, t):
    d, n = file_pos(t["file"])
    return (album.get("disc") if d is None and album.get("disc") else d), n

def _fill_blanks(titles, tracks):
    out = []
    for title, t in zip(titles, tracks):
        title = title or title_from_filename(t["file"])
        if not title:
            return None
        out.append(title)
    return out

def album_name(album, almanac):
    date = album["date"]
    if not date:
        return None
    alm = almanac.get(date) or []
    where = f"{alm[0]['venue']}, {alm[0]['city']}" if alm else ""
    lineage = re.sub(r"^.*?(?:19|20)\d{2}[-.]\d{2}[-.]\d{2}[\s._-]*", "", album["folder"].name)
    lineage = re.sub(r"\.(flac\d*|shnf?|mp3)$", "", lineage, flags=re.I).strip(" ._-")
    name = date.replace("-", "/") + (f" {where}" if where else "")
    name += f" [{lineage}]" if lineage else ""
    return name + (f" (Disc {album['disc']})" if album.get("disc") else "")


# ── Commands ─────────────────────────────────────────────────────────────────
def cmd_scan(albums):
    bad = [a for a in albums if album_untitled(a)]
    print(f"\nDMB albums in Plex: {len(albums):,}")
    print(f"Albums with file-name titles: {len(bad):,}")
    by_year = defaultdict(int)
    for a in bad:
        by_year[(a['date'] or '????')[:4]] += 1
    print("By year: " + ", ".join(f"{y}: {n}" for y, n in sorted(by_year.items())))

def cmd_run(albums, args):
    almanac = json.loads(ALMANAC_JSON.read_text()) if ALMANAC_JSON.exists() else {}
    if not almanac:
        print("⚠  dmb_almanac.json not found next to this script — album names will have no venue.")
    want_dates = {d.strip() for d in (args.dates or "").split(",") if d.strip()}
    chosen = [a for a in albums if a["date"] in want_dates and album_untitled(a)]
    pool = sorted([a for a in albums if album_untitled(a) and a not in chosen], key=lambda a: str(a["folder"]))
    random.Random(args.seed).shuffle(pool)
    chosen += pool[:args.sample]
    if not chosen:
        print("Nothing selected — check --dates / --sample.")
        return

    undo, ok, skipped = [], 0, 0
    print(f"\n{'APPLYING' if args.apply else 'DRY RUN'} — {len(chosen)} album(s)\n")
    for a in chosen:
        p = propose(a)
        name = album_name(a, almanac)
        good = p["method"] not in ("mismatch", "none")
        mark = "✓" if good else "✗ SKIP"
        print(f"=== {a['date'] or '????-??-??'}  {a['folder'].name}  ({len(a['tracks'])} tracks)  {mark}")
        print(f"    match: {p['method']}" + (f" — from {p['source']}" if p.get('source') else "")
              + (f" — {p['detail']}" if p.get('detail') else ""))
        if not good:
            skipped += 1
            print()
            continue
        print(f"    album: \"{a['album_title']}\"  →  \"{name}\"")
        for i, (t, new) in enumerate(zip(a["tracks"], p["titles"]), 1):
            print(f"    {i:>3}. {Path(t['file']).name:<40} → {new}")
        print()
        if args.apply:
            rec = {"album": a["key"], "album_title": a["album_title"], "tracks": []}
            if name:
                plex_put(a["key"], {"type": 9, "title.value": name, "title.locked": 1})
            for i, (t, new) in enumerate(zip(a["tracks"], p["titles"]), 1):
                plex_put(t["key"], {"type": 10, "title.value": new, "title.locked": 1,
                                    "index.value": i, "index.locked": 1})
                rec["tracks"].append({"key": t["key"], "title": t["title"], "index": t["index"]})
            undo.append(rec)
        ok += 1

    print(f"Summary: {ok} ready{' and applied' if args.apply else ''}, {skipped} skipped (need review).")
    if args.apply and undo:
        f = HERE / f"plex_titles_undo_{datetime.now():%Y%m%d-%H%M%S}.json"
        f.write_text(json.dumps(undo, indent=1))
        print(f"Undo file: {f.name}   (python3 plex_titles.py --undo {f.name})")
    elif not args.apply:
        print("Nothing was changed. Add --apply (same --dates/--sample/--seed) to write these to Plex.")

def cmd_undo(path):
    recs = json.loads(Path(path).read_text())
    for rec in recs:
        plex_put(rec["album"], {"type": 9, "title.value": rec["album_title"], "title.locked": 0})
        for t in rec["tracks"]:
            params = {"type": 10, "title.value": t["title"], "title.locked": 0, "index.locked": 0}
            if t.get("index") is not None:
                params["index.value"] = t["index"]
            plex_put(t["key"], params)
    print(f"Restored {len(recs)} album(s).")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scan", action="store_true", help="count untitled albums and stop")
    ap.add_argument("--dates", help="comma-separated show dates to include, e.g. 2005-06-08,1999-05-12")
    ap.add_argument("--sample", type=int, default=0, help="also include N random untitled albums")
    ap.add_argument("--seed", type=int, default=1, help="random seed for --sample (reuse it with --apply)")
    ap.add_argument("--apply", action="store_true", help="write the changes to Plex")
    ap.add_argument("--undo", metavar="FILE", help="restore from an undo file")
    args = ap.parse_args()

    TOKEN = token()
    if args.undo:
        cmd_undo(args.undo)
        sys.exit()
    print("🎧 Reading tracks from Plex…")
    albums = build_albums(all_tracks())
    if args.scan or not (args.dates or args.sample):
        cmd_scan(albums)
    else:
        cmd_run(albums, args)
