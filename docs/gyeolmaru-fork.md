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

## Tests

`tests/test_local_measurement.py` covers all four changes. Each behaviour is paired with
a control, because a check that only ever passes cannot distinguish "correct" from
"measuring nothing" — e.g. the unknown-model warning is paired with an assertion that a
known model stays silent, and the kill switch with an assertion that the call still
happens when the switch is unset.
