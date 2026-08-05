"""Read-scope and gate-escape audits for the complex probe's toolset arms.

Extracted verbatim from `complex.py` (no behavior change): the bash tokenization,
path-containment, and gate-boundary checks that verify each arm stayed within the
tools and read-scope it was granted. This half is self-contained -- it needs only
a `ToolCall` and, for `arm_violations`, an `ArmSpec`'s `allowed_tools` -- and
shares nothing with the fixture-loading or scoring halves it used to sit between.

`ArmSpec` is used only in annotations (stringized by `from __future__ import
annotations`) and via attribute access, so it is imported under `TYPE_CHECKING`
only: nothing here imports `complex` at runtime, keeping the dependency acyclic
(`transcript` <- `shell_safety` <- `complex`).
"""

from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path
from typing import TYPE_CHECKING

from toolbench.transcript import ToolCall

if TYPE_CHECKING:
    from toolbench.complex import ArmSpec


# Never grantable to any arm.
BANNED_TOOLS: tuple[str, ...] = ("Task", "Agent")


# Shell control operators that reach a SECOND command past the gated prefix:
# `;` `&&` `||` `|` `&`, command substitution (backtick, `$(`), and redirections.
# A gated arm's only permitted Bash is its scoped oracle invocation; any of these
# turns `npx vitest run` into `npx vitest run; rg ...`, which is how the serena arm
# reaches search it was never granted. Detecting the operator is enough -- I5 makes
# the escape visible, it does not have to make it impossible.
_SHELL_CHAIN_RE = re.compile(r"[;&|`$()<>\n]")


def _tool_input(call: ToolCall) -> dict[str, object] | None:
    """The kept raw input of a ToolCall as a dict, or `None` if unreadable.

    The audits reach a call's arguments here rather than re-interpreting the
    transcript: `keep_raw_input=True` (see `load_calls`) records the exact input
    object the agent sent, which is where a path argument or a gate escape lives.
    """
    if call.raw_input is None:
        return None
    try:
        payload = json.loads(call.raw_input)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _bash_command(call: ToolCall) -> str | None:
    """The `command` string of a Bash ToolCall, from its kept raw input.

    `None` when the input cannot be read as a command -- itself suspicious for a
    gated arm, so the caller treats it as an escape rather than waving it through.
    """
    payload = _tool_input(call)
    command = payload.get("command") if payload is not None else None
    return command if isinstance(command, str) else None


_SERENA_TOOL_PREFIX = "mcp__plugin_serena_serena__"

# Structured read tools -> (path-argument key, default when the key is absent).
# A `None` default means "no path argument when the key is absent" -- e.g. serena's
# `search_for_pattern`/`find_symbol` operate project-wide unless a `relative_path`
# restriction is given, so an absent key is not a read to audit. `Grep`/`Glob`
# default their scope to the cwd (".") -> the trial root, which is always in-tree.
# Keys are the LOGICAL tool name: native tools by their own name, serena tools by
# their name with `_SERENA_TOOL_PREFIX` stripped.
_READ_PATH_ARG: dict[str, tuple[str, str | None]] = {
    "Read": ("file_path", None),
    "Grep": ("path", "."),
    "Glob": ("path", "."),
    "read_file": ("relative_path", None),
    "find_file": ("relative_path", None),
    "list_dir": ("relative_path", None),
    "search_for_pattern": ("relative_path", None),
    "get_symbols_overview": ("relative_path", None),
    "find_symbol": ("relative_path", None),
    "find_referencing_symbols": ("relative_path", None),
}


def _normalized_root(trial_root: Path) -> str:
    return os.path.normpath(str(trial_root))


def _path_escapes(path_str: str, root: str) -> bool:
    """True iff `path_str` resolves outside `root`, by PURE LEXICAL logic.

    Absolute paths are normalized as-is; relative paths are joined to `root` first.
    Never stats the filesystem: the trial tree is a temp dir that may be gone at
    scoring time, so this is `os.path.normpath` against the recorded root only.
    """
    if os.path.isabs(path_str):
        normalized = os.path.normpath(path_str)
    else:
        normalized = os.path.normpath(os.path.join(root, path_str))
    return not (normalized == root or normalized.startswith(root + os.sep))


