The detective is drafting good fixes and they verify clean, but the run never actually commits them.

Every issue makes it all the way through: proposed, applied to the working tree, and cargo check/test both come back clean. Then right at the commit step it blows up and the issue gets reverted instead of committed — every single time, for every issue, even the simplest one-line ones.

If I go into the run's worktree by hand afterward and stage + commit the exact same file myself with plain git, it works with zero complaints. So the underlying git setup and permissions are fine — it's specifically something about the commit step as the pipeline drives it.

This is new; commits used to land normally.

When you believe you have found the cause, emit exactly one line:
LOCATED: {"file": "...", "symbol": "...", "lines": [start, end]}
Then fix it and make the test suite pass.
