// shell/plugins/tmos.usage/Model.js — every bit of arithmetic and formatting the surface does.
//
// Deliberately pure: it takes the document tools/usage_collector.py wrote and returns view rows.
// It never fetches, never guesses a number the collector did not provide, and never invents a
// window a provider does not publish. Colour is returned as a *token name* ("urgent" | "warn" |
// "ok" | "muted"), never a literal — the QML maps it onto qs.Commons Color tokens (gate N3).
//
// Shape of the surface, which this file serves: EVERY subscription is on the first page, always.
// The panel is a list, not a switcher — nothing is hidden behind a provider picker. An expansion
// under a row adds depth (limits with resets, the prepaid ledger, tokens by day, tokens by model)
// and only ever adds: collapsed, every provider still shows its name, plan, status and all three
// windows, so comparing subscriptions down the page never needs a click.
//
// Style: declarations sit at the top of their function and strings are built with template
// literals, which is what the JS linter the shell runs over plugin scripts expects. `var` is kept
// deliberately — it is the QML-JS idiom, and this file is loaded by the Qt V4 engine, not a browser.
//
// biome-ignore-all lint/correctness/noUnusedVariables: this is a QML library module. Its callers
// are QML (`import "Model.js" as Model` in BarWidget.qml), and a `.pragma library` file has no JS
// importers at all — the linter reports the module's whole public surface as unused because it
// cannot see across the QML boundary (it also reports `.pragma library` itself as a parse error).
.pragma library

var WINDOW_LABEL = { "5h": "5h", "week": "Week", "month": "Month" }
var WINDOW_ORDER = ["5h", "week", "month"]

var PROVIDER_LABEL = {
  "claude-code": "Claude Code",
  "codex": "Codex",
  "clinepass": "ClinePass",
  "command-code": "Command Code",
  "opencode-go": "OpenCode Go"
}

// Status badges. "estimate" is not a failure — it is a number TMOS derived rather than read, and
// it is labelled so nobody mistakes it for the provider's own meter.
var STATUS_LABEL = {
  ok: "", estimate: "estimate", unauthenticated: "sign in", unknown: "unknown", error: "error"
}
var STATUS_TONE = {
  ok: "ok", estimate: "warn", unauthenticated: "muted", unknown: "muted", error: "urgent"
}

// Local weekday names, for the day rows. Index matches Date.getDay().
var DAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

function emptyDocument(error) {
  return { ok: false, error: error, providers: [], observedAt: 0, totals: normalizeTotals(null) }
}

function parseDocument(text) {
  var doc, observedAt, rows, i
  if (!text || !String(text).trim()) return emptyDocument("no usage cache yet")
  try {
    doc = JSON.parse(String(text))
  } catch (e) {
    return emptyDocument("usage cache is not JSON")
  }
  if (!doc || typeof doc !== "object" || !Array.isArray(doc.providers))
    return emptyDocument("usage cache has no providers")
  observedAt = Date.parse(doc.observed_at || "") || 0
  rows = []
  for (i = 0; i < doc.providers.length; i++) rows.push(normalizeProvider(doc.providers[i], observedAt))
  rows.sort((a, b) => a.name.localeCompare(b.name))
  return { ok: true, error: "", providers: rows, observedAt: observedAt, totals: normalizeTotals(doc.totals) }
}

function normalizeProvider(rawRow, observedAtMs) {
  var id, windows, list, i, w, status, row
  row = rawRow || {}
  id = String(row.provider || "?")
  windows = {}
  list = Array.isArray(row.windows) ? row.windows : []
  for (i = 0; i < list.length; i++) {
    w = list[i]
    if (!w || WINDOW_LABEL[w.name] === undefined) continue
    windows[w.name] = {
      name: w.name,
      label: WINDOW_LABEL[w.name],
      usedPct: number(w.used_pct),
      remainingPct: number(w.remaining_pct),
      resetsInS: (typeof w.resets_in_s === "number") ? w.resets_in_s : -1
    }
  }
  status = String(row.status || "unknown")
  return {
    id: id,
    name: PROVIDER_LABEL[id] || id,
    plan: row.plan ? String(row.plan) : "",
    source: String(row.source || ""),
    status: status,
    statusLabel: STATUS_LABEL[status] === undefined ? status : STATUS_LABEL[status],
    statusTone: STATUS_TONE[status] || "muted",
    note: row.note ? String(row.note) : "",
    observedAt: Date.parse(row.observed_at || "") || observedAtMs || 0,
    windows: windows,
    hasWindows: WINDOW_ORDER.some((k) => windows[k] !== undefined),
    balance: normalizeBalance(row.balance),
    stats: normalizeStats(row.stats)
  }
}

