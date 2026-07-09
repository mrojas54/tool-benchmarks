# TB-8: iter_session_files drops subagent sessions under --project

Found by post-merge operator smoke checklist (2026-07-08).

sources.py:42-44 globs recursively via rglob('*.jsonl') but filters on path.parent.name:

    if project is not None and project not in path.parent.name:
        continue

Subagent transcripts live at <project>/subagents/*.jsonl, so parent.name == 'subagents',
which never contains the project substring. Every subagent session is silently dropped
whenever --project is passed. --all is unaffected (project is None short-circuits).

Evidence (project = -Users-michellerojas-wids-nyc-reading-group-assistant):
  iter_session_files(project=P)       -> 197 files,   0 subagent files
  iter_session_files() filtered to P  -> 249 files,  52 subagent files

Spec violations:
  S13 - subagents included by default, excluded only on --exclude-subagents.
        Under --project always excluded; the flag is a no-op.
  S15 - report prints 'Subagents included: yes' after scanning zero (false provenance).

Why the static audit missed it: validation-plan rows 15 (S13) and 17 (S15) were verified
against unit tests constructing SessionRef objects directly, never exercising
iter_session_files. Filter and discovery each correct alone, wrong composed.

Fix: match project against the top-level project dir under root, not parent.name.
Regression test over tmp tree with nested subagents/ dir.
