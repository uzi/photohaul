# Plan: client profiles (seed photohaul.json from a ~/.photohaul section)

Status: implemented (2026-06-06)

Extends the rights profiles of [[20260605_profiles_plan]] into full **client profiles**:
a `~/.photohaul` section can now carry not just rights but the per-shoot scaffold values
that are constant for a given client/venue (home team, venue, city, state, conference,
usage terms…), and `--init --profile NAME` snapshots them into the folder's
`photohaul.json`. Uses the field set from [[20260606_iptc_fields_plan]]. Also renames
`--init-template` → `--init` and documents every INI and JSON field with examples.

## Motivation
One photographer, several recurring clients. For the high school and univisity, I almost
always shoot in their home towns, they're the home team, and rights differ per client; for a club
like ClubName only the usage terms are stable and everything else changes per shoot.
Today `--init-template` writes an all-blank `photohaul.json`, so those stable values get
retyped every shoot. A client profile lets `--init --profile smc` pre-fill them once.

## Two-layer model (no precedence conflict)
The runtime precedence (verified in code) makes this clean rather than ambiguous:

- `source` (`photoshop:Source`) and `rightsUsage` (`xmpRights:UsageTerms`) are read at
  copy time **only** from `photohaul.json` (`fields_for` / `_iptc_fields`). The INI is
  never consulted for them. So the *only* way an INI-defined `source`/`rightsUsage`
  reaches output is by being copied into the JSON — which is exactly what `--init` does.
- `credit` resolves template → INI → `creator`, so the JSON (prefilled) always wins.

Therefore the design is a **one-time snapshot**: `--init --profile NAME` copies the
profile's values into `photohaul.json`; from then on the JSON is the single source of
truth and you edit it per shoot. **At copy time the INI still supplies only
`creator` / `copyright` / `credit` / `format`** — any scaffold keys placed in an INI
section are inert at runtime and used solely to seed `--init`. (This is worth stating in
docs so nobody wonders why an INI `venue` "isn't showing up" without an `--init`.)

## Behavior — `--init [--profile NAME]`
- Resolve the profile via the existing `load_config(NAME)`: `[default]` merged with the
  named section, and its built-in typo guard (`profile '…' not found` → exit) now also
  protects `--init`.
- For each scaffold key, fill from the resolved dict, else blank:
  `value = resolved.get(key.lower(), "")`.
