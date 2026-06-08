#!/usr/bin/env python3
#
# Tests for photohaul (stdlib unittest, zero dependencies).
#
#   python3 -m unittest discover -s tests        # or: tests/test_photohaul.py
#
# No giant raws are committed. The EXIF reader is exercised against minimal TIFF/JPEG
# fixtures built in-memory (build_tiff / build_jpeg) - photohaul reads headers only, so a
# few hundred bytes with the right IFD entries is a complete fixture, and it lets us hit
# the failure paths real files can't easily produce. A handful of skip-unless tests read
# the real files in samples/ (gitignored) for format fidelity when they happen to be there.

import contextlib
import io
import os
import stat
import struct
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))
SAMPLES = os.path.join(ROOT, 'samples')

import photohaul as ph  # noqa: E402


# ---------------------------------------------------------------------------
# Synthetic Exif fixtures (no files committed)
# ---------------------------------------------------------------------------

def build_tiff(bo='<', date='2026:05:26 14:00:24', subsec='708', offset='+09:00',
               datetime_ifd0=None):
    """A minimal valid Exif TIFF as bytes: header | IFD0 (-> Exif IFD) | Exif IFD |
    overflow. `bo` is '<' (II) or '>' (MM). Pass date=None to omit DateTimeOriginal,
    subsec/offset=None to omit those tags, datetime_ifd0 to add IFD0's DateTime (0x0132).
    ASCII values <=4 bytes go inline; larger ones into the overflow region.
    """
    def az(s):
        return s.encode('ascii') + b'\x00'

    exif = []
    if date is not None:
        exif.append((ph.DATETIME_ORIGINAL, az(date)))
    if offset is not None:
        exif.append((ph.OFFSET_TIME_ORIGINAL, az(offset)))
    if subsec is not None:
        exif.append((ph.SUBSEC_ORIGINAL, az(subsec)))
    exif.sort(key=lambda e: e[0])

    ifd0_extra = []
    if datetime_ifd0 is not None:
        ifd0_extra.append((ph.DATETIME, az(datetime_ifd0)))

    ifd0_count = len(ifd0_extra) + 1            # + EXIF_IFD_PTR
    ifd0_size = 2 + 12 * ifd0_count + 4
    exif_offset = 8 + ifd0_size
    exif_size = 2 + 12 * len(exif) + 4
    overflow_base = exif_offset + exif_size
    overflow = bytearray()

    def vfield(payload):
        if len(payload) <= 4:
            return payload.ljust(4, b'\x00')
        off = overflow_base + len(overflow)
        overflow.extend(payload)
        return struct.pack(bo + 'I', off)

    def entry(tag, typ, count, vf):
        return struct.pack(bo + 'HHI', tag, typ, count) + vf

    ifd0_items = [(tag, 2, len(p), vfield(p)) for tag, p in ifd0_extra]
    ifd0_items.append((ph.EXIF_IFD_PTR, 4, 1, struct.pack(bo + 'I', exif_offset)))
    ifd0_items.sort(key=lambda e: e[0])
    ifd0_bytes = struct.pack(bo + 'H', ifd0_count)
    for tag, typ, count, vf in ifd0_items:
        ifd0_bytes += entry(tag, typ, count, vf)
    ifd0_bytes += struct.pack(bo + 'I', 0)

    exif_bytes = struct.pack(bo + 'H', len(exif))
    for tag, p in exif:
        exif_bytes += entry(tag, 2, len(p), vfield(p))
    exif_bytes += struct.pack(bo + 'I', 0)

    header = (b'II' if bo == '<' else b'MM') + struct.pack(bo + 'H', 42) + struct.pack(bo + 'I', 8)
    return header + ifd0_bytes + exif_bytes + bytes(overflow)


def build_jpeg(**kw):
    """Minimal JPEG carrying the same Exif TIFF in an APP1/Exif segment."""
    app1 = b'Exif\x00\x00' + build_tiff(**kw)
    return b'\xff\xd8\xff\xe1' + struct.pack('>H', len(app1) + 2) + app1


def rb(path):
    with open(path, 'rb') as f:
        return f.read()


