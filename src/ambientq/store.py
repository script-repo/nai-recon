"""Local storage: SQLite by default, PostgreSQL optionally (same schema)."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ambientq.conversation import build_conversation
from ambientq.ingest import normalize as nz
from ambientq.models import (
    Conversation,
    Evidence,
    Finding,
    Insight,
    InsightStatus,
    Run,
    Span,
    Turn,
    new_id,
    now_s,
)

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS spans (
        span_id TEXT PRIMARY KEY, trace_id TEXT, parent_span_id TEXT, conversation_id TEXT,
        name TEXT, kind TEXT, start_ns BIGINT, end_ns BIGINT, status_code TEXT, status_message TEXT,
        attributes TEXT, events TEXT, resource TEXT, agent_name TEXT, ingested_at DOUBLE PRECISION)""",
    "CREATE INDEX IF NOT EXISTS ix_spans_conv ON spans(conversation_id)",
    "CREATE INDEX IF NOT EXISTS ix_spans_trace ON spans(trace_id)",
    """CREATE TABLE IF NOT EXISTS conversations (
        conversation_id TEXT PRIMARY KEY, agent_name TEXT, harness_version TEXT, source TEXT,
        started_ns BIGINT, ended_ns BIGINT, turn_count INTEGER, input_tokens INTEGER,
        output_tokens INTEGER, has_error INTEGER, turns TEXT, metadata TEXT,
        last_evaluated_run TEXT, last_evaluated_version TEXT, last_judged_run TEXT, updated_at DOUBLE PRECISION)""",
    """CREATE TABLE IF NOT EXISTS runs (
        run_id TEXT PRIMARY KEY, started_at DOUBLE PRECISION, finished_at DOUBLE PRECISION,
        run_trigger TEXT, harness_version TEXT, candidates INTEGER, sampled INTEGER, evaluated INTEGER,
        skipped_budget INTEGER, findings INTEGER, rejected INTEGER, input_tokens INTEGER,
        output_tokens INTEGER, budget_tokens INTEGER, status TEXT, notes TEXT)""",
    """CREATE TABLE IF NOT EXISTS findings (
        finding_id TEXT PRIMARY KEY, run_id TEXT, conversation_id TEXT, source TEXT, category TEXT,
        rubric_id TEXT, severity TEXT, confidence DOUBLE PRECISION, expected TEXT, actual TEXT,
        evidence TEXT, agent_name TEXT, tool_name TEXT, verified INTEGER, verifier_note TEXT,
        fingerprint TEXT, insight_id TEXT, created_at DOUBLE PRECISION)""",
    "CREATE INDEX IF NOT EXISTS ix_findings_insight ON findings(insight_id)",
    """CREATE TABLE IF NOT EXISTS insights (
        insight_id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE, title TEXT, category TEXT,
        rubric_id TEXT, agent_name TEXT, tool_name TEXT, expected TEXT, status TEXT, severity TEXT,
        first_seen_run TEXT, last_seen_run TEXT, occurrence_count INTEGER, conversation_count INTEGER,
        quiet_runs INTEGER, harness_version_first TEXT, harness_version_last TEXT,
        created_at DOUBLE PRECISION, updated_at DOUBLE PRECISION)""",
    """CREATE TABLE IF NOT EXISTS occurrences (
        insight_id TEXT, run_id TEXT, count INTEGER, conversation_ids TEXT, status_after TEXT,
        created_at DOUBLE PRECISION, PRIMARY KEY (insight_id, run_id))""",
    """CREATE TABLE IF NOT EXISTS proposals (
        proposal_id TEXT PRIMARY KEY, insight_id TEXT, status TEXT, file TEXT, start_line INTEGER,
        end_line INTEGER, diff TEXT, rationale TEXT, replay TEXT, branch TEXT, pr_url TEXT,
        approved_by TEXT, approved_at DOUBLE PRECISION, created_at DOUBLE PRECISION)""",
    """CREATE TABLE IF NOT EXISTS audit (
        id TEXT PRIMARY KEY, ts DOUBLE PRECISION, actor TEXT, action TEXT, subject TEXT, detail TEXT)""",
]


