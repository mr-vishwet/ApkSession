"""Read-only Dart static analysis: routes, screens, navigation edges, modules.

Nothing here writes to the app tree. Output is a flow map that seeds the walk
script and names the chapters; it is heuristic and reports its own confidence.
"""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

MODULE_DIRS = ("features", "modules", "screens", "pages", "views", "ui")
GEN_SUFFIXES = (".g.dart", ".freezed.dart", ".gr.dart", ".config.dart")
ENTRY_WORDS = ("splash", "welcome", "onboard", "login", "signin", "auth")

ROUTE_DECL = re.compile(
    r"(?:GoRoute|PyStackRoute|CupertinoRoute|AdaptiveRoute|MaterialRoute|RoutePage)\s*\(",
    re.M)
# `path: '/login'` and `path: AppRoutes.login` are both common; the second has to be
# resolved later, once every route constant in the project is known.
PATH_KW = re.compile(r"\bpath\s*:\s*(?:r?['\"]([^'\"]+)['\"]|([A-Za-z_]\w*(?:\.\w+)*))")
NAME_KW = re.compile(r"\bname\s*:\s*(?:r?['\"]([^'\"]+)['\"]|([A-Za-z_]\w*(?:\.\w+)*))")
MAP_ENTRY = re.compile(r"['\"](/[A-Za-z0-9_\-/:.]*)['\"]\s*:\s*[\(\<]")
CASE_ENTRY = re.compile(r"case\s+['\"](/[A-Za-z0-9_\-/:.]*)['\"]\s*:")
CONST_ROUTE = re.compile(r"static\s+const\s+String\s+(\w+)\s*=\s*['\"](/[A-Za-z0-9_\-/:.]*)['\"]")
NAV_METHODS = ("pushNamedAndRemoveUntil", "pushReplacementNamedAndRemoveUntil",
               "pushReplacementNamed", "popAndPushNamed", "pushNamed", "replaceNamed",
               "goNamed", "pushReplacement", "push", "replace", "go")
NAV_RE = re.compile(
    r"\.(" + "|".join(sorted(NAV_METHODS, key=len, reverse=True)) + r")\s*\(\s*"
    r"(?:[\w.\s]*?,\s*)?(r?'[^']*'|r?\"[^\"]*\"|[A-Za-z_][\w.]*)")
SCREEN_CLASS = re.compile(r"class\s+([A-Z]\w*(?:Screen|Page|View))\b")
BUILD_TARGET = re.compile(r"=>\s*(?:const\s+)?([A-Z]\w*(?:Screen|Page|View))\s*\(")
RETURN_TARGET = re.compile(r"return\s+(?:const\s+)?([A-Z]\w*(?:Screen|Page|View))\s*\(")


def _rel(root: Path, f: Path) -> str:
    try:
        return str(f.relative_to(root))
    except ValueError:
        return str(f)