def rt(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil
        # clear any immutable (uchg) flags a lock test set, so rmtree can remove them
        if hasattr(os, 'chflags'):
            for root, _dirs, files in os.walk(self.tmp):
                for n in files:
                    try:
                        os.chflags(os.path.join(root, n), 0)
                    except OSError:
                        pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, data):
        path = os.path.join(self.tmp, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        mode = 'wb' if isinstance(data, (bytes, bytearray)) else 'w'
        with open(path, mode) as f:
            f.write(data)
        return path


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------

class TestNaming(unittest.TestCase):
    def test_base_name_with_subsec(self):
        self.assertEqual(ph.base_name('2026:05:26 14:00:24', '708'), '20260526-140024_708')

    def test_base_name_subsec_padded_as_fraction(self):
        # subsec is a fraction of a second: '5' -> 500 ms.
        self.assertEqual(ph.base_name('2026:05:26 14:00:24', '5'), '20260526-140024_500')
        self.assertEqual(ph.base_name('2026:05:26 14:00:24', '70'), '20260526-140024_700')

    def test_base_name_no_subsec(self):
        self.assertEqual(ph.base_name('2026:05:26 14:00:24', ''), '20260526-140024')
        self.assertEqual(ph.base_name('2026:05:26 14:00:24', None), '20260526-140024')

    def test_dsc_number(self):
        self.assertEqual(ph.dsc_number('A1_02660.ARW'), '02660')
        self.assertEqual(ph.dsc_number('DSC09749.ARW'), '09749')
        self.assertEqual(ph.dsc_number('noplaceholder.arw'), '0')

    def test_stamp_re(self):
        for ok in ('20260526-140024', '20260526-140024_708', '20260526-140024_708-02660'):
            self.assertRegex(ok, ph._STAMP_RE)
        for bad in ('DSC09749', '2026-05-26', '20260526-1400'):
            self.assertNotRegex(bad, ph._STAMP_RE)


# ---------------------------------------------------------------------------
# Capture-time math
# ---------------------------------------------------------------------------

class TestCaptureTime(unittest.TestCase):
    def test_parse_time_shift(self):
        self.assertEqual(ph.parse_time_shift('+2h30m'), timedelta(hours=2, minutes=30))
        self.assertEqual(ph.parse_time_shift('-15s'), timedelta(seconds=-15))
        self.assertEqual(ph.parse_time_shift('90m'), timedelta(minutes=90))
        with self.assertRaises(ValueError):
            ph.parse_time_shift('soon')

    def test_parse_tz(self):
        self.assertEqual(ph.parse_tz('-04:00'), timedelta(hours=-4))
        self.assertEqual(ph.parse_tz('+05:30'), timedelta(hours=5, minutes=30))
        for bad in ('-4:00', '+15:00', 'Z', '-04:99'):
            with self.assertRaises(ValueError):
                ph.parse_tz(bad)

    def test_format_tz(self):
        self.assertEqual(ph.format_tz(timedelta(hours=-4)), '-04:00')
        self.assertEqual(ph.format_tz(timedelta(hours=5, minutes=30)), '+05:30')
        self.assertEqual(ph.format_tz(timedelta(0)), '+00:00')

    def test_format_shift(self):
        self.assertEqual(ph.format_shift(timedelta(hours=2, minutes=30)), '+2h30m')
        self.assertEqual(ph.format_shift(timedelta(seconds=-15)), '-15s')
        self.assertEqual(ph.format_shift(timedelta(0)), '+0s')

    def test_shift_exif_datetime(self):
        self.assertEqual(ph.shift_exif_datetime('2026:05:30 12:12:56', timedelta(hours=1)),
                         '2026:05:30 13:12:56')
        # rollover across midnight/month
        self.assertEqual(ph.shift_exif_datetime('2026:05:31 23:30:00', timedelta(minutes=45)),
                         '2026:06:01 00:15:00')
        with self.assertRaises(ValueError):
            ph.shift_exif_datetime('0000:00:00 00:00:00', timedelta(hours=1))

    def test_capture_year(self):
        self.assertEqual(ph.capture_year('2026:05:26 14:00:24'), '2026')

    def test_iso_datecreated(self):
        self.assertEqual(ph.iso_datecreated('2025:10:03 19:10:25', '-07:00'),
                         '2025-10-03T19:10:25-07:00')
        self.assertEqual(ph.iso_datecreated('2025:10:03 19:10:25', None),
                         '2025-10-03T19:10:25')


# ---------------------------------------------------------------------------
# AP style
# ---------------------------------------------------------------------------

class TestApStyle(unittest.TestCase):
    def test_ap_date(self):
        self.assertEqual(ph.ap_date('2025:10:03 14:00:24'), 'Friday, Oct. 3, 2025')

    def test_ap_date_months_spelled_vs_abbreviated(self):
        self.assertEqual(ph.ap_date('2025:03:05 00:00:00').split(', ')[1], 'March 5')
        self.assertEqual(ph.ap_date('2025:07:01 00:00:00').split(', ')[1], 'July 1')
        self.assertEqual(ph.ap_date('2025:01:15 00:00:00').split(', ')[1], 'Jan. 15')
        self.assertEqual(ph.ap_date('2025:09:30 00:00:00').split(', ')[1], 'Sept. 30')

    def test_ap_state(self):
        self.assertEqual(ph._ap_state('California'), 'Calif.')
        self.assertEqual(ph._ap_state('Ohio'), 'Ohio')          # never abbreviated
        self.assertEqual(ph._ap_state('Freedonia'), 'Freedonia')  # unknown -> passthrough


# ---------------------------------------------------------------------------
# Caption builder
# ---------------------------------------------------------------------------

class TestCaption(unittest.TestCase):
    def test_full_caption(self):
        template = {'homeShort': "Lakeside", 'awayShort': 'Riverside',
                    'event': "NCAA women's volleyball match", 'venue': 'Memorial Arena',
                    'city': 'Springfield', 'state': 'California'}
        out = ph.build_caption(template, 'Friday, Oct. 3, 2025', {'credit': 'Me/site.com'})
        self.assertEqual(
            out,
            "Lakeside vs. Riverside, NCAA women's volleyball match, at Memorial Arena, "
            "Springfield, Calif. on Friday, Oct. 3, 2025. (Photo by Me/site.com)")

    def test_credit_falls_back_to_creator(self):
        out = ph.build_caption({'event': 'Scrimmage'}, 'Mon', {'creator': 'Jane'})
        self.assertEqual(out, 'Scrimmage on Mon. (Photo by Jane)')

    def test_empty_template_is_blank(self):
        self.assertEqual(ph.build_caption({}, 'Mon', {'creator': 'Jane'}), '')


# ---------------------------------------------------------------------------
# XMP sidecar
# ---------------------------------------------------------------------------

CRS_SIDECAR = (
    '<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
    '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
    'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
    '<rdf:Description rdf:about="" '
    'xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/" '
    'crs:Exposure2012="+0.85" crs:Contrast2012="+22"/>'
    '</rdf:RDF></x:xmpmeta>\n<?xpacket end="w"?>\n')

LABEL_SIDECAR = (
    '<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
    '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
    'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
    '<rdf:Description rdf:about="" xmlns:xmp="http://ns.adobe.com/xap/1.0/" '
    'xmp:Label="Purple"/></rdf:RDF></x:xmpmeta>\n<?xpacket end="w"?>\n')


class TestSidecar(TempCase):
    def test_create_if_absent(self):
        path = os.path.join(self.tmp, 'a.xmp')
        self.assertTrue(ph.write_sidecar(path, {'rights': '© 2026 Me', 'creator': 'Me',
                                                'label': 'Purple'}, merge=False))
        self.assertTrue(os.path.exists(path))
        body = rt(path)
        self.assertIn('© 2026 Me', body)
        self.assertIn('Me', body)
        self.assertEqual(ph.read_label(path), 'Purple')

    def test_create_if_absent_leaves_existing_untouched(self):
        path = self.write('b.xmp', CRS_SIDECAR)
        before = rt(path)
        self.assertFalse(ph.write_sidecar(path, {'rights': 'new'}, merge=False))
        self.assertEqual(rt(path), before)  # byte-identical

    def test_merge_only_never_creates(self):
        # The Lightroom-revert fix: --rewrite (merge=True) must not create a sidecar.
        path = os.path.join(self.tmp, 'c.xmp')
        self.assertFalse(ph.write_sidecar(path, {'rights': 'x'}, merge=True))
        self.assertFalse(os.path.exists(path))

    def test_merge_preserves_develop_edits(self):
        path = self.write('d.xmp', CRS_SIDECAR)
        self.assertTrue(ph.write_sidecar(path, {'rights': '© 2026 Me'}, merge=True))
        body = rt(path)
        self.assertIn('Exposure2012', body)     # crs develop edits preserved
        self.assertIn('+0.85', body)
        self.assertIn('© 2026 Me', body)         # our field merged in

    def test_unparseable_sidecar_raises(self):
        path = self.write('e.xmp', 'this is not xml <<<')
        with self.assertRaises(ph.SidecarParseError):
            ph.write_sidecar(path, {'rights': 'x'}, merge=True)

    def test_no_fields_writes_nothing(self):
        path = os.path.join(self.tmp, 'f.xmp')
        self.assertFalse(ph.write_sidecar(path, {'rights': None, 'creator': ''}, merge=False))
        self.assertFalse(os.path.exists(path))


# ---------------------------------------------------------------------------
# XMP structure (parse the sidecar, assert the actual RDF shapes Lightroom reads)
# ---------------------------------------------------------------------------

class TestSidecarStructure(TempCase):
    FIELDS = {
        'label': 'Purple', 'rights': '(c) 2026 Me', 'creator': 'Jane Roe',
        'description': 'Caption text.', 'usage_terms': 'Editorial use only.',
        'keywords': ['volleyball', 'Riverside', 'PCC'],
        'headline': 'Home vs Away', 'credit': 'Jane Roe/site.com', 'source': 'site.com',
        'city': 'Springfield', 'state': 'California', 'country': 'USA',
        'location': 'Memorial Arena', 'instructions': 'Embargoed',
        'date_created': '2026-05-26T14:00:24-04:00',
    }

    def _desc(self):
        path = os.path.join(self.tmp, 's.xmp')
        ph.write_sidecar(path, self.FIELDS, merge=False)
        return path, ET.parse(path).getroot().find('.//' + ph._q('rdf', 'Description'))

    def _q(self, *pairs):
        return '/'.join(ph._q(p, l) for p, l in pairs)

    def test_creator_is_rdf_seq(self):
        _p, d = self._desc()
        li = d.find(self._q(('dc', 'creator'), ('rdf', 'Seq'), ('rdf', 'li')))
        self.assertIsNotNone(li)
        self.assertEqual(li.text, 'Jane Roe')

    def test_lang_alt_fields(self):
        _p, d = self._desc()
        for prefix, local, val in (('dc', 'rights', '(c) 2026 Me'),
                                   ('dc', 'description', 'Caption text.'),
                                   ('xmpRights', 'UsageTerms', 'Editorial use only.')):
            li = d.find(self._q((prefix, local), ('rdf', 'Alt'), ('rdf', 'li')))
            self.assertIsNotNone(li, '%s:%s missing' % (prefix, local))
            self.assertEqual(li.text, val)
            self.assertEqual(li.get(ph._XML_LANG), 'x-default')

    def test_keywords_are_rdf_bag(self):
        _p, d = self._desc()
        lis = d.findall(self._q(('dc', 'subject'), ('rdf', 'Bag'), ('rdf', 'li')))
        self.assertEqual([li.text for li in lis], ['volleyball', 'Riverside', 'PCC'])

    def test_label_and_simple_iptc_qnames(self):
        _p, d = self._desc()
        self.assertEqual(d.find(ph._q('xmp', 'Label')).text, 'Purple')
        expect = {
            ('photoshop', 'Headline'): 'Home vs Away',
            ('photoshop', 'Credit'): 'Jane Roe/site.com',
            ('photoshop', 'Source'): 'site.com',
            ('photoshop', 'City'): 'Springfield',
            ('photoshop', 'State'): 'California',          # full name; AP-abbrev is caption-only
            ('photoshop', 'Country'): 'USA',
            ('Iptc4xmpCore', 'Location'): 'Memorial Arena',
            ('photoshop', 'Instructions'): 'Embargoed',
            ('photoshop', 'DateCreated'): '2026-05-26T14:00:24-04:00',
        }
        for (prefix, local), val in expect.items():
            el = d.find(ph._q(prefix, local))
            self.assertIsNotNone(el, '%s:%s missing' % (prefix, local))
            self.assertEqual(el.text, val)

    def test_merge_replaces_not_duplicates(self):
        path, _d = self._desc()
        ph.write_sidecar(path, {'rights': '(c) 2027 Me'}, merge=True)
        root = ET.parse(path).getroot()
        rights = root.findall('.//' + ph._q('dc', 'rights'))
        self.assertEqual(len(rights), 1)                  # replaced in place, not duplicated
        li = rights[0].find(self._q(('rdf', 'Alt'), ('rdf', 'li')))
        self.assertEqual(li.text, '(c) 2027 Me')


# ---------------------------------------------------------------------------
# EXIF reader (synthetic fixtures)
# ---------------------------------------------------------------------------

class TestExifReader(TempCase):
    def test_reads_tiff(self):
        path = self.write('x.arw', build_tiff())
        self.assertEqual(ph.read_exif_datetime(path), ('2026:05:26 14:00:24', '708', '+09:00'))

    def test_big_endian(self):
        path = self.write('mm.arw', build_tiff(bo='>'))
        self.assertEqual(ph.read_exif_datetime(path), ('2026:05:26 14:00:24', '708', '+09:00'))

    def test_missing_subsec_and_offset(self):
        path = self.write('y.arw', build_tiff(subsec=None, offset=None))
        self.assertEqual(ph.read_exif_datetime(path), ('2026:05:26 14:00:24', None, None))

    def test_reads_jpeg(self):
        path = self.write('z.jpg', build_jpeg())
        self.assertEqual(ph.read_exif_datetime(path), ('2026:05:26 14:00:24', '708', '+09:00'))

    def test_malformed_date_is_returned_raw(self):
        # The reader doesn't validate; downstream (shift_exif_datetime) does.
        path = self.write('bad.arw', build_tiff(date='0000:00:00 00:00:00'))
        dt, _sub, _off = ph.read_exif_datetime(path)
        self.assertEqual(dt, '0000:00:00 00:00:00')

    def test_missing_datetimeoriginal_raises(self):
        path = self.write('nodate.arw', build_tiff(date=None, subsec=None, offset='+09:00'))
        with self.assertRaises(ValueError):
            ph.read_exif_datetime(path)

    def test_not_a_tiff_raises(self):
        path = self.write('junk.arw', b'this is not a tiff header at all')
        with self.assertRaises(ValueError):
            ph.read_exif_datetime(path)


# ---------------------------------------------------------------------------
# In-place EXIF patch (capture-time correction)
# ---------------------------------------------------------------------------

class TestExifPatch(TempCase):
    def test_shift_date_and_offset_same_size(self):
        path = self.write('p.arw', build_tiff(datetime_ifd0='2026:05:26 14:00:24'))
        before = os.path.getsize(path)
        patched = ph.shift_exif_in_place(path, timedelta(hours=1), '+10:00')
        self.assertEqual(os.path.getsize(path), before)   # byte-length unchanged
        self.assertIn('DateTimeOriginal', patched)
        self.assertIn('OffsetTimeOriginal', patched)
        self.assertIn('DateTime', patched)                # IFD0 0x0132 too
        dt, _sub, off = ph.read_exif_datetime(path)
        self.assertEqual(dt, '2026:05:26 15:00:24')
        self.assertEqual(off, '+10:00')

    def test_shift_date_only_leaves_offset(self):
        path = self.write('q.arw', build_tiff())
        ph.shift_exif_in_place(path, timedelta(hours=-2), None)
        dt, _sub, off = ph.read_exif_datetime(path)
        self.assertEqual(dt, '2026:05:26 12:00:24')
        self.assertEqual(off, '+09:00')                   # untouched


# ---------------------------------------------------------------------------
# Crash-safe copy
# ---------------------------------------------------------------------------

class TestCopyFile(TempCase):
    def test_byte_exact_copy(self):
        src = self.write('src.arw', build_tiff())
        dest = os.path.join(self.tmp, 'dest.arw')
        n = ph.copy_file(src, dest)
        self.assertEqual(n, os.path.getsize(src))
        self.assertEqual(rb(dest), rb(src))

    def test_no_clobber_guard(self):
        src = self.write('src2.arw', build_tiff())
        dest = self.write('dest2.arw', b'i was here first')
        with self.assertRaises(OSError):
            ph.copy_file(src, dest)
        self.assertEqual(rb(dest), b'i was here first')   # not overwritten
        self.assertFalse(os.path.exists(dest + '.partial'))             # cleaned up

    def test_copy_with_capture_time_patch(self):
        src = self.write('src3.arw', build_tiff())
        dest = os.path.join(self.tmp, 'dest3.arw')
        ph.copy_file(src, dest, date_delta=timedelta(hours=1), target_offset='+10:00')
        self.assertEqual(os.path.getsize(dest), os.path.getsize(src))   # same byte length
        dt, _s, off = ph.read_exif_datetime(dest)
        self.assertEqual((dt, off), ('2026:05:26 15:00:24', '+10:00'))


# ---------------------------------------------------------------------------
# Haul integration (synthetic card / dest)
# ---------------------------------------------------------------------------

CONFIG = {'copyright': '(c) {year} Test', 'creator': 'Tester'}


def silent(fn, *a, **k):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*a, **k)


