"""
Quick Plex API test — confirms connectivity and shows what album data looks like.
Usage: python3 test_plex.py YOUR_PLEX_TOKEN
"""
import sys
import urllib.request
import xml.etree.ElementTree as ET
import re

PLEX_URL = "http://localhost:32400"
PLEX_LIBRARY_NAME = "Draeger Library"

def fetch_xml(url, token):
    req = urllib.request.Request(url, headers={"X-Plex-Token": token, "Accept": "application/xml"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return ET.fromstring(resp.read())

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 test_plex.py YOUR_PLEX_TOKEN")
        sys.exit(1)

    token = sys.argv[1]

    # 1. Get machine identifier
    print("Connecting to Plex…")
    root = fetch_xml(f"{PLEX_URL}/", token)
    machine_id = root.get("machineIdentifier")
    friendly_name = root.get("friendlyName")
    print(f"  Server: {friendly_name}  (machineId: {machine_id})")

    # 2. Find library section
    sections = fetch_xml(f"{PLEX_URL}/library/sections", token)
    section_id = None
    for d in sections.findall(".//Directory"):
        print(f"  Library: {d.get('title')} (type={d.get('type')}, key={d.get('key')})")
        if d.get("title") == PLEX_LIBRARY_NAME:
            section_id = d.get("key")

    if not section_id:
        print(f"\n❌ Library '{PLEX_LIBRARY_NAME}' not found. Check name above.")
        sys.exit(1)

    print(f"\nUsing section id={section_id}")

    # 3. Fetch all albums
    albums = fetch_xml(f"{PLEX_URL}/library/sections/{section_id}/all", token)
    items = albums.findall(".//Directory")
    print(f"Total albums in library: {len(items)}\n")

    # 4. Show first 10 that match dmb date pattern
    print("Sample DMB albums (first 10 matches):")
    count = 0
    for item in items:
        title = item.get("title", "")
        match = re.search(r"dmb(\d{4}-\d{2}-\d{2})", title.lower())
        if match:
            rk = item.get("ratingKey")
            date = match.group(1)
            plex_url = f"https://app.plex.tv/desktop/#!/server/{machine_id}/details?key=%2Flibrary%2Fmetadata%2F{rk}"
            print(f"  {date}  |  {title}")
            print(f"           {plex_url}")
            count += 1
            if count >= 10:
                break

    # 5. Build full date map to show coverage
    date_map = {}
    for item in items:
        title = item.get("title", "")
        match = re.search(r"dmb(\d{4}-\d{2}-\d{2})", title.lower())
        if match:
            date = match.group(1)
            date_map.setdefault(date, []).append(title)

    print(f"\nUnique show dates found in Plex: {len(date_map)}")
    print(f"Machine ID for Plex Web links: {machine_id}")

if __name__ == "__main__":
    main()
