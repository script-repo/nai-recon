"""Investigate an insight: locate the harness lines behind it and propose a patch."""

from __future__ import annotations

import difflib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ambientq.harness import Harness
from ambientq.insights import jaccard
from ambientq.judge.rubric import Rubric, build_rubric
from ambientq.llm import LLMClient, LLMError
from ambientq.models import Conversation, Finding, Insight, Location, Patch
from ambientq.store import SQLStore

LOCATE_SYSTEM = """\
You find the root cause of a recurring defect in an AI agent's harness (its
prompt files, tool definitions and guardrails). Given a numbered listing of the
harness and the defect, name the smallest line range whose wording caused or
failed to prevent the defect. Answer ONLY JSON:
{"file": "<path as listed>", "start_line": <n>, "end_line": <n>, "reason": "<one sentence>"}
"""

FIX_SYSTEM = """\
You improve an AI agent's harness so that a recurring defect stops happening.
You are given the defect, failing conversation excerpts, and a line range of a
harness file with surrounding context. Rewrite ONLY the target lines. Keep the
file's style and format (Markdown, YAML) valid, keep every other instruction
intact, and make the smallest change that fixes the defect. Prefer explicit,
testable rules. Answer ONLY JSON:
{"replacement": "<new text for the target lines, may span several lines>",
 "rationale": "<why this fixes the defect, 2-4 sentences>"}
"""


@dataclass
class Investigation:
    insight: Insight
    location: Location
    locate_reason: str
    patch: Patch | None
    examples: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "insight_id": self.insight.insight_id, "title": self.insight.title,
            "location": str(self.location), "locate_reason": self.locate_reason,
            "target": self.patch.original if self.patch else None, "diff": self.patch.diff if self.patch else None,
            "rationale": self.patch.rationale if self.patch else None,
        }


def _search_lines(h: Harness, needle: str) -> Location | None:
    for rel in h.prompt_files:
        for i, line in enumerate(h.files[rel], 1):
            if needle and needle in line:
                return Location(rel, i, i)
    return None


def _best_line(h: Harness, text: str) -> Location | None:
    best: tuple[float, Location | None] = (0.0, None)
    for rel in h.prompt_files:
        for i, line in enumerate(h.files[rel], 1):
            s = jaccard(line, text)
            if s > best[0]:
                best = (s, Location(rel, i, i))
    return best[1] if best[0] >= 0.15 else None


def locate(insight: Insight, h: Harness, rubric: Rubric, llm: LLMClient | None,
           findings: list[Finding] | None = None) -> tuple[Location, str]:
    item = rubric.get(insight.rubric_id)
    if item and item.location and h.valid_location(item.location):
        return item.location, f"rubric item {item.rid} is defined here"
    if insight.rubric_id == "CFG-TOOLS" and insight.tool_name:
        loc = _search_lines(h, insight.tool_name)
        if loc:
            return loc, f"the prompt refers to `{insight.tool_name}`, which is not a defined tool"
    if insight.tool_name and h.tool(insight.tool_name):
        spec = h.tool(insight.tool_name)
        if spec and spec.location:
            return spec.location, f"tool `{insight.tool_name}` is defined here"
    if llm is not None:
        try:
            examples = "\n".join(f"- expected: {f.expected} | actual: {f.actual}" for f in (findings or [])[:3])
            data = llm.chat_json([
                {"role": "system", "content": LOCATE_SYSTEM},
                {"role": "user", "content": f"Defect: {insight.title}\nCategory: {insight.category}\n"
                                            f"Examples:\n{examples}\n\n<harness_listing>\n{h.listing()}\n</harness_listing>"},
            ], purpose="locate", max_tokens=300)
            loc = Location(str(data["file"]), int(data["start_line"]), int(data["end_line"]))
            if h.valid_location(loc):
                return loc, str(data.get("reason", "chosen by the investigation model"))
        except (LLMError, KeyError, TypeError, ValueError):
            pass
    loc = _best_line(h, insight.expected or insight.title)
    if loc:
        return loc, "closest matching prompt line (heuristic)"
    rel = h.prompt_files[0]
    n = len(h.files[rel])
    return Location(rel, n, n), "no specific line found; proposing an addition at the end of the system prompt"


