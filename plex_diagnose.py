#!/usr/bin/env python3
"""
Plex diagnostic — run this from your Mac to see what Plex returns
vs what the collection expects.

Usage:  python3 plex_diagnose.py
"""

import json, sys
from pathlib import Path

# ── Check token ───────────────────────────────────────────────────────────────
TOKEN_FILE = Path.home() / ".dmb_plex_token"
if not TOKEN_FILE.exists():
    print("✗  Token file not found: ~/.dmb_plex_token")
    print()
    print("Create it by running this command:")
    print("  python3 -c \"import getpass; open('/Users/jamesdraeger/.dmb_plex_token','w').write(getpass.getpass('Paste your Plex token: '))\"")
    print()
    print("Get your token: Plex web → any item → Get Info → View XML → X-Plex-Token in URL")
    sys.exit(1)

TOKEN = TOKEN_FILE.read_text().strip()
print(f"✓  Token file found ({len(TOKEN)} chars)")

try:
    import requests
except ImportError:
    print("✗  requests not installed — run: pip3 install requests")
    sys.exit(1)

PLEX_URL    = "http://localhost:32400"
LIBRARY_ID  = "2"
PLEX_HEADERS = {"X-Plex-Token": TOKEN, "Accept": "application/json"}

# ── Test connection ───────────────────────────────────────────────────────────
print("\n── Connecting to Plex ──")
try:
    r = requests.get(f"{PLEX_URL}/", headers=PLEX_HEADERS, timeout=5)
    r.raise_for_status()
    print(f"✓  Plex responding (HTTP {r.status_code})")
except Exception as e:
    print(f"✗  Cannot reach Plex at {PLEX_URL}: {e}")
    print("   Is Plex Media Server running?")
    sys.exit(1)

# ── Fetch tracks (type=10) to get real file paths ─────────────────────────────
print(f"\n── Fetching tracks from library section {LIBRARY_ID} (this may take a moment) ──")
try:
    r = requests.get(
        f"{PLEX_URL}/library/sections/{LIBRARY_ID}/all?type=10",
        headers=PLEX_HEADERS,
        timeout=120,
    )
    r.raise_for_status()
    tracks = r.json().get("MediaContainer", {}).get("Metadata", [])
except Exception as e:
    print(f"✗  Failed: {e}")
    sys.exit(1)

print(f"✓  {len(tracks):,} tracks found")

if not tracks:
    sys.exit(1)

# ── Build folder→URL map from track file paths ────────────────────────────────
from urllib.parse import quote
MACHINE_ID = "341172425e0e6f0ff15189aa5ab8d1e0a2acd625"
plex_map = {}
for t in tracks:
    parent_key = t.get("parentRatingKey", "")
    if not parent_key:
        continue
    try:
        file_path = t["Media"][0]["Part"][0]["file"]
    except (KeyError, IndexError):
        continue
    folder = Path(file_path).parent.name
    if folder and folder not in plex_map:
        key_path = f"/library/metadata/{parent_key}"
        plex_map[folder] = (
            f"https://app.plex.tv/desktop/#!/server/{MACHINE_ID}"
            f"/details?key={quote(key_path, safe='')}"
        )

print(f"✓  {len(plex_map):,} unique folders mapped from track paths")
print("\n── First 20 Plex folder names (new PLEX_MAP keys) ──")
for i, k in enumerate(sorted(plex_map)[:20]):
    print(f"  [{i+1:>2}] {k!r}")

# ── Load actual folder names from collection ──────────────────────────────────
COLLECTION = Path("/Volumes/DMB Archive/DMB Fan and Broadcast Library")
print(f"\n── First 20 folder names under {COLLECTION.name}/ (what s[11] values look like) ──")
folders = []
for year_dir in sorted(COLLECTION.iterdir()):
    if not year_dir.is_dir():
        continue
    for show_dir in sorted(year_dir.iterdir()):
        if show_dir.is_dir():
            folders.append(show_dir.name)
for i, f in enumerate(folders[:20]):
    print(f"  [{i+1:>2}] {f!r}")

# ── Cross-check ───────────────────────────────────────────────────────────────
plex_keys  = set(plex_map.keys())
folder_set = set(folders)

matched   = plex_keys & folder_set
only_plex = plex_keys - folder_set
only_disk = folder_set - plex_keys

print(f"\n── Match summary ──")
print(f"  Plex folders (from track paths) : {len(plex_keys):,}")
print(f"  Disk folders                    : {len(folder_set):,}")
print(f"  Exact matches                   : {len(matched):,}")
print(f"  Plex only                       : {len(only_plex):,}")
print(f"  Disk only                       : {len(only_disk):,}")

if matched:
    print(f"\n  ✓ Sample matches:")
    for t in sorted(matched)[:5]:
        print(f"    {t!r}")
if only_plex:
    print(f"\n  ⚠  Plex folders NOT matched on disk (first 5):")
    for t in sorted(only_plex)[:5]:
        print(f"    {t!r}")
if only_disk:
    print(f"\n  ⚠  Disk folders NOT in Plex (first 5):")
    for t in sorted(only_disk)[:5]:
        print(f"    {t!r}")
