### agentsview version

agentsview v0.36.1 (commit 4c4bb56, built 2026-07-03T19:21:24Z)

### Install method

Homebrew cask

### OS / platform

macOS 15.7.7 (arm64)

### Which agent and version

Hermes Agent v0.18.2 (2026.7.7.2), upstream 88a58ff1

### Which model(s)

n/a — the disagreement is in session enumeration, not per-model

### What happened, and what did you expect

`session list` and `stats` disagree about how many hermes sessions exist in the same
local archive, by 8.9×:

```
$ agentsview stats --agent hermes --format json | jq .totals.sessions_all
789

$ agentsview session list --agent hermes --json --limit 2000 | jq '.total, .next_cursor'
89
null
```

`next_cursor` is `null` and `total` is 89, so this is not a paging artifact — `list`
believes 89 is the complete set. Raising `--limit` to 2000 changes nothing.

I expected the two subsystems to enumerate the same sessions.

### Which is right?

`stats` appears to be. Hermes stores sessions in one SQLite DB per profile:

```
~/.hermes/state.db                          (default)
~/.hermes/profiles/aphrodite-mood/state.db
~/.hermes/profiles/light-mood/state.db
```

Counting `sessions` rows across all three gives **816**, close to `stats`' 789 and
nowhere near `list`'s 89.

The sessions `list` omits follow no obvious rule. Broken down by `sessions.source`:

| source | in archive | returned by `list` | dropped |
|---|---|---|---|
| cron | 733 | 32 | 701 |
| tui | 46 | 28 | 18 |
| cli | 32 | 27 | 5 |
| unknown | 3 | 0 | 3 |
| photon | 1 | 1 | 0 |
| whatsapp | 1 | 1 | 0 |

It is not "cron is excluded" — 32 cron sessions *are* listed. It is not archived-vs-not
(`archived` is 0 on every row), nor parent/child (`parent_session_id` is set only on 4
of the *listed* rows), nor a dedupe on `session_key` or `title` (`session_key` is NULL
on all 733 cron rows; every title is distinct).

I could not derive `list`'s selection from any column in the archive, which is what
makes me think sessions are being lost rather than filtered.

### Impact

Any consumer that enumerates via `session list` sees ~11% of the hermes sessions that
`stats` reports. For a cross-agent comparison this is worse than a missing agent: hermes
is present, plausible, and quietly under-sampled, so per-agent rates computed off
`session list` are skewed with no signal that anything is wrong.

Relatedly, `stats` reports `sessions_automation: 0` for hermes even though 733 of the
archive's sessions have `source = 'cron'`. I have not dug into that one and am not
claiming it shares a cause; mentioning it only because it may be adjacent.

### Sample session file or snippet

n/a — this is an enumeration count, not a parse. Commands above reproduce it without
any session content.

### Steps to reproduce

1. Have a hermes archive with more sessions than `list` returns (mine: 816 rows across
   three profile DBs; any archive with a few hundred cron sessions should do).
2. `agentsview stats --agent hermes --format json | jq .totals.sessions_all`
3. `agentsview session list --agent hermes --json --limit 2000 | jq '.total, .next_cursor'`
4. Compare. Ground truth, if useful:
   `sqlite3 "file:$HOME/.hermes/state.db?mode=ro" "SELECT source, COUNT(*) FROM sessions GROUP BY source;"`
   (repeat for each `~/.hermes/profiles/*/state.db`)

### Note

Found while investigating #1047 (`session export` returns the whole SQLite
archive for hermes sessions). Separate defect, filed separately, but the two together
mean hermes tool-call data is currently both under-enumerated and unreadable.
