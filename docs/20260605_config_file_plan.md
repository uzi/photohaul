# Plan: global config file (`~/.photohaul`)

Status: implemented (2026-06-05); file format later revised — see
[[20260605_profiles_plan]] (flat `key = value` → INI sections/profiles). The
fields, `{year}` token, and sidecar mapping below still apply.

## Purpose
Hold the user's *global rights defaults* — the stuff that rarely changes and
shouldn't be retyped per shoot. Read if present, silently ignored if absent
(no defaults baked into the code).

## Format
Plain **key = value**, UTF-8. Not JSON — flat global config reads better as
key/value. (The per-folder caption template stays JSON; see
[[20260605_caption_and_template_plan]].)

```
# ~/.photohaul
creator   = Your Name
copyright = © {year} Your Name / yoursite.com
credit    = Your Name/yoursite.com
```

Parsing rules:
- One `key = value` per line; split on the **first** `=`.
- Strip surrounding whitespace from key and value; lowercase the key.
- Blank lines and lines beginning with `#` are ignored.
- Value is the literal remainder of the line (no quoting; quotes are kept).

## Fields
| key | sidecar target | notes |
|-----|----------------|-------|
| `creator`   | `dc:creator` (rdf:Seq) | photographer / rights holder |
| `copyright` | `dc:rights` (rdf:Alt)  | `{year}` token expanded per-frame from capture date |
| `credit`    | (caption byline only)  | default for `Photo by …`; not its own metadata field |

## Tokens
- `{year}` — replaced with the frame's capture year (so `© {year} …` is always
  correct and never needs manual bumping). Expansion is per-frame.

## Behavior
- No file, or a field absent → that field is simply not written.
- All written values land in the `.xmp` sidecar only; the raw is never touched
  (see [[20260605_xml_reserialization_plan]]).
- Also document the config file and its fields/format in the --help output and README.md