- Write the `profile` key = `NAME` (or `""` when no `--profile`).
- `time_shift` / `shot_tz` are **always written blank** — they are per-shoot capture-time
  corrections, deliberately never profile state (keeps "corrections are explicit per
  shoot"; see [[20260605_capture_time_offset_plan]]).
- Bare `--init` (no `--profile`) still prefills from `[default]` (global rights/credit/
  source/usage seed every scaffold); `profile` stays `""`.
- Existing-file guard unchanged (refuse to overwrite an existing `photohaul.json`).
- `--init-template` is **removed** (single user; no alias kept).

### configparser case-folding
`configparser` lower-cases option names, so `homeTeam = Union` is stored as
`hometeam`. The prefill therefore looks up each camelCase JSON key case-folded
(`resolved.get(key.lower(), "")`), which works whether the INI writes `homeTeam` or
`hometeam`. Do **not** flip `optionxform` globally — `creator`/`copyright`/`credit`/
`format` are read lower-cased elsewhere and would break.

## Code touch points (`src/photohaul.py`)
- `write_template(dest_dir, profile, resolved)` — take the profile name + resolved dict;
  fill each key per the rule above. Switch the body to build an ordered dict and
  `json.dumps(d, indent=2, ensure_ascii=False)` so non-empty values are JSON-escaped
  correctly (quotes, `©`) while keeping the readable one-key-per-line layout. (Today it
  hand-writes `"key": ""`, which is unsafe once values are non-empty.)
- `main` — rename the arg to `--init` (dest `init`); in its branch do
  `config = load_config(args.profile)` then `write_template(dest_dir, args.profile, config)`.
- `--help` epilog, `README.md`, `CLAUDE.md` — rename the flag and add the field reference
  below.

## Field reference (to land in README + condensed in --help)

The three attribution fields are easy to confuse, so up front:
- **`creator`** = the person who made the photo (author/byline name) → `dc:creator`.
- **`credit`** = how the credit line should read / who to attribute → `photoshop:Credit`
  and the caption's "(Photo by …)".
- **`source`** = the owner/provider that holds & licenses the image (agency/archive),
  distinct from the author → `photoshop:Source`.

### `~/.photohaul` (INI) — read at copy time
`[default]` is inherited by every named section; a section overrides/adds.

| key | target / use | meaning | example |
|-----|--------------|---------|---------|
| `creator`   | `dc:creator` | photographer / rights-holder name | `Jane Roe` |
| `copyright` | `dc:rights`  | copyright notice; `{year}` expands per-frame from capture year | `© {year} Jane Roe` |
| `credit`    | `photoshop:Credit` + caption byline | attribution line; JSON `credit` overrides | `Jane Roe / yoursite.com` |
| `format`    | ingest filter | default file extension to copy when no CLI positional is given | `arw` |

Any **JSON scaffold key below may also appear in a section** — but only to seed `--init`;
it has no effect at copy time.

### `photohaul.json` (per-folder) — the shoot scaffold
| key | XMP target / use | meaning | example |
|-----|------------------|---------|---------|
| `profile`     | selects the INI section for rights | name of the `~/.photohaul` profile | `highschool` |
| `sport`       | `photoshop:Headline` + `dc:subject` | the sport | `women's volleyball` |
| `event`       | caption text (+ headline fallback) | generic event description | `conference volleyball match` |
| `homeTeam`    | `dc:subject` keyword | full home-team name | `Union High School` |
| `awayTeam`    | `dc:subject` keyword | full away-team name | `Riverside High School` |
| `homeShort`   | caption / headline / keyword | short home name (`{homeShort} vs. {awayShort}`) | `Union` |
| `awayShort`   | caption / headline / keyword | short away name | `Riverside` |
| `venue`       | `Iptc4xmpCore:Location` + caption | sublocation: the arena/venue within the city (`at {venue}`) | `Union High School Gymnasium` |
| `city`        | `photoshop:City` + caption | city of the shoot | `Springfield` |
| `state`       | `photoshop:State` (full) + caption (AP-abbrev) | full state name; caption auto-abbreviates AP-style ("Calif.") | `California` |
| `country`     | `photoshop:Country` | country; omitted from caption (AP omits domestically) | `USA` |
| `conference`  | `dc:subject` keyword | league / conference | `Example League` |
| `credit`      | `photoshop:Credit` + caption byline | attribution; overrides INI `credit` | `Jane Roe / yoursite.com` |
| `source`      | `photoshop:Source` | agency/owner that holds & licenses the image | `yoursite.com` |
| `rightsUsage` | `xmpRights:UsageTerms` | **licensing terms that travel with the file** — who may use it and how | `Editorial use only. No resale, advertising, NIL, merchandise, or commercial use without written permission.` |
| `assignment`  | `photoshop:Instructions` | **Special Instructions** — desk/assignment notes, embargoes, client-specific handling | `Embargoed until 2025-10-04 06:00 PT; school athletics internal use approved.` |
| `time_shift`  | capture-time correction | per-shoot clock correction; blank unless correcting | `` |
| `shot_tz`     | capture-time correction | per-shoot timezone of the shoot; blank unless correcting | `` |

## Worked example
`~/.photohaul`:
```ini
[default]
creator     = Jane Roe
copyright   = © {year} Jane Roe
credit      = Jane Roe / yoursite.com
source      = yoursite.com
rightsUsage = Creative Commons CC BY-NC-ND 4.0
format      = arw

[highschool]
homeTeam  = Union High School
homeShort = Union
venue     = Union High School Gymnasium
city      = Springfield
state     = California

[college]
homeTeam    = State University
homeShort   = State
venue       = University Arena
city        = Springfield
state       = California
conference  = Example League
rightsUsage = Editorial use only. No resale or commercial use without written permission.

[club]
# club shoots: only the usage terms are stable; team/venue change per shoot
rightsUsage = Client use only; no redistribution.
```

`photohaul --init --profile highschool` writes (inheriting credit/source/rightsUsage/
format from `[default]`, leaving per-shoot keys blank):
```json
{
  "profile": "highschool",
  "sport": "",
  "event": "",
  "homeTeam": "Union High School",
  "awayTeam": "",
  "homeShort": "Union",
  "awayShort": "",
  "venue": "Union High School Gymnasium",
  "city": "Springfield",
  "state": "California",
  "country": "",
  "conference": "",
  "credit": "Jane Roe / yoursite.com",
  "source": "yoursite.com",
  "rightsUsage": "Creative Commons CC BY-NC-ND 4.0",
  "assignment": "",
  "time_shift": "",
  "shot_tz": ""
}
```
`--profile college` would instead carry the university name/venue/conference and the
editorial usage terms; `--profile club` carries only the club usage terms, everything
else blank.

## Validation before done
1. `--init --profile highschool` against the example INI produces exactly the JSON above
   (inherited `[default]` keys present, per-shoot keys blank, `profile` set).
2. Bare `--init` prefills `[default]` rights/credit/source/usage, `profile` is `""`.
3. `--init --profile typo` exits with the existing not-found error; no file written.
4. A value containing `"` and `©` round-trips (JSON-escaped, then `load_template` reads it).
5. `--init` still refuses to overwrite an existing `photohaul.json`.
6. A profile section carrying scaffold keys does **not** alter copy-time output beyond
   rights (scaffold keys inert at runtime; only `--init` consumes them).
7. `--init-template` is gone (`--help` shows `--init`; README/CLAUDE updated).
