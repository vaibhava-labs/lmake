from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass


MARKERS = ("Signal", "Evidence", "Metric", "Concern", "Quote", "Open question")
REGRESSION_DEMO_MARKER = "DEMO_REGRESSION_MODE: lean_without_traceability"


@dataclass(frozen=True)
class Finding:
    kind: str
    source: str
    text: str


def context_parts(inputs):
    return [part for part in inputs if part.path.startswith("context/")]


def artifact_text(inputs, suffix):
    for part in inputs:
        if part.path.endswith(suffix):
            return part.text
    return ""


def artifact_part(inputs, path):
    for part in inputs:
        if part.path == path:
            return part
    return None


def extract_findings(inputs):
    findings = []
    for part in context_parts(inputs):
        for raw_line in part.text.splitlines():
            line = raw_line.strip()
            for marker in MARKERS:
                prefix = f"{marker}:"
                if line.startswith(prefix):
                    findings.append(Finding(marker, part.path, line[len(prefix):].strip()))
    return findings


def group_findings(findings):
    grouped = defaultdict(list)
    for finding in findings:
        grouped[finding.kind].append(finding)
    return grouped


def bullet(finding):
    return f"- {finding.text} _Source: {finding.source}_"


def section(title, findings):
    if not findings:
        return f"## {title}\n\n- No entries found.\n"
    return f"## {title}\n\n" + "\n".join(bullet(item) for item in findings) + "\n"


def run_extract(inputs):
    findings = extract_findings(inputs)
    grouped = group_findings(findings)
    inventory = "\n".join(
        f"- `{part.path}` ({len(part.text.split())} words, sha256:{part.sha256[:12]})"
        for part in context_parts(inputs)
    )
    content = "\n\n".join([
        "# Extracted Claims",
        section("Signals", grouped["Signal"]),
        section("Evidence", grouped["Evidence"]),
        section("Metrics", grouped["Metric"]),
        section("Concerns", grouped["Concern"]),
        section("Quotes", grouped["Quote"]),
        section("Open Questions", grouped["Open question"]),
        "## Source Inventory\n\n" + inventory + "\n",
    ])
    return {"claims": content}


def top_text(grouped, kind, limit=3):
    return [item.text for item in grouped[kind][:limit]]


def run_synthesize(inputs):
    findings = extract_findings(inputs)
    grouped = group_findings(findings)
    signals = top_text(grouped, "Signal", 4)
    metrics = top_text(grouped, "Metric", 4)
    concerns = top_text(grouped, "Concern", 4)
    quote = grouped["Quote"][0].text if grouped["Quote"] else "No direct quote found."
    content = f"""# Research Brief: Local-First LLM Workflows

## Executive Summary

The strongest signal is that teams do not merely want faster generated drafts. They want a repeatable document workflow where non-technical collaborators can edit source context, click Update, and trust that the resulting artifact has traceable provenance.

The prototype's file-based model is a good fit for this need because it treats source context, prompts, model settings, generated outputs, and run manifests as inspectable project state rather than hidden chat history.

## What The Sources Say

{chr(10).join(f"- {item}" for item in signals)}

## Evidence And Metrics

{chr(10).join(f"- {item}" for item in metrics)}

The clearest workflow pattern is: edit context, update outputs, publish a link. That path appeared in both qualitative interview notes and the product metrics snapshot.

## Product Implication

The next product milestone should center the report and hide machinery by default. A collaborator should see documents, status, an Update button, rendered outputs, and a Publish action. Fingerprints, manifests, and target graphs should remain available in Details mode for developers.

## Recommended Next Experiment

Run the demo with a real LiteLLM model on a richer source set and compare two published reports after editing one context document. The moment to test is whether a non-technical reviewer understands why the output needs updating and can share the final report without asking a developer.

## Risks To Manage

{chr(10).join(f"- {item}" for item in concerns)}

## Representative Quote

> {quote}
"""
    return {"synthesis": content}


