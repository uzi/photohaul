# Plan: IPTC field expansion (SID/editor-friendly sidecars)

Status: implemented (2026-06-06)

Builds on the caption/template work in [[20260605_caption_and_template_plan]] and
reuses the corrected capture time from [[20260605_capture_time_offset_plan]] for the
caption date. Pure sidecar-content work: **no filename touched**, so the
intrinsic-naming idempotency invariant from [[20260604_initial_plan]] is untouched and
the raw is unaffected (no in-place patch — this is all XMP).

## Purpose
Today's sidecar carries the three fields editors read first — `dc:description`
(caption), `dc:creator` (byline), `dc:rights` (copyright). That's correct but thin.
This adds the surrounding IPTC fields a sports-desk / SID workflow expects, all of
which are **constant for a shoot** and therefore belong in the folder-level
`photohaul.json` scaffold. The per-image action sentence and player IDs stay a manual
Lightroom pass — the script writes "batch truth," not per-frame truth.

## Scope — the fields to generate
The eleven properties requested, with their XMP homes and value types:

| Source (`photohaul.json`) | XMP property            | Type        |
|---------------------------|-------------------------|-------------|
| (assembled caption)       | `dc:description`        | lang-Alt    |
| `homeShort`+`awayShort`+`sport` | `photoshop:Headline` | simple text |
| `credit`                  | `photoshop:Credit`      | simple text |
| `source`                  | `photoshop:Source`      | simple text |
| `city`                    | `photoshop:City`        | simple text |
| `state`                   | `photoshop:State`       | simple text |
| `country`                 | `photoshop:Country`     | simple text |
| `venue`                   | `Iptc4xmpCore:Location` | simple text |
| `assignment`              | `photoshop:Instructions`| simple text |
| `rightsUsage`             | `xmpRights:UsageTerms`  | lang-Alt    |
| (assembled keywords)      | `dc:subject`            | Bag         |

Existing `xmp:Label`, `dc:creator`, `dc:rights` are unchanged.

**Decisions (kept simple):**
- Structured `city`/`state`/`country` store **whatever the user types** ("Springfield",
  "Calif.", "USA") and are written verbatim. No full-name→AP-abbreviation derivation,
  no state table.
- `credit` serves double duty: the caption byline *and* `photoshop:Credit` (same
  string). No separate `byline` key.
- `dc:creator` still comes from `~/.photohaul`; `photoshop:Source` is the agency/owner,
  distinct from the byline.
- `rightsUsage` → `xmpRights:UsageTerms` is the **highest-value** field (licensing
  language that travels with the file). It is a **lang-Alt**, not simple text.

## New `photohaul.json` schema
Breaking change (nothing uses the old schema yet; not documented as a migration):

```json
{
  "profile": "college",
  "sport": "women's volleyball",
  "event": "NCAA women's volleyball match",
  "homeTeam": "Lakeside University Owls",
  "awayTeam": "Riverside College Bears",
  "homeShort": "Lakeside",
  "awayShort": "Riverside",
  "venue": "Memorial Arena",
  "city": "Springfield",
  "state": "Calif.",
  "country": "USA",
  "conference": "West Coast Conference",
  "credit": "Your Name / ExamplePhoto",
  "source": "ExamplePhoto",
  "rightsUsage": "Editorial use only. No resale, advertising, NIL, merchandise, or commercial use without written permission. Credential restrictions may apply.",
  "assignment": "",
  "time_shift": "",
  "shot_tz": ""
}
```

`time_shift`/`shot_tz` are unchanged (capture-time correction). `profile` still selects
the rights preset. Blank fields are omitted from every output, exactly as today.

## Caption (`dc:description`) — folder scaffold in AP style
`build_caption` keeps producing the **context** sentence (the per-image action verb +
player IDs are added by hand in Lightroom). Changes:
- Use the short team names: `{homeShort} vs {awayShort}`.
- Append `event` (generic description), then place (`at {venue}`, `{city}, {state}`),
  then the AP date, then the credit in parentheses at the very end.
- AP date format via a new `ap_date(captured)`: `Weekday, Mon. D, YYYY` — weekday name
  + AP month abbreviation (`Jan. Feb. Aug. Sept. Oct. Nov. Dec.` abbreviated; `March
  April May June July` spelled out) + day + year. Replaces `caption_date`.

Example scaffold:
> Lakeside vs Riverside, NCAA women's volleyball match, at Memorial
> Arena, Springfield, Calif. on Friday, Oct. 3, 2025. (Photo by Your Name / ExamplePhoto)

The action sentence ("Jane Doe (7) goes up for a kill …") is appended per-frame later.

