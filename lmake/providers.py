from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .hashing import hash_json


@dataclass(frozen=True)
class InputPart:
    path: str
    sha256: str
    text: str


@dataclass(frozen=True)
class ProviderResult:
    content: str
    metadata: dict[str, Any]


def build_user_message(prompt_text: str, inputs: list[InputPart]) -> str:
    sections: list[str] = []
    sections.append(prompt_text.strip())
    if inputs:
        sections.append("\n\n# Inputs")
        for part in inputs:
            sections.append(f"\n\n## {part.path}\nsha256: {part.sha256}\n\n```\n{part.text}\n```")
    return "".join(sections).strip() + "\n"


def call_provider(
    *,
    provider: str,
    model: str,
    system: str | None,
    prompt_text: str,
    inputs: list[InputPart],
    params: dict[str, Any],
    target_name: str,
    target_fingerprint: str,
) -> ProviderResult:
    provider = provider.lower()
    if provider == "mock" or model.startswith("mock/") or model == "mock":
        return mock_completion(
            model=model,
            system=system,
            prompt_text=prompt_text,
            inputs=inputs,
            params=params,
            target_name=target_name,
            target_fingerprint=target_fingerprint,
        )
    if provider == "litellm":
        return litellm_completion(
            model=model,
            system=system,
            prompt_text=prompt_text,
            inputs=inputs,
            params=params,
        )
    raise ValueError(f"unsupported provider {provider!r}; use provider: mock or provider: litellm")


def mock_completion(
    *,
    model: str,
    system: str | None,
    prompt_text: str,
    inputs: list[InputPart],
    params: dict[str, Any],
    target_name: str,
    target_fingerprint: str,
) -> ProviderResult:
    """A deterministic local provider for testing the build contract without API keys."""
    input_summary = "\n".join(f"- {p.path} sha256:{p.sha256[:16]} bytes:{len(p.text.encode('utf-8'))}" for p in inputs)
    prompt_digest = hash_json({"system": system, "prompt": prompt_text, "params": params})
    excerpts: list[str] = []
    for part in inputs[:4]:
        body = part.text.strip().replace("\r\n", "\n")
        if len(body) > 700:
            body = body[:700] + "…"
        excerpts.append(f"\n## Excerpt: {part.path}\n{body}")
    content = (
        f"# lmake mock artifact: {target_name}\n\n"
        f"target_fingerprint: `{target_fingerprint}`\n\n"
        f"model: `{model}`\n\n"
        f"prompt_hash: `{prompt_digest}`\n\n"
        f"## Input files\n{input_summary or '- none'}\n"
        f"{''.join(excerpts)}\n"
    )
    return ProviderResult(
        content=content,
        metadata={
            "provider": "mock",
            "model": model,
            "deterministic": True,
            "prompt_hash": prompt_digest,
        },
    )


def litellm_completion(
    *,
    model: str,
    system: str | None,
    prompt_text: str,
    inputs: list[InputPart],
    params: dict[str, Any],
) -> ProviderResult:
    try:
        from litellm import completion  # type: ignore
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("provider: litellm requires `pip install litellm`") from exc

    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": build_user_message(prompt_text, inputs)})

    response = completion(model=model, messages=messages, **params)
    try:
        content = response.choices[0].message.content
    except Exception as exc:  # pragma: no cover - provider object shape may vary
        raise RuntimeError(f"could not extract LiteLLM response content: {exc}") from exc

    usage = getattr(response, "usage", None)
    if hasattr(usage, "model_dump"):
        usage_value = usage.model_dump()
    elif isinstance(usage, dict):
        usage_value = usage
    else:
        usage_value = str(usage) if usage is not None else None

    return ProviderResult(
        content=content or "",
        metadata={
            "provider": "litellm",
            "model": model,
            "usage": usage_value,
        },
    )
