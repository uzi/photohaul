# Plan: DNG + JPEG support

Status: implemented (2026-06-07)

A companion to [[20260605_nef_support_plan]], [[20260605_raf_support_plan]], and
[[20260605_cr3_support_plan]]; shares the Exif reader from [[20260604_initial_plan]].

## Purpose
Ingest Adobe/Ricoh **DNG** (`photohaul dng`) and standalone **JPEG**
(`photohaul jpg`). Verified against `samples/R0000077.DNG` (Ricoh GR) and
`samples/R0000814.JPG` (Ricoh GR), cross-checked with exiftool.

## DNG: already worked, just undocumented
A DNG is a TIFF at byte zero (`II*\0`) with an Exif-IFD pointer in IFD0 — exactly
the ARW/NEF shape — so the existing reader parsed it with **zero code changes**.
The only gap was that no docs/help listed `dng` as a format. Reader output
`2025:08:26 16:57:12` (no SubSec, no offset) matches exiftool.

## JPEG: was claimed, but actually broken
The header comment, `--help`, and the README all claimed "JPEG directly," but a
*standalone* JPEG never parsed: `_exif_tiff_base` handled only the Fuji RAF
*embedded* JPEG (via `FUJI_MAGIC`) and the Canon CR3 box tree, then fell through
to `return 0` — so a real `.jpg` (SOI `\xff\xd8`, not `II`/`MM`) raised
`not a TIFF/Exif file`. The "JPEG" support was only ever RAF's embedded JPEG.

### Fix
The RAF path already walked an embedded JPEG's APP1 markers to the TIFF header.
Extracted that walk into a shared `_jpeg_exif_base(f, jpg_off, label)`:
- `_raf_exif_base` calls it with the RAF `JpgImageOffset`.
- `_exif_tiff_base` calls it with `jpg_off=0` when the file starts with the JPEG
  SOI (`\xff\xd8`), giving standalone-JPEG support for free.

The simple marker walk assumes every segment carries a 2-byte length; that holds
because APP1/Exif always precedes the entropy data (the length-less RSTn/SOS
markers), so the walk reaches Exif before any segment it can't measure.

## Verified (samples, no camera)
All six sample formats read identical capture time / subsec / offset to exiftool:

| sample | datetime | sub | offset |
|---|---|---|---|
| 237A2133.CR3 | 2024:09:08 14:00:07 | 36 | -07:00 |
| DSC09749.ARW | 2021:02:19 12:02:28 | 999 | -08:00 |
| DSCF0103.RAF | 2024:02:18 21:03:52 | 98 | -07:00 |
| DSC_0293.NEF | 2021:12:17 13:12:25 | 77 | -08:00 |
| R0000077.DNG | 2025:08:26 16:57:12 | – | – |
| R0000814.JPG | 2013:02:21 06:18:10 | – | – |

No regression on the four pre-existing formats. End-to-end `--local` on the DNG
and JPG: renamed to `20250826-165712.dng` / `20130221-061810.jpg`, sidecars
written with the per-file copyright year (2025 / 2013).

## Not verified / not addressed
- **Lock → Purple** for DNG/JPEG: the Ricoh GR samples were hand-copied off the
  card (the [[20260606_local_mode_plan]] workflow), so no in-camera Protect bit was
  present to test. The detection is format-agnostic (`is_locked` reads the `uchg`
  flag, not file contents), so it should apply uniformly if the GR sets it.
- **Capture-time correction** (`shift_exif_in_place`) on a standalone JPEG: the
  patcher addresses fields as `base + offset`, and `base` is now the APP1 TIFF
  header, so it should work — but it is untested on JPEG. DNG patches like any
  other TIFF.
