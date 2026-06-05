# Plan: caption template (`photohaul.json`)

Status: implemented (2026-06-05)

## Purpose
Stamp the *repetitive boilerplate* of a sports caption (teams, event, venue,
location, date, credit) onto every frame at ingest, leaving the per-photo
play description for Lightroom. Per-shoot, so it lives in the destination
folder, not the global config ([[20260605_config_file_plan]]).

## File
`photohaul.json` in the destination directory. **Auto-detected** on haul — if
present, it's applied; no flag needed to use it. A flag scaffolds a blank one.

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

## Caption grammar
Any blank key is omitted, and its connective word drops with it.

1. **Matchup**: `"{teamA} vs {teamB}"` — only if *both* are present (one alone →
   matchup omitted).
2. `event`
3. Place/time, in order: `"at {venue}"`, `"{location}"`, `"on {date}"` — each
   only if present.
4. Items 1–3 are comma-joined and ended with a period.
5. Second sentence: `"Photo by {credit}."`

`date` is auto-filled per-frame from capture date, formatted `May 30, 2026`.
`credit` resolves: template `credit` → config `credit` → config `creator`
(first non-blank wins).

Examples (all filled):
> Team A vs Team B, State Finals, at City Arena, Orlando, FL on May 30, 2026. Photo by Your Name/yoursite.com.

(no event/venue):
> Team A vs Team B, Orlando, FL on May 30, 2026. Photo by Your Name/yoursite.com.

Caption → `dc:description`. If the grammar yields nothing *and* there's no
credit, no caption is written.

## When a sidecar is written
A sidecar is created/updated for any frame that has *something* to record:
- locked in-camera → `xmp:Label = Purple`, and/or
- `creator`/`copyright` from config, and/or
- a caption from the template.

A plain unlocked frame with no config and no template still gets no sidecar
(unchanged from the initial behavior). All writes are merges; see
[[20260605_xml_reserialization_plan]].

## CLI additions
- `--init-template` — write a blank `photohaul.json` into the destination and
  exit (touches nothing else).
- `--rewrite` — re-apply (merge) sidecars for every frame already present, e.g.
  after editing `photohaul.json` or locking more frames in-camera.
