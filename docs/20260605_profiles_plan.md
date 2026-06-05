# Plan: rights profiles (~/.photohaul sections)

Status: implemented (2026-06-05)

Supersedes the flat `key = value` format in [[20260605_config_file_plan]] (the
fields and `{year}` behavior there still apply; only the file format changes).

## Purpose
One camera, two contexts (personal vs work). A single global config would
stamp `© … / yoursite.com` on *everything*. Profiles let rights differ by
context while captions keep self-separating via the per-folder template
([[20260605_caption_and_template_plan]]).

Only **rights** differ between contexts (copyright / creator / credit); the
label (Purple) and extension stay the same, so a profile is just a named rights
preset.

## Config format
`~/.photohaul` becomes INI, parsed with stdlib `configparser`
(`interpolation=None` so `©`, `/`, and `{year}` pass through untouched;
`default_section='default'`). Keys in `[default]` are inherited by every profile;
a profile section overrides/adds.

```ini
[default]
creator   = Your Name
copyright = © {year} Your Name

[work]
copyright = © {year} Your Name / yoursite.com
credit    = Your Name/yoursite.com
```

- A section-less (old flat) file is treated as `[default]` for backward
  compatibility (catch `MissingSectionHeaderError`, re-parse with a synthesized
  `[default]` header).
- No config file → empty → no rights written (camera's embedded Artist/Copyright
  stands; sidecars appear only for locked → Purple).

## Profile selection
Active profile, highest precedence first:

1. `--profile NAME` flag
2. `"profile"` key in the destination's `photohaul.json`
3. none → use `[default]` only

A personal folder has no template, so it falls through to `[default]`
automatically — nothing branded leaks onto personal shots. A work folder
names itself once via its template.

Resolution:
- name given and section exists → `dict(parser[name])` (includes inherited
  `[default]` keys).
- no name → `dict(parser.defaults())` (just the base).
- name given but section missing → error and exit (typo guard).
- empty `"profile": ""` in the template is treated as not set.

## Template change
`photohaul.json` gains a top-level `"profile"` key; `--init-template` prefills it:

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

The caption grammar ignores `profile` (it only reads the caption keys), so the
extra key is harmless to captioning.

## Code touch points
- `load_config(profile)` → parse INI, resolve the profile, return the same flat
  dict `fields_for` already consumes (`creator`/`copyright`/`credit`). No change
  to caption assembly or sidecar writing.
- `main`: load template first → determine profile (`--profile` → template
  `profile` → none) → `load_config(profile)` → build `Haul`.
- `Haul` gains the active profile name for one reporting line
  (`Profile: work`).
- New CLI: `--profile NAME`. Document profiles in `--help` and README.
