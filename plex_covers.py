#!/usr/bin/env python3
"""
DMB Plex Covers — give each show album a cover with its year, venue, city and date
(era colors + sunburst), uploaded to Plex as the album poster.

Only Plex changes: the image is uploaded to Plex; nothing is written into the
recording folders, so your files and checksums are untouched.

Only albums still showing the generic placeholder art are changed (the
"Unknown Venue / DATE UNKNOWN" image) — real artwork such as official nugs
covers is left alone. Placeholders are found by spotting the same image used
on many albums.

Usage (on the iMac, in the Dashboard Page folder):
  python3 plex_covers.py
        DRY RUN: find placeholder covers, save a few sample covers to
        cover_samples/ to look at, and print a summary. Nothing changes.
  python3 plex_covers.py --limit 10 --apply
        Pilot: upload covers for 10 albums (+ undo file).
  python3 plex_covers.py --apply
        Every album that still has placeholder art.
  python3 plex_covers.py --undo plex_covers_undo_YYYYmmdd-HHMMSS.json
        Put the previous covers back.
  --no-badge    leave off the gold "I WAS THERE" badge on shows you attended
  --daily       for daily_update.py: --apply with a one-line summary

Deps: pip3 install requests pillow
"""

import argparse, colorsys, hashlib, io, json, math, re, sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import plex_titles as pt                     # Plex access + album grouping we already trust

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    sys.exit("Pillow not installed — run: pip3 install pillow")

STATE_FILE = HERE / "plex_covers_state.json"      # albums we've already given covers
HTML_FILE  = HERE / "dmb_collection.html"
SAMPLES    = HERE / "cover_samples"
W = 1000

# ── Fonts (macOS first, then common fallbacks) ───────────────────────────────
FONT_DIRS = ["/System/Library/Fonts/Supplemental", "/Library/Fonts", "/System/Library/Fonts",
             "/usr/share/fonts/truetype/liberation", "/usr/share/fonts/truetype/dejavu"]
def _font_path(names):
    for d in FONT_DIRS:
        for n in names:
            p = Path(d) / n
            if p.exists():
                return str(p)
    return None
SERIF  = _font_path(["Georgia.ttf", "Times New Roman.ttf", "LiberationSerif-Regular.ttf", "DejaVuSerif.ttf"])
SERIFB = _font_path(["Georgia Bold.ttf", "Times New Roman Bold.ttf", "LiberationSerif-Bold.ttf", "DejaVuSerif-Bold.ttf"])

def F(path, size):
    return ImageFont.truetype(path, size) if path else ImageFont.load_default()

def fit(d, text, path, size, maxw):
    while size > 18:
        f = F(path, size)
        if d.textlength(text, font=f) <= maxw:
            return f
        size -= 2
    return F(path, size)

def center(d, y, text, font, fill):
    w = d.textlength(text, font=font)
    d.text(((W - w) / 2, y), text, font=font, fill=fill)

def wrap2(d, text, path, size, maxw):
    f = F(path, size)
    if d.textlength(text, font=f) <= maxw or " " not in text:
        return [text], fit(d, text, path, size, maxw)
    words, best = text.split(), None
    for i in range(1, len(words)):
        a, b = " ".join(words[:i]), " ".join(words[i:])
        m = max(d.textlength(a, font=f), d.textlength(b, font=f))
        if best is None or m < best[0]:
            best = (m, a, b)
    f = fit(d, max(best[1], best[2], key=lambda s: d.textlength(s, font=F(path, size))), path, size, maxw)
    return [best[1], best[2]], f


# ── The cover ────────────────────────────────────────────────────────────────
def palette(year):
    hue = ((year - 1991) / 36 * 0.85 + 0.55) % 1
    def c(l, s, a=255):
        r, g, b = colorsys.hls_to_rgb(hue, l, s)
        return (int(r * 255), int(g * 255), int(b * 255), a)
    return c

