#!/usr/bin/env python3
#
# photohaul - ingest photos from a mounted camera card.
#
# Copies raw files off the card (format named on the CLI or via 'format=' in
# ~/.photohaul - Sony ARW, Nikon NEF, Fuji RAF, Canon CR3, JPEG) into the current
# folder, renaming each to a stable YYYYMMDD-hhmmss_mmm.ext name derived from Exif
# (millisecond timestamp = unique key, so names are identical no matter which subset
# is copied -> safe partial/repeat runs).
# In-camera "locked" frames (the FAT read-only bit, surfaced on macOS as the uchg flag)
# are detected, copied unlocked, and marked Purple for Lightroom via an .xmp sidecar.
# Copyright/creator (from ~/.photohaul) and a per-folder template (photohaul.json) -
# an AP-style caption plus structured IPTC fields (headline, credit/source,
# city/state/country, location, date created, usage terms, keywords) - are written to
# the same sidecar. The raw is copied byte-for-byte,
# the lone exception being an optional capture-time/timezone correction (photohaul.json)
# that overwrites the copy's EXIF date/offset fields in place at the same byte length -
# so the copy's size always matches the card original and re-runs stay idempotent.
#
# Read-only on the source: the card is never modified.
# Zero dependencies: stdlib only.

import argparse
import configparser
import json
import os
import re
import stat
import struct
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta

IS_TTY = sys.stdout.isatty()

# ---------------------------------------------------------------------------
# Exif reader (stdlib only) - pulls the capture timestamp (DateTimeOriginal +
# SubSecTimeOriginal) and recorded UTC offset (OffsetTimeOriginal) from TIFF raws
# (ARW/NEF), Fuji RAF, and Canon CR3. The same header-only IFD walk also backs the
# in-place date/offset patcher (shift_exif_in_place), which reuses read_ifd to find
# each field's file offset and overwrites it at the identical byte length.
# ---------------------------------------------------------------------------

EXIF_IFD_PTR        = 0x8769
DATETIME            = 0x0132   # IFD0 modify date/time
DATETIME_ORIGINAL   = 0x9003
DATETIME_DIGITIZED  = 0x9004
SUBSEC_ORIGINAL     = 0x9291
OFFSET_TIME         = 0x9010   # UTC offset of DateTime
OFFSET_TIME_ORIGINAL  = 0x9011 # UTC offset of DateTimeOriginal
OFFSET_TIME_DIGITIZED = 0x9012 # UTC offset of DateTimeDigitized
_TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}
FUJI_MAGIC        = b'FUJIFILMCCD-RAW'
RAF_JPEG_OFFSET   = 0x54   # RAF header field: file offset of the embedded JPEG (BE u32)
CR3_BRAND         = b'crx '   # ftyp major brand identifying a Canon CR3 (ISO-BMFF)


def _exif_tiff_base(f):
    """File offset of the Exif TIFF header.

    0 for a TIFF-at-start file (ARW/NEF, JPEG-less TIFF). Container formats keep
    their Exif elsewhere:
      - Fuji RAF: an embedded JPEG (follow JpgImageOffset to its APP1/Exif).
      - Canon CR3: an ISO-BMFF box tree (moov/uuid/CMT2 is a standalone TIFF).
    """
    head = f.read(16)
    if head[:len(FUJI_MAGIC)] == FUJI_MAGIC:
        return _raf_exif_base(f)
    if head[4:8] == b'ftyp' and head[8:12] == CR3_BRAND:
        return _cr3_exif_base(f)
    return 0


def _raf_exif_base(f):
    """Fuji RAF: walk the header's embedded JPEG to the TIFF header in its APP1/Exif."""
    f.seek(RAF_JPEG_OFFSET)
    (jpg_off,) = struct.unpack('>I', f.read(4))
    f.seek(jpg_off)
    if f.read(2) != b'\xff\xd8':                       # JPEG SOI
        raise ValueError('RAF: no embedded JPEG')
    while True:
        marker = f.read(2)
        if len(marker) < 2 or marker[0] != 0xFF or marker == b'\xff\xd9':
            raise ValueError('RAF: no Exif in embedded JPEG')
        (seglen,) = struct.unpack('>H', f.read(2))
        if seglen < 2:                                 # length includes its own 2 bytes;
            raise ValueError('RAF: bad JPEG segment length')   # < 2 would seek backward
        seg_start = f.tell()
        if marker == b'\xff\xe1' and f.read(6) == b'Exif\x00\x00':
            return f.tell()                            # TIFF header begins here
        f.seek(seg_start + seglen - 2)                 # skip to next marker (incl. XMP APP1)


def _cr3_exif_base(f):
    """Canon CR3: the Exif is a standalone TIFF in the moov/uuid/CMT2 box."""
    f.seek(0, os.SEEK_END)
    box = _bmff_find(f, [b'moov', b'uuid', b'CMT2'], 0, f.tell())
    if not box:
        raise ValueError('CR3: no CMT2 Exif box')
    return box[0]


def _bmff_find(f, path, start, end):
    """Locate a nested ISO-BMFF box by type path; return (payload_start, payload_end).

    Reads only box headers via seeks (never the mdat payload). A 'uuid' box is
    descended past its 16-byte id. Returns None if any path element is missing.
    """
    want = path[0]
    p = start
    while p + 8 <= end:
        f.seek(p)
        hdr = f.read(8)
        if len(hdr) < 8:
            break
        (size,) = struct.unpack('>I', hdr[:4])
        typ = hdr[4:8]
        body = p + 8
        if size == 1:                                  # 64-bit largesize follows
            (size,) = struct.unpack('>Q', f.read(8))
            body = p + 16
        elif size == 0:                                # box runs to end of parent
            size = end - p
        if size < 8 or p + size > end:
            break
        if typ == want:
            child = body + 16 if typ == b'uuid' else body
            if len(path) == 1:
                return (child, p + size)
            found = _bmff_find(f, path[1:], child, p + size)
            if found:
                return found
        p += size
    return None


def read_ifd(f, base, bo, off):
    """Read one TIFF IFD at base+off; return {tag: (typ, cnt, valoff, valfield_pos)}.

    `valoff` is the raw 4-byte value/offset field; `valfield_pos` is its absolute
    file position - the seek target the in-place patcher overwrites for inline values.
    """
    f.seek(base + off)
    (n,) = struct.unpack(bo + 'H', f.read(2))
    raw = f.read(n * 12)
    entries = {}
    for i in range(n):
        tag, typ, cnt = struct.unpack(bo + 'HHI', raw[i * 12:i * 12 + 8])
        valoff = raw[i * 12 + 8:i * 12 + 12]
        valfield_pos = base + off + 2 + i * 12 + 8
        entries[tag] = (typ, cnt, valoff, valfield_pos)
    return entries


def _field_loc(f, base, bo, entry):
    """Absolute (file_offset, byte_length) of an entry's value bytes.

    Values <=4 bytes sit inline in the entry; larger ones are pointed to from base.
    """
    typ, cnt, valoff, valfield_pos = entry
    size = _TYPE_SIZES.get(typ, 1) * cnt
    if size <= 4:
        return valfield_pos, size
    (o,) = struct.unpack(bo + 'I', valoff)
    return base + o, size


def ascii_val(f, base, bo, entry):
    pos, size = _field_loc(f, base, bo, entry)
    f.seek(pos)
    data = f.read(size)
    return data.split(b'\x00')[0].decode('ascii', 'replace')