def capture(fn, *a, **k):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*a, **k)
    return buf.getvalue()


class TestHaulCardMode(TempCase):
    def _card(self, name='TEST0001.ARW', **tiff):
        card = os.path.join(self.tmp, 'card')
        self.write(os.path.join('card', 'DCIM', '100MSDCF', name), build_tiff(**tiff))
        return card

    def _haul(self, card, dest, **kw):
        return ph.Haul(card=card, ext='arw', dest_dir=dest, filt='all', dry_run=False,
                       rewrite=False, config=CONFIG, template=None, profile='default', **kw)

    def test_copy_names_and_sidecar(self):
        card = self._card()
        dest = os.path.join(self.tmp, 'dest'); os.makedirs(dest)
        rc = silent(self._haul(card, dest).run)
        self.assertEqual(rc, 0)
        arw = os.path.join(dest, '20260526-140024_708.arw')
        xmp = os.path.join(dest, '20260526-140024_708.xmp')
        self.assertTrue(os.path.exists(arw))
        self.assertTrue(os.path.exists(xmp))
        # byte-exact clone of the card original
        self.assertEqual(rb(arw),
                         rb(os.path.join(card, 'DCIM', '100MSDCF', 'TEST0001.ARW')))
        body = rt(xmp)
        self.assertIn('(c) 2026 Test', body)   # {year} from capture time
        self.assertIn('Tester', body)

    def test_rerun_is_idempotent(self):
        card = self._card()
        dest = os.path.join(self.tmp, 'dest'); os.makedirs(dest)
        silent(self._haul(card, dest).run)
        h2 = self._haul(card, dest)
        silent(h2.run)
        self.assertEqual(len(h2.to_copy), 0)
        self.assertEqual(len(h2.to_skip), 1)

    def test_different_file_at_name_is_imported_under_suffix(self):
        # A different file already occupying the timestamp name is never overwritten;
        # the new frame is imported under the intrinsic -dscnumber suffix instead.
        card = self._card()
        dest = os.path.join(self.tmp, 'dest'); os.makedirs(dest)
        self.write(os.path.join('dest', '20260526-140024_708.arw'), b'different bytes')
        h = self._haul(card, dest)
        silent(h.run)
        self.assertEqual(rb(os.path.join(dest, '20260526-140024_708.arw')),
                         b'different bytes')                                # untouched
        self.assertTrue(os.path.exists(os.path.join(dest, '20260526-140024_708-0001.arw')))
        self.assertEqual(len(h.conflicts), 0)

    def test_capture_time_correction_end_to_end(self):
        card = self._card()
        dest = os.path.join(self.tmp, 'dest'); os.makedirs(dest)
        h = self._haul(card, dest, time_shift=timedelta(hours=1))
        silent(h.run)
        # shifted name + shifted EXIF in the copy
        arw = os.path.join(dest, '20260526-150024_708.arw')
        self.assertTrue(os.path.exists(arw))
        self.assertEqual(ph.read_exif_datetime(arw)[0], '2026:05:26 15:00:24')