def _bash_tokens(command: str) -> list[str]:
    """Best-effort tokenization of a shell command. Falls back to whitespace
    splitting when the command is not valid POSIX shlex (an unbalanced quote)."""
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _expand_home(token: str) -> str:
    """Resolve a leading `~`/`~user` and `$HOME`/`${HOME}` to the real home dir.

    `os.path.expanduser` handles `~` but NOT the `$HOME`/`${HOME}` env forms, so
    those are substituted explicitly. A token bearing none of these is returned
    unchanged, so this is safe to apply to any token. Containment (not the mere
    presence of `~`) then decides whether the expanded path escapes -- a trial_root
    that itself lives under home stays correct."""
    expanded = os.path.expanduser(token)
    home = os.path.expanduser("~")
    # `${HOME}` is brace-delimited so a literal replace is unambiguous, but bare
    # `$HOME` needs a variable-name boundary: a plain substring replace would eat
    # the prefix of `$HOMEBREW_PREFIX` and forge an absolute outside path, falsely
    # voiding a benign call.
    expanded = expanded.replace("${HOME}", home)
    return re.sub(r"\$HOME(?![A-Za-z0-9_])", home, expanded)


def _bash_token_escapes(token: str, root: str) -> bool:
    """Best-effort: an absolute token outside `root`, or a `..`-bearing token that
    escapes when joined to `root`. A leading `~`/`$HOME` is expanded first (it is
    otherwise neither absolute nor `..`-bearing, so `find ~` -- the literal
    motivating escape -- would slip through), then judged by containment. A token
    with none of these shapes is not path-like enough to judge and is waved through
    -- the shell can still read via indirection this cannot see, which is why
    full-shell arms are best-effort."""
    expanded = _expand_home(token)
    if os.path.isabs(expanded):
        return _path_escapes(expanded, root)
    if ".." in expanded:
        return _path_escapes(expanded, root)
    return False


def read_escapes(calls: list[ToolCall], trial_root: Path) -> tuple[str, ...]:
    """Reads whose resolved path lies outside the trial tree. Voids the trial.

    Precise for structured read tools; best-effort for full-shell Bash (a shell
    can read via indirection no static audit sees -- `bash script.sh`,
    `cat $(locate x)`, a compiled helper -- the reason per-trial filesystem
    sandboxing is the deferred stronger option for full-shell arms).

    Structured read tools (`Read`/`Grep`/`Glob`, serena symbolic reads) carry an
    explicit path argument: it is extracted, resolved against `trial_root`, and
    flagged if it is not `trial_root` or a descendant. Bash is scanned token by
    token for absolute paths outside the tree and `..` sequences that escape it --
    a tripwire, not a proof, and deliberately not a full shell parse.

    Comparison is PURE LEXICAL (`os.path.normpath` against the passed `trial_root`):
    the trial tree is a temp dir that may be gone at scoring time, so nothing here
    stats the filesystem. Returns a sorted tuple for determinism.
    """
    root = _normalized_root(trial_root)
    escapes: set[str] = set()
    for call in calls:
        if call.name == "Bash":
            command = _bash_command(call)
            if command is None:
                # Unreadable Bash is already a gate violation via `arm_violations`;
                # it carries no legible path, so it adds no read-scope signal here.
                continue
            for token in _bash_tokens(command):
                if _bash_token_escapes(token, root):
                    escapes.add(f"ReadEscape:Bash:{token}")
            continue
        if call.name.startswith(_SERENA_TOOL_PREFIX):
            logical = call.name[len(_SERENA_TOOL_PREFIX) :]
        else:
            logical = call.name
        spec = _READ_PATH_ARG.get(logical)
        if spec is None:
            continue
        key, default = spec
        payload = _tool_input(call)
        if payload is None:
            continue
        candidates: list[object] = [payload.get(key, default)]
        if logical == "Glob":
            # Glob's `path` defaults to "." (in-tree) but its `pattern` is the arg
            # that actually reaches the filesystem: a glob pattern is a path with
            # metacharacters, so an escaping literal prefix (`../corpus/**/*.ts`, an
            # absolute pattern) reads outside the tree while `path` stays ".". The
            # pattern is read RELATIVE TO `path`, so it must be resolved there before
            # the containment check -- otherwise a legit `path="web/src",
            # pattern="../*.ts"` (which stays inside the tree) is falsely voided.
            pattern = payload.get("pattern")
            if isinstance(pattern, str):
                base = payload.get("path", ".")
                base_str = base if isinstance(base, str) else "."
                candidates.append(
                    os.path.join(_expand_home(base_str), _expand_home(pattern))
                )
        for raw in candidates:
            if not isinstance(raw, str):
                continue
            if _path_escapes(_expand_home(raw), root):
                escapes.add(f"ReadEscape:{call.name}:{raw}")
    return tuple(sorted(escapes))


