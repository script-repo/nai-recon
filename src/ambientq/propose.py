"""The self-improvement loop: patch -> replay -> branch / PR -> human approval.

ambientq never merges, deploys or edits the agent's working tree. The only
write to the agent repo is a new branch built in a temporary git worktree.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from ambientq.config import Config
from ambientq.harness import Harness
from ambientq.investigate import investigate
from ambientq.llm import LLMClient
from ambientq.models import ProposalStatus, new_id, now_s
from ambientq.replay import Replayer, replay
from ambientq.store import SQLStore


def _git(root: Path | str, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {out.stderr.strip()}")
    return out.stdout.strip()


def create_branch(harness_root: Path, branch: str, rel_file: str, new_content: str, message: str) -> str:
    """Commit ``new_content`` to ``rel_file`` on a new branch, without touching the working tree."""
    top = Path(_git(harness_root, "rev-parse", "--show-toplevel"))
    prefix = _git(harness_root, "rev-parse", "--show-prefix")
    tmp = Path(tempfile.mkdtemp(prefix="ambientq-wt-"))
    shutil.rmtree(tmp)
    _git(top, "worktree", "add", "-q", "-b", branch, str(tmp), "HEAD")
    try:
        target = tmp / prefix / rel_file
        target.write_text(new_content, encoding="utf-8")
        _git(tmp, "add", str(Path(prefix) / rel_file))
        _git(tmp, "-c", "user.name=ambientq", "-c", "user.email=ambientq@localhost", "commit", "-q", "-m", message)
        return _git(tmp, "rev-parse", "HEAD")
    finally:
        _git(top, "worktree", "remove", "--force", str(tmp))


def open_pull_request(cfg: Config, branch: str, title: str, body: str) -> str:
    forge = cfg.section("forge")
    kind, api, repo = forge.get("kind"), (forge.get("api_url") or "").rstrip("/"), forge.get("repo")
    token = cfg.secret("forge", "token_env")
    if not (kind and api and repo and token):
        raise RuntimeError("forge.kind, forge.api_url, forge.repo and the token env var are required to open a PR")
    base = forge.get("base_branch", "main")
    if kind in ("github", "gitea"):
        url = f"{api}/repos/{repo}/pulls"
        payload = {"title": title, "body": body, "head": branch, "base": base}
        auth = f"Bearer {token}" if kind == "github" else f"token {token}"
    elif kind == "gitlab":
        url = f"{api}/projects/{urllib.parse.quote(repo, safe='')}/merge_requests"
        payload = {"title": title, "description": body, "source_branch": branch, "target_branch": base}
        auth = f"Bearer {token}"
    else:
        raise RuntimeError(f"unknown forge kind {kind!r}")
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Authorization": auth, "Content-Type": "application/json",
                                          "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
        data = json.loads(resp.read().decode())
    return str(data.get("html_url") or data.get("web_url") or data.get("url") or "")


def pr_body(ins_title: str, rationale: str, diff: str, rep: dict[str, Any] | None) -> str:
    lines = ["Proposed by **ambientq** (on-prem ambient quality agent). **Not applied** - review and merge "
             "through your normal change process.", "", f"**Insight:** {ins_title}", "", "**Rationale:** " + rationale]
    if rep:
        lines += ["", f"**Replay:** fixed {rep['fixed']}, still failing {rep['still_failing']}, "
                      f"regressions {rep['regressions']}.", "", "| conversation | before | after |", "|---|---|---|"]
        for r in rep["rows"]:
            if r["kind"] == "failing":
                lines.append(f"| {r['conversation_id']} | {'fail' if r['before_fail'] else 'pass'} | "
                             f"{'fail' if r['after_fail'] else 'pass'} |")
    lines += ["", "```diff", diff.rstrip(), "```"]
    return "\n".join(lines)


def propose(store: SQLStore, cfg: Config, insight_id: str, harness: Harness, llm: LLMClient, *,
            do_replay: bool | None = None, branch: bool = False, open_pr: bool = False,
            agent_llm: LLMClient | None = None, kind: str | None = None) -> dict[str, Any]:
    inv = investigate(store, insight_id, harness, llm, cfg.get("judge", "goal"), kind=kind)
    if inv.patch is None:
        raise RuntimeError("no patch could be generated")
    patch, ins = inv.patch, inv.insight
    proposal: dict[str, Any] = {
        "proposal_id": new_id("prp"), "insight_id": ins.insight_id, "status": str(ProposalStatus.DRAFT),
        "file": patch.location.file, "start_line": patch.location.start_line, "end_line": patch.location.end_line,
        "diff": patch.diff, "rationale": patch.rationale, "replay": None, "branch": None, "pr_url": None,
        "approved_by": None, "approved_at": None, "created_at": now_s(),
        "kind": patch.kind, "team": ins.team or store.default_team(ins.agent_name), "agent_name": ins.agent_name,
        "shadow": None, "decision_note": None, "original": patch.original, "replacement": patch.replacement,
    }
    patched = harness.with_replacement(patch.location, patch.replacement)
    run_replay = cfg.get("replay", "enabled", True) if do_replay is None else do_replay
    rep_dict = None
    if run_replay:
        findings = [f for f in store.findings(insight_id=ins.insight_id) if f.verified is not False]
        by_conv: dict[str, list] = {}
        for f in findings:
            by_conv.setdefault(f.conversation_id, []).append(f)
        failing = []
        for cid, fs in list(by_conv.items())[: int(cfg.get("replay", "max_conversations", 5))]:
            conv = store.get_conversation(cid)
            if conv:
                failing.append((conv, fs))
        flagged = {f.conversation_id for f in store.findings() if f.verified}
        controls = [c for c in store.list_conversations(agent=harness.agent) if c.conversation_id not in flagged]
        controls = controls[: int(cfg.get("replay", "control_sample", 2))]
        agent = agent_llm or llm.with_model(cfg.get("replay", "model"))
        rep = replay(ins, failing, harness, patched, agent, llm, controls=controls,
                     replayer=Replayer.from_config(cfg, store, harness.agent))
        rep_dict = rep.to_dict()
        proposal["replay"] = rep_dict
        proposal["status"] = str(ProposalStatus.PENDING_APPROVAL if rep.improved else ProposalStatus.NOT_IMPROVED)
    else:
        proposal["status"] = str(ProposalStatus.PENDING_APPROVAL)
    shutil.rmtree(patched.root, ignore_errors=True)

    if (branch or open_pr) and proposal["status"] == ProposalStatus.PENDING_APPROVAL:
        name = f"ambientq/{ins.insight_id}"
        lines = list(harness.files[patch.location.file])
        lines[patch.location.start_line - 1:patch.location.end_line] = patch.replacement.splitlines()
        create_branch(harness.root, name, patch.location.file, "\n".join(lines) + "\n",
                      f"ambientq: {ins.title}\n\n{patch.rationale}")
        proposal["branch"] = name
        if open_pr:
            top = _git(harness.root, "rev-parse", "--show-toplevel")
            _git(top, "push", "-u", "origin", name)
            proposal["pr_url"] = open_pull_request(cfg, name, f"ambientq: {ins.title}"[:120],
                                                   pr_body(ins.title, patch.rationale, patch.diff, rep_dict))
    store.save_proposal(proposal)
    store.audit("ambientq", "proposed", proposal["proposal_id"], {
        "insight": ins.insight_id, "status": proposal["status"], "branch": proposal["branch"],
        "pr_url": proposal["pr_url"], "location": str(patch.location), "kind": patch.kind})
    return proposal


def decide(store: SQLStore, proposal_id: str, by: str, *, approve: bool, note: str = "",
           via: str = "cli") -> dict[str, Any]:
    p = store.get_proposal(proposal_id)
    if p is None:
        raise KeyError(f"no proposal {proposal_id!r}")
    if p["status"] in (ProposalStatus.APPROVED, ProposalStatus.REJECTED):
        raise RuntimeError(f"proposal already {p['status']}")
    if not by.strip():
        raise ValueError("an approver name is required")
    p["status"] = str(ProposalStatus.APPROVED if approve else ProposalStatus.REJECTED)
    p["approved_by"], p["approved_at"] = by, now_s()
    p["decision_note"] = note or None
    store.save_proposal(p)
    store.audit(by, "approved" if approve else "rejected", p["proposal_id"], {"note": note, "insight": p["insight_id"],
                                                                           "via": via, "team": p.get("team")})
    return p
