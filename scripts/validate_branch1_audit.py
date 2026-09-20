"""Run the branch1 production-source static audit.

The audit intentionally scans production modules and operational scripts, not
tests. Test compatibility fixtures may carry a local ``branch1-audit`` ignore
annotation, but those fixtures are outside this gate by design.
"""
from __future__ import annotations

import argparse
import ast
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOTS = (PROJECT_ROOT / "src", PROJECT_ROOT / "scripts")
SELF_PATH = Path(__file__).resolve()
_IGNORED_PARTS = frozenset({"__pycache__", "node_modules", "dist", "test-results", "playwright-report"})
_GENERIC_EXCEPTIONS = {"Exception", "BaseException"}
_RULES = {
    "generic-exception",
    "or-heading",
    "float-base-compare",
    "setdefault-mode",
    "class-name-introspection",
    "queue-empty",
    "modulo-five-sentinel",
    "fake-thousand-bonus",
    "hardcoded-search-area",
}


@dataclass(frozen=True)
class Finding:
    rule: str
    path: str
    line: int
    message: str


def _python_files() -> tuple[Path, ...]:
    paths = []
    for root in SCAN_ROOTS:
        if not root.exists():
            continue
        paths.extend(
            path
            for path in root.rglob("*.py")
            if not _IGNORED_PARTS.intersection(path.parts) and path.resolve() != SELF_PATH
        )
    return tuple(sorted(paths))


def _source_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8-sig").splitlines()


def _is_ignored(lines: list[str], line: int, rule: str) -> bool:
    if line < 1 or line > len(lines):
        return False
    text = lines[line - 1]
    marker = "branch1-audit: ignore"
    if marker not in text:
        return False
    ignored = text.split(marker, 1)[1].strip().split()
    return not ignored or "all" in ignored or rule in ignored


def _finding(path: Path, line: int, rule: str, message: str, lines: list[str]):
    if rule not in _RULES or _is_ignored(lines, line, rule):
        return None
    return Finding(rule, str(path.relative_to(PROJECT_ROOT)), line, message)


def _call_names(node: ast.AST) -> set[str]:
    names = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        function = child.func
        if isinstance(function, ast.Name):
            names.add(function.id)
        elif isinstance(function, ast.Attribute):
            names.add(function.attr)
    return names


def _exception_names(handler: ast.ExceptHandler) -> set[str]:
    if handler.type is None:
        return {"bare"}
    names = set()
    nodes = handler.type.elts if isinstance(handler.type, ast.Tuple) else (handler.type,)
    for node in nodes:
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


def _generic_exception_is_accounted_for(handler: ast.ExceptHandler) -> bool:
    body = ast.Module(body=handler.body, type_ignores=[])
    calls = _call_names(body)
    text = ast.unparse(body)
    # Cleanup wrappers that re-raise preserve the original failure contract.
    if any(isinstance(node, ast.Raise) for node in ast.walk(body)):
        return True
    if handler.name and any(
        isinstance(node, ast.Name) and node.id == handler.name
        for node in ast.walk(body)
    ):
        return True
    # API and evaluation paths emit an explicit error code/type even when the
    # underlying dependency exposes only a broad exception class.
    if "_api_error" in calls or "error_type" in text or "failure_category" in text:
        return True
    log_calls = {
        "debug", "info", "warning", "error", "exception", "critical",
    }
    return bool(calls & log_calls)


def _looks_like_float_base_compare(node: ast.Compare) -> bool:
    if not any(isinstance(operator, (ast.Eq, ast.NotEq)) for operator in node.ops):
        return False
    def attr_text(value: ast.AST) -> str:
        try:
            return ast.unparse(value)
        except (AttributeError, TypeError):
            return ""

    expressions = [attr_text(node.left), *(attr_text(item) for item in node.comparators)]
    non_literals = [value for value in expressions if value and not value.isdigit()]
    return (
        len(non_literals) >= 2
        and any("position" in value for value in non_literals)
        and any("base" in value for value in non_literals)
    )


