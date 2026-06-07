# Research Brief: Local-First LLM Workflows

## Executive Summary

The strongest signal is that teams do not merely want faster generated drafts. They want a repeatable document workflow where non-technical collaborators can edit source context, click Update, and trust that the resulting artifact has traceable provenance.

The prototype's file-based model is a good fit for this need because it treats source context, prompts, model settings, generated outputs, and run manifests as inspectable project state rather than hidden chat history.

## What The Sources Say

- Teams want a shared document workspace where subject-matter experts can edit source context without learning Git or YAML.
- The strongest pain is not generation speed; it is confidence about which source material, prompt, and model produced a particular report.
- The canonical state can stay as plain files: `context/`, `prompts/`, `programs/`, `runs/`, `artifacts/`, and `.lmcache/`.
- The web layer can be a view over the same project directory rather than a separate database.

## Evidence And Metrics

- 14 recurring research briefs were generated in the last two weeks.
- 9 of 14 briefs had at least one source-context edit after the first generated draft.
- 7 of 14 briefs used a second run to compare prompt or context changes.
- 5 of 14 briefs were shared outside the immediate working group.

The clearest workflow pattern is: edit context, update outputs, publish a link. That path appeared in both qualitative interview notes and the product metrics snapshot.

## Product Implication

The next product milestone should center the report and hide machinery by default. A collaborator should see documents, status, an Update button, rendered outputs, and a Publish action. Fingerprints, manifests, and target graphs should remain available in Details mode for developers.

## Recommended Next Experiment

Run the demo with a real LiteLLM model on a richer source set and compare two published reports after editing one context document. The moment to test is whether a non-technical reviewer understands why the output needs updating and can share the final report without asking a developer.

## Risks To Manage

- Non-technical reviewers are willing to edit Markdown-like notes, but they strongly dislike terminal workflows and configuration files.
- Reviewers asked for a simple "needs update" indicator after source edits rather than a technical cache-state label.
- Agent runners should wait until there is a sandbox, trace, and tool-call contract.
- Schema validation and clearer YAML errors should arrive before broad external usage.

## Representative Quote

> "If I could update the background docs, press one button, and send the resulting brief, I would use that every week."
