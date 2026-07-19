The admin tool where I paste in a batch of candidate paper URLs and ask it to rank the best ones to suggest for the next reading group has started acting broken.

I paste in ten candidate links, leave everything at its normal defaults, and hit submit expecting a ranked shortlist back. Instead I get back just a single paper, every single time — never a shortlist, just one result. It doesn't matter how many candidates I paste in (I've tried 3, I've tried 10), I still only ever get one paper back, occasionally I get nothing back at all.

This used to give me a nice ranked list of several candidates to choose from. I haven't changed how I use the tool — same workflow, same kind of input — it just quietly started truncating to (at most) one result at some point.

When you believe you have found the cause, emit exactly one line:
LOCATED: {"file": "...", "symbol": "...", "lines": [start, end]}
Then fix it and make the test suite pass.
