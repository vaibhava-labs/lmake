# Support Tickets: AI Reporting Workflow

Source type: tagged support tickets and internal feedback from teams piloting LLM-assisted reporting.

Signal: Users repeatedly ask for replay: restore the exact prior report bytes even when the current model or prompt would produce something different.

Signal: Teams want to compare two generated outputs after changing a source file or prompt.

Evidence: Six tickets mention "what changed?" as the first question after a regenerated report.

Evidence: Four tickets mention "can I get the old version back?" after a new run was judged worse than the previous one.

Metric: Median manual reconstruction time for a prior report was estimated at 35 minutes because teams searched chat history, prompt docs, and file timestamps.

Metric: In the pilot, teams edited source notes more often than prompts by a ratio of roughly 5:1.

Concern: If every cache hit writes a new run record, run history can grow quickly unless there is a garbage-collection workflow.

Open question: Should published report bundles be committed to Git, pushed to a static host, or treated as disposable generated output?
