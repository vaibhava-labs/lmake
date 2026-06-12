Score the critique artifact as a judge verdict.

Return a structured verdict with schema `lmake.judge_verdict.v0`.

Rubric dimensions:

- `traceability`: reward an explicit Traceability Check section, artifact length
  lines, and a source document count.
- `source_accounting`: reward evidence that at least three source documents and
  multiple source types were considered.
- `readability`: reward a concise word-count band and the expected critique
  headings: Confidence, Evidence Gaps, Risks, and Suggested Next Data To
  Collect.

Scores are integers from 1 to 5. Any dimension below 3 is a failure, and the
overall verdict is `fail` when failures are present.