def _exif_ifds(f):
    """Parse the TIFF header and return (base, bo, ifd0_entries, exif_entries).

    `exif_entries` is the Exif IFD when an EXIF_IFD_PTR is present (ARW/NEF/RAF), else
    IFD0 itself (CR3 CMT2 carries the date tags directly). Raises ValueError if the
    header is not a TIFF/Exif block or no Exif IFD is found.
    """
    base = _exif_tiff_base(f)                       # 0 for TIFF (ARW/NEF); nonzero for RAF/CR3
    f.seek(base)
    head = f.read(8)
    if head[:2] == b'II':
        bo = '<'
    elif head[:2] == b'MM':
        bo = '>'
    else:
        raise ValueError('not a TIFF/Exif file')
    (magic,) = struct.unpack(bo + 'H', head[2:4])
    if magic != 42:
        raise ValueError('bad TIFF magic')
    (ifd0,) = struct.unpack(bo + 'I', head[4:8])
    e0 = read_ifd(f, base, bo, ifd0)
    if EXIF_IFD_PTR in e0:
        # ARW/NEF/RAF: the canonical DateTimeOriginal + SubSec live in the Exif IFD
        # (some bodies also copy the date into IFD0 - prefer the Exif IFD, which is
        # the one that also carries SubSecTimeOriginal).
        (exif_off,) = struct.unpack(bo + 'I', e0[EXIF_IFD_PTR][2])
        ee = read_ifd(f, base, bo, exif_off)
    elif DATETIME_ORIGINAL in e0:
        ee = e0                            # CR3 CMT2: no Exif pointer; tags in this IFD
    else:
        raise ValueError('no Exif IFD')
    return base, bo, e0, ee


def read_exif_datetime(path):
    """Return (datetime_str, subsec_str_or_None, offset_str_or_None) from a TIFF/JPEG
    raw, Fuji RAF, or Canon CR3.

    The offset is OffsetTimeOriginal (0x9011), the zone shot_tz derives its shift from.
    Reads only the header and the relevant IFD(s) via seeks - never the whole file.
    Raises ValueError on anything it can't parse.
    """
    with open(path, 'rb') as f:
        base, bo, _e0, ee = _exif_ifds(f)
        if DATETIME_ORIGINAL not in ee:
            raise ValueError('no DateTimeOriginal')
        dt = ascii_val(f, base, bo, ee[DATETIME_ORIGINAL])
        sub = ascii_val(f, base, bo, ee[SUBSEC_ORIGINAL]) if SUBSEC_ORIGINAL in ee else None
        rec_off = (ascii_val(f, base, bo, ee[OFFSET_TIME_ORIGINAL])
                   if OFFSET_TIME_ORIGINAL in ee else None)
        return dt, sub, rec_off


# ---------------------------------------------------------------------------
# Capture-time correction (clock drift + timezone)
#
# Two orthogonal, composable folder-level knobs from photohaul.json:
#   time_shift - a wall-clock-only nudge of the date fields (the old clock was wrong).
#   shot_tz    - "these were actually shot at this UTC offset"; derives an instant-
#                preserving shift from the recorded offset and restamps the offset tags.
# Both drive one corrected timestamp (filename, caption, {year}) and an in-place
# overwrite of the copied raw's EXIF date/offset bytes - same byte length, no IFD
# restructuring. See docs/20260605_capture_time_offset_plan.md.
# ---------------------------------------------------------------------------

_DATE_FMT = '%Y:%m:%d %H:%M:%S'   # EXIF date, 19 chars + NUL = 20-byte fixed width
_TZ_LEN   = 6                     # '±HH:MM', stored as 6 chars + NUL = 7-byte width


class ExifPatchError(Exception):
    """A capture-time correction could not be applied. Aborts the run (unlike the
    per-file OSError handler in copy()), so no half-patched file is left behind."""


def parse_time_shift(s):
    """Parse a signed wall-clock shift like '+2h30m', '-15s', '90m' -> timedelta.

    Units d/h/m/s, combinable, whole seconds only (so SubSecTimeOriginal and the
    filename's millisecond key are never disturbed). Raises ValueError on bad input.
    """
    m = re.fullmatch(r'([+-]?)((?:\d+[dhms])+)', s.strip())
    if not m:
        raise ValueError("bad time_shift %r (want e.g. +2h30m, -15s, 90m)" % s)
    units = {'d': 86400, 'h': 3600, 'm': 60, 's': 1}
    secs = sum(int(n) * units[u] for n, u in re.findall(r'(\d+)([dhms])', m.group(2)))
    return timedelta(seconds=(-secs if m.group(1) == '-' else secs))


def parse_tz(s):
    """Parse a strict '±HH:MM' UTC offset (e.g. '-04:00', '+05:30') -> timedelta.

    Raises ValueError on bad input.
    """
    m = re.fullmatch(r'([+-])(\d{2}):(\d{2})', s.strip())
    if not m:
        raise ValueError("bad UTC offset %r (want ±HH:MM, e.g. -04:00)" % s)
    hh, mm = int(m.group(2)), int(m.group(3))
    if hh > 14 or mm > 59:
        raise ValueError("UTC offset out of range %r" % s)
    secs = hh * 3600 + mm * 60
    return timedelta(seconds=(-secs if m.group(1) == '-' else secs))


