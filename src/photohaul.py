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
# Copyright/creator (from ~/.photohaul) and a per-folder caption template
# (photohaul.json) are written to the same sidecar. The raw is never modified, so its
# size always matches the card original and re-runs stay idempotent.
#
# Read-only on the source: the card is never modified.
# Zero dependencies: stdlib only.

import argparse
import calendar
import configparser
import json
import os
import stat
import struct
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

IS_TTY = sys.stdout.isatty()

# ---------------------------------------------------------------------------
# Exif reader (stdlib only) - pulls the capture timestamp (DateTimeOriginal +
# SubSecTimeOriginal) from TIFF raws (ARW/NEF), Fuji RAF, and Canon CR3.
# ---------------------------------------------------------------------------

EXIF_IFD_PTR      = 0x8769
DATETIME_ORIGINAL = 0x9003
SUBSEC_ORIGINAL   = 0x9291
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


def read_exif_datetime(path):
    """Return (datetime_str, subsec_str_or_None) from a TIFF/JPEG raw, Fuji RAF, or Canon CR3.

    Reads only the header and the relevant IFD(s) via seeks - never the whole file.
    Raises ValueError on anything it can't parse.
    """
    with open(path, 'rb') as f:
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

        def read_ifd(off):
            f.seek(base + off)
            (n,) = struct.unpack(bo + 'H', f.read(2))
            raw = f.read(n * 12)
            entries = {}
            for i in range(n):
                tag, typ, cnt = struct.unpack(bo + 'HHI', raw[i * 12:i * 12 + 8])
                entries[tag] = (typ, cnt, raw[i * 12 + 8:i * 12 + 12])
            return entries

        def ascii_val(entry):
            typ, cnt, valoff = entry
            size = _TYPE_SIZES.get(typ, 1) * cnt
            if size <= 4:
                data = valoff[:size]
            else:
                (o,) = struct.unpack(bo + 'I', valoff)
                f.seek(base + o)
                data = f.read(size)
            return data.split(b'\x00')[0].decode('ascii', 'replace')

        e0 = read_ifd(ifd0)
        if EXIF_IFD_PTR in e0:
            # ARW/NEF/RAF: the canonical DateTimeOriginal + SubSec live in the Exif
            # IFD (some bodies also copy the date into IFD0 - prefer the Exif IFD,
            # which is the one that also carries SubSecTimeOriginal).
            (exif_off,) = struct.unpack(bo + 'I', e0[EXIF_IFD_PTR][2])
            ee = read_ifd(exif_off)
        elif DATETIME_ORIGINAL in e0:
            ee = e0                            # CR3 CMT2: no Exif pointer; tags in this IFD
        else:
            raise ValueError('no Exif IFD')
        if DATETIME_ORIGINAL not in ee:
            raise ValueError('no DateTimeOriginal')
        dt = ascii_val(ee[DATETIME_ORIGINAL])
        sub = ascii_val(ee[SUBSEC_ORIGINAL]) if SUBSEC_ORIGINAL in ee else None
        return dt, sub


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

def is_locked(path):
    """True if the macOS immutable (uchg) flag is set - Sony's in-camera protect bit."""
    return bool(getattr(os.stat(path), 'st_flags', 0) & stat.UF_IMMUTABLE)


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
TEMPLATE_KEYS   = ['teamA', 'teamB', 'event', 'venue', 'location', 'credit']
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
    """Scaffold a blank photohaul.json (one field per line) into dest_dir."""
    if not os.path.isdir(dest_dir):
        sys.exit("Error: destination %s is not a directory" % dest_dir)
    path = os.path.join(dest_dir, TEMPLATE_NAME)
    if os.path.exists(path):
        sys.exit("Error: %s already exists; not overwriting." % path)
    keys = ['profile'] + TEMPLATE_KEYS
    lines = ['{']
    for i, key in enumerate(keys):
        tail = ',' if i < len(keys) - 1 else ''
        lines.append('  "%s": ""%s' % (key, tail))
    lines.append('}')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    print('Wrote blank caption template: %s' % path)


def capture_year(captured):
    """'2026:05:26 14:00:24' -> '2026'."""
    return captured.split(':', 1)[0]


def caption_date(captured):
    """'2026:05:26 14:00:24' -> 'May 26, 2026'."""
    y, m, d = (int(x) for x in captured.split(' ', 1)[0].split(':'))
    return '%s %d, %d' % (calendar.month_name[m], d, y)


def build_caption(template, date_str, config):
    """Assemble a caption from the template, omitting any blank field.

    Date attaches to the end of the context sentence; the 'Photo by' byline is
    only added when there is real context (so an empty template yields nothing).
    """
    def g(key):
        v = template.get(key)
        return v.strip() if isinstance(v, str) else ''

    parts = []
    if g('teamA') and g('teamB'):
        parts.append('%s vs %s' % (g('teamA'), g('teamB')))
    if g('event'):
        parts.append(g('event'))
    place = []
    if g('venue'):
        place.append('at ' + g('venue'))
    if g('location'):
        place.append(g('location'))
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
            out.append('Photo by %s.' % credit)
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
for _p, _u in {**XMP_NS, **_EXTRA_NS}.items():
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
    return '{%s}%s' % (XMP_NS[prefix], local)


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

    fields: {'label','rights','creator','description'} (falsy = leave unset).
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


