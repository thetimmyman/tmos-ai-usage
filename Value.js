// Shared subscription ordering for the budget dropdown, overview and detail tabs.
// A complete comparison period/cohort and recognized expense are mandatory.
.pragma library

function rankProviders(providers, context) {
  var rows, previous, rank
  rows = providers.map(function (provider) {
    var row, e, valid
    row = Object.assign({}, provider)
    e = provider.economics
    valid = !!(context && typeof context.period === "string" && context.period &&
      typeof context.cohort === "string" && context.cohort &&
      e && e.period === context.period && e.cohort === context.cohort &&
      e.coverage_complete === true && e.evidence_verified === true &&
      /^[0-9a-f]{64}$/.test(e.outcomes_sha256 || "") && /^[0-9a-f]{64}$/.test(e.spend_sha256 || "") &&
      e.spend_basis === "recognized_subscription_and_metered_usd" &&
      typeof e.spend_usd === "number" && isFinite(e.spend_usd) && e.spend_usd >= 0 &&
      typeof e.validated_tasks === "number" && isFinite(e.validated_tasks) &&
      e.validated_tasks >= 0 && Math.floor(e.validated_tasks) === e.validated_tasks)
    row.rankingMetric = "validated"
    row.valueScore = valid && e.spend_usd > 0 ? e.validated_tasks / e.spend_usd : null
    row.valueStatus = valid ? (e.spend_usd === 0 ? "free" : "ranked") : "unranked"
    row.tasksPerDollar = valid && e.spend_usd > 0 ? e.validated_tasks / e.spend_usd : null
    if (row.tasksPerDollar !== null && !isFinite(row.tasksPerDollar)) {
      row.valueStatus = "unranked"
      row.tasksPerDollar = null
    }
    row.valueRank = null
    row.valueLabel = row.valueStatus === "free" ? "Free" : "Unranked"
    return row
  })
  rows.sort(function (a, b) {
    var group, difference
    group = { ranked: 0, free: 1, unranked: 2 }
    difference = group[a.valueStatus] - group[b.valueStatus]
    if (difference) return difference
    if (a.valueStatus === "ranked" && a.tasksPerDollar !== b.tasksPerDollar)
      return b.tasksPerDollar - a.tasksPerDollar
    return String(a.name).localeCompare(String(b.name)) || String(a.id).localeCompare(String(b.id))
  })
  previous = null
  rank = 0
  rows.forEach(function (row, index) {
    if (row.valueStatus !== "ranked") return
    if (row.tasksPerDollar !== previous) rank = index + 1
    row.valueRank = rank
    row.valueLabel = "#" + rank
    previous = row.tasksPerDollar
  })
  return rows
}

// Deliberately distinct from task rankings: observed utilization, incomplete coverage.
function rankActivity(providers) {
  var rows = providers.map(function(p) {
    var row = Object.assign({}, p), a = p.activity || {}, price = a.price || {}, n = a.observed_turns;
    var valid = typeof n === 'number' && isFinite(n) && n >= 0 && Math.floor(n) === n
      && a.window_days === 30 && typeof price.monthly_usd === 'number' && isFinite(price.monthly_usd) && price.monthly_usd >= 0;
    row.rankingMetric = 'activity';
    row.valueStatus = valid ? (price.monthly_usd > 0 ? 'provisional' : 'free') : 'unranked';
    row.valueScore = valid && price.monthly_usd > 0 ? n / price.monthly_usd : null;
    if (row.valueScore !== null && !isFinite(row.valueScore)) { row.valueStatus = 'unranked'; row.valueScore = null; }
    row.tasksPerDollar = null;
    row.valueRank = null;
    row.valueLabel = row.valueStatus === 'free' ? 'Free*' : (price.monthly_usd === undefined ? 'Set price' : 'No activity');
    return row;
  });
  rows.sort(function(a,b) {
    var groups = {provisional:0, free:1, unranked:2};
    return groups[a.valueStatus] - groups[b.valueStatus] || (b.valueScore || 0) - (a.valueScore || 0)
      || String(a.name).localeCompare(String(b.name)) || String(a.id).localeCompare(String(b.id));
  });
  var previous = null, rank = 0;
  rows.forEach(function(row,i) {
    if (row.valueStatus !== 'provisional') return;
    if (row.valueScore !== previous) rank = i+1;
    row.valueRank = rank; row.valueLabel = '#' + rank + '*'; previous = row.valueScore;
  });
  return rows;
}
