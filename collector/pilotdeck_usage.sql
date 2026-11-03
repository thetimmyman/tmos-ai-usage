-- pilotdeck_usage.sql — the SQL projection of one pilotdeck usage row.
--
-- collector/pilotdeck.py writes the private JSON ledger in the state directory;
-- nothing in this repository opens a database for it. This file is the same row
-- for a consumer that stores usage in one, and test_pilotdeck.py executes these
-- statements, so the two cannot drift apart: a new ledger field without a column
-- here fails that test.
--
-- Idempotency is the primary key. A row's identity is (session_id, call_hash),
-- and call_hash is derived from the call's own identifiers in the driver log --
-- a turn's runId, a sub-agent's subagentId, an egress request's time and ordinal
-- -- never from a line number or an import counter. Re-importing a log, or an
-- appended copy of it, therefore conflicts and the insert does nothing.
--
-- Tokens are NULL exactly when the log did not report them; 0 means a reported
-- zero. A hidden call the egress log counted but could not price keeps its row
-- and its NULL tokens rather than a fabricated number.

CREATE TABLE IF NOT EXISTS pilotdeck_usage (
    app                 TEXT    NOT NULL CHECK (app = 'pilotdeck'),
    session_id          TEXT    NOT NULL CHECK (length(session_id) > 0),
    call_type           TEXT    NOT NULL CHECK (call_type IN
                            ('user_direct', 'memory_fetch', 'title_generation',
                             'judge_evaluation', 'subagent_call', 'hidden_call')),
    call_hash           TEXT    NOT NULL CHECK (length(call_hash) = 64),
    source_sha256       TEXT    NOT NULL CHECK (length(source_sha256) = 64),
    role                TEXT    NOT NULL CHECK (role IN ('main', 'subagent', 'background')),
    model               TEXT,
    model_source        TEXT    CHECK (model_source IS NULL OR model_source IN ('gateway', 'driver')),
    -- which marker named the call: response_format and metadata.purpose are the
    -- runtime's own, configured-model is the operator's declaration, driver is the
    -- log stating it outright.
    label_source        TEXT    CHECK (label_source IS NULL OR length(label_source) <= 64),
    provider            TEXT,
    subagent_type       TEXT,
    label               TEXT,
    input_tokens        INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
    output_tokens       INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
    cache_read_tokens   INTEGER CHECK (cache_read_tokens IS NULL OR cache_read_tokens >= 0),
    cache_write_tokens  INTEGER CHECK (cache_write_tokens IS NULL OR cache_write_tokens >= 0),
    reasoning_tokens    INTEGER CHECK (reasoning_tokens IS NULL OR reasoning_tokens >= 0),
    tokens_observed     INTEGER NOT NULL CHECK (tokens_observed IN (0, 1)),
    started_at_ms       INTEGER NOT NULL CHECK (started_at_ms >= 0),
    finished_at_ms      INTEGER CHECK (finished_at_ms IS NULL OR finished_at_ms >= started_at_ms),
    duration_ms         INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
    session_offset_ms   INTEGER NOT NULL CHECK (session_offset_ms >= 0),
    outcome             TEXT    NOT NULL CHECK (outcome IN ('success', 'error', 'unknown')),
    final_event_missing INTEGER NOT NULL DEFAULT 0 CHECK (final_event_missing IN (0, 1)),
    record_hash         TEXT    NOT NULL CHECK (length(record_hash) = 64),
    PRIMARY KEY (session_id, call_hash)
);

-- One session's calls in the order they happened, and the hidden ones by kind.
CREATE INDEX IF NOT EXISTS pilotdeck_usage_session_offset
    ON pilotdeck_usage (session_id, session_offset_ms);
CREATE INDEX IF NOT EXISTS pilotdeck_usage_call_type_started
    ON pilotdeck_usage (call_type, started_at_ms);

-- The one write this source performs. Every value is a bound parameter, and the
-- conflict target is the identity, so a second import of the same call adds no row.
INSERT INTO pilotdeck_usage (
    app, session_id, call_type, call_hash, source_sha256, role, model, model_source,
    label_source, provider, subagent_type, label, input_tokens, output_tokens, cache_read_tokens,
    cache_write_tokens, reasoning_tokens, tokens_observed, started_at_ms, finished_at_ms,
    duration_ms, session_offset_ms, outcome, final_event_missing, record_hash
) VALUES (
    :app, :session_id, :call_type, :call_hash, :source_sha256, :role, :model, :model_source,
    :label_source, :provider, :subagent_type, :label, :input_tokens, :output_tokens, :cache_read_tokens,
    :cache_write_tokens, :reasoning_tokens, :tokens_observed, :started_at_ms, :finished_at_ms,
    :duration_ms, :session_offset_ms, :outcome, :final_event_missing, :record_hash
) ON CONFLICT (session_id, call_hash) DO NOTHING;
