"""Load an agent harness (prompts, tools, guardrails, config) with line maps."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ambientq.models import Location

MANIFEST = "harness.yaml"


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    location: Location | None = None

    def openai(self) -> dict[str, Any]:
        return {"type": "function",
                "function": {"name": self.name, "description": self.description, "parameters": self.parameters}}


@dataclass
class Guardrail:
    gid: str
    text: str
    pattern: str | None = None
    location: Location | None = None


@dataclass
class Harness:
    root: Path
    agent: str
    goal: str = ""
    prompt_files: list[str] = field(default_factory=list)
    tools_file: str | None = None
    guardrails_file: str | None = None
    config_file: str | None = None
    files: dict[str, list[str]] = field(default_factory=dict)
    tools: list[ToolSpec] = field(default_factory=list)
    guardrails: list[Guardrail] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)
    manifest: dict[str, Any] = field(default_factory=dict)

    # -- loading ----------------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path) -> Harness:
        root = Path(path).resolve()
        mpath = root / MANIFEST
        if not mpath.exists():
            raise FileNotFoundError(f"no {MANIFEST} in {root}")
        man = yaml.safe_load(mpath.read_text()) or {}
        prompts = man.get("system_prompt") or []
        if isinstance(prompts, str):
            prompts = [prompts]
        h = cls(root=root, agent=str(man.get("agent", root.name)), goal=str(man.get("goal") or ""),
                prompt_files=list(prompts), tools_file=man.get("tools"),
                guardrails_file=man.get("guardrails"), config_file=man.get("config"), manifest=man)
        for rel in [MANIFEST, *h.prompt_files, h.tools_file, h.guardrails_file, h.config_file]:
            if rel:
                h.files[rel] = (root / rel).read_text(encoding="utf-8").splitlines()
        if h.tools_file:
            h.tools = _load_tools(root / h.tools_file, h.tools_file)
        if h.guardrails_file:
            h.guardrails = _load_guardrails(root / h.guardrails_file, h.guardrails_file)
        if h.config_file:
            h.config = yaml.safe_load((root / h.config_file).read_text()) or {}
        return h

    # -- views ------------------------------------------------------------------
    def system_prompt(self) -> str:
        return "\n\n".join("\n".join(self.files[p]) for p in self.prompt_files)

    def tool(self, name: str) -> ToolSpec | None:
        return next((t for t in self.tools if t.name == name), None)

    def tool_names(self) -> set[str]:
        return {t.name for t in self.tools}

    def lines(self, file: str, start: int, end: int) -> str:
        return "\n".join(self.files[file][start - 1:end])

    def listing(self, max_lines: int = 400) -> str:
        """Numbered listing of every harness file, for LLM localization."""
        out = []
        for rel, lines in self.files.items():
            if rel == MANIFEST:
                continue
            out.append(f"=== {rel} ===")
            out.extend(f"{i:4d} | {ln}" for i, ln in enumerate(lines[:max_lines], 1))
        return "\n".join(out)

    def version(self) -> str:
        sha = _git_head(self.root)
        if sha:
            return f"git:{sha[:12]}"
        h = hashlib.sha256()
        for rel in sorted(self.files):
            h.update(rel.encode())
            h.update("\n".join(self.files[rel]).encode())
        return f"sha256:{h.hexdigest()[:12]}"

    def valid_location(self, loc: Location) -> bool:
        lines = self.files.get(loc.file)
        return bool(lines) and 1 <= loc.start_line <= loc.end_line <= len(lines or [])

    # -- patching ---------------------------------------------------------------
    def with_replacement(self, loc: Location, replacement: str) -> Harness:
        """Return a harness loaded from a temp copy with ``loc`` replaced."""
        tmp = Path(tempfile.mkdtemp(prefix="ambientq-harness-"))
        for rel in self.files:
            dst = tmp / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.root / rel, dst)
        lines = list(self.files[loc.file])
        new = replacement.splitlines()
        lines[loc.start_line - 1:loc.end_line] = new
        (tmp / loc.file).write_text("\n".join(lines) + "\n", encoding="utf-8")
        return Harness.load(tmp)


def _git_head(root: Path) -> str | None:
    try:
        top = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True,
                             text=True, timeout=5)
        if top.returncode != 0:
            return None
        dirty = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--", "."],
                               capture_output=True, text=True, timeout=5)
        return None if dirty.stdout.strip() else top.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _node_loc(node: yaml.Node, rel: str) -> Location:
    start = node.start_mark.line + 1
    end = node.end_mark.line + (0 if node.end_mark.column == 0 else 1)
    return Location(rel, start, max(start, end))


def _load_tools(path: Path, rel: str) -> list[ToolSpec]:
    text = path.read_text(encoding="utf-8")
    root = yaml.compose(text)
    data = yaml.safe_load(text) or []
    if isinstance(data, dict):
        data = data.get("tools", [])
        root = next((v for k, v in root.value if k.value == "tools"), None) if isinstance(root, yaml.MappingNode) else None
    nodes = list(root.value) if isinstance(root, yaml.SequenceNode) else [None] * len(data)
    out = []
    for item, node in zip(data, nodes, strict=False):
        fn = item.get("function", item)
        out.append(ToolSpec(name=fn["name"], description=str(fn.get("description", "")).strip(),
                            parameters=fn.get("parameters") or {"type": "object", "properties": {}},
                            location=_node_loc(node, rel) if node is not None else None))
    return out


def _load_guardrails(path: Path, rel: str) -> list[Guardrail]:
    text = path.read_text(encoding="utf-8")
    root = yaml.compose(text)
    data = yaml.safe_load(text) or {}
    out: list[Guardrail] = []
    if not isinstance(root, yaml.MappingNode):
        return out
    nodes = {k.value: v for k, v in root.value}
    n = 0
    for key in ("rules", "forbidden_patterns"):
        seq = nodes.get(key)
        items = data.get(key) or []
        seq_nodes = list(seq.value) if isinstance(seq, yaml.SequenceNode) else [None] * len(items)
        for item, node in zip(items, seq_nodes, strict=False):
            n += 1
            if isinstance(item, dict):
                g = Guardrail(gid=f"R-G{n}", text=str(item.get("description") or item.get("rule") or item.get("pattern")),
                              pattern=item.get("pattern"))
            else:
                g = Guardrail(gid=f"R-G{n}", text=str(item))
            g.location = _node_loc(node, rel) if node is not None else None
            out.append(g)
    return out
