#!/usr/bin/env python3
"""Generate a dual-weight (solid+regular) FontAwesome subset CSS.

FontAwesome 7 Free `all.min.css` is ~90KB and ships every icon glyph rule plus
a large set of base utility classes (sizing, animations, stacking, list, ...).
The app uses 129 icons (no brands) and only a handful of base utilities
(e.g. `fa-spin` for spinners). This keeps:
  - the core `.fa`/`::before` rendering mechanism + `@font-face` for solid/regular
  - only the `.fa-NAME{--fa:"\\fXXX"}` rules for icons actually used
  - only base utility rules whose `fa-*` class is actually used in the app
  - only `@keyframes fa-X` for animations actually used
and drops the brands @font-face + all unused icons/utilities/keyframes.
"""
import os
import re

# Repo root = parent of the `scripts/` dir this file lives in.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "web/static/fontawesome/all.min.css")
OUT = os.path.join(ROOT, "web/static/fontawesome/fa-subset.min.css")

# Core style classes that must always be kept (their rules set up rendering).
CORE_STYLE = {"solid", "regular", "brands", "classic", "s", "r", "b"}


def scan_used_tokens():
    """Collect every `fa-<word>` class token used in the frontend source."""
    used = set()
    # search html, js, css (css may reference fa classes too)
    # IMPORTANT: skip the fontawesome assets themselves, or the scan would
    # re-collect every icon name from the very CSS we're subsetting.
    roots = [os.path.join(ROOT, "web/static"), os.path.join(ROOT, "web/templates")]
    paths = []
    for root in roots:
        for base, _, files in os.walk(root):
            bnorm = base.replace("\\", "/")
            if "fontawesome" in bnorm or "/dist/" in bnorm or "/build/" in bnorm:
                continue
            for f in files:
                if f.endswith((".html", ".js", ".css")):
                    paths.append(os.path.join(base, f))
    pat = re.compile(r"fa-[a-z0-9-]+")
    for p in paths:
        try:
            txt = open(p, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        for m in pat.findall(txt):
            used.add(m[3:])  # strip "fa-" prefix
    return used


def split_rules(css):
    rules, buf, depth = [], [], 0
    for ch in css:
        buf.append(ch)
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                rules.append("".join(buf))
                buf = []
    return rules


def classify(rule, used):
    s = rule.strip()
    if s.startswith("@font-face"):
        return None if "fa-brands" in s else s
    if s.startswith("@keyframes"):
        # @keyframes fa-X -> keep iff animation X is used
        m = re.match(r"@keyframes\s+fa-([a-z0-9-]+)", s)
        name = m.group(1) if m else None
        return s if (name and name in used) else None
    if s.startswith("@"):
        # @media and other at-rules: keep (safe; contains responsive utils)
        return s
    idx = s.find("{")
    if idx == -1:
        return s
    selector = s[:idx].strip()
    body = s[idx + 1:]
    # icon definition rule: single class `.fa-NAME{--fa:...}`
    m = re.match(r"^\.fa-([a-z0-9-]+)$", selector)
    if m and "--fa:" in body:
        return s if m.group(1) in used else None
    # other single-class base utility `.fa-X{...}`: keep iff X used
    m2 = re.match(r"^\.fa-([a-z0-9-]+)$", selector)
    if m2:
        name = m2.group(1)
        if name in CORE_STYLE or name in used:
            return s
        return None
    # compound / core selectors (e.g. `.fa-solid::before`, `.fa,.fa-brands{...}`): keep
    return s


def main():
    used = scan_used_tokens()
    with open(SRC, encoding="utf-8") as f:
        css = f.read()
    # all icon-def rules in source: .fa-NAME{--fa:...}
    src_icon_defs = set()
    for r in split_rules(css):
        s = r.strip()
        idx = s.find("{")
        if idx == -1:
            continue
        m = re.match(r"^\.fa-([a-z0-9-]+)$", s[:idx].strip())
        if m and "--fa:" in s[idx + 1:]:
            src_icon_defs.add(m.group(1))
    rules = split_rules(css)
    kept, dropped = [], 0
    kept_set = set()
    for r in rules:
        out = classify(r, used)
        if out is None:
            dropped += 1
        else:
            kept.append(out)
            idx = out.strip().find("{")
            if idx != -1:
                m = re.match(r"^\.fa-([a-z0-9-]+)$", out.strip()[:idx].strip())
                if m and "--fa:" in out.strip()[idx + 1:]:
                    kept_set.add(m.group(1))
    result = "".join(kept)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(result)
    # icon coverage check
    used_icons = sorted(src_icon_defs & used)
    missing = [i for i in used_icons if i not in kept_set]
    with open("/tmp/used_icons.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(used_icons) + "\n")
    print(f"used fa tokens (icons+utils+styles): {len(used)}")
    print(f"used icon defs (have --fa): {len(used_icons)}")
    print(f"missing icons in subset: {missing if missing else 'NONE'}")
    print(f"total rules: {len(rules)}  kept: {len(kept)}  dropped: {dropped}")
    print(f"subset size: {os.path.getsize(OUT)} bytes (was {os.path.getsize(SRC)} bytes)")


if __name__ == "__main__":
    main()
