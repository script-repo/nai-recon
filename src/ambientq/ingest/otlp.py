"""Decode OTLP trace export requests (JSON encoding; protobuf when available)."""

from __future__ import annotations

import base64
import json
from typing import Any

from ambientq.models import Span

_KIND = {0: "UNSPECIFIED", 1: "INTERNAL", 2: "SERVER", 3: "CLIENT", 4: "PRODUCER", 5: "CONSUMER"}
_STATUS = {0: "UNSET", 1: "OK", 2: "ERROR"}


def any_value(v: dict[str, Any] | None) -> Any:
    """Convert an OTLP ``AnyValue`` JSON object to a Python value."""
    if not v:
        return None
    if "stringValue" in v:
        return v["stringValue"]
    if "intValue" in v:
        return int(v["intValue"])
    if "doubleValue" in v:
        return float(v["doubleValue"])
    if "boolValue" in v:
        return bool(v["boolValue"])
    if "arrayValue" in v:
        return [any_value(x) for x in (v["arrayValue"] or {}).get("values", [])]
    if "kvlistValue" in v:
        return attrs_to_dict((v["kvlistValue"] or {}).get("values", []))
    if "bytesValue" in v:
        return v["bytesValue"]
    return None


def attrs_to_dict(attrs: list[dict[str, Any]] | dict[str, Any] | None) -> dict[str, Any]:
    if not attrs:
        return {}
    if isinstance(attrs, dict):  # already flat (lenient input)
        return dict(attrs)
    return {a["key"]: any_value(a.get("value")) for a in attrs if "key" in a}


def _hex_id(value: Any) -> str:
    """OTLP/JSON ids are hex; protobuf->dict gives base64. Normalize to hex."""
    if not value:
        return ""
    s = str(value)
    if all(c in "0123456789abcdefABCDEF" for c in s) and len(s) in (16, 32):
        return s.lower()
    try:
        raw = base64.b64decode(s, validate=True)
        if len(raw) in (8, 16):
            return raw.hex()
    except (ValueError, TypeError):
        pass
    return s


def _kind(value: Any) -> str:
    if isinstance(value, int):
        return _KIND.get(value, str(value))
    s = str(value or "")
    return s.replace("SPAN_KIND_", "")


def _status(st: dict[str, Any] | None) -> tuple[str, str]:
    if not st:
        return "UNSET", ""
    code = st.get("code", 0)
    if isinstance(code, int):
        c = _STATUS.get(code, "UNSET")
    else:
        c = str(code).replace("STATUS_CODE_", "")
    return c, st.get("message", "") or ""


def decode_export_request(payload: dict[str, Any]) -> list[Span]:
    """Decode an ``ExportTraceServiceRequest`` in its JSON shape into spans."""
    spans: list[Span] = []
    for rs in payload.get("resourceSpans", payload.get("resource_spans", [])) or []:
        resource = attrs_to_dict((rs.get("resource") or {}).get("attributes"))
        for ss in rs.get("scopeSpans", rs.get("scope_spans", rs.get("instrumentationLibrarySpans", []))) or []:
            for s in ss.get("spans", []) or []:
                code, msg = _status(s.get("status"))
                events = [
                    {
                        "name": e.get("name", ""),
                        "time_ns": int(e.get("timeUnixNano", 0) or 0),
                        "attributes": attrs_to_dict(e.get("attributes")),
                    }
                    for e in s.get("events", []) or []
                ]
                spans.append(
                    Span(
                        span_id=_hex_id(s.get("spanId")),
                        trace_id=_hex_id(s.get("traceId")),
                        parent_span_id=_hex_id(s.get("parentSpanId")) or None,
                        name=s.get("name", ""),
                        kind=_kind(s.get("kind")),
                        start_ns=int(s.get("startTimeUnixNano", 0) or 0),
                        end_ns=int(s.get("endTimeUnixNano", 0) or 0),
                        status_code=code,
                        status_message=msg,
                        attributes=attrs_to_dict(s.get("attributes")),
                        events=events,
                        resource=resource,
                    )
                )
    return spans


def decode_body(body: bytes, content_type: str) -> list[Span]:
    """Decode an OTLP/HTTP request body by content type."""
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in ("application/x-protobuf", "application/protobuf"):
        return decode_protobuf(body)
    return decode_export_request(json.loads(body.decode("utf-8") or "{}"))


def decode_protobuf(body: bytes) -> list[Span]:
    try:
        from google.protobuf.json_format import MessageToDict
        from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError(
            "protobuf OTLP needs the 'otlp-proto' extra: pip install 'ambientq[otlp-proto]', "
            "or configure your exporter for http/json"
        ) from exc
    msg = trace_service_pb2.ExportTraceServiceRequest()
    msg.ParseFromString(body)
    return decode_export_request(MessageToDict(msg))
