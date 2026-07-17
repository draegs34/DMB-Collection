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
from datetime import datetime

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

    return result  # unrecognized — still return partial


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
