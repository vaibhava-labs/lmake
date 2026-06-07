from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass


MARKERS = ("Signal", "Evidence", "Metric", "Concern", "Quote", "Open question")


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


def run_critique(inputs):
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
