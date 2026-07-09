### agentsview version

agentsview v0.36.1 (commit 4c4bb56, built 2026-07-03T19:21:24Z)

### Install method

Homebrew cask

### OS / platform

macOS 15.7.7 (arm64)

### Which agent and version

Hermes Agent v0.18.2 (2026.7.7.2), upstream 88a58ff1

### Which model(s)

n/a — affects every hermes session regardless of model

### What happened, and what did you expect

`agentsview session export hermes:<id>` exits **0** and streams a whole SQLite database
on stdout instead of that session's JSONL transcript. Every other adapter I have
(claude, codex, cowork, cursor) returns JSONL as documented.

```
$ agentsview session export hermes:cron_2d647784731c_20260708_150044 | head -c 16
SQLite format 3\0        # returncode 0, empty stderr
```

Expected: the JSONL transcript for that one session.

**The session id is validated, then ignored.** A bogus id is correctly rejected:

```
$ agentsview session export hermes:does_not_exist_at_all
fatal: session not in local archive: ...        # rc=1
```

But three *different* valid ids return byte-identical payloads:

```
hermes:cron_2d647784731c_20260708_150044   37,175,296 bytes   sha256 53ac5769ad225157…
hermes:cron_2d647784731c_20260708_050027   37,175,296 bytes   sha256 53ac5769ad225157…
hermes:cron_1ba0e70d34fd_20260707_114622   37,175,296 bytes   sha256 53ac5769ad225157…
```

That payload is `~/.hermes/state.db` verbatim — same byte count, same digest. The export
path resolves the session, then streams the backing store instead of the resolved
session's messages.

**It streams the *default profile's* database specifically.** Hermes supports profiles,
each with its own store:

```
~/.hermes/state.db                          37,175,296 bytes   (default)
~/.hermes/profiles/aphrodite-mood/state.db  24,899,584 bytes
~/.hermes/profiles/light-mood/state.db       9,940,992 bytes
```

Of the 29 hermes sessions in my most recent 500-session page, 2 live in
`profiles/aphrodite-mood`. For those, `session export` returns `rc=0` **and a database
that contains no rows for the requested session at all.** So this is not merely
"returns too much data" — for some sessions it returns the wrong database. A fix that
only scopes the export to one session's messages, without also selecting the right
profile store, would still not reach them.

### Why rc=0 is the actual defect

A nonzero exit would have degraded gracefully in every consumer. Returning **success**
with an off-contract payload is what turns "the hermes adapter cannot export yet" into
a crash downstream: a strict UTF-8 reader hits a NUL at offset 15 of the SQLite header
and dies mid-scan. Mine now sniffs the first 8 KiB for a NUL and skips the session, but
every consumer trusting `rc=0` + the documented JSONL contract inherits this.

There is also a cost: a 500-session scan reads and discards 29 × 37 MB ≈ 1.08 GB, the
same database 29 times over.

### Impact

- Hermes tool-call data is unreachable through the documented interface. Hermes
  contributes 0 tool calls and never appears in an agent breakdown.
- The data plainly exists. Joining `messages.tool_calls[].id → messages.tool_call_id`
  in `state.db` recovers 176 tool calls across those 29 sessions, 0 dangling — a count
  that agrees exactly with hermes' own `sessions.tool_call_count` column.

### Sample session file or snippet

Not attached: the payload is a 37 MB SQLite database containing my full message
archive. The header is enough to reproduce:

```
$ agentsview session export hermes:<any-valid-hermes-id> | head -c 16 | xxd
00000000: 5351 4c69 7465 2066 6f72 6d61 7420 3300  SQLite format 3.
```

### Steps to reproduce

1. Have hermes sessions in the local archive: `agentsview session list --agent hermes --json`
2. Export any one of them and inspect the first bytes:
   `agentsview session export hermes:<id> | head -c 16`
   → `SQLite format 3\0`, `rc=0`, empty stderr.
3. Export a *second, different* hermes id and compare digests:
   `agentsview session export hermes:<id2> | shasum -a 256`
   → identical to step 2's digest.
4. Compare against the on-disk store: `shasum -a 256 ~/.hermes/state.db` → same digest.
5. Confirm the id *is* validated: `agentsview session export hermes:does_not_exist_at_all`
   → `rc=1`, `fatal: session not in local archive`.