def run_critique(inputs, prompt_text=""):
    if REGRESSION_DEMO_MARKER in prompt_text:
        content = """# Executive Recommendation

The brief is strong enough to share. It clearly explains that local-first LLM workflows should center source files, generated artifacts, and a publishable report.

## Ship Decision

Move forward with the current positioning and keep the demo concise. The product story is easy to understand and the report is readable.

## Follow-Up

Polish the web UI and prepare a public demo page.
"""
        return {"critique": content}

    claims = artifact_text(inputs, "claims.md")
    synthesis = artifact_text(inputs, "synthesis.md")
    findings = extract_findings(inputs)
    grouped = group_findings(findings)
    open_questions = top_text(grouped, "Open question", 4)
    concerns = top_text(grouped, "Concern", 5)
    content = f"""# Critique And Follow-Up Plan

## Confidence

The synthesis is directionally strong because it is supported by multiple source types: interviews, support tickets, product metrics, and implementation notes. It is strongest on workflow shape and weakest on external market evidence.

## What The Brief Gets Right

- It centers the collaborator workflow instead of turning the product into a manifest dashboard.
- It preserves the local-first architecture: files and manifests remain canonical.
- It correctly delays agent runners until sandbox and trace contracts exist.

## Evidence Gaps

- The source set is internal and prototype-heavy; it needs at least one external or customer-facing workflow before pricing or packaging decisions.
- The current demo uses deterministic local hooks, so a real-model run should verify output quality with LiteLLM.
- The publish flow creates a local static bundle, but the share step still depends on GitHub Pages, S3, or another host.

## Risks

{chr(10).join(f"- {item}" for item in concerns)}

## Open Questions

{chr(10).join(f"- {item}" for item in open_questions)}

## Suggested Next Data To Collect

- Give one non-technical reviewer only the web UI and ask them to update a brief from changed context.
- Measure whether they understand the Needs update state without seeing cache terminology.
- Publish the result and ask an outside reader whether the report page contains enough provenance.
- Compare this deterministic demo against a LiteLLM run to identify where real model output changes the perceived value.

## Traceability Check

- Claims artifact length: {len(claims.split())} words.
- Synthesis artifact length: {len(synthesis.split())} words.
- Source documents inspected: {len(context_parts(inputs))}.
"""
    return {"critique": content}


def score_from_features(features):
    return max(1, min(5, 1 + sum(1 for item in features if item)))


def run_judge(inputs):
    critique = artifact_part(inputs, "artifacts/critique.md")
    critique_text = critique.text if critique is not None else ""
    words = re.findall(r"\b\w+\b", critique_text)
    word_count = len(words)
    source_type_terms = ["interviews", "support tickets", "product metrics", "implementation notes"]
    required_headings = [
        "## Confidence",
        "## Evidence Gaps",
        "## Risks",
        "## Suggested Next Data To Collect",
    ]

    traceability_features = [
        "## Traceability Check" in critique_text,
        "Claims artifact length" in critique_text,
        "Synthesis artifact length" in critique_text,
        "Source documents inspected" in critique_text,
    ]
    source_count_match = re.search(r"Source documents inspected:\s*(\d+)", critique_text)
    source_count = int(source_count_match.group(1)) if source_count_match else 0
    source_accounting_features = [
        source_count >= 3,
        sum(1 for term in source_type_terms if term in critique_text.lower()) >= 3,
        "source" in critique_text.lower(),
        "provenance" in critique_text.lower() or "traceability" in critique_text.lower(),
    ]
    readability_features = [
        120 <= word_count <= 700,
        all(heading in critique_text for heading in required_headings),
        "## Open Questions" in critique_text,
        "## What The Brief Gets Right" in critique_text,
    ]

    scores = {
        "traceability": score_from_features(traceability_features),
        "source_accounting": score_from_features(source_accounting_features),
        "readability": score_from_features(readability_features),
    }
    failures = [name for name, score in scores.items() if score < 3]
    rationale = (
        "The critique preserves traceability, source accounting, and readable review structure."
        if not failures
        else "The critique is missing rubric evidence for: " + ", ".join(failures) + "."
    )
    return {
        "verdict": {
            "schema": "lmake.judge_verdict.v0",
            "target": "critique",
            "artifact": {
                "name": "critique",
                "path": "artifacts/critique.md",
                "sha256": critique.sha256 if critique is not None else "",
            },
            "scores": scores,
            "failures": failures,
            "verdict": "pass" if not failures else "fail",
            "rationale": rationale,
            "metadata": {
                "word_count": word_count,
                "source_documents_inspected": source_count,
                "features": {
                    "traceability": traceability_features,
                    "source_accounting": source_accounting_features,
                    "readability": readability_features,
                },
            },
        }
    }