def copy_file(src, dest, on_chunk=None):
    """Crash-safe copy: write to .partial, fsync, atomic rename, then unlock.

    Calls on_chunk(nbytes) for progress. Returns bytes copied.
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
        raise IOError('size mismatch after copy (%d != %d)' % (total, src_size))
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
    captured: str          # Exif datetime, e.g. '2026:05:26 14:00:24'

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

    frames: list = field(default_factory=list)      # frames passing the filter
    featured: int = 0                               # locked frames seen (pre-filter)
    errors: list = field(default_factory=list)
    to_copy: list = field(default_factory=list)
    to_skip: list = field(default_factory=list)
    conflicts: list = field(default_factory=list)

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
            locked = is_locked(src)
            if locked:
                self.featured += 1
            if self.filt == 'locked' and not locked:
                continue
            if self.filt == 'unlocked' and locked:
                continue
            try:
                dt, sub = read_exif_datetime(src)
                base = base_name(dt, sub)
            except (ValueError, OSError) as e:
                self.errors.append('%s: cannot read Exif (%s)'
                                   % (os.path.basename(src), e))
                continue
            if base in used_names:
                used_names[base] += 1
                base = '%s-%s' % (base, dsc_number(src))
            else:
                used_names[base] = 1
            dest = os.path.join(self.dest_dir, base + '.' + self.ext)
            self.frames.append(Frame(src, os.path.getsize(src), locked, dest, dt))

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
                dt, _sub = read_exif_datetime(dest)
            except (ValueError, OSError) as e:
                self.errors.append('%s: cannot read Exif (%s)' % (name, e))
                continue
            frame = Frame(dest, os.path.getsize(dest), False, dest, dt)
            if read_label(frame.sidecar) == 'Purple':   # preserved as-is; reporting only
                frame.locked = True
                self.featured += 1
            self.frames.append(frame)

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
        meta = []
        if self.config.get('copyright') or self.config.get('creator'):
            meta.append('rights')
        if self.template is not None:
            meta.append('caption')
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
        for f in self.to_copy:
            try:
                copy_file(f.src, f.dest, on_chunk=progress.tick)
            except (OSError, IOError) as e:
                self.errors.append('%s: copy failed (%s)'
                                   % (os.path.basename(f.dest), e))
                continue
            progress.file_done()
        progress.finish()
        return progress

    def fields_for(self, frame):
        """The sidecar properties this frame should carry, given config + template."""
        rights = None
        if self.config.get('copyright'):
            rights = self.config['copyright'].replace('{year}',
                                                       capture_year(frame.captured))
        description = None
        if self.template is not None:
            description = build_caption(self.template, caption_date(frame.captured),
                                        self.config) or None
        return {
            # Under --rewrite we never set a label: write_sidecar(merge) leaves any
            # existing Purple untouched, and we can't know lock status without the card.
            'label':       None if self.rewrite else ('Purple' if frame.locked else None),
            'rights':      rights,
            'creator':     self.config.get('creator') or None,
            'description': description,
        }

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
            'Copyright/creator (from ~/.photohaul) and a per-folder caption template\n'
            '(photohaul.json) are written to that sidecar too. The card is never\n'
            'modified, and the raw stays a byte-exact clone of the card original.'),
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
            '  credit    = Your Name/site.com     -> default caption byline\n'
            '\n'
            'profile: --profile NAME, else "profile" in photohaul.json, else [default].\n'
            '  A folder with no template stays on [default] (e.g. personal, unbranded).\n'
            '\n'
            'caption template (photohaul.json in the destination, auto-detected):\n'
            '  keys: profile, teamA, teamB, event, venue, location, credit (blanks\n'
            '  omitted from the caption).\n'
            '  --> "Team A vs Team B, Event, at Venue, City, ST on May 30, 2026.\n'
            '       Photo by Your Name/site.com."\n'
            '\n'
            'notes:\n'
            '  - Exif is read natively: TIFF raws (ARW, NEF) and JPEG directly, Fuji\n'
            '    RAF from its embedded JPEG, Canon CR3 from its MP4-style moov box.\n'
            '  - The card is read-only here; it is never written to or modified.\n'
            '  - A same-named file of matching size is skipped; one of different size\n'
            '    is reported as a conflict and never overwritten.\n'
            '  - Sidecars are create-if-absent; --rewrite merges our fields into an\n'
            '    existing sidecar (copyright, creator, caption) and preserves\n'
            '    everything else, e.g. Lightroom develop edits.\n'
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
                         'Merges our fields (rights, creator, caption) into existing '
                         'sidecars and preserves the rest, including any Purple label')
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

    return Haul(card=card, ext=ext, dest_dir=dest_dir, filt=args.filter,
                dry_run=args.dry_run, rewrite=args.rewrite,
                config=config, template=template,
                profile=profile).run()


if __name__ == '__main__':
    sys.exit(main())