def _visit_python(path: Path) -> list[Finding]:
    lines = _source_lines(path)
    try:
        tree = ast.parse("\n".join(lines), filename=str(path))
    except SyntaxError as exc:
        return [Finding("syntax", str(path.relative_to(PROJECT_ROOT)), exc.lineno or 1, str(exc))]

    findings: list[Finding] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            names = _exception_names(node)
            if names & _GENERIC_EXCEPTIONS or "bare" in names:
                if not _generic_exception_is_accounted_for(node):
                    result = _finding(
                        path,
                        node.lineno,
                        "generic-exception",
                        "generic exception lacks an error context, log, or explicit re-raise",
                        lines,
                    )
                    if result:
                        findings.append(result)
        elif isinstance(node, ast.Compare):
            if _looks_like_float_base_compare(node):
                result = _finding(
                    path,
                    node.lineno,
                    "float-base-compare",
                    "base/position comparison must use the shared pose tolerance",
                    lines,
                )
                if result:
                    findings.append(result)
        elif isinstance(node, ast.Call):
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "setdefault"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "mode"
            ):
                result = _finding(
                    path,
                    node.lineno,
                    "setdefault-mode",
                    "mode must be explicitly normalized, not defaulted",
                    lines,
                )
                if result:
                    findings.append(result)
            if isinstance(node.func, ast.Attribute) and node.func.attr == "empty":
                result = _finding(
                    path,
                    node.lineno,
                    "queue-empty",
                    "queue state must use a synchronization primitive, not empty()",
                    lines,
                )
                if result:
                    findings.append(result)
        elif isinstance(node, ast.Attribute):
            if node.attr == "__name__" and isinstance(node.value, ast.Attribute):
                if node.value.attr == "__class__":
                    result = _finding(
                        path,
                        node.lineno,
                        "class-name-introspection",
                        "use an explicit exception/type contract instead of __class__.__name__",
                        lines,
                    )
                    if result:
                        findings.append(result)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            if isinstance(node.right, ast.Constant) and node.right.value == 5:
                parent = next(
                    (candidate for candidate in ast.walk(tree)
                     if isinstance(candidate, ast.Compare)
                     and any(comparator is node for comparator in candidate.comparators)),
                    None,
                )
                if isinstance(parent, ast.Compare):
                    result = _finding(
                        path,
                        node.lineno,
                        "modulo-five-sentinel",
                        "deterministic sampling must not use a modulo-five sentinel",
                        lines,
                    )
                    if result:
                        findings.append(result)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            if any(
                isinstance(value, ast.Constant) and value.value == 1000
                for value in (node.left, node.right)
            ):
                result = _finding(
                    path,
                    node.lineno,
                    "fake-thousand-bonus",
                    "candidate scoring must not add a hard-coded 1000 bonus",
                    lines,
                )
                if result:
                    findings.append(result)

    for line_number, line in enumerate(lines, start=1):
        code = line.split("#", 1)[0]
        if re.search(r"\bor\s+heading\b(?!\s+is\b)", code):
            result = _finding(
                path,
                line_number,
                "or-heading",
                "heading presence must be checked explicitly, not with truthiness",
                lines,
            )
            if result:
                findings.append(result)
        if re.search(r"\bsearch_area\b", code) or re.search(r"\bsearch area\b", code, re.I):
            result = _finding(
                path,
                line_number,
                "hardcoded-search-area",
                "search area must come from the configured mission geometry",
                lines,
            )
            if result:
                findings.append(result)
    return findings


def _visit_non_python(path: Path) -> list[Finding]:
    lines = _source_lines(path)
    findings = []
    for line_number, line in enumerate(lines, start=1):
        code = line.split("//", 1)[0]
        if re.search(r"\bsearch_area\b|\bsearch area\b", code, re.I):
            result = _finding(
                path,
                line_number,
                "hardcoded-search-area",
                "search area must come from the configured mission geometry",
                lines,
            )
            if result:
                findings.append(result)
    return findings


def run_audit() -> dict:
    findings: list[Finding] = []
    scanned: list[str] = []
    for root in SCAN_ROOTS:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if (
                not path.is_file()
                or _IGNORED_PARTS.intersection(path.parts)
                or "tests" in path.parts
                or path.resolve() == SELF_PATH
            ):
                continue
            if path.suffix == ".py":
                scanned.append(str(path.relative_to(PROJECT_ROOT)))
                findings.extend(_visit_python(path))
            elif path.suffix in {".js", ".ts", ".tsx"}:
                scanned.append(str(path.relative_to(PROJECT_ROOT)))
                findings.extend(_visit_non_python(path))
    findings.sort(key=lambda item: (item.path, item.line, item.rule))
    return {
        "status": "passed" if not findings else "failed",
        "scanned_files": len(scanned),
        "finding_count": len(findings),
        "findings": [asdict(item) for item in findings],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    args = parser.parse_args(argv)
    payload = run_audit()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(
            f"branch1 static audit: {payload['status']} "
            f"({payload['scanned_files']} files, {payload['finding_count']} findings)"
        )
        for finding in payload["findings"]:
            print(
                f"{finding['path']}:{finding['line']}: "
                f"[{finding['rule']}] {finding['message']}"
            )
    return 0 if payload["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
