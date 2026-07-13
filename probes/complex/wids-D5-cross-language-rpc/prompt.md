Every reading-group leader and even the admins are now getting blocked from regenerating the paper writeup on the public paper page — the "regenerate" control is just permanently greyed out / gated off for everyone, including people who definitely should be allowed to trigger it (chapter owners, meeting leaders).

Before this, it worked as expected: owners and the leader of the meeting that uses a given paper could trigger a regen, and it was only actually blocked for people who genuinely shouldn't have access.

Nothing about roles or meeting leadership assignments changed recently. From where I'm sitting it looks like the permission check itself just started saying "no" to everyone, across the board, with no distinction between who should and shouldn't be allowed.

When you believe you have found the cause, emit exactly one line:
LOCATED: {"file": "...", "symbol": "...", "lines": [start, end]}
Then fix it and make the test suite pass.
