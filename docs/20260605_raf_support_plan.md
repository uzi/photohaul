# Plan: Fuji RAF support

Status: implemented (2026-06-05)

Extends the stdlib Exif reader from [[20260604_initial_plan]] to a second raw
format. Shares the `tiff_base` parameterization that [[20260605_capture_time_offset_plan]]
also needs.

## Purpose
Ingest Fuji RAF (verified on an X100VI card) the same way as Sony ARW —
`photohaul raf`. Everything except the Exif reader was already format-agnostic,
so this is a one-function change.

## The problem
The reader assumed a TIFF at byte zero: it reads the first bytes and expects the
`II`/`MM` byte-order marker. A Sony ARW *is* a TIFF, so that holds. A Fuji RAF is
**not** — it begins with `FUJIFILMCCD-RAW` and stores its Exif inside an
**embedded JPEG** further into the file. So every RAF failed with
`not a TIFF/Exif file`, was logged as an unreadable-Exif error, and never copied.

Probed layout (X100VI, 2026-06-05):

```
0x00  "FUJIFILMCCD-RAW " magic (+ version, "X100VI")
0x54  JpgImageOffset  (BE uint32)  -> e.g. 148
0x58  JpgImageLength  (BE uint32)
...   embedded JPEG at JpgImageOffset; its APP1/Exif holds a standard TIFF
```

The embedded JPEG's Exif is an ordinary TIFF block — once located, the existing
IFD walker reads it unchanged; it just lives at a non-zero file offset (160 in
the sample = JpgImageOffset 148 + SOI/APP1/`Exif\0\0` preamble).

## Approach
Introduce a **TIFF base offset** — the file offset where the Exif TIFF header
begins. `0` for ARW / TIFF-at-start; computed for RAF.

- `_exif_tiff_base(f)`:
  - Not a RAF (no `FUJIFILMCCD-RAW` magic) → return `0`.
  - RAF → read `JpgImageOffset` at `0x54`, seek there, confirm JPEG `FFD8`, walk
    the marker segments to the first `APP1` (`FFE1`) whose payload starts
    `Exif\0\0`, and return the offset of the TIFF header right after it. (XMP, a
    second `APP1`, is skipped by the same loop.)
- `read_exif_datetime` calls `_exif_tiff_base`, seeks to `base`, then reads the
  `II`/`MM` header and IFDs **relative to `base`** (`read_ifd` and `ascii_val`
  add `base` to every seek). For ARW `base == 0`, so behavior is identical.

Reuses the existing `read_ifd` / `ascii_val`; no new Exif-parsing logic, no
dependency on exiftool (which would break the zero-dependency design of
[[20260604_initial_plan]] — it was used only to cross-check the parse).

## What needed no change
- **Discovery / copy / naming / sidecars** — already format-agnostic. The
  `extension` positional already selects the glob (`raf`), and `scan_source`,
  `copy_file`, and the XMP writer don't care about format.
- **Sub-second → ms.** Fuji writes `SubSecTimeOriginal` as 2 digits (`13` = 0.13s
  = 130 ms) vs Sony's 3 (`967`). `base_name` already right-pads via
  `ljust(3, '0')`, so `13` → `130` correctly. No change.
- **Lock → Purple.** Fuji's in-camera *Protect* sets the same macOS `uchg`
  immutable flag Sony uses (confirmed: a protected frame carried `uchg` and flowed
  through to a Purple sidecar). `is_locked` works as-is.

## Relationship to capture-time
The `tiff_base` parameter is exactly the refactor [[20260605_capture_time_offset_plan]]
calls for. When the in-place date/offset patch lands, it patches the date/offset
fields at `base + offset` — for RAF that targets the embedded JPEG's Exif. Still
fixed-width, same-length, and the RAF header's `JpgImageOffset`/`Length` are
unaffected because nothing changes size.

## Verification (real X100VI card, 3-file scratch card incl. a protected frame)
1. Dry run + real ingest of `.raf` → 3 copied, names derived from RAF Exif
   (`20240726-172229_130` etc.), matching exiftool.
2. Protected frame → its sidecar carries `xmp:Label = Purple`; the others none.
3. Re-run skips all 3 (idempotent).
4. `cmp` confirms each copied RAF is **byte-identical** to the card original
   (raw never modified); exiftool reads the copies cleanly.

## Not addressed
- The embedded **preview/thumbnail** JPEG inside the RAF is not parsed or touched;
  the main Exif is what tools read.
- Other RAF-bearing bodies/firmware aren't tested, but the JPEG offset is read
  from the header (not hardcoded) and the Exif is found by walking markers, so the
  approach isn't tied to the X100VI's exact byte positions.
