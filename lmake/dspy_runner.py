from __future__ import annotations

import importlib
import importlib.util
import inspect
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

from .config import TargetConfig
from .errors import ConfigError
from .hashing import hash_json
from .providers import InputPart, build_user_message


@dataclass(frozen=True)
class DspyRunnerResult:
    outputs: dict[str, bytes] | None
    metadata: dict[str, Any]


def dspy_config(target: TargetConfig) -> dict[str, Any]:
    raw = target.raw.get("dspy", {}) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"target {target.name!r} dspy must be a mapping.")
    return raw


def dspy_action(target: TargetConfig) -> str:
    action = str(dspy_config(target).get("action") or "").strip().lower()
    if action:
        return action
    return "compile" if target.name.startswith("compile-") else "run"


def program_path(root: Path, target: TargetConfig) -> Path:
    raw_program = target.raw.get("program")
    if not isinstance(raw_program, str) or not raw_program:
        raise ConfigError(f"target {target.name!r} uses runner: dspy and must specify program: programs/foo.py")
    path = (root / raw_program).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise ConfigError(f"target {target.name!r} program must be inside the project root: {raw_program}") from exc
    if not path.exists() or not path.is_file():
        raise ConfigError(f"target {target.name!r} program file is missing: {raw_program}")
    if path.suffix != ".py":
        raise ConfigError(f"target {target.name!r} program must be a Python file: {raw_program}")
    return path


def import_optional_dspy() -> ModuleType | None:
    try:
        return importlib.import_module("dspy")
    except ImportError:
        return None


def load_program_module(root: Path, target: TargetConfig) -> ModuleType:
    path = program_path(root, target)
    module_name = f"_lmake_program_{target.name.replace('-', '_')}_{hash_json({'path': str(path), 'target': target.name})[:12]}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ConfigError(f"could not load program file for target {target.name!r}: {path}")

    root_s = str(root.resolve())
    parent_s = str(path.parent.resolve())
    for item in [root_s, parent_s]:
        if item not in sys.path:
            sys.path.insert(0, item)
    try:
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    except ImportError as exc:
        if exc.name == "dspy":
            raise ConfigError("runner: dspy requires `python -m pip install -e '.[dspy]'` or `python -m pip install dspy`.") from exc
        raise


def configure_dspy_if_available(dspy: ModuleType | None, target: TargetConfig) -> dict[str, Any]:
    cfg = dspy_config(target)
    configured = False
    if dspy is None:
        return {"available": False, "configured": False}
    if cfg.get("configure") is False:
        return {"available": True, "configured": False}
    if target.provider.lower() == "mock" or target.model.startswith("mock/") or target.model == "mock":
        return {"available": True, "configured": False}

    lm_kwargs = dict(target.params)
    try:
        lm = dspy.LM(target.model, **lm_kwargs)
        if hasattr(dspy, "configure"):
            dspy.configure(lm=lm)
        elif hasattr(dspy, "settings") and hasattr(dspy.settings, "configure"):
            dspy.settings.configure(lm=lm)
        else:
            raise ConfigError("installed DSPy package has no configure/settings.configure entrypoint.")
        configured = True
    except Exception as exc:
        raise ConfigError(f"could not configure DSPy LM for target {target.name!r}: {exc}") from exc
    return {"available": True, "configured": configured}


def hook(module: ModuleType, cfg: dict[str, Any], key: str, default: str) -> Callable[..., Any] | None:
    name = cfg.get(key, default)
    if name is None:
        return None
    if not isinstance(name, str) or not name:
        raise ConfigError(f"dspy.{key} must name a function.")
    value = getattr(module, name, None)
    if value is None:
        return None
    if not callable(value):
        raise ConfigError(f"dspy.{key}={name!r} is not callable.")
    return value


def call_hook(fn: Callable[..., Any], available: dict[str, Any]) -> Any:
    signature = inspect.signature(fn)
    params = signature.parameters
    accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
    if accepts_kwargs:
        return fn(**available)
    kwargs = {name: available[name] for name in params if name in available}
    missing = [
        name
        for name, param in params.items()
        if name not in kwargs
        and param.default is inspect.Parameter.empty
        and param.kind in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}
    ]
    if missing:
        raise ConfigError(f"program hook {fn.__name__} is missing lmake-provided argument(s): {', '.join(missing)}")
    return fn(**kwargs)


