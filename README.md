# photohaul

A fast, dependency-free CLI for ingesting photos off a camera card.

Copies raw files from a mounted card into the current folder, renaming each to a
stable, millisecond-precise name derived from its Exif capture time
(`YYYYMMDD-hhmmss_mmm.ext`, e.g. `20260526-140024_708.arw`). Because the name
comes only from the frame's own metadata, re-running on the same card just skips
what already landed.

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

**Global rights** come from an optional `~/.photohaul` config file (`key = value`,
`#` comments). Missing file or field → simply not written:

```
creator   = Your Name                          # -> dc:creator
copyright = © {year} Your Name / yoursite.com  # -> dc:rights  ({year} = capture year)
credit    = Your Name/yoursite.com             # default caption byline
```

**Per-shoot captions** come from a `photohaul.json` in the destination folder,
auto-detected on ingest. Scaffold a blank one with `photohaul --init-template`:

```json
{
  "teamA": "",
  "teamB": "",
  "event": "",
  "venue": "",
  "location": "",
  "credit": ""
}
```

Blank fields are omitted. A fully filled template yields, in `dc:description`:

> Team A vs Team B, Event, at Venue, City, ST on May 30, 2026. Photo by Your Name/yoursite.com.

(`date` is auto-filled per frame; `credit` falls back to the config `credit`,
then `creator`.)

Sidecars are **create-if-absent** by default. `photohaul --rewrite` re-applies
them to frames already present, **merging** only photohaul's fields (label,
copyright, creator, caption) and preserving everything else — e.g. Lightroom
develop edits. An existing sidecar that can't be parsed is reported and left
untouched, never overwritten.

## Requirements

Python 3, standard library only — no external packages.

In-camera lock detection is macOS-specific: it reads the BSD `uchg` (immutable)
flag that the exFAT driver maps the camera's protect bit onto.

## Design

Design notes live in [`docs/`](docs/) as dated, per-topic plan documents
(`YYYYMMDD_<topic>_plan.md`), so the directory reads as a chronological record
of how the design evolved.