def make_cover(show, badge=True):
    """show: date, venue, city, tour, lineage, disc, attended"""
    dt = date.fromisoformat(show["date"])
    c = palette(dt.year)
    im = Image.new("RGBA", (W, W), c(0.13, 0.45))
    top = Image.new("RGBA", (W, 330), c(0.22, 0.5))
    ray_layer = Image.new("RGBA", (W, 330), (0, 0, 0, 0))
    rays = ImageDraw.Draw(ray_layer)
    cx, cy = W // 2, 175
    for i in range(36):                                   # sunburst
        a = i * math.pi * 2 / 36
        a2 = a + math.pi / 72
        rays.polygon([(cx, cy), (cx + 900 * math.cos(a), cy + 900 * math.sin(a)),
                      (cx + 900 * math.cos(a2), cy + 900 * math.sin(a2))], fill=c(0.3, 0.5, 70))
    rays.ellipse([cx - 120, cy - 120, cx + 120, cy + 120], fill=c(0.35, 0.55, 90))
    top.alpha_composite(ray_layer)
    im.alpha_composite(top, (0, 0))
    d = ImageDraw.Draw(im)

    yy = str(dt.year)
    fy = F(SERIFB, 230)
    d.text(((W - d.textlength(yy, font=fy)) / 2, 40), yy, font=fy, fill=c(0.7, 0.6))
    center(d, 350, "DAVE MATTHEWS BAND", F(SERIFB, 40), c(0.75, 0.4))

    venue = show.get("venue") or "Venue unknown"
    lines, fv = wrap2(d, venue, SERIFB, 76, W - 140)
    yv = 450
    for line in lines:
        center(d, yv, line, fv, (255, 255, 255, 255))
        yv += fv.size + 12
    if show.get("city"):
        center(d, yv + 10, show["city"], fit(d, show["city"], SERIF, 44, W - 160), c(0.8, 0.3))

    ds = dt.strftime("%B %-d, %Y").upper()
    center(d, 760, ds, F(SERIF, 46), (255, 255, 255, 255))
    sub = dt.strftime("%A") + (f" · {show['tour']}" if show.get("tour") else "")
    center(d, 820, sub, fit(d, sub, SERIF, 30, W - 160), c(0.7, 0.35))
    foot = " · ".join(x for x in [show.get("lineage"), f"Disc {show['disc']}" if show.get("disc") else ""] if x)
    if foot:
        center(d, 900, foot, fit(d, foot, SERIF, 26, W - 420 if (badge and show.get("attended")) else W - 160), c(0.55, 0.35))

    if badge and show.get("attended"):
        bw, bh = 330, 64
        x0, y0 = W - bw - 30, W - bh - 30
        d.rounded_rectangle([x0, y0, x0 + bw, y0 + bh], radius=32, fill=(242, 193, 46, 255))
        sx, sy, r1, r2 = x0 + 44, y0 + bh / 2, 20, 8.5          # drawn star
        pts = []
        for k in range(10):
            ang = -math.pi / 2 + k * math.pi / 5
            r = r1 if k % 2 == 0 else r2
            pts.append((sx + r * math.cos(ang), sy + r * math.sin(ang)))
        d.polygon(pts, fill=(40, 28, 0, 255))
        d.text((x0 + 78, y0 + 13), "I WAS THERE", font=F(SERIFB, 32), fill=(40, 28, 0, 255))

    out = io.BytesIO()
    im.convert("RGB").save(out, "JPEG", quality=90)
    return out.getvalue()


# ── Data about each show ─────────────────────────────────────────────────────
def attended_dates():
    if not HTML_FILE.exists():
        return set()
    m = re.search(r"const ATTENDED_DATES=new Set\((\[.*?\])\)", HTML_FILE.read_text(errors="replace"), re.S)
    return set(json.loads(m.group(1))) if m else set()

def show_info(album, almanac, attended):
    alm = (almanac.get(album["date"]) or [{}])[0]
    lineage = re.sub(r"^.*?(?:19|20)\d{2}[-.]\d{2}[-.]\d{2}[\s._-]*", "", album["folder"].name)
    lineage = re.sub(r"\.(flac\d*|shnf?|mp3)$", "", lineage, flags=re.I).strip(" ._-")
    return {"date": album["date"], "venue": alm.get("venue"), "city": alm.get("city"),
            "tour": alm.get("tour"), "lineage": lineage, "disc": album.get("disc"),
            "attended": album["date"] in attended}


# ── Plex: current covers ─────────────────────────────────────────────────────
def thumb_hash(path):
    try:
        r = pt.requests.get(pt.PLEX_URL + path, params={"X-Plex-Token": pt.TOKEN}, timeout=60)
        r.raise_for_status()
        return hashlib.md5(r.content).hexdigest(), r.content
    except Exception:
        return None, None

def selected_poster(key):
    try:
        mc = pt.plex_get(f"/library/metadata/{key}/posters")
        for p in mc.get("Metadata", []) or []:
            if p.get("selected"):
                return p.get("ratingKey")
    except Exception:
        pass
    return None