class TestHaulCollision(TempCase):
    # Two *different* photos that share an exact timestamp (e.g. a no-subsec burst, or
    # two cards). Same EXIF time+subsec, different sizes (trailing bytes are ignored by
    # the header-only reader).
    A = build_tiff(date='2026:05:26 14:00:24', subsec='708')
    B = build_tiff(date='2026:05:26 14:00:24', subsec='708') + b'\x00' * 4096

    def _card(self):
        card = os.path.join(self.tmp, 'card')
        self.write(os.path.join('card', 'DCIM', '100MSDCF', 'DSC00001.ARW'), self.A)
        self.write(os.path.join('card', 'DCIM', '100MSDCF', 'DSC00002.ARW'), self.B)
        return card

    def _haul(self, card, dest):
        return ph.Haul(card=card, ext='arw', dest_dir=dest, filt='all', dry_run=False,
                       rewrite=False, config={}, template=None, profile='default')

    def test_two_frames_same_timestamp_get_distinct_names(self):
        card = self._card()
        dest = os.path.join(self.tmp, 'dest'); os.makedirs(dest)
        h = self._haul(card, dest)
        silent(h.run)
        self.assertEqual(rb(os.path.join(dest, '20260526-140024_708.arw')), self.A)
        self.assertEqual(rb(os.path.join(dest, '20260526-140024_708-00002.arw')), self.B)
        self.assertEqual(len(h.conflicts), 0)

    def test_partial_then_full_no_duplicate_no_loss(self):
        # prior partial import placed B under the bare name; now both are on the card.
        card = self._card()
        dest = os.path.join(self.tmp, 'dest'); os.makedirs(dest)
        self.write(os.path.join('dest', '20260526-140024_708.arw'), self.B)
        silent(self._haul(card, dest).run)
        self.assertEqual(rb(os.path.join(dest, '20260526-140024_708.arw')), self.B)  # B kept, not duplicated
        self.assertEqual(rb(os.path.join(dest, '20260526-140024_708-00001.arw')), self.A)  # A imported
        copies_of_b = sum(1 for n in os.listdir(dest)
                          if n.endswith('.arw') and rb(os.path.join(dest, n)) == self.B)
        self.assertEqual(copies_of_b, 1)

    def test_collision_rerun_is_idempotent(self):
        card = self._card()
        dest = os.path.join(self.tmp, 'dest'); os.makedirs(dest)
        silent(self._haul(card, dest).run)
        first = sorted(os.listdir(dest))
        h2 = self._haul(card, dest)
        silent(h2.run)
        self.assertEqual(sorted(os.listdir(dest)), first)   # nothing added on re-run
        self.assertEqual(len(h2.to_copy), 0)
        self.assertEqual(len(h2.to_skip), 2)


