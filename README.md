# MEGA.dev S02E04: remote incident worker

This repository runs one independent worker on a standard GitHub-hosted runner.
The worker loads its own course skill and uses its job's MCP tools. The main
harness does not perform the lookup. A separate worker handles the audio job
on the student's machine.

## Privacy and cost

- The job URL is provided through the `JOB_1_URL` repository secret.
- No model API keys are needed: Ollama runs the open-source `qwen3:8b` model.
- Only standard public-repository runner compute is used.
- There are no artifact uploads, caches, paid APIs, or automatic triggers.
- Job data, model messages, receipts, and diagnostic logs are encrypted to
  `result-recipient.pem` before they are printed. Only the student's local
  harness holds the corresponding private key.
- Runtime and model files exist only on the temporary runner.

The workflow is started manually and has a 45-minute timeout. Only the named
course job MCP endpoint, course skill downloads, and the runner's local Ollama
service are available to the model. It cannot execute shell commands.

The model uses native tool calls. Identical repeated calls are blocked to
prevent loops. A successful receipt ends the run immediately; otherwise the
worker stops after 45 model turns or six redundant calls.
