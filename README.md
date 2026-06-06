# photohaul

A fast, dependency-free CLI for ingesting photos off a camera card.

Copies raw files from a mounted card into the current folder, renaming each to a
stable, millisecond-precise name derived from its Exif capture time
(`YYYYMMDD-hhmmss_mmm.ext`, e.g. `20260526-140024_708.arw`). Because the name
comes only from the frame's own metadata, re-running on the same card just skips
what already landed.

Pick the format with the `extension` argument (default `arw`). photohaul reads
Exif natively (no exiftool) from each of:

- **ARW** (Sony) and **NEF** (Nikon) — TIFF at byte zero.
- **RAF** (Fuji) — Exif lives in an embedded JPEG, read transparently.
- **CR3** (Canon) — an MP4-style container; the Exif is pulled from its `moov`
  metadata box.

So `photohaul raf`, `photohaul nef`, or `photohaul cr3` ingests that card the
same way as ARW — stable naming, byte-exact copies, and (where the camera's
in-camera Protect maps to the macOS lock bit) Purple-labelling protected frames.

Frames locked (protected) in-camera are detected, copied unlocked, and tagged
with a Purple color label for Lightroom via an `.xmp` sidecar. **The card is
never modified.**

## Usage

    src/photohaul.py [extension] [--source PATH] [--dest PATH]
                     [--locked | --unlocked | --all]
                     [--dry-run] [--rewrite] [--init-template]

See `src/photohaul.py --help` for the full list of options and examples.

## Metadata (copyright, creator, captions)

photohaul writes all metadata to the `.xmp` sidecar — never the raw — so the
copied file stays a byte-exact clone of the card original and re-runs stay
idempotent. Lightroom reads these on import.

**Rights** come from an optional `~/.photohaul` config file (INI). Keys in
`[default]` are inherited by every profile; a profile section overrides or adds.
Missing file or field → simply not written:

```ini
[default]
creator   = Your Name              ; -> dc:creator
copyright = © {year} Your Name     ; -> dc:rights  ({year} = capture year)

[work]
copyright = © {year} Your Name / yoursite.com
credit    = Your Name/yoursite.com ; default caption byline
```

**Profiles** let one camera serve multiple contexts (e.g. personal vs work)
with different rights. The active profile is chosen by, in order: `--profile NAME`,
then a `"profile"` key in the folder's `photohaul.json`, then `[default]`. A folder
with no template stays on `[default]` — so personal shots never pick up the
branded copyright. (A section-less legacy config is read as `[default]`.)

**Per-shoot captions** come from a `photohaul.json` in the destination folder,
auto-detected on ingest. Scaffold a blank one with `photohaul --init-template`:

```json
{
  "profile": "",
  "teamA": "",
  "teamB": "",
  "event": "",
  "venue": "",
  "location": "",
  "credit": ""
}
```

Blank fields are omitted from the caption (`profile` selects the rights preset
and is not part of the caption text). A fully filled template yields, in
`dc:description`:

> Team A vs Team B, Event, at Venue, City, ST on May 30, 2026. Photo by Your Name/yoursite.com.

(`date` is auto-filled per frame; `credit` falls back to the config `credit`,
then `creator`.)

Sidecars are **create-if-absent** by default. `photohaul --rewrite` refreshes
them on files **already in the destination** — no card is needed and nothing is
copied. It **merges** only photohaul's fields (copyright, creator, caption),
preserving everything else (e.g. Lightroom develop edits). An existing **Purple
label is kept as-is**; because lock status is only known from the card, rewrite
never adds or removes a label. An existing sidecar that can't be parsed is
reported and left untouched, never overwritten. `--rewrite` cannot be combined
with `--locked`/`--unlocked`.

## Requirements

Python 3, standard library only — no external packages.

In-camera lock detection is macOS-specific: it reads the BSD `uchg` (immutable)
flag that the exFAT driver maps the camera's protect bit onto.

## Design

Design notes live in [`docs/`](docs/) as dated, per-topic plan documents
(`YYYYMMDD_<topic>_plan.md`), so the directory reads as a chronological record
of how the design evolved.