def build_program(module: ModuleType, cfg: dict[str, Any]) -> Any:
    factory_name = str(cfg.get("factory", "build_program"))
    factory = getattr(module, factory_name, None)
    if factory is not None:
        if not callable(factory):
            raise ConfigError(f"dspy.factory={factory_name!r} is not callable.")
        return factory()
    if hasattr(module, "program"):
        return getattr(module, "program")
    return None


def instantiate_optimizer(dspy: ModuleType | None, cfg: dict[str, Any], module: ModuleType, available: dict[str, Any]) -> Any | None:
    optimizer_spec = cfg.get("optimizer")
    if optimizer_spec is None:
        return None
    if dspy is None:
        raise ConfigError("dspy.optimizer requires the optional DSPy package.")
    if isinstance(optimizer_spec, str):
        optimizer_name = optimizer_spec
        optimizer_params = dict(cfg.get("optimizer_params", {}) or {})
    elif isinstance(optimizer_spec, dict):
        optimizer_name = str(optimizer_spec.get("class", ""))
        optimizer_params = dict(optimizer_spec.get("params", {}) or {})
    else:
        raise ConfigError("dspy.optimizer must be a string or mapping.")
    if not optimizer_name:
        raise ConfigError("dspy.optimizer.class must not be empty.")

    optimizer_cls = getattr(dspy, optimizer_name, None)
    if optimizer_cls is None:
        optimizer_cls = getattr(module, optimizer_name, None)
    if optimizer_cls is None or not callable(optimizer_cls):
        raise ConfigError(f"could not find DSPy optimizer {optimizer_name!r}.")

    metric_fn = hook(module, cfg, "metric", "metric")
    if metric_fn is not None and "metric" not in optimizer_params:
        optimizer_params["metric"] = metric_fn
    return optimizer_cls(**optimizer_params)


def load_trainset(module: ModuleType, cfg: dict[str, Any], available: dict[str, Any]) -> Any:
    trainset_hook = hook(module, cfg, "trainset", "trainset")
    if trainset_hook is not None:
        return call_hook(trainset_hook, available)
    if "trainset" in cfg:
        return cfg["trainset"]
    return None


def load_compiled_state(program: Any, root: Path, target: TargetConfig, cfg: dict[str, Any]) -> dict[str, Any]:
    compiled = cfg.get("compiled")
    if compiled is None:
        return {"loaded": False}
    if not isinstance(compiled, str) or not compiled:
        raise ConfigError("dspy.compiled must be a relative path to a compiled DSPy state artifact.")
    path = root / compiled
    if not path.exists() or not path.is_file():
        raise ConfigError(f"target {target.name!r} compiled DSPy artifact is missing: {compiled}")
    if program is None or not hasattr(program, "load"):
        raise ConfigError(f"target {target.name!r} cannot load dspy.compiled because its program has no load() method.")
    allow_unsafe = bool(cfg.get("allow_unsafe_lm_state", False))
    try:
        program.load(str(path), allow_unsafe_lm_state=allow_unsafe)
    except TypeError:
        program.load(str(path))
    return {"loaded": True, "path": compiled, "allow_unsafe_lm_state": allow_unsafe}


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, default=str) + "\n").encode("utf-8")


def output_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    if hasattr(value, "toDict") and callable(value.toDict):
        return json_bytes(value.toDict())
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return json_bytes(value.to_dict())
    if isinstance(value, dict | list | tuple | int | float | bool) or value is None:
        return json_bytes(value)
    return str(value).encode("utf-8")


def outputs_from_value(value: Any, output_map: dict[str, str]) -> dict[str, bytes] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        if len(output_map) > 1:
            missing = sorted(set(output_map) - set(value))
            if missing:
                raise ConfigError("DSPy program result did not include output(s): " + ", ".join(missing))
            return {name: output_bytes(value[name]) for name in output_map}
        if len(output_map) == 1:
            only_name = next(iter(output_map))
            if set(value) == {only_name}:
                return {only_name: output_bytes(value[only_name])}
            return {only_name: output_bytes(value)}
    if len(output_map) != 1:
        raise ConfigError("DSPy program returned a single value but target declares multiple outputs.")
    only_name = next(iter(output_map))
    return {only_name: output_bytes(value)}