class TestHaulLocalMode(TempCase):
    def _haul(self, dest, **kw):
        return ph.Haul(card=None, ext='arw', dest_dir=dest, filt='all', dry_run=False,
                       rewrite=False, config=CONFIG, template=None, profile='default',
                       local=True, **kw)

    def test_renames_new_and_leaves_already_named_alone(self):
        dest = self.tmp
        # a camera-named new file, and an already-named file WITHOUT a sidecar
        self.write('TEST0002.ARW', build_tiff(date='2026:05:26 14:00:24', subsec='708'))
        self.write('20200101-120000.arw', build_tiff(date='2020:01:01 12:00:00', subsec=None))
        h = self._haul(dest)
        silent(h.run)
        # the camera file is renamed and gets a sidecar
        self.assertFalse(os.path.exists(os.path.join(dest, 'TEST0002.ARW')))
        self.assertTrue(os.path.exists(os.path.join(dest, '20260526-140024_708.arw')))
        self.assertTrue(os.path.exists(os.path.join(dest, '20260526-140024_708.xmp')))
        # the already-named file is left strictly alone - NO sidecar created (LR-revert fix)
        self.assertTrue(os.path.exists(os.path.join(dest, '20200101-120000.arw')))
        self.assertFalse(os.path.exists(os.path.join(dest, '20200101-120000.xmp')))

    def test_local_time_shift_renames_and_patches_in_place(self):
        dest = self.tmp
        self.write('CAM0001.ARW', build_tiff(date='2026:05:26 14:00:24', subsec='708'))
        silent(self._haul(dest, time_shift=timedelta(hours=1)).run)
        self.assertFalse(os.path.exists(os.path.join(dest, 'CAM0001.ARW')))   # original moved
        arw = os.path.join(dest, '20260526-150024_708.arw')                   # shifted name
        self.assertTrue(os.path.exists(arw))
        self.assertEqual(ph.read_exif_datetime(arw)[0], '2026:05:26 15:00:24')  # EXIF patched in place

    def test_local_shot_tz_renames_and_restamps(self):
        dest = self.tmp
        self.write('CAM0002.ARW', build_tiff(date='2026:05:26 14:00:24', subsec='708', offset='+09:00'))
        silent(self._haul(dest, shot_tz='-04:00', shot_tz_delta=ph.parse_tz('-04:00')).run)
        self.assertFalse(os.path.exists(os.path.join(dest, 'CAM0002.ARW')))
        arw = os.path.join(dest, '20260526-010024_708.arw')
        self.assertTrue(os.path.exists(arw))
        self.assertEqual(ph.read_exif_datetime(arw), ('2026:05:26 01:00:24', '708', '-04:00'))

    def test_already_named_not_double_corrected(self):
        dest = self.tmp
        # an already-timestamp-named file: a set time_shift must NOT re-shift or re-patch it
        self.write('20260526-140024_708.arw', build_tiff(date='2026:05:26 14:00:24', subsec='708'))
        before = rb(os.path.join(dest, '20260526-140024_708.arw'))
        silent(self._haul(dest, time_shift=timedelta(hours=1)).run)
        self.assertEqual(rb(os.path.join(dest, '20260526-140024_708.arw')), before)   # untouched
        self.assertFalse(os.path.exists(os.path.join(dest, '20260526-150024_708.arw')))  # no shifted copy

    def test_local_collision_distinct_names(self):
        dest = self.tmp
        self.write('CAM0001.ARW', build_tiff(date='2026:05:26 14:00:24', subsec='708'))
        self.write('CAM0002.ARW',
                   build_tiff(date='2026:05:26 14:00:24', subsec='708') + b'\x00' * 2048)
        silent(self._haul(dest).run)
        self.assertTrue(os.path.exists(os.path.join(dest, '20260526-140024_708.arw')))
        self.assertTrue(os.path.exists(os.path.join(dest, '20260526-140024_708-0002.arw')))


