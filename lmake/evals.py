from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .config import ProjectConfig
from .errors import ConfigError, RunNotFoundError
from .state import ProjectState


EVAL_SCHEMA = "lmake.eval_cases.v0"


@dataclass(frozen=True)
class EvalCaseResult:
    name: str
    status: str
    reason: str
    output: str | None = None


@dataclass(frozen=True)
class EvalSuiteResult:
    target: str
    run_id: str
    suite_path: str
    results: list[EvalCaseResult]

    @property
    def passed(self) -> int:
        return sum(1 for result in self.results if result.status == "pass")

    @property
    def failed(self) -> int:
        return sum(1 for result in self.results if result.status == "fail")

    def summary(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "run_id": self.run_id,
            "suite_path": self.suite_path,
            "passed": self.passed,
            "failed": self.failed,
        }

    def rows(self) -> list[dict[str, Any]]:
        return [
            {
                "case": result.name,
                "status": result.status,
                "output": result.output or "",
                "reason": result.reason,
            }
            for result in self.results
        ]


def eval_path(root: Path, target: str) -> Path:
    return root / "eval_cases" / f"{target}.yaml"


def listify(value: Any, field_name: str) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def as_strings(value: Any, field_name: str) -> list[str]:
    values = listify(value, field_name)
    if not all(isinstance(item, str) for item in values):
        raise ConfigError(f"{field_name} must be a string or list of strings.")
    return values


def as_int(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{field_name} must be an integer.")
    return value


def load_eval_suite(config: ProjectConfig, target: str, suite_path: Path | None = None) -> dict[str, Any] | None:
    path = suite_path or eval_path(config.root, target)
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ConfigError(f"could not parse eval suite {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"eval suite {path} must be a YAML mapping.")
    version = data.get("version", 1)
    if version not in (1, "1"):
        raise ConfigError(f"eval suite {path} version must be 1.")
    if data.get("target") not in (None, target):
        raise ConfigError(f"eval suite {path} is for target {data.get('target')!r}, not {target!r}.")
    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ConfigError(f"eval suite {path} must define a non-empty cases list.")
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise ConfigError(f"eval case {index} in {path} must be a mapping.")
        if not isinstance(case.get("name"), str) or not case.get("name"):
            raise ConfigError(f"eval case {index} in {path} must have a non-empty name.")
    data["_path"] = path
    return data


def output_item(manifest: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    outputs = manifest.get("outputs", [])
    if not outputs:
        raise ConfigError(f"run {manifest.get('run_id')} has no outputs to evaluate.")
    requested = case.get("output")
    if requested is None:
        if len(outputs) == 1:
            return outputs[0]
        raise ConfigError(f"eval case {case.get('name')!r} must specify output because the run has multiple outputs.")
    for item in outputs:
        if item.get("name") == requested or item.get("path") == requested:
            return item
    raise ConfigError(f"eval case {case.get('name')!r} references unknown output {requested!r}.")


def text_from_output(state: ProjectState, item: dict[str, Any]) -> str:
    digest = str(item.get("sha256", ""))
    if not digest:
        raise ConfigError(f"output record for {item.get('path')} is missing sha256.")
    path = state.object_path(digest)
    if not path.exists():
        raise RunNotFoundError(f"artifact object sha256:{digest} is missing from .lmcache")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"eval output {item.get('path')} is not UTF-8 text.") from exc


def word_count(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text))


def headings(text: str) -> list[str]:
    found = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            found.append(stripped.lstrip("#").strip())
    return found


def evaluate_case(text: str, case: dict[str, Any]) -> tuple[bool, str]:
    checks = 0
    missing = [needle for needle in as_strings(case.get("contains"), f"{case.get('name')}.contains") if needle not in text]
    if case.get("contains") is not None:
        checks += 1
    if missing:
        return False, "missing text: " + ", ".join(repr(item) for item in missing)

    present = [needle for needle in as_strings(case.get("not_contains"), f"{case.get('name')}.not_contains") if needle in text]
    if case.get("not_contains") is not None:
        checks += 1
    if present:
        return False, "forbidden text present: " + ", ".join(repr(item) for item in present)

    regexes = as_strings(case.get("regex"), f"{case.get('name')}.regex")
    if case.get("regex") is not None:
        checks += 1
    for pattern in regexes:
        if re.search(pattern, text, flags=re.MULTILINE) is None:
            return False, f"regex did not match: {pattern!r}"

    required = as_strings(case.get("required_headings"), f"{case.get('name')}.required_headings")
    if case.get("required_headings") is not None:
        checks += 1
    actual_headings = headings(text)
    missing_headings = [heading for heading in required if heading not in actual_headings]
    if missing_headings:
        return False, "missing heading: " + ", ".join(repr(item) for item in missing_headings)

    words = word_count(text)
    min_words = as_int(case.get("min_words"), f"{case.get('name')}.min_words")
    if min_words is not None:
        checks += 1
        if words < min_words:
            return False, f"word count {words} is below minimum {min_words}"
    max_words = as_int(case.get("max_words"), f"{case.get('name')}.max_words")
    if max_words is not None:
        checks += 1
        if words > max_words:
            return False, f"word count {words} is above maximum {max_words}"

    byte_len = len(text.encode("utf-8"))
    min_bytes = as_int(case.get("min_bytes"), f"{case.get('name')}.min_bytes")
    if min_bytes is not None:
        checks += 1
        if byte_len < min_bytes:
            return False, f"byte length {byte_len} is below minimum {min_bytes}"
    max_bytes = as_int(case.get("max_bytes"), f"{case.get('name')}.max_bytes")
    if max_bytes is not None:
        checks += 1
        if byte_len > max_bytes:
            return False, f"byte length {byte_len} is above maximum {max_bytes}"

    if checks == 0:
        raise ConfigError(f"eval case {case.get('name')!r} does not define any checks.")
    return True, f"{checks} check(s) passed"


def evaluate_target(config: ProjectConfig, target: str, *, run_id: str | None = None, suite_path: Path | None = None) -> EvalSuiteResult | None:
    config.target(target)
    suite = load_eval_suite(config, target, suite_path)
    if suite is None:
        return None
    state = ProjectState(config.root)
    state.ensure_dirs()
    state.recover_staged_runs()
    manifest = state.load_manifest(run_id) if run_id else state.latest_manifest_for_target(target)
    if manifest is None:
        raise RunNotFoundError(f"target {target!r} has no latest run to evaluate.")
    if manifest.get("target") != target:
        raise ConfigError(f"run {manifest.get('run_id')} is for target {manifest.get('target')!r}, not {target!r}.")

    results = []
    for case in suite["cases"]:
        item = output_item(manifest, case)
        text = text_from_output(state, item)
        ok, reason = evaluate_case(text, case)
        results.append(EvalCaseResult(
            name=str(case.get("name")),
            status="pass" if ok else "fail",
            reason=reason,
            output=str(item.get("name") or item.get("path")),
        ))
    return EvalSuiteResult(
        target=target,
        run_id=str(manifest.get("run_id")),
        suite_path=str(suite["_path"]),
        results=results,
    )
