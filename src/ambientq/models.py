"""Core data types shared by every stage of the pipeline."""

from __future__ import annotations

import enum
import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


def new_id(prefix: str) -> str:
    """Short, sortable-enough unique id with a readable prefix."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now_s() -> float:
    return time.time()


class InsightStatus(enum.StrEnum):
    NEW = "NEW"
    RECURRING = "RECURRING"
    RESOLVED = "RESOLVED"


class ProposalStatus(enum.StrEnum):
    DRAFT = "DRAFT"
    REPLAYED = "REPLAYED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    NOT_IMPROVED = "NOT_IMPROVED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


CATEGORIES = (
    "procedure",
    "output",
    "tool_selection",
    "tool_arguments",
    "tool_results",
    "completion",
    "grounding",
    "answer_quality",
    "configuration",
    "safety",
)

SEVERITIES = ("high", "medium", "low")


@dataclass
class Span:
    """One trace span, already decoded from OTLP or another source."""

    span_id: str
    trace_id: str
    name: str
    start_ns: int
    end_ns: int
    parent_span_id: str | None = None
    kind: str = ""
    status_code: str = "UNSET"
    status_message: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    resource: dict[str, Any] = field(default_factory=dict)

    @property
    def is_error(self) -> bool:
        return self.status_code == "ERROR"


@dataclass
class Turn:
    """One normalized step of a conversation."""

    index: int
    role: str  # user | assistant | tool | system
    content: str = ""
    tool_name: str | None = None
    tool_call_id: str | None = None
    tool_args: dict[str, Any] | None = None
    is_error: bool = False
    span_id: str | None = None
    ts_ns: int = 0
    model: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, "", False) or k in ("index", "role")}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Turn:
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__ if k in d})  # type: ignore[arg-type]


@dataclass
class Conversation:
    conversation_id: str
    agent_name: str = ""
    harness_version: str = ""
    turns: list[Turn] = field(default_factory=list)
    started_ns: int = 0
    ended_ns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    has_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def tool_calls(self) -> list[Turn]:
        return [t for t in self.turns if t.role == "assistant" and t.tool_name]

    @property
    def tool_results(self) -> list[Turn]:
        return [t for t in self.turns if t.role == "tool"]

    def transcript(self) -> list[dict[str, Any]]:
        return [t.to_dict() for t in self.turns]


@dataclass
class Evidence:
    turn: int
    quote: str = ""


@dataclass
class Finding:
    conversation_id: str
    source: str  # "judge" or "check:<name>"
    category: str
    expected: str
    actual: str
    rubric_id: str = ""
    severity: str = "medium"
    confidence: float = 1.0
    evidence: list[Evidence] = field(default_factory=list)
    agent_name: str = ""
    tool_name: str = ""
    verified: bool | None = None
    verifier_note: str = ""
    fingerprint: str = ""
    insight_id: str | None = None
    finding_id: str = field(default_factory=lambda: new_id("fnd"))
    run_id: str = ""

    def evidence_json(self) -> str:
        return json.dumps([asdict(e) for e in self.evidence])


@dataclass
class Insight:
    insight_id: str
    fingerprint: str
    title: str
    category: str
    rubric_id: str
    agent_name: str
    status: InsightStatus
    severity: str
    first_seen_run: str
    last_seen_run: str
    occurrence_count: int = 0
    conversation_count: int = 0
    quiet_runs: int = 0
    tool_name: str = ""
    expected: str = ""
    harness_version_first: str = ""
    harness_version_last: str = ""
    created_at: float = field(default_factory=now_s)
    updated_at: float = field(default_factory=now_s)


@dataclass
class Location:
    file: str
    start_line: int
    end_line: int

    def __str__(self) -> str:
        return f"{self.file}:{self.start_line}-{self.end_line}"


@dataclass
class Patch:
    insight_id: str
    location: Location
    original: str
    replacement: str
    diff: str
    rationale: str


@dataclass
class Run:
    run_id: str
    started_at: float
    run_trigger: str = "manual"
    harness_version: str = ""
    finished_at: float | None = None
    candidates: int = 0
    sampled: int = 0
    evaluated: int = 0
    skipped_budget: int = 0
    findings: int = 0
    rejected: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    budget_tokens: int = 0
    status: str = "running"
    notes: str = ""


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
