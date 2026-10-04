#!/usr/bin/env python3
"""One-time rename of FitPulse classes that collide with daisyUI 5.

Run from the repo root:
    python3 scripts/rename_legacy_classes.py            # dry run: prints every change
    python3 scripts/rename_legacy_classes.py --apply    # writes the files

Only whole class tokens are renamed, and only in these places:
  * class="..." attribute values in templates/**/*.html and workouts/**/*.py
    (tests and migrations skipped) — Django tags inside the value are fine;
  * CSS class selectors in static/css/main.css;
  * classList.add/remove/toggle/contains('<name>') calls in templates;
  * the JS selector strings listed in JS_SELECTOR_FILES ('.card' etc. inside quotes).
Python dict keys like "label", <label> tags and prose are never touched.

Buttons are deliberately NOT renamed (they switch to daisyUI's .btn), except
btn-accent → btn-primary and btn-full → w-full, which are template-only renames.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Collide with daisyUI → keep the old look under an fp- name until each page migrates.
LEGACY = {
    "card": "fp-card",
    "stat-value": "fp-stat-value",
    "badge": "fp-badge",
    "badge-accent": "fp-badge-accent",
    "badge-muted": "fp-badge-muted",
    "label": "fp-label",
}
# Button renames (markup only — their main.css rules are deleted in 02, not renamed).
MARKUP_ONLY = {
    "btn-accent": "btn-primary",
    "btn-full": "w-full",
}

CLASS_ATTR = re.compile(r'(\bclass=\\?")(.*?)(\\?")', re.S)
TOKEN = lambda name: re.compile(r'(?<![\w-])' + re.escape(name) + r'(?![\w-])')
JS_SELECTOR_FILES = ["templates/workouts/nutrition_targets.html"]


def rename_tokens(value, mapping):
    """Rename whole tokens in a class attribute value, leaving {% %} / {{ }} intact."""
    parts = re.split(r'(\{%.*?%\}|\{\{.*?\}\})', value, flags=re.S)
    out = []
    for i, part in enumerate(parts):
        if i % 2 == 1:          # a Django tag/variable: rename only bare words inside quotes-free text
            out.append(part)
            continue
        for old, new in mapping.items():
            part = TOKEN(old).sub(new, part)
        out.append(part)
    return "".join(out)


CLASSLIST = re.compile(r"""(classList\.(?:add|remove|toggle|contains)\(\s*['"])([\w-]+)(['"])""")


def fix_markup(text, mapping):
    text = CLASS_ATTR.sub(lambda m: m.group(1) + rename_tokens(m.group(2), mapping) + m.group(3), text)
    # JS: el.classList.add('btn-accent') etc. (symptoms.html, nutrition.html)
    return CLASSLIST.sub(lambda m: m.group(1) + mapping.get(m.group(2), m.group(2)) + m.group(3), text)


def fix_css(text):
    for old, new in LEGACY.items():
        text = re.sub(r'\.' + re.escape(old) + r'(?![\w-])', '.' + new, text)
    return text


def fix_js_selectors(text):
    for old, new in LEGACY.items():
        text = re.sub(r"""(['"])\.""" + re.escape(old) + r"""(?![\w-])""", r"\1." + new, text)
    return text


def main(apply):
    mapping = {**LEGACY, **MARKUP_ONLY}
    changed = {}
    files = list((ROOT / "templates").rglob("*.html"))
    files += [p for p in (ROOT / "workouts").rglob("*.py")
              if "tests" not in p.parts and "migrations" not in p.parts]
    for path in files:
        old = path.read_text()
        new = fix_markup(old, mapping)
        if str(path.relative_to(ROOT)) in JS_SELECTOR_FILES:
            new = fix_js_selectors(new)
        if new != old:
            changed[path] = (old, new)
    css = ROOT / "static/css/main.css"
    old = css.read_text()
    new = fix_css(old)
    if new != old:
        changed[css] = (old, new)

    total = 0
    for path, (old, new) in sorted(changed.items()):
        o, n = old.splitlines(), new.splitlines()
        diffs = [(i + 1, a, b) for i, (a, b) in enumerate(zip(o, n)) if a != b]
        total += len(diffs)
        print(f"\n{path.relative_to(ROOT)}  ({len(diffs)} lines)")
        for ln, a, b in diffs:
            print(f"  {ln}: - {a.strip()[:150]}\n  {' ' * len(str(ln))}  + {b.strip()[:150]}")
        if apply:
            path.write_text(new)
    print(f"\n{len(changed)} files, {total} changed lines. {'Written.' if apply else 'Dry run — pass --apply to write.'}")


if __name__ == "__main__":
    main("--apply" in sys.argv)
