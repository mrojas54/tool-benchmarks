Ran the detective end-to-end against a crate that had one obvious prompt-injection issue planted in it (a poisoned few-shot example) plus a couple of ordinary bugs and a lint.

The run completes, and the log clearly shows it noticed the poisoned prompt — it gets classified, analyzed, and a fix diff even gets drafted for it. But when I check the crate afterward, that file is completely untouched. No commit for it, no reverted attempt either — it just quietly never happened, like the propose step for that one category silently no-ops.

Meanwhile the ordinary bug fix and the lint fix both went through completely normally in the same run: proposed, applied, verified, committed. It's specifically the poisoned-prompt case that never lands, every time I try it.

Nothing about the crate's prompt file changed between runs, and I didn't touch any config. This used to work.

When you believe you have found the cause, emit exactly one line:
LOCATED: {"file": "...", "symbol": "...", "lines": [start, end]}
Then fix it and make the test suite pass.
