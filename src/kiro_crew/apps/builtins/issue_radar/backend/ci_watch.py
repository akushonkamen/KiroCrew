"""Issue Radar — CI-failure alerts for the signed-in user's own pull requests.

One pass of the in-process watcher (``watch._poll_once``), run only for the
connected GitHub repos whose per-repo setting ``notify_on_own_pr_ci_failure`` is
on. With no repo opted in it returns before touching ``gh``, so the watcher's
calls and notifications are those of the new-issue pass and the crew sweep alone.

Each cycle:

  1. one GraphQL search (per few repos) lists the viewer's open PRs in the
     opted-in repos, with each head commit (``search_viewer_open_pull_heads``);
  2. for each head commit, the Actions runs on it are read with the same client
     the PR pane's run actions use (``list_pr_workflow_runs``);
  3. a run that concluded ``failure`` or ``timed_out`` at an attempt not yet
     reported produces one dashboard notification per PR, on this app's
     ``ci-failure`` channel, so the channel's mute and priority settings in
     Settings → Notifications apply to it.

Reported attempts are kept per repo in ``ci-watch-state.json`` (see
``store.read_ci_watch_state``). The first successful read of a repo seeds that
record without notifying, so turning the setting on does not announce the red
runs already present. A head commit whose runs had all completed at its last read
is re-read every :data:`SETTLED_RECHECK_SEC` rather than every cycle; that keeps
a user with many quiet open PRs from spending a request per PR per minute, and
still catches a re-run within a few minutes.

Read-only and GitHub-only: it uses the user's existing ``gh`` session and never
writes to the provider. Any ``gh`` error skips the affected repo for the cycle
and leaves its record untouched, so nothing is lost or reported twice.
"""

from __future__ import annotations

import asyncio
import logging
import time
from functools import partial
from typing import Any

from aiohttp import web

from kiro_crew.apps.builtins.issue_radar.backend import github_client, provider, store
from kiro_crew.apps.manager import get_app_manifest
from kiro_crew.notifications.bus import NotificationPayload, NotificationValidationError

logger = logging.getLogger("kirocrew.app.issue-radar")

APP_NAME = "issue-radar"

#: Channel id, mirroring ``app.json``'s ``notifications.channels``. The bus
#: namespaces it as ``issue-radar.ci-failure``.
CHANNEL_CI_FAILURE = "ci-failure"

#: The dashboard page the notification opens. Path-only: the bus refuses any
#: external link, so the run and PR URLs travel in the note's metadata.
_APP_URL = "/issue-radar"

#: Run conclusions that count as a failure.
FAILED_CONCLUSIONS = frozenset({"failure", "timed_out"})

#: Seconds between re-reads of a head commit whose runs had all completed.
SETTLED_RECHECK_SEC = 300

#: Failed workflow names listed in one notification body before "+N more".
_NOTIFY_LIST_CAP = 3

_MAX_TITLE_CHARS = 200
_MAX_BODY_CHARS = 1000

# ``owner/repo@sha`` (lower-cased) -> monotonic time of the read that found every
# run on that commit completed. In memory only: a gateway restart re-reads each
# head once, which costs one request and cannot repeat a notification.
_settled: dict[str, float] = {}


def opted_in(settings: dict[str, Any], key: provider.RepoKey) -> bool:
    """Whether the CI-failure pass covers this repo."""
    return key.provider == provider.GITHUB and bool(settings.get("notify_on_own_pr_ci_failure"))


async def poll_ci_failures(app: web.Application, keys: list[provider.RepoKey]) -> None:
    """Run one CI-failure pass over the opted-in repos in ``keys``."""
    keys = [k for k in keys if k.provider == provider.GITHUB]
    if not keys:
        return
    try:
        found = await asyncio.to_thread(
            github_client.search_viewer_open_pull_heads, [(k.owner, k.repo) for k in keys]
        )
    except Exception:
        logger.warning("issue-radar CI watch: own-PR search failed", exc_info=True)
        return
    by_repo: dict[str, list[dict[str, Any]]] = {}
    for pull in found.get("pulls") or []:
        by_repo.setdefault(f"{pull['owner']}/{pull['repo']}".lower(), []).append(pull)
    live: set[str] = set()
    for key in keys:
        pulls = by_repo.get(f"{key.owner}/{key.repo}".lower(), [])
        live.update(_head_tag(key, p["head_sha"]) for p in pulls)
        try:
            await _check_repo(app, key, pulls)
        except Exception:
            logger.warning(
                "issue-radar CI watch failed for %s/%s", key.owner, key.repo, exc_info=True
            )
    for tag in [t for t in _settled if t not in live]:
        _settled.pop(tag, None)


def _head_tag(key: provider.RepoKey, sha: str) -> str:
    return f"{key.owner}/{key.repo}@{sha}".lower()