# ---------------------------------------------------------------------------
# Lock -> unlock -> Purple (macOS UF_IMMUTABLE; skipped elsewhere)
# ---------------------------------------------------------------------------

@unittest.skipUnless(sys.platform == 'darwin' and hasattr(os, 'chflags'),
                     'requires macOS chflags/UF_IMMUTABLE')
class TestHaulLocked(TempCase):
    def _locked_card(self):
        card = os.path.join(self.tmp, 'card')
        locked = self.write(os.path.join('card', 'DCIM', '100MSDCF', 'LOCK0001.ARW'),
                            build_tiff(date='2026:05:26 14:00:24'))
        free = self.write(os.path.join('card', 'DCIM', '100MSDCF', 'FREE0002.ARW'),
                          build_tiff(date='2026:05:26 15:00:24'))
        os.chflags(locked, stat.UF_IMMUTABLE)
        if not ph.is_locked(os.stat(locked)):
            self.skipTest('could not set UF_IMMUTABLE here')
        return card, locked, free

    def _haul(self, card, dest, filt='all'):
        return ph.Haul(card=card, ext='arw', dest_dir=dest, filt=filt, dry_run=False,
                       rewrite=False, config=CONFIG, template=None, profile='default')

    def test_locked_copy_is_unlocked_and_purple(self):
        card, locked, _free = self._locked_card()
        dest = os.path.join(self.tmp, 'dest'); os.makedirs(dest)
        h = self._haul(card, dest)
        silent(h.run)
        self.assertEqual(h.featured, 1)                                   # one locked frame seen
        locked_dest = os.path.join(dest, '20260526-140024_708.arw')
        self.assertFalse(ph.is_locked(os.stat(locked_dest)))             # copy is unlocked
        self.assertEqual(ph.read_label(os.path.join(dest, '20260526-140024_708.xmp')), 'Purple')
        self.assertIsNone(ph.read_label(os.path.join(dest, '20260526-150024_708.xmp')))
        self.assertTrue(ph.is_locked(os.stat(locked)))                   # card original untouched

    def test_filter_locked_only(self):
        card, _l, _f = self._locked_card()
        dest = os.path.join(self.tmp, 'dest'); os.makedirs(dest)
        silent(self._haul(card, dest, filt='locked').run)
        self.assertTrue(os.path.exists(os.path.join(dest, '20260526-140024_708.arw')))
        self.assertFalse(os.path.exists(os.path.join(dest, '20260526-150024_708.arw')))

    def test_filter_unlocked_only(self):
        card, _l, _f = self._locked_card()
        dest = os.path.join(self.tmp, 'dest'); os.makedirs(dest)
        silent(self._haul(card, dest, filt='unlocked').run)
        self.assertFalse(os.path.exists(os.path.join(dest, '20260526-140024_708.arw')))
        self.assertTrue(os.path.exists(os.path.join(dest, '20260526-150024_708.arw')))


