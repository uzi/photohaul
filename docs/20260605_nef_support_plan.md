# Plan: Nikon NEF support

Status: implemented (2026-06-05)

A companion to [[20260605_raf_support_plan]] and [[20260605_cr3_support_plan]];
shares the Exif reader from [[20260604_initial_plan]].

## Purpose
Ingest Nikon NEF (verified on a Z9 sample) via `photohaul nef`.

## What it took: almost nothing
A NEF is a TIFF at byte zero (`II*\0`), structurally like a Sony ARW, so the
existing reader parses it directly — no RAF/CR3-style container handling needed.
Discovery, copy, naming, and sidecars were already format-agnostic.

## The one subtlety (shared fix with CR3)
Nikon writes `DateTimeOriginal` (0x9003) into **IFD0** *and* keeps the canonical
copy — plus `SubSecTimeOriginal` (0x9291) — in the **Exif IFD**. The X100VI/ARW
path always descends into the Exif IFD, so this never mattered before.

The CR3 work generalized the reader to read the date from the first IFD when
there is no Exif-IFD pointer. A naive "first IFD wins" rule **regressed NEF**: it
found the date in IFD0 but missed the SubSec that only lives in the Exif IFD
(`13:12:25` with `sub=None` instead of `_770`). The committed rule is therefore:

- If IFD0 has an Exif-IFD pointer (0x8769) → **descend into it** (ARW/NEF/RAF);
  it carries both `DateTimeOriginal` and `SubSecTimeOriginal`.
- Else fall back to the first IFD (Canon CR3's CMT2, which has no pointer).

So NEF's only "cost" was making sure the Exif IFD stays preferred. See
[[20260605_cr3_support_plan]] for the other half of that change.

## Verified (Z9 sample, no camera)
- Reader: `2021:12:17 13:12:25` / `77` → `20211217-131225_770`, matching exiftool.
- Full ingest: copied, renamed, sidecar written, copy **byte-identical** to the
  original.

## Not verified / not addressed
- **Lock → Purple.** Whether Nikon's in-camera *Protect* sets the macOS `uchg`
  flag (as Sony and Fuji do) is untested — no Nikon body on hand. If Nikon uses a
  different mechanism, featured-detection would need a NEF-specific path.
