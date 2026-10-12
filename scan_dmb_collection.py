#!/usr/bin/env python3
"""
DMB Collection Scanner
Scans your DMB live tape collection folder structure and exports metadata to CSV.

Usage:
    python3 scan_dmb_collection.py /path/to/your/DMB/drive

Requirements (optional, for audio duration):
    pip3 install mutagen

Output:
    dmb_collection.csv  — one row per recording folder
"""

import os
import re
import csv
import sys
from pathlib import Path
from datetime import datetime, date as _date_cls

# Try to import mutagen for audio duration — optional
try:
    from mutagen.flac import FLAC
    from mutagen import File as MutagenFile
    HAS_MUTAGEN = True
except ImportError:
    HAS_MUTAGEN = False
    print("Note: mutagen not installed. Audio durations won't be extracted.")
    print("      Install with: pip3 install mutagen\n")


# ---------------------------------------------------------------------------
# Folder name parser
# ---------------------------------------------------------------------------

# Matches patterns like:
#   dmb1996-12-27
#   dmb2001-04-21
#   dm2001-11-13
#   DMB-07.29.01   (MM.DD.YY style)
DATE_PATTERNS = [
    # Standard: dmb/dm + YYYY-MM-DD (most common)
    re.compile(
        r'^(?:DMB|dmb|dm)[- _]?'
        r'(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})'
        r'(?:\.(?P<rest>.+))?$',
        re.IGNORECASE
    ),
    # Alternate: DMB-MM.DD.YY (e.g. DMB-07.29.01-SPAC, Saratoga NY-mp3)
    re.compile(
        r'^DMB-(?P<month>\d{2})\.(?P<day>\d{2})\.(?P<year2>\d{2})'
        r'(?:-(?P<rest>.+))?$',
        re.IGNORECASE
    ),
]

FORMAT_TOKENS = {'flac16', 'flac24', 'flac', 'shnf', 'shn', 'mp3', 'mp2', 'wav', 'ape'}

SOURCE_HINTS = {
    'aud': 'Audience',
    'daud': 'Digital Audience',
    'sbd': 'Soundboard',
    'dsbd': 'Digital Soundboard',
    'matrix': 'Matrix',
    'fm': 'FM Broadcast',
    'siriusxm': 'SiriusXM',
    'xm': 'SiriusXM',
    'web': 'Web/Stream Download',
    'webdl': 'Web/Stream Download',
}


def parse_folder_name(folder_name):
    """Extract date, source, format, venue, and tags from a folder name."""
    result = {
        'date': None,
        'year': None,
        'month': None,
        'day': None,
        'source': None,
        'source_type': None,
        'format': None,
        'bit_depth': None,
        'venue_hint': None,
        'tags': [],
        'raw_name': folder_name,
    }

    # Try standard YYYY-MM-DD pattern first
    m = DATE_PATTERNS[0].match(folder_name)
    if m:
        result['year'] = int(m.group('year'))
        result['month'] = int(m.group('month'))
        result['day'] = int(m.group('day'))
        result['date'] = f"{m.group('year')}-{m.group('month')}-{m.group('day')}"
        rest = m.group('rest') or ''
        _parse_rest(rest, result)
        return result

    # Try MM.DD.YY pattern
    m = DATE_PATTERNS[1].match(folder_name)
    if m:
        yy = int(m.group('year2'))
        result['year'] = 2000 + yy if yy < 50 else 1900 + yy
        result['month'] = int(m.group('month'))
        result['day'] = int(m.group('day'))
        result['date'] = f"{result['year']}-{result['month']:02d}-{result['day']:02d}"
        rest = m.group('rest') or ''
        _parse_rest(rest, result, separator='-')
        return result

    # Fallback: any other naming style (d+t…, tr3…, '1999.02.05 - Venue', 'Paris 02.05.2024', …)
    found = find_date_in_name(folder_name)
    if found:
        y, mo, d, rest = found
        result['year'], result['month'], result['day'] = y, mo, d
        result['date'] = f"{y}-{mo:02d}-{d:02d}"
        _parse_rest(rest, result)
        return result

    return result  # unrecognized — still return partial


# ── Flexible date finder (fallback when the strict patterns above don't match) ──
_MONTHS = {m: i for i, m in enumerate(
    ['jan','feb','mar','apr','may','jun','jul','aug','sep','oct','nov','dec'], 1)}
_MAX_YEAR = datetime.now().year + 1

def _valid_date(y, m, d):
    try:
        return 1989 <= y <= _MAX_YEAR and bool(_date_cls(y, m, d))
    except ValueError:
        return False

def _yy(s):
    """Expand a 2- or 4-digit year string."""
    y = int(s)
    if len(s) == 4:
        return y
    return 1900 + y if y >= 89 else 2000 + y