function number(value) {
  var n = Number(value)
  return isFinite(n) ? n : -1
}

function clamp01(value) {
  var n = Number(value)
  if (!isFinite(n)) return 0
  return Math.max(0, Math.min(1, n))
}

// Big token counts have to read at a glance in one line: "982M", "7.39B", "12.4k". Two decimals
// for billions, one for millions and thousands, and never a decimal on a raw count.
function formatTokens(value) {
  var n = Number(value)
  if (!isFinite(n) || n <= 0) return "0"
  if (n < 1000) return String(Math.round(n))
  if (n < 1e6) return `${(n / 1e3).toFixed(1)}k`
  if (n < 1e9) return `${(n / 1e6).toFixed(1)}M`
  return `${(n / 1e9).toFixed(2)}B`
}

function currencyPrefix(currency) {
  var code = String(currency || "USD").toUpperCase()
  if (code === "USD") return "$"
  if (code === "EUR") return "€"
  if (code === "GBP") return "£"
  return `${code} `
}

function moneyText(value, currency) {
  var n = Number(value)
  if (!isFinite(n)) n = 0
  return `${currencyPrefix(currency)}${n.toFixed(2)}`
}

// The prepaid ledger, or nothing. A provider that publishes no credit balance gets `available:
// false` and draws no section at all — never an empty $0.00 meter.
// The full shape, always. A QML `Text` that reads a string off this object must never be handed
// `undefined` — that is a type warning in the shell's log even when the section is not drawn, and
// a plugin on the marketplace is judged on a clean log as much as on a clean screenshot.
function emptyBalance() {
  return {
    available: false, currency: "USD", remaining: -1, funded: -1, spent: -1, estimated: false,
    remainingText: "", spentText: "", fundedText: "", ratio: -1, alarming: false, detailText: ""
  }
}

function emptyTotals() {
  return { available: false, todayText: "0", weekText: "0", subscriptions: 0, withStats: 0, text: "" }
}

function normalizeBalance(raw) {
  var funded, remaining, spent, currency, estimated, detail, fallbackDetail
  if (!raw || typeof raw !== "object") return emptyBalance()
  funded = number(raw.funded)
  remaining = number(raw.remaining)
  spent = number(raw.spent)
  currency = String(raw.currency || "USD")
  estimated = raw.estimated === true
  detail = estimated ? " · estimated" : ""
  fallbackDetail = estimated ? "estimated" : ""
  return {
    available: true,
    currency: currency,
    remaining: remaining,
    funded: funded,
    spent: spent,
    estimated: estimated,
    remainingText: moneyText(remaining, currency),
    spentText: moneyText(spent, currency),
    fundedText: moneyText(funded, currency),
    ratio: funded > 0 ? clamp01(remaining / funded) : -1,
    // A prepaid account runs low the way a subscription window fills up: the last 10% of the
    // funded credits is the same alarm.
    alarming: funded > 0 && (remaining / funded) <= 0.1,
    detailText: funded > 0
      ? `${moneyText(spent, currency)} spent of ${moneyText(funded, currency)} funded${detail}`
      : fallbackDetail
  }
}

function dayName(date) {
  var parsed = new Date(`${String(date || "")}T00:00:00`)
  if (isNaN(parsed.getTime())) return String(date || "")
  return DAY_NAMES[parsed.getDay()]
}

