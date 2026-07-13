Something's inverted with how the server decides whether it's allowed to touch the filesystem.

Started it up the normal way (no special flags) expecting normal read/write behavior, and every single write-shaped operation — writing a file, applying a patch, staging or committing anything — immediately fails, complaining that things are locked down for safety. I didn't ask for anything like that.

Out of curiosity I started it back up again, this time explicitly asking for the locked-down/safety mode instead, expecting writes to be refused — and now they go through just fine. It's completely backwards from what I'd expect: the safe mode lets writes happen, and the normal mode blocks them.

Reads, listing, and everything else that doesn't mutate anything on disk behave exactly as expected in both cases. It's only the writable/not-writable decision that's flipped.

When you believe you have found the cause, emit exactly one line:
LOCATED: {"file": "...", "symbol": "...", "lines": [start, end]}
Then fix it and make the test suite pass.
