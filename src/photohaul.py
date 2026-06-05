#!/usr/bin/env python3
#
# photohaul - ingest photos from a mounted camera card.
#
# Copies *.ARW off the card into the current folder, renaming each to a stable
# YYYYMMDD-hhmmss_mmm.ext name derived from Exif (millisecond timestamp = unique key,
# so names are identical no matter which subset is copied -> safe partial/repeat runs).
# In-camera "locked" frames (the FAT read-only bit, surfaced on macOS as the uchg flag)
# are detected, copied unlocked, and marked Purple for Lightroom via an .xmp sidecar.
#
# Read-only on the source: the card is never modified.
# Zero dependencies: stdlib only.

import argparse
import os
import stat
import struct
import sys
import time
from dataclasses import dataclass, field

IS_TTY = sys.stdout.isatty()

# ---------------------------------------------------------------------------
# Exif reader (stdlib only) - just the two standard tags we need.
# ---------------------------------------------------------------------------

EXIF_IFD_PTR      = 0x8769
DATETIME_ORIGINAL = 0x9003
SUBSEC_ORIGINAL   = 0x9291
_TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}


def read_exif_datetime(path):
    """Return (datetime_str, subsec_str_or_None) from a TIFF-structured raw/jpeg.

    Reads only the header and the two relevant IFDs via seeks - never the whole file.
    Raises ValueError on anything it can't parse.
    """
    with open(path, 'rb') as f:
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
            f.seek(off)
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
                f.seek(o)
                data = f.read(size)
            return data.split(b'\x00')[0].decode('ascii', 'replace')

        e0 = read_ifd(ifd0)
        if EXIF_IFD_PTR not in e0:
            raise ValueError('no Exif IFD')
        (exif_off,) = struct.unpack(bo + 'I', e0[EXIF_IFD_PTR][2])
        ee = read_ifd(exif_off)
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
# XMP sidecar
# ---------------------------------------------------------------------------

XMP_TEMPLATE = (
    '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
    '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
    ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
    '  <rdf:Description rdf:about=""\n'
    '    xmlns:xmp="http://ns.adobe.com/xap/1.0/"\n'
    '    xmp:Label="%s"/>\n'
    ' </rdf:RDF>\n'
    '</x:xmpmeta>\n'
    '<?xpacket end="w"?>\n'
)


def write_sidecar(dest_raw, label):
    """Create BASENAME.xmp next to the raw, only if no sidecar exists yet.

    Never overwrites an existing sidecar - it may hold Lightroom develop edits.
    Returns True if written.
    """
    sidecar = os.path.splitext(dest_raw)[0] + '.xmp'
    if os.path.exists(sidecar):
        return False
    with open(sidecar, 'w', encoding='utf-8') as f:
        f.write(XMP_TEMPLATE % label)
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
            self.frames.append(Frame(src, os.path.getsize(src), locked, dest))

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
        print('Card: %s   Extension: .%s   Filter: %s'
              % (self.card, self.ext, self.filt))
        print('Featured: %d   Total: %d (%d to copy, %d skipped%s)'
              % (self.featured, len(self.frames), len(self.to_copy), len(self.to_skip),
                 (', %d CONFLICT' % len(self.conflicts)) if self.conflicts else ''))
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

    def mark_featured(self):
        """Tag locked frames Purple via sidecar (create-if-absent). Returns count."""
        marked = 0
        for f in self.frames:
            if f.locked and os.path.exists(f.dest) and write_sidecar(f.dest, 'Purple'):
                marked += 1
        return marked

    # --- orchestration -------------------------------------------------------

    def run(self):
        self.scan()
        self.classify()
        self.report_plan()

        if self.dry_run:
            marks = sum(1 for f in self.frames
                        if f.locked and not os.path.exists(f.sidecar))
            print('Dry run: would copy %s, mark %d Purple. Nothing written.'
                  % (human_bytes(self.copy_bytes), marks))
            return self._exit_code()

        progress = self.copy()
        marked = self.mark_featured()
        print('Done: copied %d (%s in %s, %.0f MB/s), skipped %d, marked %d Purple.'
              % (progress.done_files, human_bytes(progress.done_bytes),
                 fmt_eta(progress.elapsed), progress.rate_mb,
                 len(self.to_skip), marked))
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
            'from its Exif capture time (e.g. 20260526-140024_708.arw). Re-running\n'
            'skips files already present, so it is safe to repeat on the same card.\n'
            '\n'
            'Frames locked (protected) in-camera are detected, copied unlocked, and\n'
            'tagged with a Purple color label for Lightroom via an .xmp sidecar.\n'
            'The card is never modified.'),
        epilog=(
            'examples:\n'
            '  photohaul                    # copy all ARW from the card into cwd\n'
            '  photohaul --dry-run          # show what would happen, touch nothing\n'
            '  photohaul --locked           # only the featured (locked) frames\n'
            '  photohaul jpg                # ingest .JPG instead of .ARW\n'
            '  photohaul --source /Volumes/Untitled --dest ~/Photos/game\n'
            '\n'
            'notes:\n'
            '  - The card is read-only here; it is never written to or modified.\n'
            '  - A same-named file of matching size is skipped; one of different size\n'
            '    is reported as a conflict and never overwritten.\n'
            '  - A sidecar is only created if no .xmp exists yet, so existing edits\n'
            '    are never clobbered.'))
    ap.add_argument('extension', nargs='?', default='arw',
                    help='file extension to ingest (default: arw)')
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
    ap.set_defaults(filter='all')
    return ap


def main():
    args = build_parser().parse_args()

    ext = args.extension.lstrip('.')
    card = find_card(args.source)
    dest_dir = os.path.abspath(args.dest)
    if not args.dry_run and not os.path.isdir(dest_dir):
        sys.exit("Error: destination %s is not a directory" % dest_dir)
    if not scan_source(card, ext):
        sys.exit("Error: no .%s files found under %s/DCIM" % (ext, card))

    return Haul(card, ext, dest_dir, args.filter, args.dry_run).run()


if __name__ == '__main__':
    sys.exit(main())