// Token history, or an explicit "none" that names the reason. `daily` is the whole window and
// `recent_days` its last seven; the view draws the seven and states the coverage.
function normalizeStats(raw) {
  var daily, today, todayKey, recent, peak, days, rawModels, best, models, totals, scan, files
  var todayTokens, todayPrompts, sourceParts, i, j, k, m, d, date, item, total
  var todayText, promptLabel
  if (!raw || typeof raw !== "object" || raw.available !== true) {
    return { available: false, note: (raw && raw.note) ? String(raw.note) : "", days: [], models: [],
             todayText: "", sourceText: "", source: "", coverageDays: 0, todayTokens: 0,
             todayPrompts: 0, weekTokens: 0, weekText: "" }
  }
  daily = Array.isArray(raw.daily) ? raw.daily : []
  today = raw.today && typeof raw.today === "object" ? raw.today : {}
  todayKey = String(today.date || "")
  recent = Array.isArray(raw.recent_days) ? raw.recent_days : daily.slice(-7)
  peak = 0
  for (i = 0; i < recent.length; i++) peak = Math.max(peak, number(recent[i] && recent[i].tokens))
  if (peak <= 0) peak = 1
  days = []
  for (j = 0; j < recent.length; j++) {
    d = recent[j] || {}
    date = String(d.date || "")
    days.push({
      date: date,
      label: date === todayKey ? "Today" : dayName(date),
      today: date === todayKey,
      tokens: number(d.tokens) > 0 ? number(d.tokens) : 0,
      tokenText: formatTokens(number(d.tokens)),
      prompts: number(d.prompts) > 0 ? number(d.prompts) : 0,
      sessions: number(d.sessions) > 0 ? number(d.sessions) : 0,
      ratio: clamp01(number(d.tokens) / peak)
    })
  }
  rawModels = Array.isArray(raw.models) ? raw.models : []
  best = 0
  for (k = 0; k < rawModels.length; k++) best = Math.max(best, number(rawModels[k] && rawModels[k].total_tokens))
  if (best <= 0) best = 1
  models = []
  for (m = 0; m < rawModels.length; m++) {
    item = rawModels[m] || {}
    total = number(item.total_tokens) > 0 ? number(item.total_tokens) : 0
    models.push({
      id: String(item.id || "?"),
      total: total,
      tokenText: formatTokens(total),
      share: clamp01(total / best),
      detailText: `in ${formatTokens(number(item.input_tokens))}`
        + ` · out ${formatTokens(number(item.output_tokens))}`
        + ` · cache read ${formatTokens(number(item.cache_read_tokens))}`
        + ` · cache write ${formatTokens(number(item.cache_write_tokens))}`
    })
  }
  totals = raw.totals && typeof raw.totals === "object" ? raw.totals : {}
  scan = raw.scan && typeof raw.scan === "object" ? raw.scan : {}
  files = number(scan.files_cached) + number(scan.files_scanned)
  todayTokens = number(today.tokens) > 0 ? number(today.tokens) : 0
  todayPrompts = number(today.prompts) > 0 ? number(today.prompts) : 0
  promptLabel = todayPrompts === 1 ? "prompt" : "prompts"
  if (todayTokens > 0) todayText = `${formatTokens(todayTokens)} today`
  else if (todayPrompts > 0) todayText = `${todayPrompts} ${promptLabel} today`
  else todayText = ""
  // Provenance in one line: which transcripts, how many files, and the window they cover.
  sourceParts = []
  if (String(raw.source || "")) sourceParts.push(String(raw.source))
  if (files > 0) sourceParts.push(`${files} ${files === 1 ? "file" : "files"}`)
  if (number(raw.coverage_days) > 0) sourceParts.push(`${number(raw.coverage_days)}d window`)
  return {
    available: true,
    source: String(raw.source || ""),
    coverageDays: number(raw.coverage_days),
    days: days,
    models: models,
    todayTokens: todayTokens,
    todayPrompts: todayPrompts,
    todayText: todayText,
    weekTokens: number(totals.tokens) > 0 ? number(totals.tokens) : 0,
    weekText: number(totals.tokens) > 0
      ? `${formatTokens(number(totals.tokens))} in ${number(raw.coverage_days)}d`
      : "",
    sourceText: sourceParts.join(" · ")
  }
}

function normalizeTotals(raw) {
  var today, week, subscriptions, withStats, parts
  if (!raw || typeof raw !== "object") return emptyTotals()
  today = number(raw.tokens_today) > 0 ? number(raw.tokens_today) : 0
  week = number(raw.tokens_7d) > 0 ? number(raw.tokens_7d) : 0
  subscriptions = number(raw.providers_reported) > 0 ? number(raw.providers_reported) : 0
  withStats = number(raw.providers_with_stats) > 0 ? number(raw.providers_with_stats) : 0
  parts = []
  if (today > 0) parts.push(`${formatTokens(today)} today`)
  if (week > 0) parts.push(`${formatTokens(week)} 7d`)
  if (subscriptions > 0) parts.push(`${subscriptions} subscriptions`)
  return {
    available: withStats > 0,
    todayText: formatTokens(today),
    weekText: formatTokens(week),
    subscriptions: subscriptions,
    withStats: withStats,
    text: parts.join(" · ")
  }
}

// The bar's one number: the least headroom anyone has left right now. A provider with no reading
// contributes nothing — the bar says "—" rather than implying a full tank.
function tightest(providers) {
  var best, i, k, p, w
  best = null
  for (i = 0; i < providers.length; i++) {
    p = providers[i]
    for (k = 0; k < WINDOW_ORDER.length; k++) {
      w = p.windows[WINDOW_ORDER[k]]
      if (!w || w.remainingPct < 0) continue
      if (best === null || w.remainingPct < best.remainingPct)
        best = { provider: p, window: w, remainingPct: w.remainingPct }
    }
  }
  return best
}

