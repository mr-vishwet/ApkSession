#!/usr/bin/env python3
"""Bracket-balance the dashboard's inline script.

The HTML lives inside a Python string, so no browser is around to complain when an edit
leaves an orphan ')' - and a page with a parse error looks identical to a page that is
merely idle. This catches it without a browser.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from adrd.watch import PAGE  # noqa: E402

PAIRS = {"(": ")", "[": "]", "{": "}"}


def unbalanced(src: str) -> str | None:
    stack: list[tuple[str, int]] = []
    restore: list[str | None] = []
    i, line, n, in_str = 0, 1, len(src), None
    while i < n:
        c, nx = src[i], src[i + 1:i + 2]
        if c == "\n":
            line += 1
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == in_str:
                in_str = None
            elif in_str == "`" and c == "$" and nx == "{":
                # ${ } is real code: leave string mode and remember to come back
                stack.append(("${", line))
                restore.append(in_str)
                in_str = None
                i += 2
                continue
            i += 1
            continue
        if c in "\"'`":
            in_str = c
        elif c == "/" and nx == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue
        elif c == "/" and nx == "*":
            i += 2
            while i < n - 1 and src[i:i + 2] != "*/":
                line += src[i] == "\n"
                i += 1
            i += 2
            continue
        elif c in PAIRS:
            stack.append((c, line))
        elif c in ")]}":
            if not stack:
                return f"unmatched '{c}' on line {line}"
            op, ol = stack.pop()
            if op == "${":
                if c != "}":
                    return f"'{c}' on line {line} closes '${{' from line {ol}"
                in_str = restore.pop()
            elif PAIRS.get(op) != c:
                return f"'{c}' on line {line} closes '{op}' from line {ol}"
        i += 1
    if stack:
        return f"unclosed '{stack[-1][0]}' from line {stack[-1][1]}"
    return None


def main() -> int:
    src = PAGE.split("<script>")[1].split("</script>")[0]
    bad = unbalanced(src)
    if bad:
        print(f"dashboard script is broken: {bad} - the browser will not run it")
        return 1
    print("  ok   dashboard inline script balances")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
