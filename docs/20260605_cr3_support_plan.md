# Plan: Canon CR3 support

Status: implemented (2026-06-05)

Extends the Exif reader of [[20260604_initial_plan]] to a container format.
Reuses the `tiff_base` indirection added in [[20260605_raf_support_plan]] and
shares the IFD-traversal change with [[20260605_nef_support_plan]].

## Purpose
Ingest Canon CR3 (verified on an EOS R5 II sample) via `photohaul cr3`.

## The problem
CR3 is not a TIFF. It is an ISO base-media (BMFF / MP4-style) box container — it
begins with an `ftyp` box whose major brand is `crx `. The Exif is a small,
standalone TIFF tucked deep in the box tree:

```
ftyp (brand 'crx ')
moov
  uuid (85c0b687-820f-11e0-8111-f4ce462b6a48 — Canon)
    CNCV CCTP CTBO free
    CMT1  <- TIFF: IFD0-equivalent (Make/Model/...)
    CMT2  <- TIFF: the Exif IFD (DateTimeOriginal, SubSec, OffsetTime) — what we want
    CMT3  <- TIFF: Canon MakerNotes
    CMT4  <- TIFF: GPS
    THMB
mdat (image data)
```

So the byte-0 `II`/`MM` check failed and every CR3 was reported unreadable.

Two further wrinkles vs. RAF:
- The date is in **CMT2**, and CMT2's tags sit in its **first IFD directly** —
  there is no Exif-IFD pointer (0x8769) to follow (CMT2 *is* the extracted Exif
  IFD).
- The metadata lives near the front; the multi-MB `mdat` must not be read.

## Approach (stdlib only, no exiftool)
1. **Detect** CR3 in `_exif_tiff_base`: `ftyp` box + `crx ` brand.
2. **Locate CMT2** with a tiny BMFF walker, `_bmff_find(f, [moov, uuid, CMT2])`,
   that reads only box headers via seeks (never `mdat`) and descends a `uuid`
   box past its 16-byte id. Return CMT2's payload offset as the `tiff_base`.
3. **Parse from that base** with the existing IFD reader. Generalize the
   traversal: descend the Exif-IFD pointer if IFD0 has one (ARW/NEF/RAF), else
   read the date/subsec from the first IFD (CR3 CMT2). Preferring the pointer is
   what keeps NEF's SubSec correct — see [[20260605_nef_support_plan]].

Everything downstream (discovery, copy, naming with `ljust` subsec→ms, sidecars)
was already format-agnostic and needed no change.

## Verified (R5 II sample, no camera)
- Box walk finds CMT2 at the expected offset; reader returns
  `2024:09:08 14:00:07` / `36` → `20240908-140007_360`, matching exiftool.
- Full ingest of `.cr3`: copied, renamed, sidecar written, copy **byte-identical**
  to the original; a re-run skips it (idempotent).
- No regression: ARW, NEF, and RAF samples still parse to the same names.

## Not verified / not addressed
- **Lock → Purple.** Canon's in-camera *Protect* → macOS `uchg` mapping is
  untested (no body). If Canon differs, featured-detection needs a CR3 path.
- **The embedded preview/thumbnail** (`THMB`, and the JPEG/HEIF in `mdat`) is not
  parsed or touched; CMT2 is authoritative for the tags we read.

## Future: capture-time patch (heads-up for [[20260605_capture_time_offset_plan]])
In-place date/offset correction is more involved for CR3 than for the TIFF/RAF
formats:
- The capture time is duplicated across **CMT2** and likely the **CMT3**
  MakerNote (and `mdat`'s embedded image), so a consistent edit touches several
  blocks.
- BMFF carries offset tables (e.g. `CTBO`, and `stco`/`co64` in the traks). The
  same-length ASCII overwrite we use elsewhere keeps every box size and table
  entry valid, so it stays tractable — but it is several fields, not one, and
  needs its own verification pass before being trusted on originals.