def upload(key, jpeg):
    r = pt.requests.post(f"{pt.PLEX_URL}/library/metadata/{key}/posters",
                         params={"X-Plex-Token": pt.TOKEN}, data=jpeg, timeout=60)
    r.raise_for_status()
    pt.plex_put(key, {"type": 9, "thumb.locked": 1})


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="only this many albums (pilot)")
    ap.add_argument("--dates", help="only these show dates, comma-separated")
    ap.add_argument("--no-badge", action="store_true")
    ap.add_argument("--min-repeat", type=int, default=20,
                    help="an image used on at least this many albums counts as placeholder art")
    ap.add_argument("--daily", action="store_true")
    ap.add_argument("--undo", metavar="FILE")
    args = ap.parse_args()
    if args.daily:
        args.apply = True
    pt.TOKEN = pt.token()

    if args.undo:
        recs = json.loads(Path(args.undo).read_text())
        state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
        for rec in recs:
            if rec.get("previous"):
                pt.requests.put(f"{pt.PLEX_URL}/library/metadata/{rec['album']}/poster",
                                params={"url": rec["previous"], "X-Plex-Token": pt.TOKEN}, timeout=60)
            pt.plex_put(rec["album"], {"type": 9, "thumb.locked": 0})
            state.pop(str(rec["album"]), None)
        STATE_FILE.write_text(json.dumps(state))
        print(f"Restored {len(recs)} cover(s).")
        return

    say = (lambda *a, **k: None) if args.daily else print
    say("🎧 Reading albums from Plex…")
    tracks = pt.all_tracks(show_progress=not args.daily)
    thumbs = {t.get("parentRatingKey"): t.get("parentThumb") for t in tracks}
    albums = [a for a in pt.build_albums(tracks) if a["date"]]
    almanac = json.loads(pt.ALMANAC_JSON.read_text()) if pt.ALMANAC_JSON.exists() else {}
    attended = attended_dates()
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}

    # which albums still have placeholder art?  (same image on many albums)
    keep = set(map(str, state.get("_keep", [])))            # already checked: real artwork
    todo = [a for a in albums if str(a["key"]) not in state and str(a["key"]) not in keep]
    if args.dates:
        want = {x.strip() for x in args.dates.split(",")}
        todo = [a for a in todo if a["date"] in want]
    say(f"🖼  Checking current covers on {len(todo):,} album(s)…")
    hashes, samples = {}, {}
    for i, a in enumerate(todo, 1):
        if not args.daily:
            print(f"  …{i:,} of {len(todo):,}", end="\r", flush=True)
        p = thumbs.get(a["key"])
        if not p:
            try:
                p = (pt.plex_get(f"/library/metadata/{a['key']}").get("Metadata") or [{}])[0].get("thumb")
            except Exception:
                p = None
        h, raw = thumb_hash(p) if p else ("none", None)
        hashes[a["key"]] = h or "error"
        if h and h not in samples:
            samples[h] = raw
    say()
    counts = Counter(hashes.values())
    placeholder = {h for h, n in counts.items() if h == "none" or (h != "error" and n >= args.min_repeat)}
    # the placeholder list is learned from the whole library on a full run; daily runs reuse it
    known = set(state.get("_placeholders", []))
    full_scan = not args.dates and not args.limit and not args.daily
    if full_scan:
        known |= placeholder
        state["_placeholders"] = sorted(known)
        STATE_FILE.write_text(json.dumps(state))
    elif not known and not args.limit:
        print("⚠  Run once without --dates first so the placeholder art can be recognized.")
    placeholder |= known
    targets = [a for a in todo if hashes[a["key"]] in placeholder]

    if not args.daily:
        print("Covers in use (most common first):")
        for h, n in counts.most_common(6):
            tag = "placeholder → will be replaced" if h in placeholder else "real artwork → kept"
            print(f"   {n:>6,}  {h[:10]}  {tag}")
        SAMPLES.mkdir(exist_ok=True)
        for h, n in counts.most_common(3):
            if samples.get(h):
                (SAMPLES / f"current_{h[:10]}_x{n}.jpg").write_bytes(samples[h])
    if args.limit:
        targets = targets[:args.limit]

    if not args.apply:
        for a in targets[:6]:
            info = show_info(a, almanac, attended)
            (SAMPLES / f"new_{a['date']}_{info['lineage'][:30] or a['key']}.jpg".replace("/", "-")).write_bytes(
                make_cover(info, badge=not args.no_badge))
        print(f"\nDRY RUN — {len(targets):,} album(s) would get a new cover.")
        print(f"Look in {SAMPLES.name}/ : current_* are today's most common covers, new_* are samples of the new ones.")
        print("Nothing was changed. Add --apply (or --limit 10 --apply for a pilot).")
        return

    undo, done = [], 0
    for i, a in enumerate(targets, 1):
        if not args.daily:
            print(f"  …uploading {i:,} of {len(targets):,}", end="\r", flush=True)
        info = show_info(a, almanac, attended)
        try:
            prev = selected_poster(a["key"])
            upload(a["key"], make_cover(info, badge=not args.no_badge))
        except Exception as e:
            print(f"\n⚠  {a['date']} {a['folder'].name}: {e}")
            continue
        undo.append({"album": a["key"], "previous": prev})
        state[str(a["key"])] = {"date": a["date"], "folder": a["folder"].name,
                                "at": datetime.now().isoformat(timespec="seconds")}
        done += 1
        if args.limit or args.dates or args.daily:
            print(f"   {a['date']}  {a['folder'].name}" + ("  ★" if info.get("attended") else ""))
    state["_placeholders"] = sorted(known)
    if full_scan or args.daily:                     # remember albums whose real artwork we kept
        state["_keep"] = sorted(keep | {str(a["key"]) for a in todo
                                        if hashes[a["key"]] not in placeholder and hashes[a["key"]] != "error"})
    STATE_FILE.write_text(json.dumps(state))
    if undo:
        f = HERE / f"plex_covers_undo_{datetime.now():%Y%m%d-%H%M%S}.json"
        f.write_text(json.dumps(undo, indent=1))
    if args.daily:
        print(f"Plex covers: {done} new cover(s)." + (f" Undo file: {f.name}" if undo else ""))
    else:
        print(f"\n✅ {done:,} cover(s) uploaded." + (f"  Undo file: {f.name}" if undo else ""))


if __name__ == "__main__":
    main()
