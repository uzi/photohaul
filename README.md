# photohaul

A fast, dependency-free CLI for ingesting photos off a camera card.

Copies raw files from a mounted card into the current folder, renaming each to a
stable, millisecond-precise name derived from its Exif capture time
(`YYYYMMDD-hhmmss_mmm.ext`, e.g. `20260526-140024_708.ext`). Because the name
comes only from the frame's own metadata, re-running on the same card just skips
what already landed.

Pick the format with the `format` argument (e.g. `photohaul nef`), or set a
default `format` in `~/.photohaul` (below) and just run `photohaul`. The argument
wins when both are present; there is no built-in default, so with neither set
photohaul reports an error rather than guessing. photohaul reads Exif natively
(no exiftool) from each of:

- **ARW** (Sony) and **NEF** (Nikon) — TIFF at byte zero.
- **RAF** (Fuji) — Exif lives in an embedded JPEG, read transparently.
- **CR3** (Canon) — an MP4-style container; the Exif is pulled from its `moov`
  metadata box.

So `photohaul <format>` ingests any of them the same way — stable naming,
byte-exact copies, and (where the camera's in-camera Protect maps to the macOS
lock bit) Purple-labelling of protected frames.

Frames locked (protected) in-camera are detected, copied unlocked, and tagged
with a Purple color label for Lightroom via an `.xmp` sidecar. **The card is
never modified.**

## Usage

    src/photohaul.py [format] [--source PATH] [--dest PATH] [--local]
                     [--locked | --unlocked | --all]
                     [--dry-run] [--rewrite] [--init] [--profile NAME]

See `src/photohaul.py --help` for the full list of options and examples.

## Metadata (copyright, creator, captions)

