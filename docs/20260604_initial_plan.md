# Plan: `photohaul` — Sony A1 ingest tool

## Goal
A single-file Python 3 CLI that copies `.ARW` files off a mounted Sony card into the
current folder, renames them to a stable millisecond-precision timestamp, detects
in-camera–locked ("featured") frames, clears the lock on the copy, and marks those
frames with a **Purple** Lightroom color label — idempotently and crash-safely.

## CLI
```
photohaul [extension] [--source PATH] [--dest PATH]
         [--locked | --unlocked | --all] [--dry-run]
```
- `extension` — positional, default `arw`. Lets you also pull A1 JPEG/HEIF (anything
  carrying standard Exif date+subsec). **Not** for video — `.mp4`/XAVC lack
  `SubSecTimeOriginal`; use `rawsort` for those.
- `--source` — card path. **Default: auto-detect** — scan `/Volumes/*` for a `DCIM/`
  dir; use it if exactly one, error if zero or several (robust to the card label
  changing from "Untitled").
- `--dest` — default current directory.
- `--locked` / `--unlocked` / `--all` — which frames to copy. **Default `--all`.** The
  filter controls *what's copied*; the purple marking is always driven by the lock bit
  independently.
- `--dry-run` — full scan + plan + status, touches nothing.

## Naming
- `YYYYMMDD-hhmmss_mmm.<ext>` from `DateTimeOriginal` + `SubSecTimeOriginal`
  (e.g. `20260526-140024_708.arw`).
- The millisecond is the unique key — no sequence letters. If `SubSecTimeOriginal` is
  ever absent, drop the `_mmm`; if that bare name then collides *within a run*, fall
  back to the original DSC file number (the only electronic-shutter-safe intrinsic id).
- No `ShutterCount` anywhere — it's mechanical-only and meaningless on this body.

## Lock detection (read-only on the source)
- A frame is "featured" iff `os.stat(path).st_flags & stat.UF_IMMUTABLE` (the
  `uchg`/Locked flag macOS's exFAT driver maps Sony's protect bit onto — verified on
  the card: 3 of 36).
- We **never write to the card.**

## Pipeline (per file)
1. **Pre-scan** the whole card first: read each file's lock bit + Exif date/subsec
   (header-only), compute destination name and total byte count. Gives the status bar
   its denominators and lets us detect skips before copying a byte.
2. **Skip rule:** if the final dest name exists **and** its size matches the source →
   already landed, skip the copy. (The "set 1 / set 2, same card" resume.)
3. **Mismatch guard:** dest exists but size differs → **stop and warn**, never overwrite.
4. **Crash-safe copy:** copy to `…_708.arw.partial`, then `os.rename` to the final name
   (atomic, same filesystem). A leftover `.partial` is never mistaken for done.
5. **Unlock the copy:** ensure no `uchg` and that it's writable.
6. **Featured marking:** if the source was locked → write `BASENAME.xmp` sidecar with
   `xmp:Label = "Purple"`, **only if no sidecar already exists** (protects Lightroom
   edits). Applies even when the raw itself was skipped, so frames locked after the
   first import still get marked on re-run.

## Status display
Live-updating line + final summary, computed from the pre-scan:
```
Featured: 3   Total: 36 (33 to copy, 3 skipped)
[#######-------]  18/33   1.21/2.30 GB   412 MB/s   ETA 0:03
```
Final: counts copied this session, skipped, featured/marked, elapsed, average rate.

## Implementation notes
- **Pure Python, single file**, shebang, lives in the repo; copied to `~/bin` by hand.
- **Zero dependencies:** a tiny stdlib-only Exif reader pulls `DateTimeOriginal` (0x9003)
  and `SubSecTimeOriginal` (0x9291) straight from the TIFF/Exif IFD — header-only seeks,
  no exiftool, no perl, no `exifread` (avoids the Homebrew PEP-668 install friction).
  Validated byte-identical to exiftool on the sample (`2026:05:26 14:00:24` / `708`).
- Sidecar XMP is a tiny fixed packet with `xmp:Label`; no XMP library needed.

## Validation before done
1. Confirm `exifread` returns the same `708` ms and date from the sample ARW (and
   benchmark vs. the old exiftool path).
2. `--dry-run` against the mounted card must report **36 total, 3 featured**
   (`A1_02660`, `A1_02668`, `A1_02693`) and the right copy/skip split.
3. Real copy into a scratch dir → verify names, that the 3 sidecars carry Purple, that
   all copies are unlocked, and that an immediate re-run skips all 36.
