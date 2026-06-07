# Extracted Claims

## Signals

- Teams want a shared document workspace where subject-matter experts can edit source context without learning Git or YAML. _Source: context/customer_interviews.md_
- The strongest pain is not generation speed; it is confidence about which source material, prompt, and model produced a particular report. _Source: context/customer_interviews.md_
- The canonical state can stay as plain files: `context/`, `prompts/`, `programs/`, `runs/`, `artifacts/`, and `.lmcache/`. _Source: context/implementation_notes.md_
- The web layer can be a view over the same project directory rather than a separate database. _Source: context/implementation_notes.md_
- The most common successful path was "edit context, update outputs, publish a link." _Source: context/product_metrics.md_
- Users repeatedly ask for replay: restore the exact prior report bytes even when the current model or prompt would produce something different. _Source: context/support_tickets.md_
- Teams want to compare two generated outputs after changing a source file or prompt. _Source: context/support_tickets.md_


## Evidence

- One research lead said, "We can get a draft quickly, but we cannot explain why last Thursday's draft differs from today's unless someone kept notes manually." _Source: context/customer_interviews.md_
- Two teams already save generated reports in shared drives, but neither records prompt changes, model settings, or upstream document versions. _Source: context/customer_interviews.md_
- Target fingerprints include source files, prompts, program files, upstream fingerprints, upstream output hashes, model settings, and tool version. _Source: context/implementation_notes.md_
- Downstream fingerprints intentionally exclude upstream run IDs, preventing harmless reuse manifests from causing bogus recomputation. _Source: context/implementation_notes.md_
- Atomic output promotion uses run staging so crashes do not leave unexplained bytes in `artifacts/`. _Source: context/implementation_notes.md_
- Users with no command-line experience completed the context-editing step when presented with a document editor and a single Update button. _Source: context/product_metrics.md_
- Six tickets mention "what changed?" as the first question after a regenerated report. _Source: context/support_tickets.md_
- Four tickets mention "can I get the old version back?" after a new run was judged worse than the previous one. _Source: context/support_tickets.md_


## Metrics

- 14 recurring research briefs were generated in the last two weeks. _Source: context/product_metrics.md_
- 9 of 14 briefs had at least one source-context edit after the first generated draft. _Source: context/product_metrics.md_
- 7 of 14 briefs used a second run to compare prompt or context changes. _Source: context/product_metrics.md_
- 5 of 14 briefs were shared outside the immediate working group. _Source: context/product_metrics.md_
- Median manual reconstruction time for a prior report was estimated at 35 minutes because teams searched chat history, prompt docs, and file timestamps. _Source: context/support_tickets.md_
- In the pilot, teams edited source notes more often than prompts by a ratio of roughly 5:1. _Source: context/support_tickets.md_


## Concerns

- Non-technical reviewers are willing to edit Markdown-like notes, but they strongly dislike terminal workflows and configuration files. _Source: context/customer_interviews.md_
- Reviewers asked for a simple "needs update" indicator after source edits rather than a technical cache-state label. _Source: context/customer_interviews.md_
- Agent runners should wait until there is a sandbox, trace, and tool-call contract. _Source: context/implementation_notes.md_
- Schema validation and clearer YAML errors should arrive before broad external usage. _Source: context/implementation_notes.md_
- The current artifact viewer must render Markdown as a report, not raw text, for non-technical reviewers to trust it. _Source: context/product_metrics.md_
- The first demo must avoid looking like a dashboard for manifests; the report should be the center of the experience. _Source: context/product_metrics.md_
- If every cache hit writes a new run record, run history can grow quickly unless there is a garbage-collection workflow. _Source: context/support_tickets.md_


## Quotes

- "If I could update the background docs, press one button, and send the resulting brief, I would use that every week." _Source: context/customer_interviews.md_


## Open Questions

- Do reviewers need real-time collaborative cursors, or is optimistic locking with a friendly conflict screen enough? _Source: context/customer_interviews.md_
- How much provenance should be visible in default collaborator mode versus Details mode? _Source: context/implementation_notes.md_
- Should published report bundles be committed to Git, pushed to a static host, or treated as disposable generated output? _Source: context/support_tickets.md_


## Source Inventory

- `context/customer_interviews.md` (185 words, sha256:0b62305e6611)
- `context/implementation_notes.md` (138 words, sha256:315b60678542)
- `context/product_metrics.md` (148 words, sha256:186c27b02c96)
- `context/support_tickets.md` (177 words, sha256:b1bf47b7d4da)
