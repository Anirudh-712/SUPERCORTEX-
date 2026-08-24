"""Health checks the operator can act on.

The hook checks here exist because of a specific failure mode: a control that is *written*
but not *registered*. The file sits in the repository, the tests import it and pass, the
diff looks complete — and nothing ever fires. Configuration drift of that shape is silent
by construction, so something has to look at it deliberately.

`doctor` is that something. It reports the state rather than inferring it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

PRE_HOOK = "no_fabricated_numbers_pre.py"
POST_HOOK = "no_fabricated_numbers.py"
NUMBER_HOOKS = (PRE_HOOK, POST_HOOK)
WATCHED_MATCHER = "Write|Edit"


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


def _registers_pre_hook(config_path: Path) -> tuple[bool, str]:
    """Is the preventive hook wired to PreToolUse for Write|Edit?

    Event and matcher are both checked. A hook registered under PostToolUse would verify
    rather than prevent; one registered only for Bash would never see a document write.
    Either way the file exists, the mirror matches, and the guarantee is gone.
    """
    if not config_path.exists():
        return False, f"{config_path.name} is missing"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return False, f"{config_path.name} is unreadable or will not parse: {exc}"

    hooks = config.get("hooks")
    if not isinstance(hooks, dict):
        return False, f"{config_path.name} has no hooks section"

    for entry in hooks.get("PreToolUse") or []:
        if entry.get("matcher") != WATCHED_MATCHER:
            continue
        for hook in entry.get("hooks") or []:
            if PRE_HOOK in str(hook.get("command", "")):
                return True, f"PreToolUse / {WATCHED_MATCHER}"

    elsewhere = [
        event
        for event, entries in hooks.items()
        for entry in entries or []
        for hook in entry.get("hooks") or []
        if PRE_HOOK in str(hook.get("command", ""))
    ]
    if elsewhere:
        return False, (
            f"NOT REGISTERED for PreToolUse / {WATCHED_MATCHER} — found instead under "
            f"{', '.join(sorted(set(elsewhere)))}, where it cannot prevent a write"
        )
    return False, f"NOT REGISTERED in {config_path.name}"


def _adapters_synchronised(project: Path) -> tuple[bool, str]:
    drifted: list[str] = []
    missing: list[str] = []
    for name in NUMBER_HOOKS:
        claude = project / ".claude" / "hooks" / name
        codex = project / ".codex" / "hooks" / name
        if not claude.exists() or not codex.exists():
            missing.append(name)
            continue
        if claude.read_bytes() != codex.read_bytes():
            drifted.append(name)
    if missing:
        return False, f"cannot compare, absent from one side: {', '.join(missing)}"
    if drifted:
        return False, (
            f"DRIFTED: {', '.join(drifted)}. Whichever tool loads the stale copy silently "
            f"gets the older, weaker logic."
        )
    return True, f"{len(NUMBER_HOOKS)} adapters byte-identical across .claude and .codex"


def hook_checks(project: Path) -> list[Check]:
    """The five checks that confirm preventive validation is actually enforced."""
    claude_ok, claude_detail = _executable(project / ".claude" / "hooks" / PRE_HOOK)
    codex_ok, codex_detail = _executable(project / ".codex" / "hooks" / PRE_HOOK)
    claude_reg, claude_reg_detail = _registers_pre_hook(
        project / ".claude" / "settings.json"
    )
    codex_reg, codex_reg_detail = _registers_pre_hook(project / ".codex" / "hooks.json")
    synced, sync_detail = _adapters_synchronised(project)

    return [
        Check("preventive hook present (.claude)", claude_ok, claude_detail),
        Check("preventive hook registered (.claude)", claude_reg, claude_reg_detail),
        Check("preventive hook present (.codex)", codex_ok, codex_detail),
        Check("preventive hook registered (.codex)", codex_reg, codex_reg_detail),
        Check("number-hook adapters synchronised", synced, sync_detail),
    ]
