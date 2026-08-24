"""Health checks the operator can act on.

The hook checks here exist because of a specific failure mode: a control that is *written*
but not *enforced*. There are three ways that happens, and each is silent:

* the file is missing or not executable
* it is registered under the wrong event or matcher, so it never fires on a document write
* it is registered with a command that does not *invoke* it — a moved checkout, a stale
  absolute path, a filename that only appears as an argument

The third is the nastiest, because everything looks correct: the file exists and its name
appears in the configuration. Only resolving the invocation reveals the gap.

Every check is total. `settings.json` is hand-edited and attacker-adjacent, so a malformed
shape must produce a red row with a diagnostic — never a traceback that takes `doctor` down
and leaves the operator with no report at all. And a *partly* malformed configuration is
not a partly working one: the entry the parser had to skip may be the very entry the client
honours, so any damage under the inspected event fails the check outright.
"""
from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PRE_HOOK = "no_fabricated_numbers_pre.py"
POST_HOOK = "no_fabricated_numbers.py"
WATCHED_MATCHER = "Write|Edit"

#: Substitutions a hook command may legitimately use to stay checkout-relative, so a fresh
#: clone or a moved directory still resolves. `${CLAUDE_PROJECT_DIR}` is the documented
#: form; the `git rev-parse` spellings are expanded by the shell that runs the command.
PORTABLE_SUBSTITUTIONS = (
    "${CLAUDE_PROJECT_DIR}",
    "$CLAUDE_PROJECT_DIR",
    "$(git rev-parse --show-toplevel)",
    "`git rev-parse --show-toplevel`",
)

#: `python`, `python3`, `python3.12` — the interpreters a hook may reasonably be run under.
_INTERPRETER = re.compile(r"^python(\d+(\.\d+)?)?$")

#: Interpreter flags after which no script path is executed at all: `-c` runs inline code
#: and `-m` runs a module, so a hook named anywhere after either would never run.
_FLAGS_ENDING_THE_SCAN = ("-c", "-m")

#: Interpreter flags that consume the *next* token as their value. The script still follows
#: — `python3 -W ignore hook.py` does run `hook.py` — so the value is skipped, not treated
#: as the script and not treated as the end of the scan.
_FLAGS_WITH_VALUE = ("-W", "-X", "--check-hash-based-pycs")


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def _executable(path: Path) -> tuple[bool, str]:
    if not path.exists():
        return False, f"MISSING at {path.parent.name}/{path.name}"
    if not path.stat().st_mode & 0o111:
        return False, "present but NOT executable — a hook that cannot run is not a hook"
    return True, "present and executable"


def _load_config(path: Path) -> tuple[dict[str, Any] | None, str]:
    """Read a hook configuration, or explain precisely why it could not be read."""
    if not path.exists():
        return None, f"{path.name} is missing"
    if path.is_dir():
        return None, f"{path.name} is a directory, not a file"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"{path.name} is unreadable: {exc}"
    try:
        config = json.loads(raw)
    except ValueError as exc:
        return None, f"{path.name} will not parse as JSON: {exc}"
    if not isinstance(config, dict):
        return None, (
            f"{path.name} has a top-level {type(config).__name__}, expected an object"
        )
    return config, ""


def _handler_commands(config: dict[str, Any], event: str) -> tuple[list[tuple[str, str]], str]:
    """Every (matcher, command) pair under `event`, plus a note describing any damage.

    Malformed entries are skipped rather than raising, but they are *reported*: a
    configuration of the wrong shape must say why it yielded nothing, because silence is
    indistinguishable from "the hook is simply not registered".
    """
    hooks = config.get("hooks")
    if not isinstance(hooks, dict):
        return [], f"hooks is a {type(hooks).__name__}, expected an object"

    entries = hooks.get(event)
    if entries is None:
        return [], ""
    if not isinstance(entries, list):
        return [], f"hooks.{event} is a {type(entries).__name__}, expected a list"

    pairs: list[tuple[str, str]] = []
    damaged: list[str] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            damaged.append(f"{event}[{index}] is a {type(entry).__name__}")
            continue
        matcher = entry.get("matcher")
        handlers = entry.get("hooks")
        if handlers is None:
            continue
        if not isinstance(handlers, list):
            damaged.append(f"{event}[{index}].hooks is a {type(handlers).__name__}")
            continue
        for position, handler in enumerate(handlers):
            if not isinstance(handler, dict):
                damaged.append(
                    f"{event}[{index}].hooks[{position}] is a {type(handler).__name__}"
                )
                continue
            command = handler.get("command")
            if not isinstance(command, str):
                damaged.append(
                    f"{event}[{index}].hooks[{position}].command is a "
                    f"{type(command).__name__}"
                )
                continue
            pairs.append((matcher if isinstance(matcher, str) else "", command))
    return pairs, "; ".join(damaged)


