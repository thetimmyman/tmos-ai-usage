# Getting started

TMOS AI Usage adds an AI budget dropdown and an Omarchy-themed **Inference X-RAY**
comparison page. You can use quotas and supported usage history immediately;
validated-task comparisons need additional evidence.

## 1. Install and open Setup

```sh
omarchy plugin add https://github.com/thetimmyman/tmos-ai-usage --enable
```

Click the AI usage item on the bar. On the first visit, the setup guide opens
automatically, initializes private local folders and refreshes the collector.
Choose **Finish setup** when ready; optional tools and provider sign-ins are not required.
Reopen the guide from **Set up AI Usage · start here** or, after finishing,
**Setup · providers and optional tracking**. The X-RAY header also has **Setup**.
No account sign-in or inference runs during setup.

The core plugin needs Omarchy's Quickshell runtime and Python 3. It has no pip
packages to install. Setup does not install Codex, Claude Code, Pi, or other
provider applications.

## 2. Connect the accounts you already use

Sign into each desired provider using its own CLI or application. TMOS reads
supported existing credentials and local histories; do not paste keys into the
plugin. Open the budget dropdown and choose **Refresh now**, or wait for the
next automatic refresh. The collector starts shortly after the shell starts and
runs every five minutes.

Supported built-ins are Claude Code, Codex, ClinePass, Command Code, and OpenCode
Go. Their reporting depth differs. **Sign in**, **unknown**, and **error** mean a
reading is unavailable; they do not mean zero usage. Check the account's own CLI
first if a provider is missing or needs sign-in. See
[provider coverage](PROVIDER-COVERAGE.md) for the available sources and limits.

## 3. Confirm prices and compare

Choose **Open Inference X-RAY**, then open each provider tab and save its
**Subscription price** and Monthly or Annual billing cycle. Enter your own amount;
no user's personal prices are bundled. Annual amounts are divided by twelve for
the provisional monthly comparison. Dated published estimates expire after 30
days, and provider quotes are estimates too. Confirm the fee you actually pay.
A current price entry does not reconstruct an earlier plan or invoice history.

The overview compares all detected subscriptions. Provider tabs and the budget
dropdown use the same selected metric and rank order:

- **Observed turns / $ · provisional** gives a useful initial comparison from
  observed local activity and monthly fees. `#1*` is provisional, not a quality
  score or buying recommendation. Missing logs can change the order.
- **Validated tasks / $** requires accepted task outcomes and recognized spend
  covering the same period and workload. Missing evidence stays unranked; a
  successful request or assistant completion is not a validated task.

## 4. Add detail only where you need it

**Provider exports.** Copy supported OpenCode request-log JSON or Console CSV
exports into `~/.local/state/tmos-ai-usage/imports/`, then refresh. The collector
imports bounded batches automatically, retains originals and deduplicates
recognized records. It does **not** scan Downloads automatically or accept every
provider's JSON format. Console organization reports are distinct from personal
Go subscription activity. See [report imports](ECONOMICS.md#reports).

**Task capture.** In Setup, choose **Enable** beside **Task command** to install
`tmos-ai-task` into `~/.local/bin/`. It links the bundled wrapper and refuses an unrelated existing command. Ensure that directory
is on your PATH, then run `tmos-ai-task --help`. You choose the native agent command
to wrap; running it can consume that provider's allowance. Captured work stays
pending until checks and explicit review. Follow [Task evidence review](TASK-REVIEW.md).

**Pi observation.** If you already use Pi 0.87+, choose **Enable** beside **Pi observer**.
Setup links the bundled extension to `~/.pi/agent/extensions/tmos-ai-usage/` by
default, or the absolute `PI_CODING_AGENT_DIR` inherited by the shell.
If the action says **Unavailable**, confirm Pi is installed and on the shell's PATH.
Start a new Pi session or use `/reload`. It records future pending provider-session
activity, not completed tasks. It does not import historical conversations or change models and routing. Separate Pi
agent directories need their own installation; see the [Pi guide](integration/pi/README.md).

**Invoices.** Invoice import is explicit and separate from the export inbox. It
requires a structured invoice record and its source evidence; dropping a PDF into
Downloads does not import it. Follow [invoice history](ECONOMICS.md#invoice-history-and-service-period-expense)
when you need service-period spend or historical plan changes.

## Your files and the plugin's limits

Private state defaults to `~/.local/state/tmos-ai-usage/`: `usage.json` is the
display cache, `imports/` is the report inbox, and `outcome-events/` accepts supported
harness events. `TMOS_USAGE_STATE_DIR` selects another state directory; set it
consistently for the shell, task command and Pi sessions.

The plugin observes and compares. It does not buy subscriptions, move inference
between accounts, or apply a displayed deal automatically. Discount-aware dispatch
requires the separate, explicitly configured Odysseus integration. No Odysseus
installation is needed for this dashboard.

If a figure is missing, inspect the provider's coverage and source note, refresh,
and check [ECONOMICS.md](ECONOMICS.md) before adding evidence. Never fill a missing
measurement with an assumed zero or mark an observation as a validated task.

## Disable optional capture or remove the plugin

In **Setup**, choose **Disable** beside the task command or Pi observer before
removing the plugin. Only the exact symlinks managed by this installation are
removed. Unrelated files and pre-existing copied extensions are left unchanged;
manage those copies yourself. Reload Pi or start a new session to unload its
observer.

Then run `omarchy plugin remove tmos.usage` if you want to remove the dashboard.
Keep `~/.local/state/tmos-ai-usage/` to preserve your invoices, history and task
evidence. Erasing that plugin-specific directory is a separate, explicit choice;
never delete the shared `~/.local/state/` directory. If you set
`TMOS_USAGE_STATE_DIR`, retain or remove that selected plugin state directory instead.
