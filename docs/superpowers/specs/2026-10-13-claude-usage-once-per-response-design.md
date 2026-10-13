# Claude usage counted once per API response (S43)

## Problem

S26 already records the transcript shape: Claude Code writes one API response as
several JSONL entries (`thinking` / `text` / each `tool_use`) that share a
`requestId` and repeat **the same `usage` object**. S39's session totals and S40's
branch buckets were built on `_account_usage`, which adds `usage` from every entry
that carries it. Every multi-block response is therefore billed once per block.

Evidence (2026-10-13, one real Claude Code transcript, 123 usage-bearing entries):

| | entries summed (today) | once per `requestId` |
|---|---|---|
| usage-bearing records | 123 | 69 responses |
| cache read | 23,618,077 | 13,501,892 (1.75x) |
| cache creation | 2,156,853 | 915,122 (2.36x) |
| output | 67,577 | 35,705 (1.89x) |

45 of the 69 responses spanned more than one entry (up to 4); all 45 repeated an
identical `usage` on every copy. `requestId` and `message.id` mapped one-to-one.
On the hand-built split-message fixture used to find this, the inflation was 2x to
3.4x. The size of the error depends on blocks per response, so it is not a
constant that a reader could divide out: a session heavy in parallel tool calls is
inflated more than a prose session, which makes S39/S40 *comparisons* wrong, not
just their absolute values.

This is the project's signature failure in the other direction: a number that
looks measured and is silently overcounted.

## Design

**One response, one charge.** `_UsageTally` gains a `seen` set of billing keys.
`_account_usage` derives the key, and if it has been charged already in this parse
the entry is a no-op for usage (it still flows through turn tracking and the
tool-use join unchanged).

The key, in order:

1. `requestId` (the S26 billing unit), when it is a non-empty string;
2. else `message.id`, when it is a non-empty string;
3. else no key: the entry is charged as today.

Rule 3 keeps hand-written fixtures and any id-less export behaving exactly as
before; there is nothing to dedupe on, and inventing a key (timestamp, content
hash) would be guessing. S30 already rejected timestamp keying.

**First copy wins.** Copies observed in real data are identical, so first-wins and
any reconciliation policy agree today. Max-per-field reconciliation was considered
and rejected: it needs per-key state plus delta bookkeeping in every branch and
`cwd` bucket, for a divergence with no observed instance. A test pins first-wins so
a future change in policy is a visible decision.

**Dedup before bucketing.** The guard sits at the top of `_account_usage`, so the
S39 session totals and the S40 `usage_by_branch` / `detached_usage_by_cwd` buckets
see the same charged set. `session total == sum of buckets` keeps holding.

**`session_usage_messages` now counts billed responses**, not usage-bearing
entries. Nothing ranks or divides by it; the rename of meaning is documented.

**Scope is one parse.** The `seen` set lives on the per-parse tally. Subagent
transcripts are separate files with their own requestIds, so no cross-file state
is needed. Hermes (session row) and Codex (`ABSENT_BY_SCHEMA`) are untouched.

## Not in scope

- Splitting `cache_creation` into 5m and 1h TTLs.
- Pricing or cost weighting.
- Dedup across files (the same session read via two sources).

## Acceptance (S43)

- Entries sharing a `requestId` are charged once, in session totals and buckets.
- Without `requestId`, entries sharing `message.id` are charged once.
- Entries with neither are each charged (unchanged behaviour).
- First copy wins when copies disagree.
- `session total == sum of branch buckets` still holds with split responses.
- `session_usage_messages` equals the number of distinct billed responses.

## Verification

After the change, `ClaudeParser` on the same transcript reports cache read
13,501,892, creation 915,122, input 135, output 35,705 and 69 billed responses,
matching an independent once-per-`requestId` sum of the file exactly.