_MON_RX = r'(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*'
# (regex, converter -> (year, month, day)); tried in order, first valid date wins
_FALLBACK_DATE_RES = [
    # TR3 folders: tr32008-07-03...
    (re.compile(r'^tr3(\d{4})-(\d{2})-(\d{2})(?!\d)', re.I),
     lambda m: (int(m[1]), int(m[2]), int(m[3]))),
    # ISO-ish anywhere: d+t1999-02-05, 1999.02.05 - ..., dmb2014-6-10, 2022_06_06, 2026/01/22
    (re.compile(r'(?<!\d)(19[89]\d|20[0-4]\d)[-._/ ](\d{1,2})[-._/ ](\d{1,2})(?!\d)'),
     lambda m: (int(m[1]), int(m[2]), int(m[3]))),
    # Compact YYYYMMDD: dmb20070802.mk4..., 20250823_DMB_...
    (re.compile(r'(?<!\d)(19[89]\d|20[0-4]\d)(\d{2})(\d{2})(?!\d)'),
     lambda m: (int(m[1]), int(m[2]), int(m[3]))),
    # Short-year typos right after a prefix: dmb021-07-31 -> 2021, dmb205-10-24 -> 2005
    (re.compile(r'^[A-Za-z+&]+(0\d{2}|20\d)-(\d{2})-(\d{2})(?!\d)'),
     lambda m: ((2000 + int(m[1])) if m[1].startswith('0') else (2000 + int(m[1][2])), int(m[2]), int(m[3]))),
    # Compact MMDDYYYY: dmb06062022_..., dmb 05202022 ...
    (re.compile(r'(?<!\d)(\d{2})(\d{2})(19[89]\d|20[0-4]\d)(?!\d)'),
     lambda m: (int(m[3]), int(m[1]), int(m[2]))),
    # US style M-D-Y: 07-15-1999, 9/21/2021, 11.12.21, 6.20.25, SJCA-12-3-94
    (re.compile(r'(?<![\d./])(\d{1,2})[-./](\d{1,2})[-./](\d{4}|\d{2})(?![\d./-])'),
     lambda m: (_yy(m[3]), int(m[1]), int(m[2]))),
    # European D.M.Y when US order is impossible: Dublin 27.04.2024
    (re.compile(r'(?<![\d./])(\d{1,2})[-./](\d{1,2})[-./](\d{4}|\d{2})(?![\d./-])'),
     lambda m: (_yy(m[3]), int(m[2]), int(m[1]))),
    # December 7, 2012 / May 25th 2012
    (re.compile(r'\b' + _MON_RX + r'\.? (\d{1,2})(?:st|nd|rd|th)?,? (\d{4})\b', re.I),
     lambda m: (int(m[3]), _MONTHS[m[1][:3].lower()], int(m[2]))),
    # 20APR2019 / Boston 4 Feb 1995
    (re.compile(r'(?<!\d)(\d{1,2}) ?' + _MON_RX + r' ?(\d{4})(?!\d)', re.I),
     lambda m: (int(m[3]), _MONTHS[m[2][:3].lower()], int(m[1]))),
]

def find_date_in_name(name, known_dates=None):
    """Best-effort show date from any folder-name style.
    Returns (year, month, day, rest) or None. When known_dates (a set of
    'YYYY-MM-DD' strings, e.g. the almanac) is given, an ambiguous name like
    'Paris 02.05.2024' resolves to the reading that is a real show date."""
    first = None
    for rx, conv in _FALLBACK_DATE_RES:
        for m in rx.finditer(name):
            y, mo, d = conv(m)
            if not _valid_date(y, mo, d):
                continue
            cand = (y, mo, d, name[m.end():].lstrip('._- '))
            if not known_dates or f"{y}-{mo:02d}-{d:02d}" in known_dates:
                return cand
            if first is None:
                first = cand
    return first


def _parse_rest(rest, result, separator='.'):
    """Parse the suffix after the date portion."""
    if not rest:
        return

    # If there's a dash-separated venue hint (like "SPAC, Saratoga NY-mp3")
    # split on last dash to separate format from venue
    parts = rest.replace('-', '.').split('.')

    format_parts = []
    source_parts = []
    tag_parts = []
    venue_parts = []

    for part in parts:
        pl = part.lower().strip()

        # Known audio formats
        if pl in ('flac16', 'flac24'):
            result['format'] = 'FLAC'
            result['bit_depth'] = 16 if '16' in pl else 24
            continue
        if pl in FORMAT_TOKENS:
            result['format'] = pl.upper().replace('SHNF', 'SHN')
            continue

        # Bit depth standalone
        if pl in ('16', '24'):
            result['bit_depth'] = int(pl)
            continue

        # Known source type hints
        matched_source = False
        for key, label in SOURCE_HINTS.items():
            if pl == key or pl.startswith(key):
                result['source_type'] = label
                source_parts.append(part)
                matched_source = True
                break
        if matched_source:
            continue

        # Special tags
        if pl in ('virgin', 'letterman', 'trl', 'reconvert', 'de-edited',
                  'remaster', 'remastered', 'webdl', 'web-dl'):
            tag_parts.append(part)
            continue

        # Looks like a venue/city (contains spaces, commas, or capital letters mid-string)
        if ' ' in part or ',' in part or (len(part) > 3 and part[0].isupper()):
            venue_parts.append(part)
            continue

        # Otherwise treat as source/taper identifier
        source_parts.append(part)

    if source_parts:
        result['source'] = '.'.join(source_parts)
    if tag_parts:
        result['tags'] = tag_parts
    if venue_parts:
        result['venue_hint'] = ' '.join(venue_parts)