def _line(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def module_of(rel_path: str) -> str:
    parts = [p for p in rel_path.replace("\\", "/").split("/") if p]
    if parts and parts[0] == "lib":
        parts = parts[1:]
    for i, seg in enumerate(parts):
        if seg.lower() in MODULE_DIRS and i + 1 < len(parts):
            nxt = parts[i + 1]
            return nxt[:-5] if nxt.endswith(".dart") else nxt
    for seg in parts:
        if seg.lower() not in ("src", "view", "views", "screen", "screens", "page",
                               "pages", "widgets", "presentation"):
            return seg[:-5] if seg.endswith(".dart") else seg
    return "app"


def dart_files(root: Path) -> list[Path]:
    lib = root / "lib"
    if not lib.exists():
        raise FileNotFoundError(f"{root} has no lib/ directory - not a Flutter project")
    out = []
    for p in lib.rglob("*.dart"):
        if any(p.name.endswith(s) for s in GEN_SUFFIXES):
            continue
        if p.stat().st_size > 400_000:
            continue
        out.append(p)
    return sorted(out)


def app_id(root: Path) -> str | None:
    for cand in ("android/app/build.gradle.kts", "android/app/build.gradle"):
        f = root / cand
        if f.exists():
            m = re.search(r"applicationId\s*=?\s*[\"']([\w.]+)[\"']", f.read_text())
            if m:
                return m.group(1)
    xc = root / "ios/Runner.xcodeproj/project.pbxproj"
    if xc.exists():
        m = re.search(r"PRODUCT_BUNDLE_IDENTIFIER\s*=\s*([\w.\-]+)", xc.read_text())
        if m:
            return m.group(1)
    return None


def app_name(root: Path) -> str:
    pub = root / "pubspec.yaml"
    if pub.exists():
        m = re.search(r"^name:\s*(\S+)", pub.read_text(), re.M)
        if m:
            return m.group(1)
    return root.name


def _lit(m) -> str:
    return (m.group(1) or "") if m else ""


def _ref(m) -> str:
    return (m.group(2) or "") if m else ""


def _scan_go_router(text: str, rel: str) -> list[dict]:
    routes = []
    for m in ROUTE_DECL.finditer(text):
        window = text[m.end():m.end() + 1400]
        depth, i = 1, 0
        while i < len(window) and depth:
            if window[i] == "(":
                depth += 1
            elif window[i] == ")":
                depth -= 1
            i += 1
        body = window[:i]
        p, n = PATH_KW.search(body), NAME_KW.search(body)
        target = BUILD_TARGET.search(body) or RETURN_TARGET.search(body)
        if not (p or n):
            continue
        routes.append({
            "route": _lit(p) or _lit(n) or "",
            "path_ref": _ref(p) or _ref(n) or "",
            "name": _lit(n) or "",
            "name_ref": _ref(n),
            "screen": target.group(1) if target else "",
            "file": rel,
            "line": _line(text, m.start()),
            "package": "go_router",
        })
    return routes


def _scan_other(text: str, rel: str) -> list[dict]:
    routes = []
    for pat, pkg in ((MAP_ENTRY, "MaterialApp.routes"), (CASE_ENTRY, "onGenerateRoute")):
        for m in pat.finditer(text):
            target = BUILD_TARGET.search(text[m.end():m.end() + 400])
            routes.append({"route": m.group(1), "name": "", "path_ref": "",
                           "name_ref": "", "screen": target.group(1) if target else "",
                           "file": rel, "line": _line(text, m.start()),
                           "package": pkg})
    return routes


class ConstTable:
    """`AppRoutes.login` and `ApiEndpoints.login` are not the same string.

    Keying constants by member name alone silently resolved route paths to API
    endpoints in a real project, so the owning class is part of the key and a bare
    name is only trusted when exactly one class defines it.
    """

    def __init__(self) -> None:
        self.qualified: dict[str, str] = {}
        self.bare: dict[str, set] = {}

    def add(self, cls: str, name: str, route: str) -> None:
        if cls:
            self.qualified[f"{cls}.{name}"] = route
        self.bare.setdefault(name, set()).add(route)

    def resolve(self, ref: str) -> str:
        tail = ref.split(".")[-1]
        if "." in ref:
            hit = self.qualified.get(ref)
            if hit:
                return hit
        opts = self.bare.get(tail, set())
        return next(iter(opts)) if len(opts) == 1 else ""


def _constants(text: str, rel: str, table: "ConstTable") -> None:
    classes = [(m.start(), m.group(1)) for m in re.finditer(r"^\s*(?:abstract\s+)?class\s+(\w+)", text, re.M)]
    for m in CONST_ROUTE.finditer(text):
        owner = ""
        for pos, name in classes:
            if pos < m.start():
                owner = name
            else:
                break
        table.add(owner, m.group(1), m.group(2))


def _edges(text: str, rel: str) -> list[dict]:
    """Extraction only; targets resolve later once every declared route is known."""
    edges = []
    for m in NAV_RE.finditer(text):
        raw = m.group(2)
        quoted = raw.startswith(("'", '"', "r'", 'r"'))
        ref = raw.lstrip("r").strip("'\"")
        if not ref:
            continue
        edges.append({"verb": m.group(1), "ref": ref, "quoted": quoted,
                      "raw": ref, "file": rel, "line": _line(text, m.start()),
                      "from_module": module_of(rel), "to": "", "resolved": False})
    return edges


def _base(ref: str) -> str:
    return ref.split("?")[0].split("#")[0]


def build(root: Path | str) -> dict:
    root = Path(root).expanduser().resolve()
    sources = [(f, _rel(root, f), f.read_text(encoding="utf-8", errors="ignore"))
               for f in dart_files(root)]

    # Pass 1: route-name constants, declarations and screen classes.
    consts = ConstTable()
    raw_routes: list[dict] = []
    screens: dict[str, dict] = {}
    for f, rel, text in sources:
        _constants(text, rel, consts)
        raw_routes += _scan_go_router(text, rel)
        raw_routes += _scan_other(text, rel)
        for c in SCREEN_CLASS.finditer(text):
            screens.setdefault(c.group(1), {
                "class": c.group(1), "file": rel, "module": module_of(rel),
                "line": _line(text, c.start())})

    for r in raw_routes:
        if not r["route"] and r.get("path_ref"):
            r["route"] = consts.resolve(r["path_ref"])
        if not r["name"] and r.get("name_ref"):
            r["name"] = consts.resolve(r["name_ref"])
        r.pop("path_ref", None), r.pop("name_ref", None)
        r["module"] = module_of(r["file"])
        if not r["screen"]:
            r["screen"] = _guess_screen(r, screens)
        # A route declared in app_router.dart still belongs to the module that owns
        # the screen it shows; file-based grouping would collapse everything into core.
        if r["screen"] in screens:
            r["module"] = screens[r["screen"]]["module"]

    merged: dict[str, dict] = {}
    for r in raw_routes:
        key = _base(r["route"]) or r["name"] or f"{r['file']}#{r['line']}"
        cur = merged.setdefault(key, r)
        cur["seen_in"] = cur.get("seen_in", 1) + 1

    index: dict[str, dict] = {}
    for r in merged.values():
        if r["route"]:
            index.setdefault(_base(r["route"]), r)
        if r["name"]:
            index.setdefault(r["name"], r)
    def resolve_ref(ref: str) -> str:
        return consts.resolve(ref)

    # Pass 2: resolve navigation calls against the finished route table.
    navs: list[dict] = []
    for _, rel, text in sources:
        navs += _edges(text, rel)
    for e in navs:
        hit = None
        if e["quoted"]:
            base = _base(e["ref"])
            e["to"] = base
            hit = index.get(base)
            if not hit and not base.startswith("/"):
                hit = index.get(e["ref"])
        else:
            path = consts.resolve(e["ref"])
            e["to"] = path or e["ref"]
            hit = index.get(_base(path)) if path else index.get(e["ref"])
        e["resolved"] = bool(hit)
        if hit:
            e["to_module"] = hit["module"]
            e["to_screen"] = hit["screen"]
            e["to"] = hit["route"] or hit["name"]

    by_module: dict[str, dict] = defaultdict(
        lambda: {"module": "", "routes": [], "screens": [], "edges": []})
    for r in merged.values():
        m = by_module[r["module"]]
        m["module"] = r["module"]
        m["routes"].append(r)
        if r["screen"]:
            m["screens"].append(r["screen"])
    for s in screens.values():
        m = by_module[s["module"]]
        m["module"] = s["module"]
        if s["class"] not in m["screens"]:
            m["screens"].append(s["class"])
    for e in navs:
        m = by_module[e["from_module"]]
        m["module"] = e["from_module"]
        m["edges"].append(e)

    modules = sorted(by_module.values(), key=lambda m: -len(m["routes"]))
    for m in modules:
        m["routes"].sort(key=lambda r: r["file"])
        m["screens"] = sorted(set(m["screens"]))
        m["edges"] = sorted(m["edges"], key=lambda e: (e["file"], e["line"]))[:80]

    order = _demo_order(modules)
    declared = len([r for r in merged.values() if r["route"].startswith("/")])
    return {
        "app": app_name(root),
        "package": app_id(root),
        "root": str(root),
        "dart_files": len(sources),
        "routes": sorted(merged.values(), key=lambda r: (r["module"], r["route"])),
        "modules": [{"module": m["module"], "route_count": len(m["routes"]),
                     "screens": m["screens"],
                     "routes": [r["route"] or r["name"] for r in m["routes"]]}
                    for m in modules],
        "edges": [e for e in navs if e["to"]][:600],
        "demo_order": order,
        "confidence": {
            "declared_routes": declared,
            "resolved_pushes": sum(1 for e in navs if e["resolved"] and e["to"]),
            "unresolved_pushes": sum(1 for e in navs if not e["resolved"]),
            "note": ("route table is exact for go_router/MaterialApp declarations; "
                     "module grouping is inferred from folder structure and needs "
                     "a human pass before chapters are trusted"),
        },
    }


def _guess_screen(route: dict, screens: dict[str, dict]) -> str:
    token = re.sub(r"[/_\-:.]", " ", route["route"] or route["name"]).lower().split()
    scored = []
    for name in screens:
        low = re.sub(r"(screen|page|view)$", "", name).lower()
        if any(t in low for t in token if len(t) > 2):
            scored.append((sum(len(t) for t in token if t in low), name))
    return max(scored)[1] if scored else ""


def _demo_order(modules: list[dict]) -> list[str]:
    """Entry-ish modules first: splash/onboarding/login tend to be the opening chapters."""
    def hay(m: dict) -> str:
        paths = " ".join(r["route"] for r in m["routes"])
        return f"{m['module']} {paths} {' '.join(m['screens'])}".lower()

    entry = [m["module"] for m in modules if any(w in hay(m) for w in ENTRY_WORDS)]
    rest = [m["module"] for m in modules if m["module"] not in entry]
    ordered: list[str] = []
    for m in entry + rest:
        if m not in ordered:
            ordered.append(m)
    return ordered


def markdown(flow: dict) -> str:
    lines = [f"# Flow map - {flow['app']}", "",
             f"- source: `{flow['root']}`",
             f"- package id: `{flow.get('package') or 'unknown'}`",
             f"- dart files scanned: {flow['dart_files']}",
             f"- routes declared: {flow['confidence']['declared_routes']}",
             f"- navigation calls resolved: {flow['confidence']['resolved_pushes']} "
             f"(unresolved {flow['confidence']['unresolved_pushes']})", "",
             "> Route table is read from source. Module grouping is folder-based inference - "
             "review the suggested order before recording.", "",
             "## Suggested demo order", ""]
    for i, m in enumerate(flow["demo_order"], 1):
        lines.append(f"{i}. {m}")
    lines += ["", "## Modules", ""]
    for m in flow["modules"]:
        lines.append(f"### {m['module']}  ({m['route_count']} routes)")
        for r in m["routes"]:
            lines.append(f"- `{r}`")
        if m["screens"]:
            lines.append(f"- screens: {', '.join(m['screens'][:24])}")
        lines.append("")
    lines += ["## Route inventory", "",
              "| route | name | screen | module | file:line |", "|---|---|---|---|---|"]
    for r in flow["routes"]:
        lines.append(f"| `{r['route']}` | {r['name']} | {r['screen']} | "
                     f"{r['module']} | `{r['file']}:{r['line']}` |")
    lines += ["", "## Navigation edges", "",
              "| from module | verb | to | resolved | file:line |", "|---|---|---|---|---|"]
    for e in flow["edges"]:
        lines.append(f"| {e['from_module']} | {e['verb']} | `{e['to']}` | "
                     f"{'yes' if e['resolved'] else 'no'} | `{e['file']}:{e['line']}` |")
    return "\n".join(lines) + "\n"
