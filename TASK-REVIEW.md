# Capturing and reviewing real tasks

Inference X-RAY's provider detail tabs include **Task evidence review**. A run,
passing checks, and a reviewer decision are separate events. Assistant completions
and HTTP success do not grant task credit.

## Capture and verify

The optional `bin/tmos-ai-task` command wraps an existing native agent command
without a shell, records the provider and task identity, and leaves its result
pending. It uses `~/.local/state/tmos-ai-usage` unless `TMOS_USAGE_STATE_DIR` selects
another directory. It does not change any provider settings or CLI shims.

For convenient access, create a symlink only if that command name is unused:

```sh
mkdir -p ~/.local/bin
ln -s ~/.config/omarchy/plugins/tmos.usage/bin/tmos-ai-task ~/.local/bin/tmos-ai-task
```

Run `tmos-ai-task --help` for the command shape. Supply your native agent's argv
after `--`, with a stable task ID, explicit subscription provider and workload
cohort. Use `--label` for a short task description and `--artifact` to bind a result
file or patch through verification. Use `verify --check-label` to describe the
checks you actually run; verification runs your supplied check argv after `--`.
The dashboard displays labels, timestamps and receipt hashes, not commands, paths,
prompts or command output. Review the result in its original workspace before
attesting that it satisfies the task's requirements.

Provider IDs are `codex`, `claude-code`, `opencode-go`, `command-code`, and
`clinepass`. They identify the subscription you explicitly used; do not attribute
generic API or local-model work to a subscription. A captured CLI invocation is
one observed wrapper turn, not a count of all internal model requests.

## Decide in the dashboard

Open Inference X-RAY, select the provider, and refresh **Task evidence review**.
The queue shows up to 100 recently updated captured tasks. Enter a reviewer name.
Acceptance requires successful checks for the displayed execution and an explicit
confirmation that you reviewed its result. `Mark failed` and `Abandon` are separate
explicit decisions; failure is never inferred just from an unsuccessful command.

The backend rechecks the exact execution and verification IDs under a task lock.
A rerun, replacement check, changed artifact or changed receipt invalidates the old
review. A busy task refuses the decision instead of waiting behind a long model
run. Repeating the same completed acceptance is idempotent. Failed and abandoned
UI decisions retain a private reviewer attestation bound to their ledger event.

Rerunning a finalized task reopens it and clears prior acceptance/check state;
rework remains visible in the outcome ledger. No queue action executes an arbitrary
command or reruns inference.

## What does not qualify

Pi's optional automatic observer captures pending provider-session segments,
not task boundaries. Those aggregates must never be accepted as completed tasks.
Use a separate actual task ID/cohort and the capture/check/review workflow above,
or the verified Odysseus task bridge. Other native application sessions are not
silently converted into task completions.

Validated-task **rankings** additionally require complete outcomes and recognized
spend for the same interval and workload cohort. Accepting one task does not
certify historical billing or populate missing providers. The comparison publisher
remains the explicit evidence gate documented in [ECONOMICS.md](ECONOMICS.md).
