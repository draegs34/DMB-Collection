#!/usr/bin/env python3
"""
DMB Plex Playlists — one Plex playlist per year folder ("DMB 1999", …).

Each playlist holds every recording in that year's folder of the archive,
in show-date order, tracks in order. Playlists show up in Plexamp.
Only Plex changes; your files are never touched.

Usage (on the iMac, in the Dashboard Page folder):
  python3 plex_playlists.py                  DRY RUN: list years and track counts
  python3 plex_playlists.py --apply          create / refresh the playlists
  python3 plex_playlists.py --years 1999,2026 --apply
  python3 plex_playlists.py --daily          for daily_update.py (refresh changed years only)
"""

import argparse, io, json, re, sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import plex_titles as pt

PREFIX = "DMB "
stats = {}                                              # year -> (shows, recordings)
BATCH = 200


def machine_id():
    return pt.plex_get("/identity").get("machineIdentifier") or pt.plex_get("/").get("machineIdentifier")

def uri_for(machine, keys):
    return f"server://{machine}/com.plexapp.plugins.library/library/metadata/{','.join(map(str, keys))}"

def req(method, path, params=None, data=None):
    p = {"X-Plex-Token": pt.TOKEN}
    p.update(params or {})
    r = pt.requests.request(method, pt.PLEX_URL + path, params=p, data=data,
                            headers={"Accept": "application/json"}, timeout=120)
    r.raise_for_status()
    return r.json().get("MediaContainer", {}) if r.content and "json" in r.headers.get("Content-Type", "") else {}

def existing_playlists():
    mc = pt.plex_get("/playlists", {"playlistType": "audio"})
    return {p["title"]: p for p in (mc.get("Metadata") or [])}

def playlist_item_keys(pid):
    mc = pt.plex_get(f"/playlists/{pid}/items")
    return [int(t["ratingKey"]) for t in (mc.get("Metadata") or [])]

def tracks_by_year(albums):
    """{year: [track ratingKeys in order]} using the year folder each recording lives in."""
    root = Path(pt.COLLECTION_ROOT)
    groups = defaultdict(list)
    stats.clear()
    for a in albums:
        try:
            year = Path(a["tracks"][0]["file"]).relative_to(root).parts[0]
        except (ValueError, IndexError):
            continue
        if re.fullmatch(r"(19|20)\d{2}", year):
            groups[year].append(a)
    out = {}
    for year, alist in groups.items():
        alist.sort(key=lambda a: (a["date"] or "9999", pt.natural_key(str(a["folder"])), a.get("disc") or 0))
        out[year] = [int(t["key"]) for a in alist for t in a["tracks"]]
        stats[year] = (len({a["date"] for a in alist if a["date"]}), len(alist))
    return out

def year_cover(year, shows, recs):
    """Sunburst year cover for the playlist (same look as the album covers)."""
    import math
    import plex_covers as pc
    from PIL import Image, ImageDraw
    W = pc.W
    c = pc.palette(int(year))
    im = Image.new("RGBA", (W, W), c(0.13, 0.45))
    rays = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    d = ImageDraw.Draw(rays)
    cx, cy = W // 2, 400
    for i in range(36):
        a = i * math.pi * 2 / 36
        a2 = a + math.pi / 72
        d.polygon([(cx, cy), (cx + 1400 * math.cos(a), cy + 1400 * math.sin(a)),
                   (cx + 1400 * math.cos(a2), cy + 1400 * math.sin(a2))], fill=c(0.3, 0.5, 60))
    d.ellipse([cx - 220, cy - 220, cx + 220, cy + 220], fill=c(0.3, 0.55, 80))
    im.alpha_composite(rays)
    d = ImageDraw.Draw(im)
    fy = pc.F(pc.SERIFB, 300)
    d.text(((W - d.textlength(year, font=fy)) / 2, 230), year, font=fy, fill=c(0.72, 0.6))
    pc.center(d, 640, "DAVE MATTHEWS BAND", pc.F(pc.SERIFB, 44), c(0.8, 0.4))
    pc.center(d, 720, f"{shows:,} shows  ·  {recs:,} recordings", pc.F(pc.SERIF, 40), (255, 255, 255, 255))
    pc.center(d, 800, "Every recording from the year", pc.F(pc.SERIF, 30), c(0.7, 0.35))
    out = io.BytesIO()
    im.convert("RGB").save(out, "JPEG", quality=90)
    return out.getvalue()

def set_cover(pid, year, shows, recs):
    try:
        req("POST", f"/library/metadata/{pid}/posters", data=year_cover(year, shows, recs))
    except Exception:
        pass                                            # a cover is a nice-to-have


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--years", help="only these years, comma-separated")
    ap.add_argument("--daily", action="store_true")
    args = ap.parse_args()
    if args.daily:
        args.apply = True
    pt.TOKEN = pt.token()
    say = (lambda *a, **k: None) if args.daily else print

    say("🎧 Reading tracks from Plex…")
    albums = pt.build_albums(pt.all_tracks(show_progress=not args.daily))
    years = tracks_by_year(albums)
    if args.years:
        want = {y.strip() for y in args.years.split(",")}
        years = {y: k for y, k in years.items() if y in want}
    have = existing_playlists()
    machine = machine_id() if args.apply else None

    changed = []
    say(f"\n{'APPLYING' if args.apply else 'DRY RUN'} — {len(years)} year playlist(s)\n")
    for year in sorted(years):
        keys = years[year]
        title = PREFIX + year
        pl = have.get(title)
        if pl and int(pl.get("leafCount") or 0) == len(keys) and (args.daily or not args.apply or
                                                                 playlist_item_keys(pl["ratingKey"]) == keys):
            say(f"   {title}: {len(keys):,} tracks — up to date")
            continue
        action = "refresh" if pl else "create"
        say(f"   {title}: {len(keys):,} tracks — {action}")
        if not args.apply:
            continue
        if pl:
            pid = pl["ratingKey"]
            req("DELETE", f"/playlists/{pid}/items")
            start = 0
        else:
            mc = req("POST", "/playlists", {"type": "audio", "title": title, "smart": 0,
                                            "uri": uri_for(machine, keys[:BATCH])})
            pid = (mc.get("Metadata") or [{}])[0].get("ratingKey")
            if not pid:
                pid = existing_playlists()[title]["ratingKey"]
            start = BATCH
            set_cover(pid, year, *stats[year])
        for i in range(start, len(keys), BATCH):
            req("PUT", f"/playlists/{pid}/items", {"uri": uri_for(machine, keys[i:i + BATCH])})
        changed.append(f"{title} ({len(keys):,})")

    if args.daily:
        print("Plex playlists: " + (", ".join(changed) + " updated." if changed else "all up to date."))
    elif not args.apply:
        print("\nNothing was changed. Add --apply to create the playlists.")
    else:
        print(f"\n✅ {len(changed)} playlist(s) created or refreshed.")


if __name__ == "__main__":
    main()
