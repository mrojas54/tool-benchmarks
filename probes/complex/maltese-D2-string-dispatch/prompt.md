Ran the detective end-to-end against a crate we had deliberately planted one prompt-injection sample in (a booby-trapped example sitting inside a prompt file), alongside a couple of ordinary logic errors and one cosmetic formatting complaint.

The run completes, and the log clearly shows it noticed the injected instructions — that finding gets picked up, classified, analyzed, and a fix diff even gets drafted for it. But when I check the crate afterward, the file it was supposed to repair is completely untouched. No commit for it, no reverted attempt either — it just quietly never happened, as though that one category of finding drops on the floor at the last moment.

Meanwhile, in the very same run, the ordinary logic errors and the formatting complaint go all the way through: drafted, applied, verified, committed. It's specifically the injected-instructions finding that never lands, every time I try it.

Nothing about the crate's prompt file changed between runs, and I didn't touch any config. This used to work.

When you believe you have found the cause, emit exactly one line:
LOCATED: {"file": "...", "symbol": "...", "lines": [start, end]}
Then fix it and make the test suite pass.