def _expand(command: str, project: Path) -> str:
    expanded = command
    for token in PORTABLE_SUBSTITUTIONS:
        expanded = expanded.replace(token, str(project))
    return expanded


def _script_argument(tokens: list[str]) -> str | None:
    """The script an interpreter would run, or None if it would run something else.

    `python3 -m supercortex.x` and `python3 -c '…'` run no script file at all, and a hook
    named after either would never execute. Options that merely take a value are different:
    `python3 -W ignore hook.py` does run `hook.py`, so the value is stepped over and the
    scan continues. A value-consuming option with nothing after it runs no script either —
    it must return None rather than fall through and mistake something else for one.
    """
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in _FLAGS_ENDING_THE_SCAN:
            return None                      # -c / -m: no script path is executed
        if token in _FLAGS_WITH_VALUE:
            if index + 1 >= len(tokens):
                return None                  # the option swallowed the end of the command
            index += 2                       # skip the option and the value it consumes
            continue
        if token.startswith("-"):
            index += 1                       # a switch such as -u or -E
            continue
        return token
    return None


def _invoked_target(command: str, project: Path, filename: str) -> tuple[Path | None, str]:
    """The path this command would actually *execute*, if it executes `filename` at all.

    Position is the whole point. `--hook x.py` names the file as a flag value and
    `cat x.py` reads it; neither runs it. Only two shapes count:

        /path/to/x.py                 the hook is the executable
        python3 /path/to/x.py         a Python interpreter runs the hook as its script

    Returns `(target, note)`. A note without a target means the filename was mentioned
    somewhere that would never run it — which is exactly the silent failure being hunted.
    """
    try:
        tokens = shlex.split(_expand(command, project))
    except ValueError as exc:
        return None, f"command cannot be parsed ({exc})"
    if not tokens:
        return None, ""

    mentioned = any(Path(token).name == filename for token in tokens)

    # `env` and leading VAR=value assignments precede the real command.
    start = 0
    if Path(tokens[0]).name == "env":
        start = 1
        while start < len(tokens) and "=" in tokens[start] and not tokens[start].startswith("-"):
            start += 1
    rest = tokens[start:]
    if not rest:
        return None, "command runs nothing" if mentioned else ""

    if Path(rest[0]).name == filename:
        return Path(rest[0]), ""

    if _INTERPRETER.match(Path(rest[0]).name):
        script = _script_argument(rest[1:])
        if script is not None and Path(script).name == filename:
            return Path(script), ""

    if mentioned:
        return None, (
            f"{filename} appears in the command but not in a position that runs it: "
            f"{command!r}"
        )
    return None, ""