def repo_prefix(root: Path) -> str:
    try:
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-prefix"], capture_output=True,
                             text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def make_diff(h: Harness, loc: Location, replacement: str) -> str:
    old = [ln + "\n" for ln in h.files[loc.file]]
    new = list(old)
    new[loc.start_line - 1:loc.end_line] = [ln + "\n" for ln in replacement.splitlines()]
    path = repo_prefix(h.root) + loc.file
    return "".join(difflib.unified_diff(old, new, fromfile=f"a/{path}", tofile=f"b/{path}"))


def _context(h: Harness, loc: Location, pad: int = 10) -> str:
    lines = h.files[loc.file]
    lo, hi = max(1, loc.start_line - pad), min(len(lines), loc.end_line + pad)
    out = []
    for i in range(lo, hi + 1):
        mark = ">>" if loc.start_line <= i <= loc.end_line else "  "
        out.append(f"{mark}{i:4d} | {lines[i - 1]}")
    return "\n".join(out)


def _excerpt(conv: Conversation, f: Finding, pad: int = 2) -> dict[str, Any]:
    idx = {e.turn for e in f.evidence} or {len(conv.turns) - 1}
    keep = {i + d for i in idx for d in range(-pad, pad + 1)}
    return {"conversation_id": conv.conversation_id, "expected": f.expected, "actual": f.actual,
            "turns": [t.to_dict() for t in conv.turns if t.index in keep]}


def _validate(h: Harness, loc: Location, replacement: str) -> None:
    """Reject patches that leave a structured harness file unparseable."""
    if not loc.file.endswith((".yaml", ".yml", ".json")):
        return
    lines = list(h.files[loc.file])
    lines[loc.start_line - 1:loc.end_line] = replacement.splitlines()
    try:
        yaml.safe_load("\n".join(lines))
    except yaml.YAMLError as exc:
        raise LLMError(f"proposed patch makes {loc.file} invalid: {exc}") from exc


def propose_patch(insight: Insight, h: Harness, loc: Location, examples: list[dict[str, Any]],
                  llm: LLMClient) -> Patch:
    target = h.lines(loc.file, loc.start_line, loc.end_line)
    user = "\n\n".join([
        f"Defect ({insight.category}, rule {insight.rubric_id or 'n/a'}): {insight.title}",
        f"Seen {insight.occurrence_count} time(s) in {insight.conversation_count} conversation(s).",
        "<failing_examples_json>\n" + json.dumps(examples[:3], indent=1) + "\n</failing_examples_json>",
        f"Target: {loc.file} lines {loc.start_line}-{loc.end_line} (marked >>):",
        "<file_context>\n" + _context(h, loc) + "\n</file_context>",
        "<target_lines>\n" + target + "\n</target_lines>",
    ])
    data = llm.chat_json([{"role": "system", "content": FIX_SYSTEM}, {"role": "user", "content": user}],
                         purpose="fix", max_tokens=1200)
    replacement = str(data.get("replacement", "")).rstrip("\n") if isinstance(data, dict) else ""
    if not replacement.strip() or replacement == target:
        raise LLMError("fix model returned no change")
    _validate(h, loc, replacement)
    diff = make_diff(h, loc, replacement)
    return Patch(insight_id=insight.insight_id, location=loc, original=target, replacement=replacement,
                 diff=diff, rationale=str(data.get("rationale", "")))


def investigate(store: SQLStore, insight_id: str, h: Harness, llm: LLMClient | None,
                goal: str | None = None) -> Investigation:
    ins = store.get_insight(insight_id)
    if ins is None:
        raise KeyError(f"no insight {insight_id!r} (or prefix is ambiguous)")
    rubric = build_rubric(h, goal)
    findings = [f for f in store.findings(insight_id=ins.insight_id) if f.verified is not False]
    loc, reason = locate(ins, h, rubric, llm, findings)
    examples = []
    for f in findings[:3]:
        conv = store.get_conversation(f.conversation_id)
        if conv:
            examples.append(_excerpt(conv, f))
    patch = propose_patch(ins, h, loc, examples, llm) if llm is not None else None
    store.audit("ambientq", "investigated", ins.insight_id, {"location": str(loc), "patched": bool(patch)})
    return Investigation(insight=ins, location=loc, locate_reason=reason, patch=patch, examples=examples)
