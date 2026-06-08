# AGENTS.md

Guidance for working in this repo.

## What this is
`photohaul` — a single-file Python 3 CLI (`src/photohaul.py`) that ingests photos
off a mounted camera card into a folder, renames them to a stable
millisecond-precise timestamp, and writes Lightroom-friendly XMP sidecars
(color label, copyright/creator, an AP-style caption, and the structured IPTC
fields a photo desk expects — headline, credit/source, city/state/country,
location, usage terms, keywords). Reads Exif from Sony ARW, Nikon NEF, DNG
(Adobe/Ricoh), Fuji RAF, Canon CR3, and JPEG. **Zero dependencies** — stdlib only,
including a hand-rolled Exif reader (TIFF — covering ARW/NEF/DNG — plus standalone
JPEG and the RAF/CR3 containers) and XMP writer. No exiftool, no pip installs.

The script is run from a destination folder; it's copied to `~/bin` by hand.

## Invariants — do not break these
- **The card is read-only** *(card mode)*. It is never written to or modified. Lock
  detection is `os.stat(...).st_flags & UF_IMMUTABLE` (the camera's in-camera protect bit;
  verified on Sony and Fuji), a read. `--local` mode uses no card; it **renames files in
  place** in the working folder (`docs/20260606_local_mode_plan.md`).
- **Re-runs are idempotent.** Card mode: destination names derive purely from intrinsic
  capture time, a present file of matching size is skipped, a different size is a reported
  conflict, never overwritten. `--local`: a file whose name already matches the timestamp
  pattern (`_STAMP_RE`) is left alone, so re-runs only touch newly-added camera files.
  Anything that makes names depend on extrinsic input must be deterministic across runs
  (see capture-time offset).
- **The raw is a byte-exact clone of the card original** (card mode; in `--local` the raw
  *is* the original, only renamed) — with exactly one *planned* exception: in-place
  correction of EXIF date and timezone-offset fields under a capture-time/timezone
  correction (`docs/20260605_capture_time_offset_plan.md`), applied in `--local` via a
  crash-safe copy-patch-replace. If you add another exception, justify it and document it
  here.
- **Sidecars are written only for files we just placed** (copied/renamed); a file
  already in the folder never gets a freshly-created sidecar. Creating a develop-less
  sidecar next to a raw already imported and edited in Lightroom makes LR sync from it
  and revert catalog-only edits, so already-present files are left strictly alone.
  `--rewrite` is **merge-only**: it merges photohaul's fields into a sidecar that
  already exists (preserving everything else, e.g. Lightroom develop edits) and skips
  (reports) files that have none — it never creates one. An unparseable sidecar is
  reported and left untouched, never clobbered.

## Configuration surfaces
- `~/.photohaul` — INI, parsed with `configparser` (`interpolation=None`).
  `[default]` is inherited by named profile sections. Holds the default `format`
  (overridden by the CLI positional; no built-in default) and **rights** (creator
  / copyright / credit); `{year}` in copyright expands to the capture year. A
  profile section may *also* carry the `photohaul.json` scaffold keys below as
  client/venue defaults — these are inert at copy time and used only to seed
  `--init --profile NAME`. See `docs/20260605_profiles_plan.md` and
  `docs/20260606_client_profiles_plan.md`.
- `photohaul.json` in the destination — the per-folder shoot scaffold:
  caption/IPTC keys (sport, event, homeTeam/awayTeam,
  homeShort/awayShort, venue, city, state, country, conference, credit, source,
  rightsUsage, assignment) plus `profile`, and the capture-time correction keys
  `time_shift` / `shot_tz`. Caption is AP-style; the per-image action sentence
  stays a manual Lightroom pass. Scaffolded by `--init` (seeded from a profile when
  `--profile` is given; `time_shift`/`shot_tz` always blank). See
  `docs/20260606_iptc_fields_plan.md` and `docs/20260606_client_profiles_plan.md`.

## `--rewrite` is card-free
`--rewrite` is a destination-only metadata refresh: no card, no copying. It scans
the destination for already-copied files, reads their EXIF, and merges
rights/creator/caption into **existing** sidecars only — **merge-only**: a dest file
with no sidecar is skipped and reported, never given a freshly-created (develop-less)
one, since that would make Lightroom revert catalog-only edits. An existing Purple
label is preserved as-is and never added or removed (lock status is unknown without
the card). It errors if combined with `--locked`/`--unlocked`.

## `--local` is card-free too (but renames)
`--local` ingests files already sitting in the destination (you copied them off the card
by hand — X100VI, Ricoh GR): no card, no copy. It **renames** camera-named files in place
to the timestamp name and writes create-if-absent sidecars **for those renamed files
only**; files already matching the timestamp pattern get no sidecar at all (writing one
next to a raw already imported and edited in Lightroom makes LR sync from it and revert
catalog-only develop edits — `--rewrite` updates an existing sidecar in place but won't
create one either).
Unlike `--rewrite`, it changes filenames (and, under a
capture-time correction, EXIF). The in-camera protect bit survives a hand-copy off the
card, so a camera-named locked frame is detected, **unlocked in place** (clearing the
`uchg` flag — in-bounds here because the folder is ours, unlike the read-only card) and
marked Purple, exactly as card mode copies-unlocked-and-Purples. Already-named files are
left strictly alone, lock bit included. Errors if combined with `--rewrite`, `--source`,
or `--locked`/`--unlocked`. See `docs/20260606_local_mode_plan.md`.

## Working conventions
- **Plans live in `docs/`** as `YYYYMMDD_<name>_plan.md`, with a `Status:` line
  (`planned` / `implemented (date)`) and `[[wiki-style]]` cross-links to related
  plans. Write the plan before implementing a non-trivial feature; flip the
  status when it lands.
- When adding or changing a flag, update **all three**: `--help` (the argparse
  epilog), `README.md`, and the relevant plan doc.
- **Keep comments and docstrings current with the code.** When you change
  behavior, re-read the surrounding comments, docstrings, and section headers and
  fix any that no longer match — counts ("the two tags"), format lists, file
  offsets, and described control flow drift silently otherwise. A stale comment is
  a bug.
- **Match the existing code's style** — terse, dependency-free, header-only Exif
  seeks, small pure functions. No new third-party packages.
- **Test against the real card or a scratch dir.** A dry run against the mounted
  card should report the right total/featured split; a real copy into a scratch
  dir verifies names, sidecars, unlocked copies, and that an immediate re-run
  skips everything. Build a small fake card (`DCIM/<dir>/*.ARW`, `chflags uchg`
  to simulate a lock) with `--source` when a controlled fixture is easier.
- There are samples in the samples/ folder of various image types for testing, but don't check them in

## Map of the source
`src/photohaul.py`, top to bottom: Exif reader → naming helpers → card discovery
→ config/template loaders → caption builder → XMP sidecar read/write → crash-safe
copy → progress display → `Frame`/`Haul` (scan, classify, copy, write_metadata,
run) → argparse `main`.