def _j(v: Any) -> str:
    return json.dumps(v, sort_keys=True, default=str)


class SQLStore:
    """One class for both dialects; SQL is written in the common subset."""

    def __init__(self, url: str) -> None:
        self.url = url
        self._lock = threading.RLock()
        if url.startswith("sqlite:///") or url == "sqlite://:memory:" or url == ":memory:":
            path = ":memory:" if url in ("sqlite://:memory:", ":memory:") else url[len("sqlite:///"):]
            if path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.dialect = "sqlite"
            self.conn: Any = sqlite3.connect(path, check_same_thread=False)
            self.conn.execute("PRAGMA journal_mode=WAL") if path != ":memory:" else None
        elif url.startswith(("postgres://", "postgresql://")):
            try:
                import psycopg
            except ImportError as exc:  # pragma: no cover - optional extra
                raise RuntimeError("PostgreSQL needs: pip install 'ambientq[postgres]'") from exc
            self.dialect = "postgres"
            self.conn = psycopg.connect(url, autocommit=False)
        else:
            raise ValueError(f"unsupported store url: {url}")
        self.migrate()

    # -- plumbing ---------------------------------------------------------------
    def _sql(self, sql: str) -> str:
        if self.dialect == "postgres":
            sql = sql.replace("?", "%s").replace("DOUBLE PRECISION", "DOUBLE PRECISION")
        else:
            sql = sql.replace("DOUBLE PRECISION", "REAL").replace("BIGINT", "INTEGER")
        return sql

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        with self._lock:
            cur = self.conn.cursor()
            cur.execute(self._sql(sql), tuple(params))
            return cur

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            cur = self.execute(sql, params)
            cols = [d[0] for d in cur.description] if cur.description else []
            return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]

    def commit(self) -> None:
        with self._lock:
            self.conn.commit()

    def migrate(self) -> None:
        for stmt in SCHEMA:
            self.execute(stmt)
        self.commit()

    def close(self) -> None:
        self.conn.close()

    # -- audit ------------------------------------------------------------------
    def audit(self, actor: str, action: str, subject: str = "", detail: dict[str, Any] | None = None) -> None:
        self.execute("INSERT INTO audit(id, ts, actor, action, subject, detail) VALUES (?,?,?,?,?,?)",
                     (new_id("aud"), now_s(), actor, action, subject, _j(detail or {})))
        self.commit()

    def audit_log(self, limit: int = 200) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM audit ORDER BY ts DESC LIMIT ?", (limit,))

    # -- ingest -----------------------------------------------------------------
    def ingest_spans(self, spans: Iterable[Span]) -> set[str]:
        """Store spans and rebuild every conversation they touch. Returns its ids."""
        spans = list(spans)
        touched: set[str] = set()
        trace_conv: dict[str, str] = {}
        for s in spans:
            k = nz.conversation_key(s)
            if k != s.trace_id:
                trace_conv.setdefault(s.trace_id, k)
        for s in spans:
            cid = nz.conversation_key(s)
            if cid == s.trace_id:
                cid = trace_conv.get(s.trace_id) or self._conv_for_trace(s.trace_id) or cid
            touched.add(cid)
            self.execute(
                """INSERT INTO spans(span_id, trace_id, parent_span_id, conversation_id, name, kind,
                   start_ns, end_ns, status_code, status_message, attributes, events, resource,
                   agent_name, ingested_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(span_id) DO UPDATE SET conversation_id=excluded.conversation_id,
                   attributes=excluded.attributes, events=excluded.events, end_ns=excluded.end_ns,
                   status_code=excluded.status_code, status_message=excluded.status_message""",
                (s.span_id, s.trace_id, s.parent_span_id, cid, s.name, s.kind, s.start_ns, s.end_ns,
                 s.status_code, s.status_message, _j(s.attributes), _j(s.events), _j(s.resource),
                 nz.agent_name(s), now_s()),
            )
        # A late root span with a session id re-homes earlier spans of its trace.
        for trace_id, cid in trace_conv.items():
            old = self.query("SELECT DISTINCT conversation_id FROM spans WHERE trace_id=? AND conversation_id<>?",
                             (trace_id, cid))
            if old:
                self.execute("UPDATE spans SET conversation_id=? WHERE trace_id=?", (cid, trace_id))
                for row in old:
                    self.execute("DELETE FROM conversations WHERE conversation_id=?", (row["conversation_id"],))
                touched.add(cid)
        self.commit()
        for cid in touched:
            conv = build_conversation(cid, self.spans_for(cid))
            self.upsert_conversation(conv, source="otel")
        self.commit()
        return touched

    def _conv_for_trace(self, trace_id: str) -> str | None:
        rows = self.query("SELECT conversation_id FROM spans WHERE trace_id=? AND conversation_id<>? LIMIT 1",
                          (trace_id, trace_id))
        return rows[0]["conversation_id"] if rows else None

    def spans_for(self, conversation_id: str) -> list[Span]:
        rows = self.query("SELECT * FROM spans WHERE conversation_id=? ORDER BY start_ns", (conversation_id,))
        return [
            Span(span_id=r["span_id"], trace_id=r["trace_id"], parent_span_id=r["parent_span_id"],
                 name=r["name"], kind=r["kind"], start_ns=int(r["start_ns"] or 0), end_ns=int(r["end_ns"] or 0),
                 status_code=r["status_code"], status_message=r["status_message"] or "",
                 attributes=json.loads(r["attributes"] or "{}"), events=json.loads(r["events"] or "[]"),
                 resource=json.loads(r["resource"] or "{}"))
            for r in rows
        ]

    def upsert_conversation(self, conv: Conversation, source: str = "record") -> None:
        self.execute(
            """INSERT INTO conversations(conversation_id, agent_name, harness_version, source, started_ns,
               ended_ns, turn_count, input_tokens, output_tokens, has_error, turns, metadata, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(conversation_id) DO UPDATE SET agent_name=excluded.agent_name,
               harness_version=excluded.harness_version, started_ns=excluded.started_ns,
               ended_ns=excluded.ended_ns, turn_count=excluded.turn_count,
               input_tokens=excluded.input_tokens, output_tokens=excluded.output_tokens,
               has_error=excluded.has_error, turns=excluded.turns, metadata=excluded.metadata,
               updated_at=excluded.updated_at""",
            (conv.conversation_id, conv.agent_name, conv.harness_version, source, conv.started_ns,
             conv.ended_ns, len(conv.turns), conv.input_tokens, conv.output_tokens, int(conv.has_error),
             _j([asdict(t) for t in conv.turns]), _j(conv.metadata), now_s()),
        )
        self.commit()

    @staticmethod
    def _row_to_conv(r: dict[str, Any]) -> Conversation:
        return Conversation(
            conversation_id=r["conversation_id"], agent_name=r["agent_name"] or "",
            harness_version=r["harness_version"] or "",
            turns=[Turn.from_dict(t) for t in json.loads(r["turns"] or "[]")],
            started_ns=int(r["started_ns"] or 0), ended_ns=int(r["ended_ns"] or 0),
            input_tokens=int(r["input_tokens"] or 0), output_tokens=int(r["output_tokens"] or 0),
            has_error=bool(r["has_error"]), metadata=json.loads(r["metadata"] or "{}"),
        )

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        rows = self.query("SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,))
        return self._row_to_conv(rows[0]) if rows else None

    def list_conversations(self, *, agent: str | None = None, since_ns: int = 0,
                           not_evaluated_for: str | None = None, limit: int = 100_000) -> list[Conversation]:
        sql = "SELECT * FROM conversations WHERE started_ns >= ?"
        params: list[Any] = [since_ns]
        if agent:
            sql += " AND agent_name = ?"
            params.append(agent)
        if not_evaluated_for is not None:
            sql += " AND (last_evaluated_version IS NULL OR last_evaluated_version <> ?)"
            params.append(not_evaluated_for)
        sql += " ORDER BY started_ns, conversation_id LIMIT ?"
        params.append(limit)
        return [self._row_to_conv(r) for r in self.query(sql, params)]

    def mark_evaluated(self, conversation_ids: Iterable[str], run_id: str, version: str) -> None:
        for cid in conversation_ids:
            self.execute("UPDATE conversations SET last_evaluated_run=?, last_evaluated_version=? "
                         "WHERE conversation_id=?", (run_id, version, cid))
        self.commit()

    def mark_judged(self, conversation_ids: Iterable[str], run_id: str) -> None:
        for cid in conversation_ids:
            self.execute("UPDATE conversations SET last_judged_run=? WHERE conversation_id=?", (run_id, cid))
        self.commit()

    def judged_ids(self) -> set[str]:
        rows = self.query("SELECT conversation_id FROM conversations WHERE last_judged_run IS NOT NULL")
        return {r["conversation_id"] for r in rows}

    def counts(self) -> dict[str, int]:
        out = {}
        for t in ("spans", "conversations", "runs", "findings", "insights", "proposals"):
            out[t] = int(self.query(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"])
        return out

    # -- runs -------------------------------------------------------------------
    def save_run(self, run: Run) -> None:
        d = asdict(run)
        cols = list(d)
        self.execute(
            f"INSERT INTO runs({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
            f"ON CONFLICT(run_id) DO UPDATE SET {','.join(f'{c}=excluded.{c}' for c in cols if c != 'run_id')}",
            [d[c] for c in cols],
        )
        self.commit()

    def list_runs(self, limit: int = 50) -> list[Run]:
        return [Run(**r) for r in self.query("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,))]

    def get_run(self, run_id: str) -> Run | None:
        rows = self.query("SELECT * FROM runs WHERE run_id=?", (run_id,))
        return Run(**rows[0]) if rows else None

    # -- findings ---------------------------------------------------------------
    def save_finding(self, f: Finding) -> None:
        self.execute(
            """INSERT INTO findings(finding_id, run_id, conversation_id, source, category, rubric_id, severity,
               confidence, expected, actual, evidence, agent_name, tool_name, verified, verifier_note,
               fingerprint, insight_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(finding_id) DO UPDATE SET verified=excluded.verified,
               verifier_note=excluded.verifier_note, fingerprint=excluded.fingerprint,
               insight_id=excluded.insight_id""",
            (f.finding_id, f.run_id, f.conversation_id, f.source, f.category, f.rubric_id, f.severity,
             f.confidence, f.expected, f.actual, f.evidence_json(), f.agent_name, f.tool_name,
             None if f.verified is None else int(f.verified), f.verifier_note, f.fingerprint, f.insight_id,
             now_s()),
        )

    @staticmethod
    def _row_to_finding(r: dict[str, Any]) -> Finding:
        return Finding(
            finding_id=r["finding_id"], run_id=r["run_id"], conversation_id=r["conversation_id"],
            source=r["source"], category=r["category"], rubric_id=r["rubric_id"] or "",
            severity=r["severity"], confidence=float(r["confidence"] or 0), expected=r["expected"],
            actual=r["actual"], evidence=[Evidence(**e) for e in json.loads(r["evidence"] or "[]")],
            agent_name=r["agent_name"] or "", tool_name=r["tool_name"] or "",
            verified=None if r["verified"] is None else bool(r["verified"]),
            verifier_note=r["verifier_note"] or "", fingerprint=r["fingerprint"] or "",
            insight_id=r["insight_id"],
        )

    def findings(self, *, run_id: str | None = None, insight_id: str | None = None,
                 conversation_id: str | None = None) -> list[Finding]:
        sql, params = "SELECT * FROM findings WHERE 1=1", []
        for col, val in (("run_id", run_id), ("insight_id", insight_id), ("conversation_id", conversation_id)):
            if val:
                sql += f" AND {col}=?"
                params.append(val)
        return [self._row_to_finding(r) for r in self.query(sql + " ORDER BY created_at", params)]

    # -- insights ---------------------------------------------------------------
    def save_insight(self, ins: Insight) -> None:
        d = asdict(ins)
        d["status"] = str(ins.status)
        cols = list(d)
        self.execute(
            f"INSERT INTO insights({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
            f"ON CONFLICT(insight_id) DO UPDATE SET "
            f"{','.join(f'{c}=excluded.{c}' for c in cols if c not in ('insight_id', 'created_at'))}",
            [d[c] for c in cols],
        )

    @staticmethod
    def _row_to_insight(r: dict[str, Any]) -> Insight:
        r = dict(r)
        r["status"] = InsightStatus(r["status"])
        return Insight(**r)

    def insights(self, status: str | None = None, agent: str | None = None) -> list[Insight]:
        sql, params = "SELECT * FROM insights WHERE 1=1", []
        if status:
            sql += " AND status=?"
            params.append(status.upper())
        if agent:
            sql += " AND agent_name=?"
            params.append(agent)
        sql += " ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, occurrence_count DESC"
        return [self._row_to_insight(r) for r in self.query(sql, params)]

    def get_insight(self, insight_id: str) -> Insight | None:
        rows = self.query("SELECT * FROM insights WHERE insight_id=? OR insight_id LIKE ?",
                          (insight_id, insight_id + "%"))
        return self._row_to_insight(rows[0]) if len(rows) == 1 else None

    def insight_by_fingerprint(self, fp: str) -> Insight | None:
        rows = self.query("SELECT * FROM insights WHERE fingerprint=?", (fp,))
        return self._row_to_insight(rows[0]) if rows else None

    def save_occurrence(self, insight_id: str, run_id: str, count: int, conversation_ids: list[str],
                        status_after: str) -> None:
        self.execute(
            """INSERT INTO occurrences(insight_id, run_id, count, conversation_ids, status_after, created_at)
               VALUES (?,?,?,?,?,?) ON CONFLICT(insight_id, run_id) DO UPDATE SET count=excluded.count,
               conversation_ids=excluded.conversation_ids, status_after=excluded.status_after""",
            (insight_id, run_id, count, _j(sorted(set(conversation_ids))), status_after, now_s()),
        )

    def occurrences(self, insight_id: str) -> list[dict[str, Any]]:
        rows = self.query("SELECT * FROM occurrences WHERE insight_id=? ORDER BY created_at", (insight_id,))
        for r in rows:
            r["conversation_ids"] = json.loads(r["conversation_ids"] or "[]")
        return rows

    # -- proposals --------------------------------------------------------------
    def save_proposal(self, p: dict[str, Any]) -> None:
        p = dict(p)
        p.setdefault("created_at", now_s())
        if isinstance(p.get("replay"), (dict, list)):
            p["replay"] = _j(p["replay"])
        cols = list(p)
        self.execute(
            f"INSERT INTO proposals({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
            f"ON CONFLICT(proposal_id) DO UPDATE SET "
            f"{','.join(f'{c}=excluded.{c}' for c in cols if c not in ('proposal_id', 'created_at'))}",
            [p[c] for c in cols],
        )
        self.commit()

    def proposals(self, insight_id: str | None = None) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM proposals", []
        if insight_id:
            sql += " WHERE insight_id=?"
            params.append(insight_id)
        rows = self.query(sql + " ORDER BY created_at DESC", params)
        for r in rows:
            r["replay"] = json.loads(r["replay"]) if r.get("replay") else None
        return rows

    def get_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        rows = self.query("SELECT * FROM proposals WHERE proposal_id=? OR proposal_id LIKE ?",
                          (proposal_id, proposal_id + "%"))
        if len(rows) != 1:
            return None
        r = rows[0]
        r["replay"] = json.loads(r["replay"]) if r.get("replay") else None
        return r


def open_store(url: str) -> SQLStore:
    return SQLStore(url)
