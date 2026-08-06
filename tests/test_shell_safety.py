"""The shell-safety audits: arm enforcement, gate-token boundaries, read escapes.

Extracted from `test_complex.py` when `shell_safety.py` became its own module.
These import from `toolbench.shell_safety` directly rather than through
`complex.py`'s re-exports, so the module under test is named by the file that
tests it and a broken re-export cannot masquerade as a passing audit.

`complex.py` keeps the scoring, profile, and defect-loading tests.
"""

import json
import os
import unittest
from pathlib import Path

from toolbench.complex import ArmSpec, build_arms, derive_test_gate, load_calls
from toolbench.shell_safety import (
    _command_escapes_gate,
    arm_violations,
    read_escapes,
)
from toolbench.transcript import ToolCall, UsageProvenance

FIXTURE = "tests/fixtures/complex_session_located.jsonl"

# Mirrors test_complex.py: the gate is derived from its own defect's oracle,
# never hand-picked to match a fixture's expectations.
GATE = derive_test_gate(("npx", "vitest", "run", "tests/schedule.test.ts"))


def _arm(name: str) -> ArmSpec:
    return next(a for a in build_arms(GATE) if a.name == name)


class ArmViolationTests(unittest.TestCase):
    """Arm enforcement is verified from the transcript, never trusted from the flag."""

    def test_a_call_outside_the_arm_is_a_violation(self) -> None:
        calls = load_calls(FIXTURE)  # fixture uses Grep + Edit
        self.assertEqual(arm_violations(calls, _arm("serena")), ("Edit", "Grep"))

    def test_the_agent_tool_is_a_violation_even_for_the_control_arm(self) -> None:
        # The ban is verified from the transcript, never trusted from the flag.
        calls = load_calls("tests/fixtures/complex_session_agent_escape.jsonl")
        self.assertIn("Task", arm_violations(calls, _arm("control")))

    def test_a_bash_call_that_chains_past_the_gate_is_a_violation(self) -> None:
        # The whole point of I5: a serena-arm agent that reaches rg by chaining
        # inside its test-command gate (`npx vitest run; rg formatSlot`) escaped
        # the restriction. Collapsing every Bash rule to the tool name "Bash" made
        # that invisible to the audit built to catch it -- inspect the command.
        calls = load_calls("tests/fixtures/complex_session_gate_escape.jsonl")
        violations = arm_violations(calls, _arm("serena"))
        self.assertTrue(
            any(v.startswith("Bash:") and "rg formatSlot" in v for v in violations),
            violations,
        )

    def test_a_clean_gated_bash_call_is_not_flagged(self) -> None:
        # The first call in the fixture is exactly the permitted oracle invocation
        # (`npx vitest run <path>`). It must NOT be reported, or the audit cries
        # wolf on every legitimate fix checkpoint.
        calls = load_calls("tests/fixtures/complex_session_gate_escape.jsonl")
        violations = arm_violations(calls, _arm("serena"))
        self.assertFalse(
            any(v.startswith("Bash:") and "hint.test.ts" in v for v in violations),
            violations,
        )

    def test_the_full_bash_arm_is_not_flagged_for_chaining(self) -> None:
        # The bash arm is granted a full shell on purpose; chaining is not an
        # escape there, so command inspection must not fire.
        calls = load_calls("tests/fixtures/complex_session_gate_escape.jsonl")
        self.assertEqual(arm_violations(calls, _arm("bash")), ())

class GateTokenBoundaryTests(unittest.TestCase):
    """F3: `_command_escapes_gate` matched the prefix with a bare `startswith`, so
    `npx vitest runx` and `cargo testevil` shared the prefix, carried no shell
    operator, and were scored PERMITTED though they are different binaries. Since
    the gate audit is the primary enforcement (the --allowedTools flag does not
    restrict reads), a prefix must match only at a token boundary."""

    PREFIXES = ("npx vitest run", "cargo test")

    def test_prefix_glued_to_more_word_chars_escapes(self) -> None:
        self.assertTrue(_command_escapes_gate("npx vitest runx", self.PREFIXES))
        self.assertTrue(_command_escapes_gate("cargo testevil", self.PREFIXES))

    def test_exact_prefix_is_permitted(self) -> None:
        self.assertFalse(_command_escapes_gate("npx vitest run", self.PREFIXES))
        self.assertFalse(_command_escapes_gate("cargo test", self.PREFIXES))

    def test_prefix_then_space_is_permitted(self) -> None:
        self.assertFalse(
            _command_escapes_gate("npx vitest run tests/foo.test.ts", self.PREFIXES)
        )

    def test_prefix_then_tab_is_permitted(self) -> None:
        self.assertFalse(_command_escapes_gate("cargo test\t--lib", self.PREFIXES))

    def test_a_chained_command_after_the_prefix_still_escapes(self) -> None:
        # The shell-operator half of the check is unchanged.
        self.assertTrue(
            _command_escapes_gate("npx vitest run; rg formatSlot", self.PREFIXES)
        )