photohaul writes all metadata to the `.xmp` sidecar — never the raw — so the
copied file stays a byte-exact clone of the card original and re-runs stay
idempotent. (The lone exception is an opt-in capture-time correction, below, which
rewrites the copy's EXIF date/offset fields in place.) Lightroom reads these on
import.

**Rights** come from an optional `~/.photohaul` config file (INI). Keys in
`[default]` are inherited by every profile; a profile section overrides or adds.
Missing file or field → simply not written:

```ini
# Inline comments are not supported — keep comments on their own line, or the
# "; ..." would be read as part of the value.
[default]
# format: default file type to ingest when none is given on the CLI
format    = arw
# creator -> dc:creator ; copyright -> dc:rights  ({year} = capture year)
creator   = Your Name
copyright = © {year} Your Name

[work]
copyright = © {year} Your Name / yoursite.com
# credit -> photoshop:Credit and the default caption byline
credit    = Your Name/yoursite.com
```

A ready-to-edit [`example.photohaul`](example.photohaul) ships in the repo with a
`[default]` plus sample `highschool` / `college` / `club` profiles — copy it to
`~/.photohaul` as a starting point.

**Profiles** let one camera serve multiple contexts (e.g. personal vs. work)
with different rights. The active profile is chosen by, in order: `--profile NAME`,
then a `"profile"` key in the folder's `photohaul.json`, then `[default]`. A folder
with no template stays on `[default]` — so personal shots never pick up the
branded copyright. (A section-less legacy config is read as `[default]`.)

**Client profiles.** A profile section may also carry the per-shoot template keys
below (home team, venue, city, state, conference, usage terms…) for a recurring
client or venue. `photohaul --init --profile NAME` then **seeds** the new
`photohaul.json` from them, so stable values aren't retyped each shoot:

```ini
[highschool]
homeTeam  = Union High School
homeShort = Union
venue     = Union High School Gymnasium
city      = Springfield
state     = California
```

This is a one-time copy at `--init`; the JSON is the source of truth thereafter
(those template keys are inert at copy time, where only `creator`/`copyright`/
`credit`/`format` are read from the config). Bare `--init` still seeds from
`[default]`.

**Per-shoot caption + IPTC fields** come from a `photohaul.json` in the
destination folder, auto-detected on ingest. Scaffold one with `photohaul --init`
(blank, or seeded from a profile, above):

```json
{
  "profile": "",
  "sport": "",
  "event": "",
  "homeTeam": "",
  "awayTeam": "",
  "homeShort": "",
  "awayShort": "",
  "venue": "",
  "city": "",
  "state": "",
  "country": "",
  "conference": "",
  "credit": "",
  "source": "",
  "rightsUsage": "",
  "assignment": "",
  "time_shift": "",
  "shot_tz": ""
}
```

Blank fields are omitted (`profile` selects the rights preset; `time_shift` /
`shot_tz` are the capture-time correction below). From these, photohaul writes an
AP-style caption **and** the structured IPTC fields a photo desk / SID workflow
expects, all into the sidecar. What goes in each key, with examples:

| `photohaul.json` key | Example value | Written to |
|----------------------|---------------|------------|
| `profile`     | `college` | selects the `~/.photohaul` rights preset (not written to the sidecar) |
| `sport`       | `women's volleyball` | `photoshop:Headline` + keywords |
| `event`       | `NCAA women's volleyball match` | caption (generic event description) |
| `homeTeam`    | `State University Wolves` | keywords |
| `awayTeam`    | `City College Hawks` | keywords |
| `homeShort`   | `State` | caption (`State vs. City`), headline, keywords |
| `awayShort`   | `City` | caption, headline, keywords |
| `venue`       | `University Arena` | `Iptc4xmpCore:Location` (sublocation) + caption |
| `city`        | `Springfield` | `photoshop:City` + caption |
| `state`       | `California` | `photoshop:State` (full name; caption auto-abbreviates to `Calif.`) |
| `country`     | `USA` | `photoshop:Country` (omitted from the caption) |
| `conference`  | `Example Conference` | keywords |
| `credit`      | `Jane Roe / yoursite.com` | `photoshop:Credit` + caption byline |
| `source`      | `yoursite.com` | `photoshop:Source` |
| `rightsUsage` | `Editorial use only. No resale or commercial use without written permission.` | `xmpRights:UsageTerms` |
| `assignment`  | `Embargoed until 2025-10-04 06:00 PT` | `photoshop:Instructions` (Special Instructions) |
| `time_shift`  | `+2h30m` | capture-time correction (see below) |
| `shot_tz`     | `-04:00` | capture-time correction (see below) |

Three fields are assembled, not typed directly: **Caption** (`dc:description`,
below), **Keywords** (`dc:subject`, from `sport` + team names + `conference`), and
**Date created** (`photoshop:DateCreated`, per frame as ISO 8601 + offset).

The three attribution fields are easy to confuse: **creator** (`dc:creator`,
from the config) is the person who made the photo; **credit** (`photoshop:Credit`)
is how the credit line should read ("Photo by …"); **source** (`photoshop:Source`)
is the owner/agency that holds and licenses the image. **`rightsUsage`**
(`xmpRights:UsageTerms`) is the licensing language that travels with the file, and
**`assignment`** (`photoshop:Instructions`) is the Special Instructions field —
desk/assignment notes, embargoes, client-specific handling.

`city`/`state`/`country` go into the structured fields as typed, so use full
names (e.g. `state: "California"`). The **caption** abbreviates the state to AP
style on its own (`"California"` → `"Calif."`; the eight AP never abbreviates and
anything unrecognized pass through unchanged) — so `photoshop:State` reads
"California" while the caption reads "Calif." `photoshop:DateCreated` is derived
per frame from the (corrected) capture time and offset, so it agrees with the
filename and caption rather than the raw card EXIF. A fully filled template
yields, in `dc:description`:

> Lakeside vs. Riverside, NCAA women's volleyball match, at Memorial Arena, Springfield, Calif. on Friday, Oct. 3, 2025. (Photo by Your Name/yoursite.com)

The date is auto-filled per frame in **AP style** (weekday + abbreviated month,
except March–July spelled out); `credit` falls back to the config `credit`, then
`creator`. This caption is the **folder-level scaffold** — the per-image action
sentence and player IDs (e.g. "Jane Doe (7) goes up for a kill …") stay a manual
Lightroom pass; photohaul writes only what's constant for the shoot.

## Capture-time correction (clock drift & timezone)

Two optional, composable keys in `photohaul.json` fix a wrong capture time. Both
drive the destination filename, the caption date, and `{year}`, **and** rewrite
the copied raw's EXIF date/offset fields in place (a same-length overwrite — no
structural change, size unchanged). The **card is never touched**; only the copy
is corrected.

