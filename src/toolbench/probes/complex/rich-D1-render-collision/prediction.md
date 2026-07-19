# Pre-registered prediction

**Predicted winner: serena**

`render` is defined by 20 distinct classes across 8 files in this corpus, and
`rg '\brender\b'` returns 336 hits repo-wide (`rg 'def render'` alone returns 29).
Twelve of those twenty — RenderableColumn, SpinnerColumn, TextColumn, BarColumn,
TimeElapsedColumn, TaskProgressColumn, TimeRemainingColumn, FileSizeColumn,
TotalFileSizeColumn, MofNCompleteColumn, DownloadColumn, TransferSpeedColumn —
share the byte-identical signature `def render(self, task: "Task") -> Text:`
stacked within ~400 lines of the same file (rich/progress.py). Grep and plain
signature matching cannot rank or separate these candidates; a native
Grep+Edit or bash-rg agent working from the symptom alone has to read through
most or all twelve bodies (or guess) to find the one that actually produces the
percentage-vs-blank readout described in the bug report. An LSP-backed symbolic
tool that resolves the *instantiated column type* used by the default Progress
columns (or a referencing-symbols query from a call site) lands on exactly one
of the twenty definitions without reading the other nineteen, making this the
one defect class in the full benchmark where symbolic reference-resolution has
a structural advantage over text search rather than an incidental one.

Falsified if: native or bash reaches a correct LOCATED in fewer context tokens than serena.