async def _check_repo(
    app: web.Application, key: provider.RepoKey, pulls: list[dict[str, Any]]
) -> None:
    """Read the runs on each own PR's head in one repo and report new failures."""
    owner, repo = key.owner, key.repo
    scope = store.provider_root(root=None, provider=key.provider, host=key.host)
    record = await asyncio.to_thread(store.read_ci_watch_state, owner, repo, scope)
    notified: dict[str, int] = dict(record["notified"])
    failures: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    changed = False
    for pull in pulls:
        sha = pull["head_sha"]
        tag = _head_tag(key, sha)
        last = _settled.get(tag)
        if last is not None and time.monotonic() - last < SETTLED_RECHECK_SEC:
            continue
        runs = await asyncio.to_thread(
            partial(github_client.list_pr_workflow_runs, owner, repo, sha)
        )
        new_failed: list[dict[str, Any]] = []
        for run in runs:
            if run.get("status") != "completed":
                continue
            if run.get("conclusion") not in FAILED_CONCLUSIONS:
                continue
            attempt = _attempt(run)
            run_id = str(run.get("id"))
            if notified.get(run_id, 0) >= attempt:
                continue
            notified.pop(run_id, None)
            notified[run_id] = attempt
            changed = True
            new_failed.append(run)
        if runs and all(r.get("status") == "completed" for r in runs):
            _settled[tag] = time.monotonic()
        else:
            _settled.pop(tag, None)
        if new_failed:
            failures.append((pull, new_failed))

    if not record["seeded"]:
        await asyncio.to_thread(
            partial(store.write_ci_watch_state, owner, repo, notified, root=scope)
        )
        return
    if not changed:
        return
    # Record BEFORE notifying so a delivery hiccup cannot re-announce next cycle.
    await asyncio.to_thread(partial(store.write_ci_watch_state, owner, repo, notified, root=scope))
    state = app.get("state")
    for pull, runs in failures:
        push_ci_failure(state, key, pull, runs)


def _attempt(run: dict[str, Any]) -> int:
    value = run.get("attempt")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return 1
    return value


def declared_channels() -> dict[str, str]:
    """``{channel id: default priority}`` from the INSTALLED manifest, or ``{}``."""
    try:
        manifest = get_app_manifest(APP_NAME)
    except Exception:  # noqa: BLE001 — a missing manifest only means no push
        return {}
    if manifest is None:
        return {}
    return {ch.id: ch.defaultPriority for ch in manifest.notifications.channels}


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def push_ci_failure(
    state: Any, key: provider.RepoKey, pull: dict[str, Any], runs: list[dict[str, Any]]
) -> bool:
    """Push one CI-failure note for a PR. Returns whether it was delivered.

    Goes through the bus on the manifest-declared ``ci-failure`` channel,
    registering it on first use with the manifest's default priority (a later
    registration would overwrite a runtime priority), and draws from the same
    per-app rate limiter as an HTTP push. Never raises.
    """
    bus = getattr(state, "notification_bus", None) if state is not None else None
    if bus is None or not runs:
        return False
    try:
        declared = declared_channels()
        if CHANNEL_CI_FAILURE not in declared:
            logger.warning("issue-radar: the %r channel is not declared", CHANNEL_CI_FAILURE)
            return False
        channel = f"{APP_NAME}.{CHANNEL_CI_FAILURE}"
        if not bus.is_registered(channel):
            bus.register_channel(channel, declared[CHANNEL_CI_FAILURE])
        owner, repo, number = key.owner, key.repo, int(pull["number"])
        names = [str(r.get("name") or "workflow") for r in runs]
        listed = ", ".join(names[:_NOTIFY_LIST_CAP])
        if len(names) > _NOTIFY_LIST_CAP:
            listed += f" +{len(names) - _NOTIFY_LIST_CAP} more"
        pr_title = str(pull.get("title") or "").strip() or "(no title)"
        payload = NotificationPayload(
            source=f"app:{APP_NAME}",
            channel=channel,
            title=_clip(f"CI failed · {owner}/{repo}#{number}", _MAX_TITLE_CHARS),
            body=_clip(f"{listed} failed on {pr_title}", _MAX_BODY_CHARS),
            group_key=f"ci-failure:{owner}/{repo}#{number}".lower(),
            url=_APP_URL,
            meta={
                "app": APP_NAME,
                "owner": owner,
                "repo": repo,
                "number": number,
                "pr_url": str(pull.get("url") or ""),
                "run_url": str(runs[0].get("url") or ""),
                "count": len(runs),
            },
        )
        try:
            payload.validate()
        except NotificationValidationError as exc:
            logger.warning("issue-radar: dropped an invalid CI-failure note: %s", exc)
            return False
        limiter = getattr(state, "notification_rate_limiter", None)
        if limiter is not None and not limiter.allow(APP_NAME):
            logger.info("issue-radar: notification rate limit reached, dropping a CI note")
            return False
        bus.push(payload)
    except Exception as exc:  # noqa: BLE001 — never fatal to a watch cycle
        logger.warning("issue-radar: CI-failure notification failed: %s", exc)
        return False
    logger.info("issue-radar notified CI failure on %s/%s#%s", key.owner, key.repo, pull["number"])
    return True
