# Bug report

We use progress bars for long-running jobs, some of which have a known total step
count and some of which don't (we just don't know how many items we'll process
up front).

Since the last update, the ones where we *don't* know the total are showing a
blank/garbled percentage-style readout instead of the "unknown length" display
we used to get, and the ones where we *do* know the total have started showing
the wrong kind of readout too — it's like the two display modes swapped.

Nothing crashes, there's no traceback, and the bar itself still animates fine.
It's purely the little status readout next to the bar that's showing the wrong
thing depending on whether the job has a known total or not. A few of our
long-running scripts print this to a log file, and the log now shows the swapped
readout in exactly the cases where it worked before.

Can you find what's causing this and fix it? I don't know exactly where in the
codebase this lives — I just noticed the visible symptom.

When you believe you have found the cause, emit exactly one line:
LOCATED: {"file": "...", "symbol": "...", "lines": [start, end]}
Then fix it and make the test suite pass.
