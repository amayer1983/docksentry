#!/usr/bin/env python3
"""Contract linter — catches the bug classes that bit us in production.

1. INTRA-function return-arity drift: a function that returns a 3-tuple on
   one path and a bool/2-tuple on another. Callers unpack one shape and
   crash on the other ("cannot unpack non-iterable bool object", v1.37.1).

2. CROSS-class doppelgängers: same-named methods in different classes with
   DIFFERENT return arities. The v1.37.1 cascade crash was exactly this —
   bot._wait_healthy returned a bool while checker._wait_healthy returned a
   3-tuple, and the cascade called the wrong one.

Pure stdlib (ast). Exits non-zero on findings so it can gate commits.
Intentionally conservative: only flags when BOTH shapes are concrete
(literal tuple vs non-tuple); `return None` early-outs and bare `return`
are ignored (common "nothing to do" pattern that callers null-check).
"""
import ast
import os
import sys

APP = os.path.join(os.path.dirname(__file__), "..", "app")

# Same-name-different-contract pairs that are fine: intentionally different
# helpers that no caller mixes up (verified manually). Keep this list SHORT.
ALLOWED_DOPPELGANGERS = {
    "__init__",  # constructors trivially differ
    "_get_pinned",  # checker reads file, bot reads store — same List[str] contract
}


def _return_arity(node):
    """Shape of a return statement, judged only where syntax is conclusive:
      - literal N-tuple            → N
      - literal scalar constant    → "scalar"  (True/False/int/str — the
                                     v1.37.1 bug returned bare True/False)
      - `return` / `return None`   → None (early-out, ignored)
      - anything else (calls, names, subscripts, conditionals) → None:
        the value may itself be a tuple at runtime (delegation like
        `return self._update_standalone(...)`), so we can't judge it."""
    if node.value is None:
        return None
    if isinstance(node.value, ast.Constant):
        if node.value.value is None:
            return None
        return "scalar"
    if isinstance(node.value, ast.Tuple):
        return len(node.value.elts)
    return None


def _collect(path):
    """[(qualname, name, {arities}), ...] for every function in the file."""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    out = []

    class V(ast.NodeVisitor):
        def __init__(self):
            self.stack = []

        def _walk_fn(self, node):
            self.stack.append(node.name)
            # Collect returns owned by THIS function only — don't descend
            # into nested defs/lambdas (their returns aren't ours).
            arities = set()

            def owned_returns(n):
                for c in ast.iter_child_nodes(n):
                    if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                        continue
                    if isinstance(c, ast.Return):
                        a = _return_arity(c)
                        if a is not None:
                            arities.add(a)
                    owned_returns(c)

            owned_returns(node)
            out.append((".".join(self.stack), node.name, arities, path, node.lineno))
            self.generic_visit(node)
            self.stack.pop()

        def visit_FunctionDef(self, node):
            self._walk_fn(node)

        def visit_AsyncFunctionDef(self, node):
            self._walk_fn(node)

        def visit_ClassDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

    V().visit(tree)
    return out


def main():
    findings = []
    all_fns = []
    for fname in sorted(os.listdir(APP)):
        if not fname.endswith(".py"):
            continue
        all_fns.extend(_collect(os.path.join(APP, fname)))

    # 1. intra-function arity drift
    for qual, name, arities, path, line in all_fns:
        if len(arities) > 1:
            findings.append(
                f"MIXED-ARITY {os.path.basename(path)}:{line} {qual} returns {sorted(arities, key=str)} "
                f"— callers unpacking one shape will crash on the other")

    # 2. cross-class doppelgängers with different arities
    by_name = {}
    for qual, name, arities, path, line in all_fns:
        if "." not in qual or name in ALLOWED_DOPPELGANGERS or not arities:
            continue
        by_name.setdefault(name, []).append((qual, arities, os.path.basename(path), line))
    for name, entries in by_name.items():
        if len(entries) < 2:
            continue
        shapes = {frozenset(a) for _, a, _, _ in entries}
        if len(shapes) > 1:
            desc = "; ".join(f"{q} → {sorted(a, key=str)} ({p}:{ln})" for q, a, p, ln in entries)
            findings.append(
                f"DOPPELGANGER '{name}' has conflicting return shapes: {desc} "
                f"— the v1.37.1 _wait_healthy crash class")

    if findings:
        print(f"❌ {len(findings)} contract finding(s):")
        for f in findings:
            print("  -", f)
        return 1
    print(f"✅ contract lint clean ({len(all_fns)} functions checked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