function barText(providers) {
  var best = tightest(providers)
  if (best === null) return "AI —"
  return `AI ${Math.round(best.remainingPct)}%`
}

function barTone(providers, warnPct, lowPct) {
  var best = tightest(providers)
  if (best === null) return "muted"
  return tone(best.remainingPct, warnPct, lowPct)
}

// The bar's text for ONE provider: its name, then every window it publishes in the fixed order
// D (5h), W (week), M (month) — each with its one-letter label, so the numbers stay readable
// while providers rotate: "Claude Code D12% W45% M—". The percents are remaining headroom,
// the same numbers the popup meters show. A window a provider does not publish (Anthropic has
// no month) is that letter with a dash ("M—"). A provider with no reading at all shows its status
// word ("sign in", "error") rather than a fake percent.
var BAR_WINDOW_LETTER = { "5h": "D", "week": "W", "month": "M" }

function barWindowText(provider, key) {
  var w = provider.windows[key]
  if (!w || w.remainingPct < 0) return `${BAR_WINDOW_LETTER[key]}—`
  return `${BAR_WINDOW_LETTER[key]}${Math.round(w.remainingPct)}%`
}

function barTextFor(provider) {
  var best, parts, i
  if (!provider) return "AI —"
  best = tightest([provider])
  if (best === null) return `${provider.name} ${provider.statusLabel || "—"}`
  parts = []
  for (i = 0; i < WINDOW_ORDER.length; i++) parts.push(barWindowText(provider, WINDOW_ORDER[i]))
  return `${provider.name} ${parts.join(" ")}`
}

function barToneFor(provider, warnPct, lowPct) {
  var best
  if (!provider) return "muted"
  best = tightest([provider])
  if (best === null) return "muted"
  return tone(best.remainingPct, warnPct, lowPct)
}

function tone(remainingPct, warnPct, lowPct) {
  if (remainingPct < 0) return "muted"
  if (remainingPct <= lowPct) return "urgent"
  if (remainingPct <= warnPct) return "warn"
  return "ok"
}

// "1d 20h", "3h 05m", "4m", "now". Same shape the providers' own UIs use.
function countdown(seconds) {
  var minutes, days, hours, mins
  if (typeof seconds !== "number" || seconds < 0) return ""
  if (seconds < 60) return "now"
  minutes = Math.floor(seconds / 60)
  days = Math.floor(minutes / 1440)
  hours = Math.floor((minutes % 1440) / 60)
  mins = minutes % 60
  if (days > 0) return hours > 0 ? `${days}d ${hours}h` : `${days}d`
  if (hours > 0) return `${hours}h ${pad(mins)}m`
  return `${mins}m`
}

function pad(n) {
  return n < 10 ? `0${n}` : `${n}`
}

function resetText(window) {
  var text
  if (!window) return ""
  text = countdown(window.resetsInS)
  return text === "" ? "" : `resets in ${text}`
}

function percentText(window) {
  if (!window || window.remainingPct < 0) return "—"
  return `${Math.round(window.remainingPct)}%`
}

function ageText(observedAtMs, nowMs) {
  var seconds
  if (!observedAtMs) return "never"
  seconds = Math.max(0, Math.round((nowMs - observedAtMs) / 1000))
  if (seconds < 90) return "just now"
  return `${countdown(seconds)} ago`
}

function isStale(observedAtMs, nowMs, staleAfterMin) {
  if (!observedAtMs) return true
  return (nowMs - observedAtMs) > staleAfterMin * 60000
}

// One tooltip line per provider, plus the freshness of the whole document. A provider that has
// token history adds it — the tooltip is the one place the bar reports activity without the panel
// being open.
function tooltip(providers, observedAtMs, nowMs) {
  var parts, i, p, best, line
  if (!providers.length) return "TMOS usage — no collector output yet"
  parts = []
  for (i = 0; i < providers.length; i++) {
    p = providers[i]
    best = tightest([p])
    line = `${p.name}: ${best ? `${Math.round(best.remainingPct)}% left (${best.window.label})` : (p.statusLabel || "—")}`
    if (p.stats && p.stats.available && p.stats.todayText) line += ` · ${p.stats.todayText}`
    parts.push(line)
  }
  return `${parts.join("\n")}\nread ${ageText(observedAtMs, nowMs)}`
}

// The hover detail on a day row: what the bar height cannot say — the exact count, and for today
// the prompts and sessions that produced it.
function dayTooltip(day) {
  var text
  if (!day) return ""
  text = `${day.label} · ${day.tokens} tokens`
  if (day.today) text += ` · ${day.prompts} prompts · ${day.sessions} sessions`
  return text
}
