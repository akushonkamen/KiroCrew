"""The crewmate's dashboard upkeep skill: what each field it writes means, when, and how.

rfc-crewmate-dashboard-upkeep-skill.md. A fold field (``{"fold", "path"}``) is filled
by the gateway from the crew log, so nothing here is about it. An agentic field
(``{"agentic": true}``) is the crewmate's to write, and the ``[DASHBOARD]`` block only
names it. This module writes one generated skill per crewmate covering exactly those
fields, plus a COPY of each shipped helper script that fetches data the crew log does
not hold and prints a ready ``dashboard_write`` payload.

**Where.** ``<skills dir>/.dashboard-upkeep/<slug>/SKILL.md`` and ``scripts/``. The
leading dot keeps it out of the skills index -- the tree walk skips dot directories --
because it is reached through the one pointer line in ``[DASHBOARD]``, so no other
session pays an index line for it.

**From what.** The field NAMES come from the page in force
(``dashboard_agentic.page_in_force``), the same lookup ``[DASHBOARD]`` reads. The TEXT
and the helpers come from the SHIPPED catalog entry, never from the crewmate's copy of
the manifest: that copy sits under ``members/<slug>/dashboard/`` on the agent-writable
side, and ``instance.edit`` may change it. Only built-in templates render
(``instance.RENDERABLE_SOURCES``), so every page in force has a shipped entry.

**When.** :func:`sync` writes only what differs from disk, so it is cheap to call and it
also puts back a skill someone edited. Two callers: ``instance._commit`` (adopt, apply,
rollback and edit all commit through it) and ``dashboard_agentic.turn_block`` (a
crewmate on the default page never commits, and a gateway upgrade can change the
shipped text). Every function here is TOTAL: an upkeep skill that cannot be written
costs the pointer line, never the turn or the commit.
"""

from __future__ import annotations

import logging
import os
import stat
import threading
from pathlib import Path
from typing import Any, Final

logger = logging.getLogger(__name__)

#: The dot-directory under the skills dir. The skill walk skips dot directories.
UPKEEP_DIR_NAME: Final[str] = ".dashboard-upkeep"

_lock = threading.Lock()


def skill_dir(slug: str) -> Path:
    """``<skills dir>/.dashboard-upkeep/<slug>``. *slug* is checked, never trusted."""
    from kiro_crew.members import validate_slug
    from kiro_crew.skills import skills_dir

    return skills_dir() / UPKEEP_DIR_NAME / validate_slug(slug)


def pointer(skill: Path) -> str:
    """The one line ``[DASHBOARD]`` carries, so the skill survives every compaction."""
    return (
        f"Upkeep: before you write a field, read {skill} -- what each field means, "
        "when to write it, and how."
    )


def render(
    slug: str, *, page: tuple[Any, Any] | None = None
) -> tuple[str, dict[str, bytes]] | None:
    """The skill text and ``{script name: bytes}`` for *slug*'s page, or ``None``."""
    from kiro_crew import dashboard_agentic
    from kiro_crew.dashboard_templates import catalog

    if page is None:
        page = dashboard_agentic.page_in_force(slug)
    if page is None:
        return None
    record, manifest = page
    names = sorted(dashboard_agentic.agentic_fields(manifest))
    try:
        entry = catalog.load_one(record.template_id)
        shipped = entry.manifest.fields
        helpers_dir = entry.directory / "helpers"
    except Exception:
        logger.debug("no shipped template for %r", record.template_id, exc_info=True)
        shipped, helpers_dir = {}, None

    head = [
        "---",
        f"name: {UPKEEP_DIR_NAME}/{slug}",
        f"description: Keep {slug}'s Dashboard fields current "
        f"(template {manifest.id} v{manifest.version})",
        "source: generated",
        "---",
        f"# Dashboard upkeep: {manifest.id} v{manifest.version}",
        "",
    ]
    if not names:
        body = [
            "Every field on this page fills itself from the crew log. Nothing here is "
            "yours to write; do not republish it.",
        ]
        return "\n".join(head + body) + "\n", {}

    body = ["Fold fields fill themselves from the crew log. These are yours:"]
    scripts: dict[str, bytes] = {}
    for name in names:
        spec = shipped.get(name)
        upkeep = getattr(spec, "upkeep", None) if spec is not None and spec.agentic else None
        body.append("")
        body.append(f"## {name}")
        if upkeep is None:
            # Every renderable page is a built-in, and every built-in agentic field
            # declares upkeep (pinned by a test), so this section only names the field.
            continue
        body.append(f"- Means: {upkeep.means}.")
        body.append(f"- When: {upkeep.when}.")
        how = f"- How: {upkeep.how}."
        if upkeep.helper and helpers_dir is not None:
            try:
                scripts[upkeep.helper] = (helpers_dir / upkeep.helper).read_bytes()
            except OSError:
                logger.debug("helper %s unreadable", upkeep.helper, exc_info=True)
            else:
                how += (
                    f" Helper: `scripts/{upkeep.helper}`, beside this file; run it with "
                    "python3 and `--help` for its arguments. It prints the dashboard_write "
                    "payload."
                )
        body.append(how)
    return "\n".join(head + body) + "\n", scripts