# ---------------------------------------------------------------------------
# shot_tz timezone correction
# ---------------------------------------------------------------------------

class TestShotTz(TempCase):
    def _card(self, **tiff):
        card = os.path.join(self.tmp, 'card')
        self.write(os.path.join('card', 'DCIM', '100MSDCF', 'T.ARW'), build_tiff(**tiff))
        return card

    def _haul(self, card, dest, **kw):
        return ph.Haul(card=card, ext='arw', dest_dir=dest, filt='all', dry_run=False,
                       rewrite=False, config=CONFIG, template=None, profile='default', **kw)

    def test_shot_tz_is_instant_preserving(self):
        # 14:00:24 +09:00 reinterpreted as -04:00 -> same instant, 01:00:24 -04:00.
        card = self._card(date='2026:05:26 14:00:24', subsec='708', offset='+09:00')
        dest = os.path.join(self.tmp, 'dest'); os.makedirs(dest)
        h = self._haul(card, dest, shot_tz='-04:00', shot_tz_delta=ph.parse_tz('-04:00'))
        silent(h.run)
        arw = os.path.join(dest, '20260526-010024_708.arw')
        self.assertTrue(os.path.exists(arw))                             # name from corrected time
        self.assertEqual(ph.read_exif_datetime(arw),
                         ('2026:05:26 01:00:24', '708', '-04:00'))       # date shifted, offset restamped

    def test_shot_tz_without_recorded_offset_aborts(self):
        card = self._card(date='2026:05:26 14:00:24', subsec='708', offset=None)
        dest = os.path.join(self.tmp, 'dest'); os.makedirs(dest)
        h = self._haul(card, dest, shot_tz='-04:00', shot_tz_delta=ph.parse_tz('-04:00'))
        with self.assertRaises(ph.ExifPatchError):
            h.run()


