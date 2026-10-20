import json
import re
from datetime import UTC, datetime

import pytest

from ambientq import __version__
from ambientq.siem import (
    cef_ext,
    cef_header,
    cef_line,
    export_audit,
    sd_param,
    severity,
    syslog_line,
    syslog_severity,
)

TS = datetime(2026, 10, 20, 9, 30, 1, 250000, tzinfo=UTC).timestamp()


def row(action="approved", actor="alice@example.com", subject="prp_1", detail=None, at=TS):
    return {"id": "aud_1", "ts": at, "actor": actor, "action": action, "subject": subject,
            "detail": json.dumps(detail if detail is not None else {"note": "ok"})}


def test_severity_mapping():
    assert severity("sign_in_failed") > severity("approved") > severity("sign_in") > severity("run_finished")
    assert severity("run_finished") > severity("llm_call")
    assert severity("rejected") == severity("approved")
    assert syslog_severity("sign_in_failed") == 4
    assert syslog_severity("approved") == 5
    assert syslog_severity("run_started") == 6
    assert syslog_severity("llm_call") == 7


def test_jsonl():
    out = export_audit([row(detail={"note": "line1\nline2", "n": 1}), row(action="llm_call", detail=None)],
                       "jsonl")
    lines = out.splitlines()
    assert len(lines) == 2 and out.endswith("\n")
    ev = json.loads(lines[0])
    assert ev == {"ts": "2026-10-20T09:30:01.250Z", "id": "aud_1", "actor": "alice@example.com",
                  "action": "approved", "subject": "prp_1", "severity": 7,
                  "detail": {"note": "line1\nline2", "n": 1}}
    bad = json.loads(export_audit([{**row(), "detail": "not json"}], "jsonl"))
    assert bad["detail"] == {"raw": "not json"}


def test_cef_escaping():
    assert cef_header("a|b\\c\nd") == "a\\|b\\\\c d"
    assert cef_ext("a=b\\c|d\r\ne\nf\rg") == "a\\=b\\\\c|d\\ne\\nf\\rg"


def test_cef_line():
    line = cef_line(row(actor="ev|l=x\\y", detail={"note": "ship it\nnow = yes"}))
    assert "\n" not in line
    *head, ext = line.split("|", 7)
    assert head == ["CEF:0", "ambientq", "ambientq", __version__, "approved", "Proposal approved", "7"]
    assert "rt=1792488601250 " in ext
    assert "suser=ev|l\\=x\\\\y " in ext  # pipe unescaped in extensions, = and \\ escaped
    assert "act=approved " in ext and "externalId=aud_1 " in ext
    assert "cs2Label=subject cs2=prp_1 " in ext
    assert ext.endswith("msg=ship it\\nnow \\= yes")
    # every unescaped '=' in the extension starts a key
    keys = re.findall(r"(?:^| )(\w+)=", re.sub(r"\\.", "", ext))
    assert keys == ["rt", "suser", "act", "externalId", "cs1Label", "cs1", "cs2Label", "cs2", "msg"]


def test_cef_header_escapes_action():
    line = cef_line(row(action="weird|act\\ion"))
    assert line.startswith(f"CEF:0|ambientq|ambientq|{__version__}|weird\\|act\\\\ion|")


def test_syslog_line():
    line = syslog_line(row(actor='ev"il]\\x', subject="prp_1", detail={"note": "a\nb"}), hostname="aq-host")
    pri = 13 * 8 + 5
    assert line.startswith(f"<{pri}>1 2026-10-20T09:30:01.250Z aq-host ambientq - approved [ambientq@32473 ")
    assert 'actor="ev\\"il\\]\\\\x"' in line and 'subject="prp_1"' in line and 'id="aud_1"' in line
    assert line.endswith('] {"note": "a\\nb"}')
    assert "\n" not in line


def test_syslog_tokens_and_sd_escaping():
    assert sd_param('a"b]c\\d\ne') == 'a\\"b\\]c\\\\d e'
    line = syslog_line(row(action="has space and ünïcode " + "x" * 40), hostname="my host")
    parts = line.split(" ")
    assert parts[2] == "my_host"
    assert len(parts[5]) == 32 and all(33 <= ord(c) <= 126 for c in parts[5])
    assert syslog_line(row(), hostname="").split(" ")[2] == "-"


def test_export_order_and_format_check():
    rows = [row(at=TS), row(action="llm_call", at=TS + 1)]
    out = export_audit(rows, "syslog", hostname="h").splitlines()
    assert "approved" in out[0] and "llm_call" in out[1]
    with pytest.raises(ValueError):
        export_audit(rows, "xml")