def sync(slug: str, *, page: tuple[Any, Any] | None = None) -> Path | None:
    """Make *slug*'s skill on disk match its page; return ``SKILL.md`` or ``None``. TOTAL."""
    try:
        rendered = render(slug, page=page)
    except Exception:
        logger.warning("dashboard upkeep skill for %r not rendered", slug, exc_info=True)
        return None
    return _write(slug, rendered) if rendered is not None else None


def view(slug: str) -> dict[str, Any] | None:
    """What the profile shows: the pointer, the skill text and the helper names. TOTAL."""
    try:
        rendered = render(slug)
    except Exception:
        logger.warning("dashboard upkeep skill for %r not rendered", slug, exc_info=True)
        return None
    if rendered is None:
        return None
    text, scripts = rendered
    # A read writes nothing: the prompt path keeps the files current, so the pointer
    # names the path that path writes.
    return {
        "pointer": pointer(skill_dir(slug) / "SKILL.md"),
        "skill": text,
        "scripts": sorted(scripts),
    }


def _write(slug: str, rendered: tuple[str, dict[str, bytes]]) -> Path | None:
    """Write only what differs from disk, and drop a script the page does not name.

    The tree sits under the data home, which a sandboxed crewmate can write, and this
    runs in the gateway. So every directory from the data home down is opened through
    :func:`pinned_fs.open_pinned_descendant_dir`, which refuses a link at each step, and
    every read, write, listing and unlink is relative to those descriptors and refuses
    a link at the leaf. A planted link costs the pointer line, never a file elsewhere.
    The removal is what keeps a page swap from leaving the old board's helper behind.
    """
    text, scripts = rendered
    try:
        from kiro_crew import pinned_fs
        from kiro_crew.config.loader import config_dir
        from kiro_crew.members import validate_slug
        from kiro_crew.owner_only_files import mkdirs_owner_only
        from kiro_crew.skills import SKILLS_DIR_NAME

        parts = (SKILLS_DIR_NAME, UPKEEP_DIR_NAME, validate_slug(slug))
        # The data home itself is the trusted root; a fresh one may not exist yet.
        mkdirs_owner_only(config_dir())
        with (
            _lock,
            pinned_fs.open_pinned_descendant_dir(
                config_dir(), parts, what="dashboard upkeep skill", create=True
            ) as base_fd,
            pinned_fs.open_pinned_descendant_dir(
                config_dir(), (*parts, "scripts"), what="dashboard upkeep scripts", create=True
            ) as scripts_fd,
        ):
            base = skill_dir(slug)
            _put(base_fd, base, "SKILL.md", text.encode("utf-8"))
            for name, data in scripts.items():
                _put(scripts_fd, base / "scripts", name, data)
            for name in _names(scripts_fd, base / "scripts"):
                if name not in scripts and _is_regular(scripts_fd, base / "scripts", name):
                    if scripts_fd is None:
                        (base / "scripts" / name).unlink()
                    else:
                        os.unlink(name, dir_fd=scripts_fd)
        return base / "SKILL.md"
    except Exception:
        logger.warning("dashboard upkeep skill for %r not written", slug, exc_info=True)
        return None


def _put(dir_fd: int | None, where: Path, name: str, data: bytes) -> None:
    """Replace *name* with *data* unless it already holds exactly that. Refuses a link."""
    from kiro_crew import pinned_fs
    from kiro_crew.atomic_write import atomic_write, atomic_write_at

    if dir_fd is None:
        # No descriptor-relative IO on this platform: the chain was lstat-checked, and
        # the leaf is checked here, which refuses a planted link.
        st = pinned_fs.lstat_by_name(where / name)
        if st is not None and not stat.S_ISREG(st.st_mode):
            raise OSError(f"{where / name} is not a regular file")
        if st is None or (where / name).read_bytes() != data:
            atomic_write(where / name, data)
        return
    st = pinned_fs.stat_at(dir_fd, name)
    if st is not None:
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"{where / name} is not a regular file")
        fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)
        with os.fdopen(fd, "rb") as fh:
            if fh.read(len(data) + 1) == data:
                return
    atomic_write_at(dir_fd, name, data.decode("utf-8"))


def _names(dir_fd: int | None, where: Path) -> list[str]:
    return os.listdir(dir_fd) if dir_fd is not None else os.listdir(where)


def _is_regular(dir_fd: int | None, where: Path, name: str) -> bool:
    from kiro_crew import pinned_fs

    if dir_fd is not None:
        return pinned_fs.is_regular_at(dir_fd, name)
    st = pinned_fs.lstat_by_name(where / name)
    return st is not None and stat.S_ISREG(st.st_mode)
