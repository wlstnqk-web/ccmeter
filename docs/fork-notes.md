# Fork notes

Why this fork exists and what it changes. Everything here is additive: with no
environment set, behaviour matches upstream.

## 1. JSONL root is configurable

Upstream hardcodes `~/.claude/projects`. Our sessions live on another drive, and the
failure mode is what made this worth patching rather than working around: the glob
finds nothing, the report comes out empty, and **an empty report reads as "no usage"
rather than "wrong directory"**. Zeros look like a measurement.

Resolution order:

1. `CLAUDE_PROJECTS_DIR`
2. `CLAUDE_CONFIG_DIR/projects`
3. `~/.claude/projects` (upstream default)

A blank value is ignored — `Path("")` would silently scan the working directory.

`scan()` now writes to stderr when the root is missing, and when the root exists but
holds no session files. Those are different failures and neither should be silent.

## 2. Per-request `effort`

Upstream's notes list effort under "known confounders" with *"Not observable from
JSONL."* **That is no longer true.** Measured on one session file
(2026-09-02, Claude Code writing `claude-opus-5`):

| records | with `.message.usage` | of those, carrying top-level `effort` |
|---|---|---|
| 22,246 | 3,931 | **3,930** (the one exception is a `<synthetic>` record) |

Distribution inside that **single** session:

| model | effort | records |
|---|---|---|
| claude-opus-5 | low | 2,251 |
| claude-opus-5 | high | 1,597 |
| claude-opus-5 | medium | 63 |
| claude-opus-5 | xhigh | 19 |

Two consequences:

- `effort` is read from the record's top level and carried on `TokenEvent`, so
  usage can be grouped by `(model, effort)`.
- **Effort cannot be treated as a session-level property.** Four buckets appear in one
  session, so labelling a whole session with one effort would average them. Any
  experiment that assigns "one effort per session per day" is measuring something else.

`CACHE_VERSION` is bumped to 5 so pre-existing cache rows are re-parsed rather than
reused without the new field.

## 3. Unpriced models are announced

`PRICING` has no Claude 5 entry, so those models fall through to
`FALLBACK_PRICING` (= Opus 4.6 rates). That is not a rounding error: **pricing is the
comparison axis**, so an unpriced Sonnet billed at Opus rates makes the two models look
equivalent by construction — exactly the comparison this tool exists to make.

`pricing_for()` now warns once per unknown model on stderr. It does **not** invent a
rate; a guessed number in the pricing table would be worse than a loud gap. Add real
entries to `PRICING` when official rates are available.

## 4. Version check can be switched off

`CCMETER_NO_VERSION_CHECK=1` skips the package-index lookup in `check_version()`.

That lookup sends no data — it is a GET of public package metadata — but it is the only
outbound request to a host outside the Anthropic API, and on a locked-down machine an
unaccounted connection reads as an incident. Default is unchanged (check enabled).

## 5. `ccmeter update` is not to be called here

The `update` subcommand downloads from the package index and runs
`pip install --force-reinstall`. It is never invoked automatically — only by that
explicit subcommand — but in this deployment it is **out of bounds**: we install from
this fork's source, and a self-update would silently replace audited code with an
unaudited build. Update by pulling this repository instead.

## 6. Poll events are timestamped and share one stream

⛔**Not additive — this changes what the daemon writes.** Recorded here because the
fork note otherwise promises upstream-identical behaviour with no environment set.

Upstream logs samples to stdout and retries to stderr, and neither carries a
timestamp. Measured 2026-09-04 against a live `poll.err` holding 93
`retry in 120s [429]` lines: the count was readable, the window was not. Whether
those rate limits were ongoing or a finished episode could not be decided from the
file, and the successes that would have bracketed them were in a different file with
no timestamps either.

- Every operational event now carries a UTC timestamp (`_event`).
- Retries and credential refreshes moved to stdout, so failures interleave with the
  samples around them. `poll.err` keeps startup errors and tracebacks — after this,
  anything in it is a fault rather than routine noise.
- `health.json` gained `failure_counts` (by kind: `429`, `401`, `network`) and
  `counts_since`. `recent_errors` holds five entries, so a failure *rate* was never
  readable from it — five entries look the same whether they came from five failures
  or five hundred. Counts reset on restart, which is why the window is written beside
  them: a count without its window is not a rate.

This is instrumentation only. It does not change retry timing, and deliberately so —
the backoff question is a separate change, and folding them together would make it
impossible to tell which one moved the numbers.

## 7. A 429 changes the retry cadence

⛔**Not additive** — like §6, this changes daemon behaviour and is recorded for that
reason. This is the separate change §6 said was coming.

Upstream computed the post-429 delay as `max(interval, 60)`. At the default 120s
interval that returns 120 — **the interval itself**. The daemon answered "you are
asking too often" by asking again at exactly the rate that produced the limit, so an
episode could only end when the server relented. The 93 identical `retry in 120s [429]`
lines quoted in §6 are that arithmetic, not a coincidence.

Now, on a 429:

- If the server sent `Retry-After`, that wins outright — including when it asks for
  longer than our own ceiling. The server knows its limit better than our guess.
- Otherwise the delay doubles, floored at `max(interval, 60)` and capped at 900s
  (a tenth of the five-hour window). The floor matters in both directions: backing off
  *below* the normal cadence would be a speed-up wearing a retreat's name, and the 60
  is the one part of the old expression worth keeping — at a 10s interval, a doubled
  20s backoff is arithmetically a retreat and practically still hammering.

`Retry-After` is defined in two forms (RFC 7231 §7.1.3) and both are now parsed:
delta-seconds and HTTP-date. Reading only the integer form let a date-valued header
fall through to our own guess **silently** — the server named a time and nothing
recorded that we ignored it. An unreadable header now says so on the event stream;
an absent or blank one stays quiet, because that is not a failure.

A date already in the past parses to 0, which is falsy, so it falls through to our own
backoff rather than becoming an immediate retry. That behaviour rests entirely on 0
being falsy — precisely the kind of thing a later reader "corrects" into `is not None`,
at which point a stale header becomes an instant retry against a rate limit. There is a
test pinning it for that reason.

The other failure paths (401/403, network/5xx) are untouched, and a negative control
asserts it: widening the rate-limit branch could otherwise swallow them while every
other assertion still passed.

**This does not explain the rate limits.** It stops the daemon from sustaining them.
Why the limit is reached at all is not answered here, and the counters from §6 need
days of accumulation before they can answer it.

## Tests

`tests/test_local_measurement.py` covers §1–§4; `tests/test_poll_instrumentation.py`
covers §6 and `tests/test_poll_backoff.py` covers §7. §5 is a deployment rule, not a
code change, so nothing tests it — saying so is better than implying coverage that
does not exist.

Each behaviour is paired with a control, because a check that only ever passes cannot
distinguish "correct" from "measuring nothing" — e.g. the unknown-model warning is
paired with an assertion that a known model stays silent, the kill switch with an
assertion that the call still happens when the switch is unset, and the rate-limit
backoff with an assertion that the 401/403 and network paths still return their own
delays.