```json
{
  "time_shift": "+2h30m",
  "shot_tz": "-04:00"
}
```

- **`time_shift`** — *clock drift.* A wall-clock-only nudge of the date fields
  (the camera clock was simply wrong). Signed, units `d`/`h`/`m`/`s`, combinable
  (`+2h30m`, `-15s`, `90m`); whole seconds only (the millisecond key is never
  disturbed). The UTC-offset tags are left alone.
- **`shot_tz`** — *timezone / travel* (the main use case). "These frames were
  actually shot at this UTC offset." It derives the shift from the camera's
  recorded offset, so it both moves the displayed time **and** restamps the three
  offset tags — the absolute instant is preserved. Strict `±HH:MM`. **`shot_tz`
  alone is the complete travel fix** — you don't also set `time_shift`. Worked
  example: home `-07:00`, you fly three zones east and forget to change the
  camera, shoot 3:00 pm local → the camera stamps `12:00 / -07:00`; `shot_tz:
  "-04:00"` yields `15:00 / -04:00`, same instant.

When both are set they compose (date fields shift by the sum; offsets set to
`shot_tz`). Both are validated once at startup — bad input aborts before any copy.

> **Set these before the first ingest of a folder.** The corrections feed the
> filename, so changing them after files exist produces *new names* (duplicates),
> not updates — re-ingest the folder cleanly instead. There is deliberately no CLI
> flag (a forgotten flag could silently rename everything). `--rewrite` ignores
> both keys, since the destination files already carry the corrected time.

Sidecars are **create-if-absent** by default. `photohaul --rewrite` refreshes
them on files **already in the destination** — no card is needed and nothing is
copied. It **merges** only photohaul's fields (copyright, creator, caption),
preserving everything else (e.g. Lightroom develop edits). An existing **Purple
label is kept as-is**; because lock status is only known from the card, rewrite
never adds or removes a label. An existing sidecar that can't be parsed is
reported and left untouched, never overwritten. `--rewrite` cannot be combined
with `--locked`/`--unlocked`.

## `--local` — rename files already in a folder

For the "I copied a few frames off the card by hand" workflow (e.g. an X100VI or
Ricoh GR), `photohaul --local raf` renames camera-named files **already in the
destination** to the timestamp name, **in place** — no card, no copy:

```
$ photohaul --local raf            # operates on the current folder (or --dest)
DSCF1234.RAF  ->  20260501-123456_708.raf
```

Files whose names already match the timestamp pattern are left untouched, so it's
safe to re-run after dropping in more photos — only the new ones are renamed.
A new photo whose timestamp collides with an existing one is kept under a
`…-<number>` suffix (the existing file is never overwritten). Sidecars are written
create-if-absent, exactly as in card mode (rights from `~/.photohaul`, caption/IPTC
from a `photohaul.json` if present). A capture-time correction (`time_shift` /
`shot_tz`) is honored, applied to the renamed file via a crash-safe
copy-patch-replace. `--local` cannot be combined with `--rewrite`, `--source`, or
`--locked`/`--unlocked`.

## Requirements

Python 3, standard library only — no external packages.

In-camera lock detection is macOS-specific: it reads the BSD `uchg` (immutable)
flag that the exFAT driver maps the camera's protect bit onto.

## Design

Design notes live in [`docs/`](docs/) as dated, per-topic plan documents
(`YYYYMMDD_<topic>_plan.md`), so the directory reads as a chronological record
of how the design evolved.