def _gate_prefixes(arm: ArmSpec) -> tuple[str, ...]:
    """The command prefixes the arm's `Bash(<prefix>:*)` rules permit."""
    return tuple(
        rule[len("Bash(") : -len(":*)")]
        for rule in arm.allowed_tools
        if rule.startswith("Bash(") and rule.endswith(":*)")
    )


def arm_violations(calls: list[ToolCall], arm: ArmSpec) -> tuple[str, ...]:
    """Tool names the arm used but was not granted -- plus any banned tool, always,
    plus any gated Bash call whose command escaped the gate.

    The restriction is verified from the transcript, never trusted from the
    `--allowedTools` flag. A flag that silently fails to restrict is the TB-29
    `--exclude-subagents` no-op: the suite ratified it while it did nothing.

    For an arm without a full shell, granting `Bash(<oracle>:*)` collapses to the
    tool name "Bash", so a call that chains `rg` after the oracle is invisible to a
    name-only audit -- the very escape the gate exists to prevent. So each such
    Bash call's command string is inspected here: permitted iff it starts with a
    gate prefix and reaches no second command. The full-shell arms (bare "Bash")
    are exempt: chaining is not an escape when the shell is the point.
    """
    granted = {name for name in arm.allowed_tools if not name.startswith("Bash(")}
    full_shell = "Bash" in granted
    gate_prefixes = _gate_prefixes(arm)
    if gate_prefixes:
        granted.add("Bash")
    used = {call.name for call in calls}
    violations = (used - granted) | (used & set(BANNED_TOOLS))

    if not full_shell and gate_prefixes:
        for call in calls:
            if call.name != "Bash":
                continue
            command = _bash_command(call)
            if command is None:
                violations.add("Bash:<unreadable command>")
            elif _command_escapes_gate(command, gate_prefixes):
                violations.add(f"Bash:{command}")
    return tuple(sorted(violations))


def _command_escapes_gate(command: str, gate_prefixes: tuple[str, ...]) -> bool:
    """True iff `command` is not one of the arm's permitted oracle invocations.

    Permitted means: it matches a granted prefix at a TOKEN BOUNDARY AND carries no
    shell operator that would reach a second command. Both halves are load-bearing
    -- a command that matches the prefix but chains (`npx vitest run; rg`) escapes
    just as surely as one that never matched the prefix at all.

    The boundary matters: a bare `startswith` would permit `npx vitest runx` and
    `cargo testevil`, which share the prefix `npx vitest run` / `cargo test` but are
    different binaries/commands. A prefix counts as matched only if the command
    equals it exactly or the next character after it is whitespace.
    """
    stripped = command.strip()
    if not any(_matches_prefix_at_boundary(stripped, prefix) for prefix in gate_prefixes):
        return True
    return _SHELL_CHAIN_RE.search(stripped) is not None


def _matches_prefix_at_boundary(command: str, prefix: str) -> bool:
    """True iff `command` is `prefix` exactly or `prefix` followed by whitespace."""
    if not command.startswith(prefix):
        return False
    rest = command[len(prefix) :]
    return rest == "" or rest[0].isspace()
