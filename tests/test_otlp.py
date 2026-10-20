import base64
import json

from ambientq.ingest.otlp import any_value, attrs_to_dict, decode_body, decode_export_request


def test_any_value_types():
    assert any_value({"stringValue": "x"}) == "x"
    assert any_value({"intValue": "42"}) == 42
    assert any_value({"doubleValue": 1.5}) == 1.5
    assert any_value({"boolValue": True}) is True
    assert any_value({"arrayValue": {"values": [{"intValue": "1"}, {"stringValue": "a"}]}}) == [1, "a"]
    assert any_value({"kvlistValue": {"values": [{"key": "k", "value": {"stringValue": "v"}}]}}) == {"k": "v"}
    assert any_value(None) is None


def test_attrs_flat_dict_passthrough():
    assert attrs_to_dict({"a": 1}) == {"a": 1}


def _req(**span):
    base = {"traceId": "0af7651916cd43dd8448eb211c80319c", "spanId": "b7ad6b7169203331", "name": "chat",
            "startTimeUnixNano": "100", "endTimeUnixNano": "200"}
    base.update(span)
    return {"resourceSpans": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "svc"}}]},
                               "scopeSpans": [{"spans": [base]}]}]}


def test_decode_export_request_fields():
    spans = decode_export_request(_req(kind=3, status={"code": 2, "message": "boom"},
                                       attributes=[{"key": "a", "value": {"intValue": "3"}}],
                                       events=[{"name": "e", "timeUnixNano": "150", "attributes": []}]))
    s = spans[0]
    assert s.trace_id == "0af7651916cd43dd8448eb211c80319c" and s.span_id == "b7ad6b7169203331"
    assert s.kind == "CLIENT" and s.status_code == "ERROR" and s.status_message == "boom" and s.is_error
    assert s.attributes == {"a": 3} and s.resource == {"service.name": "svc"}
    assert s.start_ns == 100 and s.end_ns == 200 and s.events[0]["time_ns"] == 150


def test_string_enums_and_base64_ids():
    tid = base64.b64encode(bytes.fromhex("0af7651916cd43dd8448eb211c80319c")).decode()
    spans = decode_export_request(_req(traceId=tid, kind="SPAN_KIND_SERVER", status={"code": "STATUS_CODE_OK"}))
    assert spans[0].trace_id == "0af7651916cd43dd8448eb211c80319c"
    assert spans[0].kind == "SERVER" and spans[0].status_code == "OK"


def test_decode_body_json():
    body = json.dumps(_req()).encode()
    assert len(decode_body(body, "application/json; charset=utf-8")) == 1