def _rc(name: str, **inp: object) -> ToolCall:
    """A ToolCall carrying `inp` as its kept raw_input -- the shape read_escapes sees."""
    return ToolCall(
        agent="claude-code",
        source="raw",
        project="p",
        name=name,
        input_chars=0,
        output_chars=0,
        session_id="s",
        ts="2026-01-01T00:00:00Z",
        usage=None,
        usage_provenance=UsageProvenance.ABSENT_BY_SCHEMA,
        duration_ms=None,
        error=None,
        model=None,
        raw_input=json.dumps(inp),
    )


_SERENA = "mcp__plugin_serena_serena__"
# A concrete, absent-from-disk temp path: read_escapes must be pure lexical and
# never stat it, so it need not exist.
TRIAL_ROOT = Path("/tmp/tb-trial-xyz")


class ReadEscapeTests(unittest.TestCase):
    """The primary arm-enforcement gate: any read outside the trial tree voids
    the trial. Precise for structured read tools; best-effort for full-shell Bash."""

    def test_native_read_escaping_the_tree_is_flagged_intree_is_not(self) -> None:
        escaped = read_escapes([_rc("Read", file_path="../../secret.txt")], TRIAL_ROOT)
        self.assertTrue(any(e.startswith("ReadEscape:") for e in escaped), escaped)
        clean = read_escapes([_rc("Read", file_path="src/schedule.ts")], TRIAL_ROOT)
        self.assertEqual(clean, ())

    def test_native_grep_with_absolute_path_outside_the_tree_is_flagged(self) -> None:
        calls = [_rc("Grep", pattern="x", path="/Users/me/corpus/wids")]
        escaped = read_escapes(calls, TRIAL_ROOT)
        self.assertTrue(any("/Users/me/corpus/wids" in e for e in escaped), escaped)

    def test_serena_read_file_escaping_relative_path_is_flagged_intree_is_not(self) -> None:
        out = read_escapes(
            [_rc(f"{_SERENA}read_file", relative_path="../../corpus/wids/web/lib/x.ts")],
            TRIAL_ROOT,
        )
        self.assertTrue(any(e.startswith("ReadEscape:") for e in out), out)
        intree = read_escapes(
            [_rc(f"{_SERENA}read_file", relative_path="web/lib/x.ts")], TRIAL_ROOT
        )
        self.assertEqual(intree, ())

    def test_bash_absolute_read_outside_is_flagged_oracle_and_intree_are_not(self) -> None:
        outside = read_escapes(
            [_rc("Bash", command="cat /Users/me/corpus/wids/web/lib/x.ts")], TRIAL_ROOT
        )
        self.assertTrue(any("/Users/me/corpus/wids/web/lib/x.ts" in e for e in outside), outside)
        self.assertEqual(read_escapes([_rc("Bash", command="npx vitest run")], TRIAL_ROOT), ())
        self.assertEqual(read_escapes([_rc("Bash", command="cat ./src/x.ts")], TRIAL_ROOT), ())

    def test_bash_dotdot_read_escaping_the_tree_is_flagged(self) -> None:
        out = read_escapes([_rc("Bash", command="rg formatSlot ../../corpus")], TRIAL_ROOT)
        self.assertTrue(any("../../corpus" in e for e in out), out)

    def test_serena_find_referencing_symbols_escaping_is_flagged_intree_is_not(self) -> None:
        # B1: find_referencing_symbols is granted to serena/control and carries a
        # relative_path, but was omitted from the audited dict -> escapes read the
        # pristine source unflagged.
        out = read_escapes(
            [
                _rc(
                    f"{_SERENA}find_referencing_symbols",
                    name_path="formatSlot",
                    relative_path="../../corpus/wids/web/src/lib/schedule.ts",
                )
            ],
            TRIAL_ROOT,
        )
        self.assertTrue(any(e.startswith("ReadEscape:") for e in out), out)
        intree = read_escapes(
            [
                _rc(
                    f"{_SERENA}find_referencing_symbols",
                    name_path="formatSlot",
                    relative_path="web/src/lib/schedule.ts",
                )
            ],
            TRIAL_ROOT,
        )
        self.assertEqual(intree, ())

    def test_bash_tilde_and_home_reads_are_flagged(self) -> None:
        # B2: ~ / $HOME tokens are neither isabs nor `..`, so they slipped past the
        # tripwire. `find ~` is the literal motivating escape.
        for cmd in (
            "find ~ -name schedule.ts",
            "cat ~/tool-benchmarks/corpus/x.ts",
            "cat $HOME/corpus/x.ts",
            "cat ${HOME}/corpus/x.ts",
        ):
            out = read_escapes([_rc("Bash", command=cmd)], TRIAL_ROOT)
            self.assertTrue(
                any(e.startswith("ReadEscape:Bash:") for e in out), (cmd, out)
            )

    def test_bash_tilde_resolving_inside_trial_root_is_not_flagged(self) -> None:
        # B2: flag by CONTAINMENT, not a blanket "any ~ escapes". A trial_root that
        # itself sits under home must stay clean when the ~ token resolves inside it.
        fake_home = Path("/tmp/tb-fake-home")
        trial_root = fake_home / "trial"
        old = os.environ.get("HOME")
        os.environ["HOME"] = str(fake_home)
        try:
            out = read_escapes(
                [_rc("Bash", command="cat ~/trial/src/x.ts")], trial_root
            )
        finally:
            if old is None:
                del os.environ["HOME"]
            else:
                os.environ["HOME"] = old
        self.assertEqual(out, ())

    def test_glob_pattern_escaping_the_tree_is_flagged(self) -> None:
        # B3: Glob's pattern is the path-bearing arg; path stays "." (in-tree).
        out = read_escapes(
            [_rc("Glob", pattern="../../corpus/**/*.ts")], TRIAL_ROOT
        )
        self.assertTrue(any(e.startswith("ReadEscape:") for e in out), out)

    def test_glob_intree_pattern_is_not_flagged(self) -> None:
        self.assertEqual(
            read_escapes([_rc("Glob", pattern="**/*.ts")], TRIAL_ROOT), ()
        )
        self.assertEqual(
            read_escapes([_rc("Glob", pattern="src/**/*.ts")], TRIAL_ROOT), ()
        )

    def test_glob_absolute_path_arg_is_flagged_via_path(self) -> None:
        out = read_escapes(
            [_rc("Glob", path="/etc", pattern="*.conf")], TRIAL_ROOT
        )
        self.assertTrue(any("/etc" in e for e in out), out)

    def test_glob_relative_pattern_resolves_against_its_path_not_the_root(self) -> None:
        # Over-restriction guard: `pattern` is read relative to `path`, so a `..`
        # pattern under a non-default in-tree `path` stays inside the tree and must
        # NOT be voided. `web/src` + `../*.ts` -> `web/*.ts`, in-tree.
        self.assertEqual(
            read_escapes(
                [_rc("Glob", path="web/src", pattern="../*.ts")], TRIAL_ROOT
            ),
            (),
        )
        # But a pattern that escapes even after resolving against `path` is flagged.
        out = read_escapes(
            [_rc("Glob", path="web", pattern="../../corpus/**/*.ts")], TRIAL_ROOT
        )
        self.assertTrue(any(e.startswith("ReadEscape:") for e in out), out)

    def test_home_prefixed_env_var_is_not_expanded_as_HOME(self) -> None:
        # Over-restriction guard: `$HOME` expansion must respect a variable-name
        # boundary. `$HOMEBREW_PREFIX` is a different variable, not `$HOME` + suffix;
        # a substring replace would rewrite it into an absolute outside path and
        # falsely void a benign call.
        self.assertEqual(
            read_escapes(
                [_rc("Bash", command="cat $HOMEBREW_PREFIX/share/x.ts")], TRIAL_ROOT
            ),
            (),
        )

    def test_escapes_are_returned_sorted(self) -> None:
        calls = [
            _rc("Bash", command="cat /z/late.ts"),
            _rc("Read", file_path="/a/early.ts"),
        ]
        out = read_escapes(calls, TRIAL_ROOT)
        self.assertEqual(list(out), sorted(out))
        self.assertEqual(len(out), 2)