# ---------------------------------------------------------------------------
# File inspection
# ---------------------------------------------------------------------------

AUDIO_EXTENSIONS = {'.flac', '.shn', '.mp3', '.mp2', '.wav', '.ape', '.ogg'}
TEXT_EXTENSIONS = {'.txt', '.nfo', '.md', '.log'}


def inspect_folder(folder_path):
    """Walk a show folder and collect file stats."""
    audio_files = []
    text_files = []
    total_size_bytes = 0

    for entry in os.scandir(folder_path):
        if entry.is_file():
            ext = Path(entry.name).suffix.lower()
            size = entry.stat().st_size
            total_size_bytes += size
            if ext in AUDIO_EXTENSIONS:
                audio_files.append(entry.path)
            elif ext in TEXT_EXTENSIONS:
                text_files.append(entry.path)

    duration_seconds = None
    if HAS_MUTAGEN and audio_files:
        duration_seconds = 0
        for af in audio_files:
            try:
                audio = MutagenFile(af)
                if audio and audio.info:
                    duration_seconds += audio.info.length
            except Exception:
                pass

    # Read first text file if present
    notes_snippet = None
    if text_files:
        try:
            with open(text_files[0], 'r', errors='replace') as f:
                notes_snippet = f.read(500).strip().replace('\n', ' | ')
        except Exception:
            pass

    return {
        'audio_file_count': len(audio_files),
        'text_file_count': len(text_files),
        'total_size_mb': round(total_size_bytes / (1024 * 1024), 1),
        'duration_minutes': round(duration_seconds / 60, 1) if duration_seconds else None,
        'has_notes': len(text_files) > 0,
        'notes_snippet': notes_snippet,
    }


# ---------------------------------------------------------------------------
# Main scan
# ---------------------------------------------------------------------------

def scan_collection(root_path):
    root = Path(root_path)
    if not root.exists():
        print(f"Error: path not found: {root_path}")
        sys.exit(1)

    records = []
    skipped = []

    # Walk year-level subdirectories
    year_dirs = sorted([d for d in root.iterdir() if d.is_dir()])
    
    for year_dir in year_dirs:
        show_dirs = sorted([d for d in year_dir.iterdir() if d.is_dir()])
        
        if not show_dirs:
            # Maybe the year folder IS the show folder level — try parsing it directly
            show_dirs = [year_dir]

        print(f"Scanning {year_dir.name}... ({len(show_dirs)} folders)")

        for show_dir in show_dirs:
            parsed = parse_folder_name(show_dir.name)
            file_info = inspect_folder(show_dir)

            record = {
                'folder_name': show_dir.name,
                'parent_year_folder': year_dir.name,
                'date': parsed['date'],
                'year': parsed['year'],
                'month': parsed['month'],
                'day': parsed['day'],
                'source': parsed['source'],
                'source_type': parsed['source_type'],
                'format': parsed['format'],
                'bit_depth': parsed['bit_depth'],
                'venue_hint': parsed['venue_hint'],
                'tags': ', '.join(parsed['tags']) if parsed['tags'] else '',
                **file_info,
            }
            records.append(record)

            if not parsed['date']:
                skipped.append(show_dir.name)

    return records, skipped


def write_csv(records, output_path):
    if not records:
        print("No records found.")
        return

    fieldnames = [
        'folder_name', 'date', 'year', 'month', 'day',
        'source', 'source_type', 'format', 'bit_depth',
        'venue_hint', 'tags',
        'audio_file_count', 'total_size_mb', 'duration_minutes',
        'has_notes', 'notes_snippet',
        'parent_year_folder',
    ]

    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(records)

    print(f"\n✅ Exported {len(records)} recordings to: {output_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python3 scan_dmb_collection.py /path/to/DMB/drive")
        print("\nExample:")
        print("  python3 scan_dmb_collection.py /Volumes/DMB\\ Archive")
        sys.exit(1)

    root_path = sys.argv[1]
    output_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'dmb_collection.csv')

    print(f"🎵 DMB Collection Scanner")
    print(f"   Scanning: {root_path}")
    print(f"   Output:   {output_file}")
    if HAS_MUTAGEN:
        print(f"   Audio durations: YES (mutagen found)")
    print()

    records, skipped = scan_collection(root_path)
    write_csv(records, output_file)

    if skipped:
        print(f"\n⚠️  {len(skipped)} folders didn't match expected naming patterns:")
        for s in skipped[:10]:
            print(f"   - {s}")
        if len(skipped) > 10:
            print(f"   ... and {len(skipped) - 10} more")

    print(f"\nSummary:")
    dated = [r for r in records if r['date']]
    years = sorted(set(r['year'] for r in dated if r['year']))
    print(f"   Total recordings: {len(records)}")
    print(f"   Date range: {years[0]} – {years[-1]}" if years else "   No dated shows found")
    print(f"\nUpload dmb_collection.csv to Claude to build your dashboard!")