def _registers_pre_hook(
    config_path: Path, project: Path, expected: Path
) -> tuple[bool, str]:
    """Is the preventive hook wired to PreToolUse / Write|Edit, and does it run from here?

    Four independent ways to fail, all silent in production:
      * absent from the configuration
      * present under the wrong event or matcher — it would verify, or never see a write
      * present but named where it would never be executed
      * present but pointing somewhere else — the classic moved-checkout case, where the
        configuration is committed with an absolute path and the clone runs nothing
    """
    config, why = _load_config(config_path)
    if config is None:
        return False, why

    pairs, damaged = _handler_commands(config, "PreToolUse")
    if damaged:
        # Partly malformed is not partly working. The skipped entry may be the one the
        # client honours, so a valid registration elsewhere proves nothing about it.
        return False, (
            f"{config_path.name} is structurally malformed under PreToolUse ({damaged}), "
            f"so what actually runs on a document write cannot be determined. Repair the "
            f"configuration before trusting any row above."
        )

    resolved_expected = expected.resolve()
    misplaced: list[str] = []

    for matcher, command in pairs:
        target, note = _invoked_target(command, project, PRE_HOOK)
        if note:
            misplaced.append(note)
        if target is None:
            continue
        if matcher != WATCHED_MATCHER:
            misplaced.append(
                f"registered under matcher {matcher!r}, not {WATCHED_MATCHER!r}, so it "
                f"never fires on a document write"
            )
            continue
        if not target.exists():
            misplaced.append(f"command runs {target}, which does not exist")
            continue
        if target.resolve() != resolved_expected:
            misplaced.append(
                f"command runs {target}, which does not resolve to this checkout's "
                f"{resolved_expected}"
            )
            continue
        if not target.stat().st_mode & 0o111:
            misplaced.append(f"{target} is not executable")
            continue
        return True, f"PreToolUse / {WATCHED_MATCHER} -> {target.name}"

    # Registered somewhere, but not where it prevents anything.
    for event in ("PostToolUse", "SessionStart", "Stop"):
        others, _ = _handler_commands(config, event)
        for _matcher, command in others:
            if _invoked_target(command, project, PRE_HOOK)[0] is not None:
                misplaced.append(f"found under {event}, where it cannot prevent a write")

    if misplaced:
        return False, (
            f"NOT REGISTERED effectively in {config_path.name}: {'; '.join(misplaced[:3])}. "
            f"Repoint the command at {resolved_expected}, or use the checkout-relative "
            f"form \"${{CLAUDE_PROJECT_DIR}}/{expected.relative_to(project)}\"."
        )
    return False, f"NOT REGISTERED in {config_path.name}"


def _post_hook_verifies(config_path: Path, project: Path, expected: Path) -> tuple[bool, str]:
    """The verifying half must also be wired, or Law 1 is left with one layer.

    The preventive hook judges intent; this one re-reads the bytes that landed. Losing it
    is invisible — every write still succeeds — which is why it gets its own row.
    """
    config, why = _load_config(config_path)
    if config is None:
        return False, why

    pairs, damaged = _handler_commands(config, "PostToolUse")
    if damaged:
        return False, (
            f"{config_path.name} is structurally malformed under PostToolUse ({damaged}), "
            f"so what runs after a document write cannot be determined."
        )

    resolved_expected = expected.resolve()
    for matcher, command in pairs:
        target, _note = _invoked_target(command, project, POST_HOOK)
        if target is None or matcher != WATCHED_MATCHER:
            continue
        if target.exists() and target.resolve() == resolved_expected:
            return True, f"PostToolUse / {WATCHED_MATCHER} -> {target.name}"
    return False, (
        f"{POST_HOOK} is NOT REGISTERED effectively under PostToolUse / "
        f"{WATCHED_MATCHER} in {config_path.name}, so nothing verifies what landed."
    )


def hook_checks(project: Path) -> list[Check]:
    """The checks that confirm Law 1 is enforced rather than merely written down."""
    settings = project / ".claude" / "settings.json"
    pre_hook = project / ".claude" / "hooks" / PRE_HOOK
    post_hook = project / ".claude" / "hooks" / POST_HOOK

    pre_ok, pre_detail = _executable(pre_hook)
    post_ok, post_detail = _executable(post_hook)
    pre_reg, pre_reg_detail = _registers_pre_hook(settings, project, pre_hook)
    post_reg, post_reg_detail = _post_hook_verifies(settings, project, post_hook)

    return [
        Check("preventive hook present", pre_ok, pre_detail),
        Check("preventive hook registered", pre_reg, pre_reg_detail),
        Check("verifying hook present", post_ok, post_detail),
        Check("verifying hook registered", post_reg, post_reg_detail),
    ]
