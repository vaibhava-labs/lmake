# Implementation Notes

Source type: engineering notes from the local-first prototype.

Signal: The canonical state can stay as plain files: `context/`, `prompts/`, `programs/`, `runs/`, `artifacts/`, and `.lmcache/`.

Signal: The web layer can be a view over the same project directory rather than a separate database.

Evidence: Target fingerprints include source files, prompts, program files, upstream fingerprints, upstream output hashes, model settings, and tool version.

Evidence: Downstream fingerprints intentionally exclude upstream run IDs, preventing harmless reuse manifests from causing bogus recomputation.

Evidence: Atomic output promotion uses run staging so crashes do not leave unexplained bytes in `artifacts/`.

Concern: Agent runners should wait until there is a sandbox, trace, and tool-call contract.

Concern: Schema validation and clearer YAML errors should arrive before broad external usage.

Open question: How much provenance should be visible in default collaborator mode versus Details mode?