def format_tz(delta):
    """timedelta -> normalized '±HH:MM' (the canonical bytes written to offset tags)."""
    total = int(delta.total_seconds())
    sign = '-' if total < 0 else '+'
    total = abs(total)
    return '%s%02d:%02d' % (sign, total // 3600, (total % 3600) // 60)


def format_shift(delta):
    """timedelta -> compact signed '+2h30m' / '-15s' / '+0s' for reporting."""
    total = int(delta.total_seconds())
    sign = '-' if total < 0 else '+'
    total = abs(total)
    out = ''
    for unit, sec in (('d', 86400), ('h', 3600), ('m', 60), ('s', 1)):
        if total >= sec:
            out += '%d%s' % (total // sec, unit)
            total %= sec
    return sign + (out or '0s')


def shift_exif_datetime(dt_str, delta):
    """'2026:05:30 12:12:56' + delta -> shifted string of identical 19-char width.

    Handles minute/hour/day/month/year rollover via datetime. Raises ValueError if
    the stored value isn't a parseable EXIF datetime.
    """
    try:
        dt = datetime.strptime(dt_str.strip(), _DATE_FMT)
    except ValueError:
        raise ValueError('unparseable EXIF datetime %r' % dt_str)
    return (dt + delta).strftime(_DATE_FMT)


def _patch_date_field(f, base, bo, entries, tag, delta):
    """Shift one present ASCII date field in place by delta; return True if patched.

    Same-length 19-byte overwrite (the trailing NUL of the 20-byte field is kept).
    Raises ExifPatchError on unexpected type/width or an unparseable stored value.
    """
    if tag not in entries:
        return False
    typ, cnt = entries[tag][0], entries[tag][1]
    pos, size = _field_loc(f, base, bo, entries[tag])
    if typ != 2 or cnt != 20:
        raise ExifPatchError('tag 0x%04x: unexpected date type/width (%d/%d)'
                             % (tag, typ, cnt))
    f.seek(pos)
    old = f.read(size).split(b'\x00')[0].decode('ascii', 'replace')
    try:
        new = shift_exif_datetime(old, delta).encode('ascii')
    except ValueError as e:
        raise ExifPatchError('tag 0x%04x: %s' % (tag, e))
    if len(new) != 19:
        raise ExifPatchError('tag 0x%04x: reformatted date is not 19 bytes' % tag)
    f.seek(pos)
    f.write(new)
    return True


def _patch_offset_field(f, base, bo, entries, tag, target):
    """Overwrite one present ASCII offset tag in place with `target` ('±HH:MM').

    Same-length 6-byte overwrite (the NUL of the 7-byte field is kept). Returns True
    if patched; raises ExifPatchError on unexpected type/width.
    """
    if tag not in entries:
        return False
    typ, cnt = entries[tag][0], entries[tag][1]
    pos, _size = _field_loc(f, base, bo, entries[tag])
    if typ != 2 or cnt != 7:
        raise ExifPatchError('tag 0x%04x: unexpected offset type/width (%d/%d)'
                             % (tag, typ, cnt))
    new = target.encode('ascii')
    if len(new) != _TZ_LEN:
        raise ExifPatchError('tag 0x%04x: offset target is not %d bytes' % (tag, _TZ_LEN))
    f.seek(pos)
    f.write(new)
    return True


def shift_exif_in_place(path, date_delta, target_offset):
    """Correct the copied raw's EXIF in place: shift each present date field by
    date_delta, and (when target_offset is given) restamp each present offset tag.

    Same-length ASCII overwrites only - no IFD restructuring, no MakerNote pointer
    touched, so the file size is unchanged. Returns the list of patched field names.
    Raises ExifPatchError on any anomaly; the caller removes the .partial.
    """
    patched = []
    with open(path, 'r+b') as f:
        try:
            base, bo, e0, ee = _exif_ifds(f)
        except ValueError as e:
            raise ExifPatchError(str(e))
        # Date fields - shift each individually (preserves any rare per-field diff).
        for entries, tag, name in ((e0, DATETIME, 'DateTime'),
                                   (ee, DATETIME_ORIGINAL, 'DateTimeOriginal'),
                                   (ee, DATETIME_DIGITIZED, 'DateTimeDigitized')):
            if _patch_date_field(f, base, bo, entries, tag, date_delta):
                patched.append(name)
        # Offset tags - only when shot_tz gave a target.
        if target_offset is not None:
            for tag, name in ((OFFSET_TIME_ORIGINAL, 'OffsetTimeOriginal'),
                              (OFFSET_TIME, 'OffsetTime'),
                              (OFFSET_TIME_DIGITIZED, 'OffsetTimeDigitized')):
                if _patch_offset_field(f, base, bo, ee, tag, target_offset):
                    patched.append(name)
        f.flush()
        os.fsync(f.fileno())
    return patched


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------

def base_name(dt_str, subsec):
    """'2026:05:26 14:00:24', '708' -> '20260526-140024_708'."""
    date_part, time_part = dt_str.split(' ', 1)
    stamp = date_part.replace(':', '') + '-' + time_part.replace(':', '')
    if subsec and subsec.strip():
        # Normalize to 3-digit milliseconds.
        ms = int(subsec.strip().ljust(3, '0')[:3])
        stamp += '_%03d' % ms
    return stamp


def dsc_number(filename):
    """Trailing digit run from the original card name, e.g. A1_02660.ARW -> '02660'."""
    stem = os.path.splitext(os.path.basename(filename))[0]
    digits = ''
    for ch in reversed(stem):
        if ch.isdigit():
            digits = ch + digits
        else:
            break
    return digits or '0'


# ---------------------------------------------------------------------------
# Lock detection (read-only on source)
# ---------------------------------------------------------------------------

def is_locked(st):
    """True if the macOS immutable (uchg) flag is set on a stat result - the in-camera
    protect bit. Takes an os.stat_result so the caller can reuse one stat for size too."""
    return bool(getattr(st, 'st_flags', 0) & stat.UF_IMMUTABLE)


# ---------------------------------------------------------------------------
# Card discovery
# ---------------------------------------------------------------------------

def find_card(source):
    """Resolve the card root (a dir containing DCIM/). Auto-detect under /Volumes."""
    if source:
        if not os.path.isdir(os.path.join(source, 'DCIM')):
            sys.exit("Error: no DCIM/ under %s" % source)
        return source
    candidates = []
    for entry in sorted(os.listdir('/Volumes')):
        root = os.path.join('/Volumes', entry)
        if os.path.isdir(os.path.join(root, 'DCIM')):
            candidates.append(root)
    if not candidates:
        sys.exit("Error: no card with a DCIM/ folder found under /Volumes. "
                 "Use --source to point at one.")
    if len(candidates) > 1:
        sys.exit("Error: multiple cards found (%s). Use --source to pick one."
                 % ', '.join(candidates))
    return candidates[0]


def scan_source(card, ext):
    """All matching files under DCIM/, sorted by path."""
    suffix = '.' + ext.lower()
    found = []
    dcim = os.path.join(card, 'DCIM')
    for dirpath, _dirs, files in os.walk(dcim):
        for name in files:
            if name.lower().endswith(suffix):
                found.append(os.path.join(dirpath, name))
    return sorted(found)


# ---------------------------------------------------------------------------
# Config (~/.photohaul) and caption template (photohaul.json)
# ---------------------------------------------------------------------------

CONFIG_PATH     = os.path.expanduser('~/.photohaul')
TEMPLATE_NAME   = 'photohaul.json'
# Folder-level shoot scaffold. Caption-text keys + structured IPTC keys (city/state/
# country, credit/source, rightsUsage/assignment) + keyword sources (teams, sport,
# conference). See docs/20260606_iptc_fields_plan.md.
TEMPLATE_KEYS   = ['sport', 'event', 'homeTeam', 'awayTeam', 'homeShort', 'awayShort',
                   'venue', 'city', 'state', 'country', 'conference',
                   'credit', 'source', 'rightsUsage', 'assignment']
DEFAULT_PROFILE = 'default'


def load_config(profile=None, path=CONFIG_PATH):
    """Resolve rights from ~/.photohaul (INI; [default] is inherited by profiles).

    profile=None (or 'default') -> the [default] keys only. A named profile is
    merged over [default]. Missing file -> empty dict. Exits if a named profile
    is absent. A section-less (legacy flat) file is treated as [default].
    """
    parser = configparser.ConfigParser(default_section=DEFAULT_PROFILE,
                                        interpolation=None)
    try:
        with open(path, encoding='utf-8') as f:
            text = f.read()
    except FileNotFoundError:
        return {}
    try:
        parser.read_string(text)
    except configparser.MissingSectionHeaderError:
        parser.read_string('[%s]\n%s' % (DEFAULT_PROFILE, text))

    if profile and profile != DEFAULT_PROFILE:
        if not parser.has_section(profile):
            sys.exit("Error: profile '%s' not found in %s" % (profile, path))
        return dict(parser[profile])
    return dict(parser.defaults())


def load_template(dest_dir):
    """Load photohaul.json from dest_dir, or None if absent. Exits on bad JSON."""
    path = os.path.join(dest_dir, TEMPLATE_NAME)
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as e:
        sys.exit("Error: %s is not valid JSON (%s)" % (path, e))
    if not isinstance(data, dict):
        sys.exit("Error: %s must be a JSON object" % path)
    return data


def write_template(dest_dir):
    """Scaffold a blank photohaul.json (caption/IPTC keys + corrections, one per line)."""
    if not os.path.isdir(dest_dir):
        sys.exit("Error: destination %s is not a directory" % dest_dir)
    path = os.path.join(dest_dir, TEMPLATE_NAME)
    if os.path.exists(path):
        sys.exit("Error: %s already exists; not overwriting." % path)
    keys = ['profile'] + TEMPLATE_KEYS + ['time_shift', 'shot_tz']
    lines = ['{']
    for i, key in enumerate(keys):
        tail = ',' if i < len(keys) - 1 else ''
        lines.append('  "%s": ""%s' % (key, tail))
    lines.append('}')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    print('Wrote blank photohaul.json template: %s' % path)


def capture_year(captured):
    """'2026:05:26 14:00:24' -> '2026'."""
    return captured.split(':', 1)[0]


# AP date style: weekday, then month abbreviated except March-July (spelled out),
# then day, year. Names are hardcoded (not strftime) so the caption stays English
# AP style regardless of the host LC_TIME locale. Index 0 of months is a placeholder
# so the 1-based month number indexes directly; weekdays follow date.weekday() (Mon=0).
_AP_MONTHS = ['', 'Jan.', 'Feb.', 'March', 'April', 'May', 'June', 'July',
              'Aug.', 'Sept.', 'Oct.', 'Nov.', 'Dec.']
_WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']


def ap_date(captured):
    """'2025:10:03 14:00:24' -> 'Friday, Oct. 3, 2025' (AP caption date)."""
    d = datetime.strptime(captured.split(' ', 1)[0], '%Y:%m:%d')
    return '%s, %s %d, %d' % (_WEEKDAYS[d.weekday()], _AP_MONTHS[d.month], d.day, d.year)


def iso_datecreated(captured, offset):
    """'2025:10:03 19:10:25', '-07:00' -> '2025-10-03T19:10:25-07:00' (IPTC DateCreated).

    ISO 8601 from the *corrected* capture time + effective offset, so the structured
    date agrees with the corrected filename/caption. Offset None (camera recorded none,
    no shot_tz) -> a zoneless local timestamp.
    """
    d = datetime.strptime(captured, _DATE_FMT)
    return d.strftime('%Y-%m-%dT%H:%M:%S') + (offset or '')


# Full state name -> AP caption abbreviation. The eight AP never abbreviates (Alaska,
# Hawaii, Idaho, Iowa, Maine, Ohio, Texas, Utah) and any unlisted/non-US value pass
# through unchanged - so a value already typed as "Calif." or a half-filled table still
# yields a readable caption. Used for the caption text only; photoshop:State keeps the
# full name the template holds.
_AP_STATES = {
    'Alabama': 'Ala.', 'Arizona': 'Ariz.', 'Arkansas': 'Ark.', 'California': 'Calif.',
    'Colorado': 'Colo.', 'Connecticut': 'Conn.', 'Delaware': 'Del.', 'Florida': 'Fla.',
    'Georgia': 'Ga.', 'Illinois': 'Ill.', 'Indiana': 'Ind.', 'Kansas': 'Kan.',
    'Kentucky': 'Ky.', 'Louisiana': 'La.', 'Maryland': 'Md.', 'Massachusetts': 'Mass.',
    'Michigan': 'Mich.', 'Minnesota': 'Minn.', 'Mississippi': 'Miss.', 'Missouri': 'Mo.',
    'Montana': 'Mont.', 'Nebraska': 'Neb.', 'Nevada': 'Nev.', 'New Hampshire': 'N.H.',
    'New Jersey': 'N.J.', 'New Mexico': 'N.M.', 'New York': 'N.Y.', 'North Carolina': 'N.C.',
    'North Dakota': 'N.D.', 'Oklahoma': 'Okla.', 'Oregon': 'Ore.', 'Pennsylvania': 'Pa.',
    'Rhode Island': 'R.I.', 'South Carolina': 'S.C.', 'South Dakota': 'S.D.',
    'Tennessee': 'Tenn.', 'Vermont': 'Vt.', 'Virginia': 'Va.', 'Washington': 'Wash.',
    'West Virginia': 'W.Va.', 'Wisconsin': 'Wis.', 'Wyoming': 'Wyo.',
}


def _ap_state(name):
    """Full state name -> AP caption abbreviation; unknown/never-abbreviated -> unchanged."""
    return _AP_STATES.get(name, name)


def build_caption(template, date_str, config):
    """Assemble the folder-level context caption (AP style), omitting blank fields.

    This is the constant scaffold for the whole shoot - "{home} vs. {away}, {event},
    at {venue}, {city}, {state} on {date}. (Photo by {credit})". The per-image action
    sentence and player IDs are added by hand later in Lightroom. Country is in the
    structured photoshop:Country field, not the caption text (AP omits it domestically).
    An empty template yields nothing.
    """
    def g(key):
        v = template.get(key)
        return v.strip() if isinstance(v, str) else ''

    parts = []
    if g('homeShort') and g('awayShort'):
        parts.append('%s vs. %s' % (g('homeShort'), g('awayShort')))
    if g('event'):
        parts.append(g('event'))
    place = []
    if g('venue'):
        place.append('at ' + g('venue'))
    # State is AP-abbreviated for the caption only; photoshop:State keeps the full name.
    locale = ', '.join(p for p in (g('city'), _ap_state(g('state'))) if p)
    if locale:
        place.append(locale)
    if place:
        parts.append(', '.join(place))

    sentence = ', '.join(parts)
    if sentence and date_str:
        sentence += ' on ' + date_str

    credit = g('credit') or config.get('credit', '') or config.get('creator', '')
    out = []
    if sentence:
        out.append(sentence + '.')
        if credit:
            out.append('(Photo by %s)' % credit)
    return ' '.join(out)


# ---------------------------------------------------------------------------
# XMP sidecar (build fresh or merge into existing; option B)
# ---------------------------------------------------------------------------

XMP_NS = {
    'x':   'adobe:ns:meta/',
    'rdf': 'http://www.w3.org/1999/02/22-rdf-syntax-ns#',
    'xmp': 'http://ns.adobe.com/xap/1.0/',
    'dc':  'http://purl.org/dc/elements/1.1/',
}
# Register the prefixes we write, plus the common Adobe/Lightroom ones, so that
# when we merge into an existing sidecar its other properties (e.g. develop edits)
# round-trip with their conventional prefixes instead of ET's auto "ns0/ns1".
_EXTRA_NS = {
    'crs':          'http://ns.adobe.com/camera-raw-settings/1.0/',
    'photoshop':    'http://ns.adobe.com/photoshop/1.0/',
    'tiff':         'http://ns.adobe.com/tiff/1.0/',
    'exif':         'http://ns.adobe.com/exif/1.0/',
    'aux':          'http://ns.adobe.com/exif/1.0/aux/',
    'lr':           'http://ns.adobe.com/lightroom/1.0/',
    'xmpMM':        'http://ns.adobe.com/xap/1.0/mm/',
    'xmpRights':    'http://ns.adobe.com/xap/1.0/rights/',
    'stEvt':        'http://ns.adobe.com/xap/1.0/sType/ResourceEvent#',
    'stRef':        'http://ns.adobe.com/xap/1.0/sType/ResourceRef#',
    'Iptc4xmpCore': 'http://iptc.org/std/Iptc4xmpCore/1.0/xmlns/',
    'Iptc4xmpExt':  'http://iptc.org/std/Iptc4xmpExt/2008-02-29/',
}
_ALL_NS = {**XMP_NS, **_EXTRA_NS}
for _p, _u in _ALL_NS.items():
    ET.register_namespace(_p, _u)
_XML_LANG = '{http://www.w3.org/XML/1998/namespace}lang'

_SKELETON = (
    '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
    '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
    '<rdf:Description rdf:about="" '
    'xmlns:xmp="http://ns.adobe.com/xap/1.0/" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/"/>'
    '</rdf:RDF></x:xmpmeta>'
)
_XPACKET_HEAD = '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
_XPACKET_TAIL = '\n<?xpacket end="w"?>\n'


class SidecarParseError(Exception):
    """An existing sidecar could not be parsed; caller should warn and leave it."""


def _q(prefix, local):
    return '{%s}%s' % (_ALL_NS[prefix], local)


def _clear_prop(descs, qname):
    """Remove a property in either attribute or element form from all Descriptions."""
    for d in descs:
        d.attrib.pop(qname, None)
        for child in list(d):
            if child.tag == qname:
                d.remove(child)


def _set_label(desc, value):
    ET.SubElement(desc, _q('xmp', 'Label')).text = value


def _set_seq(desc, qname, value):
    seq = ET.SubElement(ET.SubElement(desc, qname), _q('rdf', 'Seq'))
    ET.SubElement(seq, _q('rdf', 'li')).text = value


def _set_alt(desc, qname, value):
    alt = ET.SubElement(ET.SubElement(desc, qname), _q('rdf', 'Alt'))
    li = ET.SubElement(alt, _q('rdf', 'li'))
    li.set(_XML_LANG, 'x-default')
    li.text = value


def _set_simple(desc, qname, value):
    ET.SubElement(desc, qname).text = value


def _set_bag(desc, qname, values):
    bag = ET.SubElement(ET.SubElement(desc, qname), _q('rdf', 'Bag'))
    for v in values:
        ET.SubElement(bag, _q('rdf', 'li')).text = v


# Simple-text IPTC fields: fields-dict key -> XMP (prefix, local). photoshop:Credit is
# the agency attribution line (distinct from the dc:creator byline); Iptc4xmpCore:Location
# is the sublocation (the venue within the city); date_created is per-frame (ISO 8601),
# the rest folder-constant.
_SIMPLE_IPTC = [
    ('headline',     ('photoshop', 'Headline')),
    ('credit',       ('photoshop', 'Credit')),
    ('source',       ('photoshop', 'Source')),
    ('city',         ('photoshop', 'City')),
    ('state',        ('photoshop', 'State')),
    ('country',      ('photoshop', 'Country')),
    ('location',     ('Iptc4xmpCore', 'Location')),
    ('instructions', ('photoshop', 'Instructions')),
    ('date_created', ('photoshop', 'DateCreated')),
]


def read_label(sidecar):
    """Return the xmp:Label of an existing sidecar (attribute or element form), else None.

    Used only for reporting under --rewrite, so we can say how many Purple sidecars
    were preserved. A missing or unparseable sidecar reads as None.
    """
    if not os.path.exists(sidecar):
        return None
    try:
        root = ET.parse(sidecar).getroot()
    except ET.ParseError:
        return None
    qname = _q('xmp', 'Label')
    for d in root.findall('.//' + _q('rdf', 'Description')):
        if qname in d.attrib:
            return d.attrib[qname] or None
        for child in d:
            if child.tag == qname:
                return (child.text or '').strip() or None
    return None


def write_sidecar(sidecar, fields, merge):
    """Write our XMP properties into `sidecar`.

    fields keys (any falsy value = leave unset): 'label', 'rights', 'creator',
    'description', 'usage_terms', 'keywords' (a list), plus the simple-text IPTC keys
    in _SIMPLE_IPTC ('headline','credit','source','city','state','country','location',
    'instructions','date_created').
    merge=False -> create-if-absent (an existing sidecar is left untouched).
    merge=True  -> set only our properties, preserving everything else (e.g.
                   Lightroom develop edits).
    Returns True if written. Raises SidecarParseError if an existing sidecar
    can't be parsed - we never clobber it.
    """
    if not any(fields.values()):
        return False
    exists = os.path.exists(sidecar)
    if exists and not merge:
        return False
    if exists:
        try:
            root = ET.parse(sidecar).getroot()
        except ET.ParseError as e:
            raise SidecarParseError('unparseable sidecar, left as-is (%s)' % e)
    else:
        root = ET.fromstring(_SKELETON)

    descs = root.findall('.//' + _q('rdf', 'Description'))
    if not descs:
        raise SidecarParseError('no rdf:Description, left as-is')
    target = descs[0]

    # Replace only the properties we own; never remove a label we aren't setting.
    if fields.get('label'):
        _clear_prop(descs, _q('xmp', 'Label'))
        _set_label(target, fields['label'])
    if fields.get('rights'):
        _clear_prop(descs, _q('dc', 'rights'))
        _set_alt(target, _q('dc', 'rights'), fields['rights'])
    if fields.get('creator'):
        _clear_prop(descs, _q('dc', 'creator'))
        _set_seq(target, _q('dc', 'creator'), fields['creator'])
    if fields.get('description'):
        _clear_prop(descs, _q('dc', 'description'))
        _set_alt(target, _q('dc', 'description'), fields['description'])
    if fields.get('usage_terms'):
        _clear_prop(descs, _q('xmpRights', 'UsageTerms'))   # lang-Alt, like dc:rights
        _set_alt(target, _q('xmpRights', 'UsageTerms'), fields['usage_terms'])
    for key, (prefix, local) in _SIMPLE_IPTC:
        if fields.get(key):
            q = _q(prefix, local)
            _clear_prop(descs, q)
            _set_simple(target, q, fields[key])
    if fields.get('keywords'):
        q = _q('dc', 'subject')
        _clear_prop(descs, q)
        _set_bag(target, q, fields['keywords'])

    body = ET.tostring(root, encoding='unicode')
    tmp = sidecar + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(_XPACKET_HEAD + body + _XPACKET_TAIL)
    os.replace(tmp, sidecar)
    return True


# ---------------------------------------------------------------------------
# Copy
# ---------------------------------------------------------------------------

CHUNK = 8 * 1024 * 1024


def copy_file(src, dest, on_chunk=None, date_delta=timedelta(0), target_offset=None):
    """Crash-safe copy: write to .partial, fsync, size-check, optionally correct the
    EXIF in place, atomic rename, then unlock.

    Calls on_chunk(nbytes) for progress. Returns bytes copied. The EXIF patch is a
    same-length overwrite, so the size check still holds and dest size still equals
    the card original. Patching the .partial *before* the rename keeps the invariant
    that a file at its final path is always fully corrected; an ExifPatchError removes
    the .partial and propagates (aborting the run) rather than stranding a bad file.
    """
    partial = dest + '.partial'
    total = 0
    with open(src, 'rb') as r, open(partial, 'wb') as w:
        while True:
            buf = r.read(CHUNK)
            if not buf:
                break
            w.write(buf)
            total += len(buf)
            if on_chunk:
                on_chunk(len(buf))
        w.flush()
        os.fsync(w.fileno())
    src_size = os.path.getsize(src)
    if total != src_size:
        os.remove(partial)
        raise OSError('size mismatch after copy (%d != %d)' % (total, src_size))
    if date_delta or target_offset is not None:
        try:
            shift_exif_in_place(partial, date_delta, target_offset)
        except ExifPatchError:
            os.remove(partial)
            raise
    os.rename(partial, dest)
    # Belt-and-suspenders: ensure the copy is unlocked and writable.
    try:
        os.chflags(dest, 0)
    except OSError:
        pass
    os.chmod(dest, os.stat(dest).st_mode | stat.S_IWUSR)
    return total


# ---------------------------------------------------------------------------
# Status display
# ---------------------------------------------------------------------------

def human_bytes(n):
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return '%.2f %s' % (n, unit)
        n /= 1024.0


def fmt_eta(seconds):
    seconds = int(seconds)
    return '%d:%02d' % (seconds // 60, seconds % 60)


class Progress:
    """Tracks copy progress and renders a single self-updating status line (TTY only)."""
    BAR_WIDTH = 24

    def __init__(self, total_files, total_bytes):
        self.total_files = total_files
        self.total_bytes = total_bytes
        self.done_files = 0
        self.done_bytes = 0
        self.start = time.time()

    def tick(self, nbytes):
        """Account for nbytes just written (a copy_file chunk callback)."""
        self.done_bytes += nbytes
        self._draw()

    def file_done(self):
        self.done_files += 1
        self._draw()

    def finish(self):
        if self.total_files and IS_TTY:
            sys.stdout.write('\n')

    @property
    def elapsed(self):
        return max(time.time() - self.start, 1e-6)

    @property
    def rate_mb(self):
        return self.done_bytes / self.elapsed / (1024 * 1024)

    def _draw(self):
        if not IS_TTY:
            return
        frac = (self.done_bytes / self.total_bytes) if self.total_bytes else 1.0
        filled = int(frac * self.BAR_WIDTH)
        bar = '#' * filled + '-' * (self.BAR_WIDTH - filled)
        rate = self.done_bytes / self.elapsed
        eta = fmt_eta((self.total_bytes - self.done_bytes) / rate) if rate > 0 else '?'
        sys.stdout.write('\r[%s] %d/%d  %s/%s  %.0f MB/s  ETA %s'
                         % (bar, self.done_files, self.total_files,
                            human_bytes(self.done_bytes), human_bytes(self.total_bytes),
                            rate / (1024 * 1024), eta))
        sys.stdout.flush()


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

@dataclass
class Frame:
    """One source file and its computed destination."""
    src: str
    size: int
    locked: bool
    dest: str
    captured: str          # corrected Exif datetime, e.g. '2026:05:26 14:00:24'
    date_delta: timedelta = timedelta(0)   # wall-clock shift to patch into the copy
    offset: str = None     # effective UTC offset ('±HH:MM') for IPTC DateCreated, or None

    @property
    def sidecar(self):
        return os.path.splitext(self.dest)[0] + '.xmp'


@dataclass
class Haul:
    """Plans and performs one ingest from a card into a destination directory."""
    card: str
    ext: str
    dest_dir: str
    filt: str
    dry_run: bool
    rewrite: bool
    config: dict
    template: dict          # None if no photohaul.json present
    profile: str            # active profile name (DEFAULT_PROFILE if none)
    time_shift: timedelta = timedelta(0)   # drift nudge (forced 0 under --rewrite)
    shot_tz: str = None     # normalized '±HH:MM' target, or None if no tz correction
    shot_tz_delta: timedelta = None         # parsed shot_tz, for deriving zone_delta

    frames: list = field(default_factory=list)      # frames passing the filter
    featured: int = 0                               # locked frames seen (pre-filter)
    errors: list = field(default_factory=list)
    to_copy: list = field(default_factory=list)
    to_skip: list = field(default_factory=list)
    conflicts: list = field(default_factory=list)
    _iptc: dict = field(default=None, init=False, repr=False)   # cached folder-constant fields

    # --- planning ------------------------------------------------------------

    def scan(self):
        """Pre-scan the card: read Exif + lock bit, compute names, apply the filter.

        Builds destination names purely from intrinsic metadata so re-runs are stable.
        On the (near-impossible, single-body) chance two frames map to one name, fall
        back to the intrinsic DSC file number rather than ever risk an overwrite.
        """
        if self.rewrite:
            return self.scan_dest()
        used_names = {}
        for src in scan_source(self.card, self.ext):
            st = os.stat(src)               # one stat for both lock bit and size
            locked = is_locked(st)
            if locked:
                self.featured += 1
            if self.filt == 'locked' and not locked:
                continue
            if self.filt == 'unlocked' and locked:
                continue
            try:
                dt, sub, rec_off = read_exif_datetime(src)
            except (ValueError, OSError) as e:
                self.errors.append('%s: cannot read Exif (%s)'
                                   % (os.path.basename(src), e))
                continue
            # Correct the wall-clock time before naming so the dest name, caption,
            # and {year} all derive from the corrected instant. A shot_tz with no
            # recorded offset to derive from is a hard error (aborts the run).
            date_delta = self._date_delta(src, rec_off)
            dt = shift_exif_datetime(dt, date_delta)
            base = base_name(dt, sub)
            if base in used_names:
                used_names[base] += 1
                base = '%s-%s' % (base, dsc_number(src))
            else:
                used_names[base] = 1
            dest = os.path.join(self.dest_dir, base + '.' + self.ext)
            # Effective offset is shot_tz's target when set (we restamp the tags to it),
            # else the camera's recorded offset (unchanged without shot_tz).
            offset = self.shot_tz if self.shot_tz is not None else rec_off
            self.frames.append(Frame(src, st.st_size, locked, dest, dt,
                                     date_delta=date_delta, offset=offset))

    def scan_dest(self):
        """Card-free scan for --rewrite: find already-copied files in the destination
        and read their Exif so rights/caption can be recomputed. The card is never
        touched; an existing Purple label is read only for reporting, never changed.
        """
        suffix = '.' + self.ext.lower()
        names = sorted(n for n in os.listdir(self.dest_dir)
                       if n.lower().endswith(suffix))
        for name in names:
            dest = os.path.join(self.dest_dir, name)
            try:
                dt, _sub, off = read_exif_datetime(dest)   # dest already carries corrected offset
            except (ValueError, OSError) as e:
                self.errors.append('%s: cannot read Exif (%s)' % (name, e))
                continue
            frame = Frame(dest, os.path.getsize(dest), False, dest, dt, offset=off)
            if read_label(frame.sidecar) == 'Purple':   # preserved as-is; reporting only
                frame.locked = True
                self.featured += 1
            self.frames.append(frame)

    def _date_delta(self, src, rec_off):
        """Total wall-clock shift for a frame: zone_delta (from shot_tz) + time_shift.

        zone_delta = shot_tz target - the frame's recorded OffsetTimeOriginal, so a
        shot_tz correction is instant-preserving. shot_tz with no recorded offset to
        derive from raises ExifPatchError.
        """
        zone_delta = timedelta(0)
        if self.shot_tz is not None:
            if rec_off is None:
                raise ExifPatchError(
                    '%s: shot_tz set but no recorded OffsetTimeOriginal to derive from'
                    % os.path.basename(src))
            zone_delta = self.shot_tz_delta - parse_tz(rec_off)
        return zone_delta + self.time_shift

    def classify(self):
        """Split planned frames into copy / skip (already present) / conflict."""
        for f in self.frames:
            if not os.path.exists(f.dest):
                self.to_copy.append(f)
            elif os.path.getsize(f.dest) == f.size:
                self.to_skip.append(f)
            else:
                self.conflicts.append(f)

    @property
    def copy_bytes(self):
        return sum(f.size for f in self.to_copy)

    # --- reporting -----------------------------------------------------------

    def report_plan(self):
        if self.rewrite:
            print('Rewrite: %s   Extension: .%s' % (self.dest_dir, self.ext))
            print('Files: %d (%d Purple preserved)' % (len(self.frames), self.featured))
        else:
            print('Card: %s   Extension: .%s   Filter: %s'
                  % (self.card, self.ext, self.filt))
            print('Featured: %d   Total: %d (%d to copy, %d skipped%s)'
                  % (self.featured, len(self.frames), len(self.to_copy), len(self.to_skip),
                     (', %d CONFLICT' % len(self.conflicts)) if self.conflicts else ''))
        if self.profile != DEFAULT_PROFILE:
            print('Profile: %s' % self.profile)
        tparts = []
        if self.shot_tz is not None:
            tparts.append('tz %s' % self.shot_tz)
        if self.time_shift:
            tparts.append('shift %s' % format_shift(self.time_shift))
        if tparts:
            print('Time: %s (corrects copied EXIF in place)' % ', '.join(tparts))
        meta = []
        if self.config.get('copyright') or self.config.get('creator'):
            meta.append('rights')
        if self.template is not None:
            meta.append('caption + IPTC')
        if meta:
            print('Metadata: %s%s' % (', '.join(meta),
                                      ' (rewrite/merge)' if self.rewrite else ''))
        if self.errors:
            print('  %d file(s) skipped - unreadable Exif:' % len(self.errors))
            for e in self.errors:
                print('    ! ' + e)
        for f in self.conflicts:
            print('  ! CONFLICT (different size, not overwriting): %s'
                  % os.path.basename(f.dest))

    # --- doing ---------------------------------------------------------------

    def copy(self):
        """Copy every to-copy frame, updating a live progress display."""
        progress = Progress(len(self.to_copy), self.copy_bytes)
        try:
            for f in self.to_copy:
                try:
                    copy_file(f.src, f.dest, on_chunk=progress.tick,
                              date_delta=f.date_delta, target_offset=self.shot_tz)
                except OSError as e:
                    self.errors.append('%s: copy failed (%s)'
                                       % (os.path.basename(f.dest), e))
                    continue
                progress.file_done()
        finally:
            # Always terminate the progress line - even if an ExifPatchError aborts the
            # run mid-copy - so the error message starts on its own line.
            progress.finish()
        return progress

    def fields_for(self, frame):
        """The sidecar properties this frame should carry, given config + template.

        Rights/creator come from ~/.photohaul; the caption and the structured IPTC
        fields come from the per-folder template (only when one is present, so personal
        shots with no photohaul.json stay minimal).
        """
        rights = None
        if self.config.get('copyright'):
            rights = self.config['copyright'].replace('{year}',
                                                       capture_year(frame.captured))
        # Per-frame fields (label, rights {year}, caption date) over the folder-constant
        # IPTC set, which is built once and cached.
        fields = {
            # Under --rewrite we never set a label: write_sidecar(merge) leaves any
            # existing Purple untouched, and we can't know lock status without the card.
            'label':       None if self.rewrite else ('Purple' if frame.locked else None),
            'rights':      rights,
            'creator':     self.config.get('creator') or None,
            'description': None,
        }
        if self.template is not None:
            fields['description'] = build_caption(self.template, ap_date(frame.captured),
                                                  self.config) or None
            # DateCreated is per-frame (capture time + offset), so it stays out of the
            # cached folder-constant _iptc set.
            fields['date_created'] = iso_datecreated(frame.captured, frame.offset)
            fields.update(self._iptc_fields())
        return fields

    def _iptc_fields(self):
        """Folder-constant IPTC structured fields from the template (with the config
        credit/creator as the photoshop:Credit fallback, matching the caption byline).

        Depends only on template + config, not the frame, so it is built once and cached.
        """
        if self._iptc is None:
            def g(key):
                v = self.template.get(key)
                return (v.strip() if isinstance(v, str) else '') or None
            credit = g('credit') or self.config.get('credit') or self.config.get('creator')
            self._iptc = {
                'headline':     self._headline(),
                'credit':       credit or None,
                'source':       g('source'),
                'city':         g('city'),
                'state':        g('state'),
                'country':      g('country'),
                'location':     g('venue'),     # Iptc4xmpCore:Location = sublocation
                'instructions': g('assignment'),
                'usage_terms':  g('rightsUsage'),
                'keywords':     self._keywords(),
            }
        return self._iptc

    def _headline(self):
        """'{homeShort} vs. {awayShort} {sport}', else the event, else None."""
        def g(key):
            v = self.template.get(key)
            return v.strip() if isinstance(v, str) else ''
        home, away, sport = g('homeShort'), g('awayShort'), g('sport')
        if home and away:
            base = '%s vs. %s' % (home, away)
            return ('%s %s' % (base, sport)) if sport else base
        return g('event') or None

    def _keywords(self):
        """Deterministic dc:subject Bag from the team/sport/conference keys (deduped)."""
        seen = []
        for key in ('sport', 'homeShort', 'awayShort', 'homeTeam', 'awayTeam', 'conference'):
            v = self.template.get(key)
            v = v.strip() if isinstance(v, str) else ''
            if v and v not in seen:
                seen.append(v)
        return seen or None

    def would_write(self, frame):
        """For a frame, whether a sidecar would be written and if it's a Purple one."""
        fields = self.fields_for(frame)
        if not any(fields.values()):
            return None
        if os.path.exists(frame.sidecar) and not self.rewrite:
            return None
        # In rewrite mode we never set a label, but report any preserved Purple.
        return frame.locked if self.rewrite else bool(fields['label'])

    def write_metadata(self):
        """Write sidecars (create-if-absent, or merge under --rewrite). Returns counts."""
        written = purple = 0
        for f in self.frames:
            if not os.path.exists(f.dest):
                continue   # copy failed earlier
            try:
                if write_sidecar(f.sidecar, self.fields_for(f), merge=self.rewrite):
                    written += 1
                    if f.locked:
                        purple += 1
            except SidecarParseError as e:
                self.errors.append('%s: %s' % (os.path.basename(f.sidecar), e))
        return written, purple

    # --- orchestration -------------------------------------------------------

    def run(self):
        self.scan()
        if not self.rewrite:
            self.classify()
        self.report_plan()

        if self.rewrite:
            return self.run_rewrite()

        if self.dry_run:
            planned = [self.would_write(f) for f in self.frames]
            wcount = sum(1 for p in planned if p is not None)
            pcount = sum(1 for p in planned if p)
            print('Dry run: would copy %s, write %d sidecars (%d Purple). '
                  'Nothing written.' % (human_bytes(self.copy_bytes), wcount, pcount))
            return self._exit_code()

        progress = self.copy()
        written, purple = self.write_metadata()
        print('Done: copied %d (%s in %s, %.0f MB/s), skipped %d. '
              'Sidecars: %d written (%d Purple).'
              % (progress.done_files, human_bytes(progress.done_bytes),
                 fmt_eta(progress.elapsed), progress.rate_mb,
                 len(self.to_skip), written, purple))
        if self.errors:
            print('  %d error(s):' % len(self.errors))
            for e in self.errors:
                print('    ! ' + e)
        return self._exit_code()

    def run_rewrite(self):
        """Metadata-only refresh of the destination: no card, no copying."""
        if self.dry_run:
            planned = [self.would_write(f) for f in self.frames]
            wcount = sum(1 for p in planned if p is not None)
            pcount = sum(1 for p in planned if p)
            print('Dry run: would write %d sidecars (%d Purple preserved). '
                  'Nothing written.' % (wcount, pcount))
            return self._exit_code()

        written, purple = self.write_metadata()
        print('Done: rewrote %d sidecars (%d Purple preserved).' % (written, purple))
        if self.errors:
            print('  %d error(s):' % len(self.errors))
            for e in self.errors:
                print('    ! ' + e)
        return self._exit_code()

    def _exit_code(self):
        return 1 if (self.errors or self.conflicts) else 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_parser():
    ap = argparse.ArgumentParser(
        prog='photohaul',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            'Ingest photos from a mounted camera card into the current folder.\n'
            '\n'
            'Each file is copied and renamed to a stable, millisecond-precise name\n'
            'from its Exif capture time (e.g. 20260526-140024_708.ext). Re-running\n'
            'skips files already present, so it is safe to repeat on the same card.\n'
            '\n'
            'Frames locked (protected) in-camera are detected, copied unlocked, and\n'
            'tagged with a Purple color label for Lightroom via an .xmp sidecar.\n'
            'Copyright/creator (from ~/.photohaul) and a per-folder template\n'
            '(photohaul.json) - an AP-style caption plus structured IPTC fields - are\n'
            'written to that sidecar too. The card is never modified; the copied raw\n'
            'is a byte-exact clone unless a capture-time\n'
            'correction (time_shift / shot_tz in photohaul.json) is set, which rewrites\n'
            "the copy's EXIF date/offset fields in place at the same byte length."),
        epilog=(
            'examples:\n'
            '  photohaul                    # use format= from ~/.photohaul\n'
            '  photohaul [format]           # or name it: arw Sony, cr3 Canon, nef Nikon, raf Fuji, jpg\n'
            '  photohaul --dry-run          # show what would happen, touch nothing\n'
            '  photohaul --locked           # only the featured (locked) frames\n'
            '  photohaul --init-template    # scaffold a blank photohaul.json here\n'
            '  photohaul --rewrite          # refresh copyright/caption sidecars (no card)\n'
            '  photohaul --profile personal # apply a named rights preset\n'
            '  photohaul --source /Volumes/Untitled --dest ~/Photos/game\n'
            '\n'
            'config file (~/.photohaul, optional, INI; [default] inherited by profiles):\n'
            '  [default]\n'
            '  format    = arw                    -> default format when none is given\n'
            '  creator   = Your Name              -> dc:creator\n'
            '  copyright = (c) {year} Your Name   -> dc:rights ({year} = capture year)\n'
            '  [work]\n'
            '  copyright = (c) {year} Your Name / site.com\n'
            '  credit    = Your Name/site.com     -> caption byline + photoshop:Credit\n'
            '\n'
            'profile: --profile NAME, else "profile" in photohaul.json, else [default].\n'
            '  A folder with no template stays on [default] (e.g. personal, unbranded).\n'
            '\n'
            'caption + IPTC template (photohaul.json in the destination, auto-detected):\n'
            '  keys: profile; sport, event, homeTeam, awayTeam, homeShort, awayShort,\n'
            '  venue, city, state, country, conference; credit, source, rightsUsage,\n'
            '  assignment (blanks omitted). Writes the AP-style caption plus structured\n'
            '  IPTC fields (Headline, Credit, Source, City/State/Country, Location,\n'
            '  Instructions, UsageTerms, keywords) editors and wire desks expect. The\n'
            '  per-image action sentence + player IDs stay a manual Lightroom pass.\n'
            '  --> "Lakeside vs. Riverside, NCAA women\'s volleyball match, at\n'
            '       Memorial Arena, Springfield, Calif. on Friday, Oct. 3, 2025.\n'
            '       (Photo by Your Name/site.com)"\n'
            '\n'
            'capture-time correction (photohaul.json, optional; corrects the copy only):\n'
            '  time_shift: "+2h30m"   drift fix - nudge the wall-clock time (units\n'
            '                         d/h/m/s, signed, whole seconds; offset tags kept).\n'
            '  shot_tz:    "-04:00"   travel fix - "shot at this UTC offset"; shifts the\n'
            '                         time to stay the same instant AND restamps the\n'
            '                         offset tags. shot_tz alone is the full travel fix.\n'
            '  Both compose. They drive the dest name, caption date, and an in-place\n'
            '  rewrite of the copied raw\'s EXIF date/offset bytes (same size). SET THEM\n'
            '  BEFORE the first ingest of a folder - changing them after files exist\n'
            '  makes new names (duplicates), not updates. --rewrite ignores both.\n'
            '\n'
            'notes:\n'
            '  - Exif is read natively: TIFF raws (ARW, NEF) and JPEG directly, Fuji\n'
            '    RAF from its embedded JPEG, Canon CR3 from its MP4-style moov box.\n'
            '  - The card is read-only here; it is never written to or modified.\n'
            '  - A same-named file of matching size is skipped; one of different size\n'
            '    is reported as a conflict and never overwritten.\n'
            '  - Sidecars are create-if-absent; --rewrite merges our fields into an\n'
            '    existing sidecar (copyright, creator, caption, IPTC fields) and\n'
            '    preserves everything else, e.g. Lightroom develop edits.\n'
            '  - --rewrite works on the destination only - no card needed and nothing\n'
            '    copied. A Purple label already in a sidecar is kept; it is never added\n'
            '    or removed (lock status is unknown without the card).'))
    ap.add_argument('extension', nargs='?', default=None, metavar='format',
                    help="raw/jpeg format to ingest (arw, cr3, nef, raf, jpg); overrides "
                         "'format' in ~/.photohaul. No built-in default.")
    ap.add_argument('--source', help='card root (default: auto-detect under /Volumes)')
    ap.add_argument('--dest', default='.', help='destination dir (default: cwd)')
    sel = ap.add_mutually_exclusive_group()
    sel.add_argument('--locked', action='store_const', const='locked', dest='filter',
                     help='only ingest locked/featured frames')
    sel.add_argument('--unlocked', action='store_const', const='unlocked', dest='filter',
                     help='only ingest non-locked frames')
    sel.add_argument('--all', action='store_const', const='all', dest='filter',
                     help='ingest everything (default)')
    ap.add_argument('-n', '--dry-run', action='store_true',
                    help='scan and report; touch nothing')
    ap.add_argument('--rewrite', action='store_true',
                    help='refresh sidecar metadata on already-copied files in the '
                         'destination; the card is not used and nothing is copied. '
                         'Merges our fields (rights, creator, caption, IPTC) into '
                         'existing sidecars and preserves the rest, including any Purple label')
    ap.add_argument('--init-template', action='store_true',
                    help='write a blank %s into the destination and exit' % TEMPLATE_NAME)
    ap.add_argument('--profile',
                    help='rights profile from ~/.photohaul (overrides the template); '
                         'default falls back to the "profile" key in %s' % TEMPLATE_NAME)
    ap.set_defaults(filter='all')
    return ap


def main():
    args = build_parser().parse_args()

    dest_dir = os.path.abspath(args.dest)

    if args.init_template:
        write_template(dest_dir)
        return 0

    if args.rewrite and args.filter != 'all':
        sys.exit("Error: --rewrite cannot be combined with --locked/--unlocked "
                 "(lock status isn't available without the card)")

    template = load_template(dest_dir)
    profile = args.profile or (template or {}).get('profile') or DEFAULT_PROFILE
    config = load_config(profile)

    # Capture-time corrections live only in photohaul.json (no CLI flag, so a forgotten
    # flag can't silently rename everything). Validated once here, before any copy.
    # --rewrite reads the already-corrected destination files, so it forces both off to
    # avoid double-shifting (it still uses the JSON for caption content).
    time_shift = timedelta(0)
    shot_tz = shot_tz_delta = None
    if not args.rewrite and template:
        ts = (template.get('time_shift') or '').strip()
        tz = (template.get('shot_tz') or '').strip()
        try:
            if ts:
                time_shift = parse_time_shift(ts)
            if tz:
                shot_tz_delta = parse_tz(tz)
                shot_tz = format_tz(shot_tz_delta)
        except ValueError as e:
            sys.exit('Error: %s in %s' % (e, os.path.join(dest_dir, TEMPLATE_NAME)))

    # Format: the positional overrides the config; there is no built-in default.
    ext = (args.extension or config.get('format') or '').lstrip('.').lower()
    if not ext:
        sys.exit("Error: no format given. Pass one (e.g. 'photohaul arw') or set "
                 "'format = arw' in ~/.photohaul.")

    if args.rewrite:
        # Metadata-only refresh of the destination; the card is not used.
        card = None
        if not os.path.isdir(dest_dir):
            sys.exit("Error: destination %s is not a directory" % dest_dir)
    else:
        card = find_card(args.source)
        if not args.dry_run and not os.path.isdir(dest_dir):
            sys.exit("Error: destination %s is not a directory" % dest_dir)
        if not scan_source(card, ext):
            sys.exit("Error: no .%s files found under %s/DCIM" % (ext, card))

    try:
        return Haul(card=card, ext=ext, dest_dir=dest_dir, filt=args.filter,
                    dry_run=args.dry_run, rewrite=args.rewrite,
                    config=config, template=template, profile=profile,
                    time_shift=time_shift, shot_tz=shot_tz,
                    shot_tz_delta=shot_tz_delta).run()
    except ExifPatchError as e:
        sys.exit('Error: capture-time correction failed: %s' % e)


if __name__ == '__main__':
    sys.exit(main())
