# CLAUDE.md

Guidance for working in this repo.

## What this is
`photohaul` — a single-file Python 3 CLI (`src/photohaul.py`) that ingests photos
off a mounted Sony camera card into a folder, renames them to a stable
millisecond-precise timestamp, and writes Lightroom-friendly XMP sidecars
(color label, copyright/creator, caption). **Zero dependencies** — stdlib only,
including a tiny hand-rolled TIFF/Exif reader and XMP writer. No exiftool, no
pip installs.

The script is run from a destination folder; it's copied to `~/bin` by hand.

## Invariants — do not break these
- **The card is read-only.** It is never written to or modified. Lock detection
  is `os.stat(...).st_flags & UF_IMMUTABLE` (Sony's in-camera protect bit), a
  read.
- **Re-runs are idempotent.** Destination names derive purely from intrinsic
  capture time, and a present file of matching size is skipped; a different size
  is a reported conflict, never overwritten. Anything that makes names depend on
  extrinsic input must be deterministic across runs (see capture-time offset).
- **The raw is a byte-exact clone of the card original** — with exactly one
  *planned* exception: in-place correction of EXIF date and timezone-offset
  fields under a capture-time/timezone correction
  (`docs/20260605_capture_time_offset_plan.md`). If you add
  another exception, justify it and document it here.
- **Sidecars are create-if-absent**, and `--rewrite` merges only photohaul's
  fields while preserving everything else (e.g. Lightroom develop edits). An
  unparseable sidecar is reported and left untouched, never clobbered.

## Configuration surfaces
- `~/.photohaul` — INI, parsed with `configparser` (`interpolation=None`).
  `[default]` is inherited by named profile sections. Holds **rights** only
  (creator / copyright / credit); `{year}` in copyright expands to the capture
  year. See `docs/20260605_profiles_plan.md`.
- `photohaul.json` in the destination — per-folder caption template (teamA,
  teamB, event, venue, location, credit) plus `profile`, and (planned)
  `time_shift`. Scaffolded by `--init-template`.

## `--rewrite` is card-free
`--rewrite` is a destination-only metadata refresh: no card, no copying. It scans
the destination for already-copied files, reads their EXIF, and merges
rights/creator/caption into existing sidecars. An existing Purple label is
preserved as-is and never added or removed (lock status is unknown without the
card). It errors if combined with `--locked`/`--unlocked`.

## Working conventions
- **Plans live in `docs/`** as `YYYYMMDD_<name>_plan.md`, with a `Status:` line
  (`planned` / `implemented (date)`) and `[[wiki-style]]` cross-links to related
  plans. Write the plan before implementing a non-trivial feature; flip the
  status when it lands.
- When adding or changing a flag, update **all three**: `--help` (the argparse
  epilog), `README.md`, and the relevant plan doc.
- **Match the existing code's style** — terse, dependency-free, header-only Exif
  seeks, small pure functions. No new third-party packages.
- **Test against the real card or a scratch dir.** A dry run against the mounted
  card should report the right total/featured split; a real copy into a scratch
  dir verifies names, sidecars, unlocked copies, and that an immediate re-run
  skips everything. Build a small fake card (`DCIM/<dir>/*.ARW`, `chflags uchg`
  to simulate a lock) with `--source` when a controlled fixture is easier.

## Map of the source
`src/photohaul.py`, top to bottom: Exif reader → naming helpers → card discovery
→ config/template loaders → caption builder → XMP sidecar read/write → crash-safe
copy → progress display → `Frame`/`Haul` (scan, classify, copy, write_metadata,
run) → argparse `main`.