def save_compiled_value(value: Any, output_map: dict[str, str], staging_dir: Path, cfg: dict[str, Any]) -> dict[str, bytes] | None:
    if value is None:
        return None
    if hasattr(value, "save") and callable(value.save):
        if len(output_map) != 1:
            raise ConfigError("DSPy compiled program save() requires exactly one declared output.")
        if bool(cfg.get("save_program", False)):
            raise ConfigError("dspy.save_program=true writes a directory; lmake v0 only hashes file outputs.")
        logical_name = next(iter(output_map.keys()))
        staged_path = staging_dir / logical_name
        try:
            value.save(str(staged_path), save_program=False)
        except TypeError:
            value.save(str(staged_path))
        return None
    if hasattr(value, "dump_state") and callable(value.dump_state):
        return outputs_from_value(value.dump_state(), output_map)
    return outputs_from_value(value, output_map)


def run_dspy_target(
    *,
    root: Path,
    target: TargetConfig,
    prompt_text: str,
    inputs: list[InputPart],
    output_map: dict[str, str],
    target_fingerprint: str,
    staging_dir: Path,
) -> DspyRunnerResult:
    cfg = dspy_config(target)
    action = dspy_action(target)
    if action not in {"compile", "run"}:
        raise ConfigError(f"target {target.name!r} has unsupported dspy.action {action!r}; use compile or run.")

    module = load_program_module(root, target)
    dspy = import_optional_dspy()
    dspy_meta = configure_dspy_if_available(dspy, target)
    program = build_program(module, cfg)
    prompt_payload = build_user_message(prompt_text, inputs)
    available = {
        "root": root,
        "target": target,
        "dspy": dspy,
        "program": program,
        "inputs": inputs,
        "prompt_text": prompt_text,
        "prompt_payload": prompt_payload,
        "params": target.params,
        "target_fingerprint": target_fingerprint,
        "output_map": output_map,
        "dspy_config": cfg,
    }

    if action == "compile":
        compile_hook = hook(module, cfg, "compile", "compile_program")
        if compile_hook is not None:
            compiled = call_hook(compile_hook, available)
        else:
            if program is None:
                raise ConfigError(f"target {target.name!r} needs a build_program() factory, program variable, or compile_program() hook.")
            optimizer = instantiate_optimizer(dspy, cfg, module, available)
            if optimizer is None:
                compiled = program
            else:
                trainset = load_trainset(module, cfg, available)
                if trainset is None:
                    raise ConfigError("dspy.optimizer requires a trainset hook or dspy.trainset value.")
                compiled = optimizer.compile(program, trainset=trainset)
        outputs = save_compiled_value(compiled, output_map, staging_dir, cfg)
        metadata = {
            "runner": "dspy",
            "action": "compile",
            "dspy": dspy_meta,
            "program_hook": compile_hook.__name__ if compile_hook else None,
            "saved_by_program": outputs is None,
        }
        return DspyRunnerResult(outputs=outputs, metadata=metadata)

    load_meta = load_compiled_state(program, root, target, cfg)
    run_hook = hook(module, cfg, "run", "run_program")
    if run_hook is not None:
        result = call_hook(run_hook, {**available, "program": program})
    else:
        if program is None or not callable(program):
            raise ConfigError(f"target {target.name!r} needs a callable program or run_program() hook.")
        input_field = str(cfg.get("input_field", "context"))
        kwargs = dict(cfg.get("kwargs", {}) or {})
        kwargs[input_field] = prompt_payload
        result = program(**kwargs)
    outputs = outputs_from_value(result, output_map)
    metadata = {
        "runner": "dspy",
        "action": "run",
        "dspy": dspy_meta,
        "compiled": load_meta,
        "program_hook": run_hook.__name__ if run_hook else None,
    }
    return DspyRunnerResult(outputs=outputs, metadata=metadata)
