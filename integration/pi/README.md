# Pi activity observer

This optional user extension connects normal Pi sessions to TMOS AI Usage's outcome
inbox without changing inference, prompts, models, routing or tools. It records
**observed provider session segments**, not completed user tasks. Segments always
remain pending. `agent_end`, `agent_settled`, exit status and successful requests
never grant validation or ranking credit.

## Installation

Requires an already installed Pi 0.87+ and Python 3 (standard library only).
In the plugin budget dropdown or X-RAY header, open **Setup** and choose
**Enable** beside **Pi observer**. By default it creates a symlink from
`~/.pi/agent/extensions/tmos-ai-usage/` to this bundled directory and refuses an
unrelated existing destination. It installs the observer, not Pi or provider CLIs,
and does not sign into any account. See [Getting started](../../GETTING-STARTED.md).

For manual installation, install this entire directory as
`~/.pi/agent/extensions/tmos-ai-usage/`, including `index.js`, `observer.js`,
`write_event.py` and `package.json`. Pi discovers user extension directories and the
manifest explicitly names `index.js`. Start a new Pi session, or `/reload` in a
running session. Existing processes do not load newly installed extensions until
reload. This installation does not edit CLI shims or provider settings.

Setup honors an absolute `PI_CODING_AGENT_DIR` inherited by the shell.
For other isolated workers using `PI_CODING_AGENT_DIR`, install under that agent directory's
`extensions/`, or add `--extension /absolute/path/to/integration/pi/index.js` to the
existing Pi invocation. The default user extension does not automatically cover a
separate isolated agent directory. No historical session content is imported on
install, resume or branch navigation.

`TMOS_USAGE_STATE_DIR` may select an absolute state directory. The default is
`~/.local/state/tmos-ai-usage`. Private `outcome-events/pi-<hash>.jsonl` files are
picked up by the plugin's normal collector. The extension writes only hashed
session/entry identities, explicit provider IDs, timestamps and observed counts.
It never reads assistant content, prompts, tool arguments, outputs or error text.
Session entries are inspected through Pi's in-memory SessionManager metadata API;
no transcript files are scanned.

Mappings are explicit: `commandcode`/`command-code` → `command-code`, `clinepass`,
`opencode-go`, and `openai-codex` → `codex`. Unknown/local providers and generic
`anthropic` API use are excluded; a model name never proves a subscription identity.
Each provider within a mixed session gets a distinct pending segment in cohort
`pi-observed-provider-session`. Never accept these aggregate segments as completed
tasks: that could double-credit one mixed-provider task. For real task value credit,
use the existing task-runner run/verify/accept workflow with its own stable task ID,
explicit provider, checks and independent reviewer, or the Odysseus verified bridge.

A turn here means a persisted assistant response, including error/abort responses,
not a user prompt or a count of tool calls. Pi `stopReason:error` does **not** prove
an upstream provider failure. The ledger's upstream-error increment remains zero;
deduplicated generic error observations go into private
`*.jsonl.assistant-errors.json` sidecars labelled
`pi_assistant_errors_not_verified_upstream`. Those sidecars are not currently
published as upstream errors or used for provider value rankings.

## Disable the observer

Open the plugin's **Setup** and choose **Disable** beside **Pi observer** before
removing the plugin. This removes only the symlink managed by this installation;
a pre-existing copied extension or unrelated destination is left unchanged.
Remove a manual copy through your own installation process. Start a new Pi
session or `/reload` to unload the observer. Existing outcome history remains in
the plugin state directory; disabling observation does not erase it.

## Failure and resource behavior

Callbacks enqueue metadata without awaiting filesystem work. One bounded Python
writer runs at a time, with a two-second process timeout, 256 queued observations,
and atomic replacement under OS advisory `flock`. A crash releases its lock even
though the harmless private `.lock` file remains. Independent Pi processes writing
the same session serialize and replay is idempotent via stable entry IDs. A segment
is capped at 5,000 events / 2 MiB. Export failure or queue/batch limits produce one
UI warning per extension instance and leave inference running; observation coverage
is incomplete. Shutdown waits at most 250 ms, so an interrupted final write or
backlogged queue may lose observations. No completeness claim is made. The normal
inbox also has its documented 1,000-file bound; review/archive only through a
retention-aware workflow when it approaches that bound, never silently delete
unresolved files. There is one JSONL per observed session/provider, not per turn.

## Verification

From the plugin repository root:

```sh
node --test integration/pi/observer.test.js
```

Tests use temporary state only and include real Python inbox/ledger ingestion,
resume deduplication, concurrent writers, privacy getters, bounded queues,
symlink refusal, generic error separation and branch navigation. No model calls.


Lifecycle compatibility was checked against Pi v0.87.1's
[extension event declarations](https://github.com/earendil-works/pi/blob/v0.87.1/packages/coding-agent/src/core/extensions/types.ts):
`session_start` covers startup, reload, new, resume and fork; there is no
`session_switch` event in that version. `session_tree` rebases observation state
without importing the selected branch's historical entries. Shutdown closes and
clears the writer so a subsequent start cannot inherit a closed queue.

Loader smoke verification used the actual installed Pi 0.87.1 binary in RPC mode
with a temporary agent directory, temporary TMOS state, and no session persistence.
Both explicit `--extension .../index.js` and automatic
`<agent-dir>/extensions/tmos-ai-usage/` discovery answered `get_state` successfully,
with empty stderr and zero outcome files. No prompt or inference was submitted.