# ---------------------------------------------------------------------------
# --rewrite (merge-only, destination metadata refresh)
# ---------------------------------------------------------------------------

class TestRewrite(TempCase):
    def _haul(self, dest):
        return ph.Haul(card=None, ext='arw', dest_dir=dest, filt='all', dry_run=False,
                       rewrite=True, config=CONFIG, template=None, profile='default')

    def test_merge_preserves_crs_and_adds_rights(self):
        self.write('X.arw', build_tiff())
        self.write('X.xmp', CRS_SIDECAR)
        silent(self._haul(self.tmp).run)
        body = rt(os.path.join(self.tmp, 'X.xmp'))
        self.assertIn('Exposure2012', body)            # develop edits preserved
        self.assertIn('+0.85', body)
        self.assertIn('(c) 2026 Test', body)           # rights merged ({year} from EXIF)
        self.assertIn('Tester', body)

    def test_skips_file_without_sidecar(self):
        self.write('Y.arw', build_tiff())
        out = capture(self._haul(self.tmp).run)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'Y.xmp')))   # never created
        self.assertIn('without a sidecar', out)                            # and reported

    def test_preserves_existing_purple_label(self):
        self.write('Z.arw', build_tiff())
        self.write('Z.xmp', LABEL_SIDECAR)
        silent(self._haul(self.tmp).run)
        self.assertEqual(ph.read_label(os.path.join(self.tmp, 'Z.xmp')), 'Purple')


# ---------------------------------------------------------------------------
# argparse smoke
# ---------------------------------------------------------------------------

class TestParser(unittest.TestCase):
    def test_parses_basic_args(self):
        args = ph.build_parser().parse_args(['arw', '--dry-run', '--dest', '/tmp/x'])
        self.assertEqual(args.extension, 'arw')
        self.assertTrue(args.dry_run)
        self.assertEqual(args.dest, '/tmp/x')
        self.assertEqual(args.filter, 'all')


# ---------------------------------------------------------------------------
# Real-format fidelity (only when samples/ is present; nothing committed)
# ---------------------------------------------------------------------------

class TestRealSamples(unittest.TestCase):
    # (date, subsec, offset) ground truth per format, for confidence in the real
    # container parsers (RAF/CR3/JPEG/DNG) the synthetic fixtures don't exercise.
    # Each runs only when its file is present in samples/ (gitignored, nothing committed).
    EXPECTED = {
        'DSC09749.ARW': ('2021:02:19 12:02:28', '999', '-08:00'),   # Sony  (TIFF)
        'DSC_0293.NEF': ('2021:12:17 13:12:25', '77', '-08:00'),    # Nikon (TIFF)
        'R0000077.DNG': ('2025:08:26 16:57:12', None, None),        # Ricoh (DNG/TIFF)
        'DSCF0103.RAF': ('2024:02:18 21:03:52', '98', '-07:00'),    # Fuji  (embedded JPEG)
        '237A2133.CR3': ('2024:09:08 14:00:07', '36', '-07:00'),    # Canon (ISO-BMFF)
        'R0000814.JPG': ('2013:02:21 06:18:10', None, None),        # standalone JPEG
    }

    def test_expected_values_per_format(self):
        present = {n: v for n, v in self.EXPECTED.items()
                   if os.path.exists(os.path.join(SAMPLES, n))}
        if not present:
            self.skipTest('no samples/ fixtures present')
        for name, expected in present.items():
            with self.subTest(sample=name):
                self.assertEqual(ph.read_exif_datetime(os.path.join(SAMPLES, name)), expected)


if __name__ == '__main__':
    unittest.main(verbosity=2)