## Keywords (`dc:subject`)
Deterministic Bag from the distinct, non-empty values of `sport`, `homeShort`,
`awayShort`, `homeTeam`, `awayTeam`, `conference` (order fixed, deduped). No splitting
or stemming — keep it a plain controlled list the user can extend in the template.

## Code touch points (`src/photohaul.py`)
- **`_q`** — resolve against `_ALL_NS = {**XMP_NS, **_EXTRA_NS}` so `photoshop`,
  `xmpRights`, `Iptc4xmpCore` resolve (they are already registered for round-tripping;
  only `_q`'s lookup table is too narrow). Backward compatible superset.
- **New setters** beside `_set_seq`/`_set_alt`: `_set_simple(desc, qname, value)` and
  `_set_bag(desc, qname, values)`.
- **`write_sidecar`** — after the existing `dc:description` block, write each present
  field: the simple-text group via `_set_simple`, `usage_terms` via `_set_alt` (it's
  lang-Alt), `keywords` via `_set_bag`. Each guarded by `fields.get(...)` and preceded
  by `_clear_prop` so `--rewrite` still replaces-not-clobbers. The `any(fields.values())`
  short-circuit still holds (an empty keywords list is falsy).
- **`ap_date(captured)`** replaces `caption_date`; add an AP month-abbreviation map.
- **`build_caption`** — reworked for the new keys (short teams, event, place, AP date,
  parenthetical credit).
- **`fields_for`** — assemble the widened fields dict: `headline`, `credit`, `source`,
  `city`, `state`, `country`, `location` (=`venue`), `instructions` (=`assignment`),
  `usage_terms` (=`rightsUsage`), `keywords` (list). `{year}` in `copyright` still
  expands from `frame.captured`; all the new fields are template-constant.
- **`TEMPLATE_KEYS`** and **`write_template`** — new schema for `--init-template`.
- **`--help` epilog, `README.md`, `CLAUDE.md`** — document the new keys and that the
  sidecar now carries the IPTC structured set.

## `--rewrite` interaction
All new fields are folder-constant (read from the template, which `--rewrite` already
loads), and the caption date derives from the already-corrected dest EXIF. So
`--rewrite` populates them on existing sidecars and merges them in, preserving Lightroom
develop edits — same `_clear_prop`/merge path as the current fields. No per-card data
needed.

## Recommended order
1. `_q` fix + `_set_simple`/`_set_bag` + the simple `photoshop:*` fields +
   `xmpRights:UsageTerms` + `Iptc4xmpCore:Location` (the high-value, low-ambiguity set).
2. AP caption rework (`ap_date`, `build_caption`) + `dc:subject` keywords.
3. New `--init-template` scaffold + docs.

## Follow-up additions (2026-06-06)
Two items first deferred were added in a follow-up, since each closed a real
consistency gap:
- **`photoshop:DateCreated`** (ISO-8601 + offset). Per-frame, so it lives in `fields_for`
  (not the cached `_iptc`); `Frame` now carries the effective `offset` (shot_tz target when
  set, else the recorded offset; the dest's corrected offset under `--rewrite`). Without
  it the structured date would read raw EXIF while the filename/caption reflect
  shot_tz/time_shift corrections — they'd disagree.
- **AP state-abbreviation derivation.** `state` was double-duty: the caption wants the AP
  abbreviation ("Calif.") but `photoshop:State` wants the full name ("California"). The
  template now holds the full name (the single source of truth); `_ap_state()` derives the
  AP abbreviation for the caption text only. Unknown/never-abbreviated values pass through,
  so a half-filled table never breaks a caption.

## Non-goals (noted for later)
- **`dc:title`** (Object Name) — marginal; the filename slug already identifies the file.
- **Per-image captioning** (action verb, player numbers) — stays a manual Lightroom pass.
- **Manifest / copy verification** — a separate effort, not metadata.

## Validation before done
1. A fully-filled template writes all eleven properties with the right XMP types
   (simple vs lang-Alt vs Bag) and conventional prefixes (no `ns0:`); parse the sidecar
   and assert each.
2. Blank fields are omitted (empty `assignment`/`rightsUsage` → no element).
3. `ap_date`: a May date stays "May", an October date becomes "Oct.", weekday correct;
   spot-check March–July spelled out.
4. `--rewrite` against a sidecar carrying mock Lightroom `crs:*` develop edits: the new
   IPTC fields are added/updated and the `crs:*` block round-trips untouched.
5. Re-running `--rewrite` is stable (replace-not-duplicate; one `photoshop:City`, not two).
6. An unparseable sidecar is still reported and left as-is (unchanged behavior).
