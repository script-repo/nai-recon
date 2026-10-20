import json

import pytest

from ambientq.calibrate import (
    Counts,
    LabelDefect,
    LabeledConversation,
    LabelError,
    calibrate,
    cohen_kappa,
    conversation_to_record,
    load_labeled,
    parse_labeled,
    recommend_threshold,
    score,
    select_for_labeling,
    sweep,
    turns_equal,
    wilson,
)
from ambientq.cli import main
from ambientq.demo import synthetic
from ambientq.demo.calibration import AMBIGUOUS, SAMPLE_PATH, build_sample, render
from ambientq.harness import Harness
from ambientq.ingest.jsonl import parse_records, record_to_conversation
from ambientq.models import Conversation, Finding, Turn
from ambientq.store import SQLStore
from tests.conftest import DEMO_HARNESS


def conv(cid: str) -> Conversation:
    return Conversation(cid, turns=[Turn(0, "user", "hi"), Turn(1, "assistant", "hello")])


def lab(cid: str, *defects: tuple[str, str], clean: bool | None = None) -> LabeledConversation:
    ds = [LabelDefect(c, r) for c, r in defects]
    return LabeledConversation(conv(cid), ds, clean=not ds if clean is None else clean)


def fnd(cid: str, cat: str, rid: str = "", conf: float = 0.9, verified: bool | None = True) -> Finding:
    return Finding(cid, "judge", cat, "e", "a", rubric_id=rid, confidence=conf, verified=verified)


# --- metrics math -------------------------------------------------------------------


def test_counts_precision_recall_f1():
    c = Counts(tp=6, fp=2, fn=3, tn=9)
    assert c.precision == 0.75 and c.recall == pytest.approx(6 / 9)
    assert c.f1 == pytest.approx(2 * 0.75 * (6 / 9) / (0.75 + 6 / 9))
    assert c.support == 9 and c.predicted == 8 and c.n == 20
    empty = Counts(tn=5)
    assert empty.precision is None and empty.recall is None and empty.f1 is None
    d = c.to_dict()
    assert d["precision_ci95"][0] < 0.75 < d["precision_ci95"][1]


def test_cohen_kappa_known_values():
    # Classic textbook example: 20 yes/yes, 5 yes/no, 10 no/yes, 15 no/no -> kappa 0.4
    assert cohen_kappa(tp=20, fp=10, fn=5, tn=15) == pytest.approx(0.4)
    assert cohen_kappa(10, 0, 0, 10) == 1.0
    assert cohen_kappa(0, 0, 0, 10) == 1.0  # perfect agreement on a single class
    assert cohen_kappa(5, 5, 5, 5) == pytest.approx(0.0)
    assert cohen_kappa(0, 0, 0, 0) == 0.0


def test_wilson_interval():
    lo, hi = wilson(8, 10)
    assert lo == pytest.approx(0.4902, abs=1e-3) and hi == pytest.approx(0.9433, abs=1e-3)
    assert wilson(0, 0) == (0.0, 1.0)
    lo, hi = wilson(10, 10)
    assert hi == 1.0 and 0.69 < lo < 0.73
    assert all(0.0 <= x <= 1.0 for x in wilson(0, 3))


def test_score_categories_rubric_flag_and_disagreements():
    labeled = [
        lab("a", ("procedure", "R-P1")),            # judge right
        lab("b", ("safety", "R-P4")),               # judge misses
        lab("c"),                                   # judge false alarm (procedure R-P3)
        lab("d"),                                   # both clean
        lab("e", ("tool_results", "")),             # category-only label, judge right
        lab("f", ("procedure", "R-P1")),            # judge finding rejected by verifier -> miss
    ]
    preds = {
        "a": [fnd("a", "procedure", "R-P1")],
        "b": [],
        "c": [fnd("c", "procedure", "R-P3")],
        "d": [],
        "e": [fnd("e", "tool_results", "R-P1")],
        "f": [fnd("f", "procedure", "R-P1", verified=False)],
    }
    s = score(labeled, preds)
    assert (s.flag.tp, s.flag.fp, s.flag.fn, s.flag.tn) == (2, 1, 2, 1)
    assert s.agreement == pytest.approx(0.5)
    assert s.kappa == pytest.approx(cohen_kappa(2, 1, 2, 1))
    p = s.categories["procedure"]
    assert (p.tp, p.fp, p.fn, p.tn) == (1, 1, 1, 3)
    assert s.categories["safety"].recall == 0.0 and s.categories["tool_results"].f1 == 1.0
    # rubric level skips "e" (category-only label): its R-P1 finding is neither a TP nor an FP
    r1 = s.rubric["R-P1"]
    assert (r1.tp, r1.fp, r1.fn, r1.tn) == (1, 0, 1, 3)
    assert s.rubric["R-P3"].fp == 1
    assert {d["conversation_id"] for d in s.disagreements} == {"b", "c", "f"}
    miss = next(d for d in s.disagreements if d["conversation_id"] == "f")
    assert miss["missed"] == ["procedure"] and miss["judge"][0]["counted"] is False
    m = s.micro
    assert (m.tp, m.fp, m.fn) == (2, 1, 2)


def test_threshold_sweep_and_recommendation():
    labeled = [lab("a", ("procedure", "")), lab("b"), lab("c", ("safety", "")), lab("d")]
    preds = {
        "a": [fnd("a", "procedure", conf=0.9)],
        "b": [fnd("b", "procedure", conf=0.4)],   # low-confidence false alarm
        "c": [fnd("c", "safety", conf=0.6)],
        "d": [fnd("d", "grounding", conf=0.2)],   # very low-confidence false alarm
    }
    rows = {r["threshold"]: r for r in sweep(labeled, preds, (0.0, 0.3, 0.5, 0.7))}
    assert rows[0.0]["precision"] == 0.5 and rows[0.0]["recall"] == 1.0 and rows[0.0]["findings"] == 4
    assert rows[0.3]["precision"] == pytest.approx(2 / 3, abs=1e-4)
    assert rows[0.5]["precision"] == 1.0 and rows[0.5]["recall"] == 1.0
    assert rows[0.7]["recall"] == 0.5
    assert recommend_threshold(list(rows.values())) == 0.5
    # ties on F1 go to higher precision, then to the lower threshold
    tie = [{"threshold": 0.2, "f1": 0.8, "precision": 0.7}, {"threshold": 0.4, "f1": 0.8, "precision": 0.9},
           {"threshold": 0.6, "f1": 0.8, "precision": 0.9}]
    assert recommend_threshold(tie) == 0.4
    assert recommend_threshold([{"threshold": 0.0, "f1": None, "precision": None}]) is None


# --- labeled format ---------------------------------------------------------------------


def _rec(cid: str = "x1") -> dict:
    return {"conversation_id": cid, "messages": [{"role": "user", "content": "hi"},
                                                  {"role": "assistant", "content": "hello"}]}


def test_parse_labeled_accepts_records_and_otlp():
    records, truth = synthetic.generate(1, 8)
    lines = [json.dumps({**r, "labels": {"clean": True, "labeler": "z"}}) for r in records]
    out = parse_labeled(lines)
    assert not out.errors and len(out.items) == 8
    assert {i.conversation_id for i in out.items} == set(truth)
    assert any("resourceSpans" in r for r in records) and any("messages" in r for r in records)


def test_parse_labeled_reports_bad_lines():
    lines = [
        "{not json",
        json.dumps([1, 2]),
        json.dumps(_rec("a")),                                                      # no labels
        json.dumps({**_rec("b"), "labels": {"defects": [{"category": "vibes"}]}}),
        json.dumps({**_rec("c"), "labels": {"clean": True, "defects": [{"category": "safety"}]}}),
        json.dumps({**_rec("d"), "labels": {"clean": "yes"}}),
        json.dumps({**_rec("e"), "labels": {"defects": [{"category": "safety", "rubric_id": 3}]}}),
        json.dumps({"foo": 1, "labels": {"clean": True}}),                          # unknown shape
        json.dumps({**_rec("ok"), "labels": {"defects": [{"category": "Safety", "rubric_id": "G-SAFETY"}]}}),
        json.dumps({**_rec("ok"), "labels": {"clean": True}}),                      # duplicate
        json.dumps({**_rec("stub"), "labels": {"defects": [], "clean": None, "labeler": ""}}),
        "",
    ]
    out = parse_labeled(lines, "set.jsonl")
    msgs = "\n".join(out.errors)
    assert len(out.errors) == 9, msgs
    for needle in ("set.jsonl:1: not valid JSON", "set.jsonl:2: a line must be a JSON object",
                   "3: no 'labels'", "'vibes' is not one of", "clean' is true but defects",
                   "true, false or null", "rubric_id must be a string", "unrecognized shape",
                   "already labeled on line 9"):
        assert needle in msgs
    assert [i.conversation_id for i in out.items] == ["ok"]
    assert out.items[0].defects[0].category == "safety" and not out.items[0].clean
    assert out.unlabeled == ["stub"]


def test_load_labeled_raises_with_every_bad_line(tmp_path):
    p = tmp_path / "l.jsonl"
    p.write_text(json.dumps({**_rec("a"), "labels": {"clean": True}}) + "\n{bad\n[]\n")
    with pytest.raises(LabelError) as exc:
        load_labeled(p)
    assert "2 bad line(s)" in str(exc.value) and "l.jsonl:2" in str(exc.value) and "l.jsonl:3" in str(exc.value)


def test_flagged_without_category_counts_only_at_conversation_level():
    out = parse_labeled([json.dumps({**_rec("a"), "labels": {"clean": False}})])
    lc = out.items[0]
    assert lc.flagged and not lc.defects and not lc.rubric_resolved()
    s = score([lc], {"a": [fnd("a", "procedure", "R-P1")]})
    assert s.flag.tp == 1 and s.categories["procedure"].fp == 1 and s.rubric["R-P1"].n == 0


# --- sample set ------------------------------------------------------------------------


def test_shipped_sample_matches_generator():
    records, truth = build_sample()
    assert SAMPLE_PATH.read_text(encoding="utf-8") == render(records)
    assert set(truth.values()) == set(synthetic.SCENARIOS)
    assert len(records) == 40
    assert sum("resourceSpans" in r for r in records) and sum("messages" in r for r in records)
    planted = [r for r in records if "planted ambiguity" in r["labels"].get("note", "")]
    assert len(planted) == len(AMBIGUOUS) == 3
    items = load_labeled(SAMPLE_PATH)
    assert len(items) == 40


def test_end_to_end_on_sample_with_mock(llm, mock):
    items = load_labeled(SAMPLE_PATH)
    harness = Harness.load(DEMO_HARNESS)
    rep = calibrate(items, harness, llm, min_confidence=0.5, conversations_per_day=500, sampling_rate=0.1)
    s = rep.scores
    assert rep.judged == 40 and not rep.errors
    assert 0.8 < s.agreement < 1.0            # the planted ambiguous items disagree
    assert s.flag.fp == 1 and s.flag.fn == 2
    assert s.categories["safety"].recall == 1.0 and s.categories["safety"].precision == 1.0
    assert s.categories["tool_results"].f1 == 1.0
    assert s.categories["procedure"].fp == 1  # the "bump it" item a human called clean
    assert s.categories["grounding"].recall == 0.0  # judge reports the root cause (R-P1) once
    assert s.rubric["R-P1"].f1 == 1.0 and s.rubric["R-P4"].recall == 1.0
    ids = {d["conversation_id"] for d in s.disagreements}
    planted = {f"cal-{i:03d}" for i, r in enumerate(build_sample()[0]) if r["labels"].get("note")}
    assert planted <= ids
    # usage and sizing come from the mock's token counts
    u = rep.usage()
    assert u["judge_prompt_tokens"]["mean"] > 500 and u["judge_completion_tokens"]["mean"] > 0
    assert u["verify_calls"]["mean"] > 0 and {"mean", "p50", "p95"} <= set(u["judge_latency_s"])
    assert u["sizing"]["judged_per_day"] == 50.0
    assert abs(u["sizing"]["tokens_per_day"] - 50 * u["tokens_per_conversation"]) < 50
    purposes = {c["purpose"] for c in mock.calls}
    assert purposes == {"judge", "verify"}
    d = rep.to_dict()
    json.dumps(d)
    assert d["conversation"]["confusion"]["human_clean"]["judge_flagged"] == 1
    assert d["recommended_threshold"] == 0.0 and len(d["sweep"]) == 10
    text = rep.to_text()
    assert "Cohen's kappa" in text and "disagreements" in text and "planted ambiguity" in text


def test_without_verifier_threshold_sweep_finds_the_low_confidence_noise(llm):
    items = load_labeled(SAMPLE_PATH)
    rep = calibrate(items, Harness.load(DEMO_HARNESS), llm, verify=False, min_confidence=0.5)
    assert rep.verifier_model is None
    assert rep.scores.categories["grounding"].fp > 0  # the mock's unsupported docs claim (0.6)
    rows = {r["threshold"]: r for r in rep.sweep}
    assert rows[0.7]["precision"] > rows[0.5]["precision"]
    assert rep.recommended_threshold == 0.7
    assert all(c.verify_calls == 0 for c in rep.costs)


def test_with_checks_and_llm_errors(llm, mock):
    items = load_labeled(SAMPLE_PATH)[:6]
    calls = {"n": 0}
    real = mock.transport

    def flaky(url, headers, body):
        calls["n"] += 1
        if headers.get("X-Ambientq-Purpose") == "judge" and calls["n"] == 1:
            return 400, {"error": {"message": "bad"}}
        return real(url, headers, body)

    llm.transport = flaky
    llm.max_retries = 0
    rep = calibrate(items, Harness.load(DEMO_HARNESS), llm, with_checks=True)
    assert rep.with_checks and rep.judged == 5 and len(rep.errors) == 1 and "judge failed" in rep.errors[0]


# --- CLI ------------------------------------------------------------------------------


def _config(tmp_path, **extra) -> str:
    import yaml

    p = tmp_path / "ambientq.yaml"
    p.write_text(yaml.safe_dump({"store": {"url": f"sqlite:///{tmp_path / 'aq.db'}"}, **extra}))
    return str(p)


def test_cli_calibrate_sample_json(tmp_path, capsys):
    cfgp = _config(tmp_path)
    out = tmp_path / "report.json"
    assert main(["-c", cfgp, "calibrate", "--sample", "--base-url", "mock", "--json", "--out", str(out)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data == json.loads(out.read_text())
    assert data["summary"]["judged"] == 40 and data["summary"]["agreement"] < 1.0
    assert data["categories"]["safety"]["recall"] == 1.0
    store = SQLStore(f"sqlite:///{tmp_path / 'aq.db'}")
    ev = [e for e in store.audit_log() if e["action"] == "calibrated"]
    assert len(ev) == 1 and ev[0]["subject"] == "labeled.jsonl"
    detail = ev[0]["detail"] if isinstance(ev[0]["detail"], dict) else json.loads(ev[0]["detail"])
    assert detail["kappa"] == data["summary"]["kappa"] and "messages" not in json.dumps(detail)
    usage = store.usage_rows()
    assert usage and {u["purpose"] for u in usage} == {"judge", "verify"}
    assert {u["agent_name"] for u in usage} == {synthetic.AGENT}


def test_cli_calibrate_text_and_errors(tmp_path, capsys):
    cfgp = _config(tmp_path, llm={"base_url": "mock", "model": "m1"})
    assert main(["-c", cfgp, "calibrate", "--sample", "--no-verify", "--min-confidence", "0.7"]) == 0
    text = capsys.readouterr().out
    assert "model m1, verifier off" in text and "min_confidence 0.70" in text
    bad = tmp_path / "bad.jsonl"
    bad.write_text("{oops\n")
    assert main(["-c", cfgp, "calibrate", str(bad), "--harness", str(DEMO_HARNESS)]) == 2
    assert "bad.jsonl:1" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["-c", cfgp, "calibrate"])


def test_export_labeling_round_trip(tmp_path, capsys):
    cfgp = _config(tmp_path)
    store = SQLStore(f"sqlite:///{tmp_path / 'aq.db'}")
    import time

    now = int(time.time() * 1e9)
    records, truth = synthetic.generate(1, 12, t0_ns=now - 3600 * 10**9)
    parsed = parse_records(records)
    store.ingest_spans(parsed.spans)
    for c in parsed.conversations:
        store.upsert_conversation(c)
    flagged_id = sorted(truth)[0]
    store.save_finding(fnd(flagged_id, "procedure", "R-P1"))
    store.commit()
    out = tmp_path / "to_label.jsonl"
    assert main(["-c", cfgp, "export-labeling", "--since", "7d", "--n", "5", "--out", str(out)]) == 0
    assert "wrote 5 conversation(s)" in capsys.readouterr().out
    lines = [json.loads(x) for x in out.read_text().splitlines()]
    assert len(lines) == 5 and flagged_id in {r["conversation_id"] for r in lines}
    assert all(r["labels"] == {"defects": [], "clean": None, "labeler": "", "note": ""} for r in lines)
    # every exported conversation reads back to the same turns
    for r in lines:
        back = record_to_conversation(r)
        assert turns_equal(back.turns, store.get_conversation(r["conversation_id"]).turns)
    # unlabeled stubs are skipped, not errors; labeling them makes them count
    parsed_l = parse_labeled(out.read_text().splitlines())
    assert not parsed_l.errors and not parsed_l.items and len(parsed_l.unlabeled) == 5
    lines[0]["labels"].update(clean=True, labeler="me")
    out.write_text("".join(json.dumps(r) + "\n" for r in lines))
    assert [i.conversation_id for i in load_labeled(out)] == [lines[0]["conversation_id"]]
    assert any(e["action"] == "exported_for_labeling" for e in store.audit_log())


def test_conversation_to_record_pairs_tool_results_without_ids():
    c = Conversation("z", agent_name="a", turns=[
        Turn(0, "user", "status?"),
        Turn(1, "assistant", tool_name="get_cluster_status", tool_args={"cluster": "c1"}),
        Turn(2, "tool", '{"error": "timeout"}', tool_name="get_cluster_status", is_error=True),
        Turn(3, "assistant", "It timed out."),
    ])
    rec = conversation_to_record(c)
    assert rec["messages"][2]["tool_call_id"] == rec["messages"][1]["tool_calls"][0]["id"]
    assert turns_equal(record_to_conversation(rec).turns, c.turns)


def test_select_for_labeling_mixes_flagged_and_is_deterministic():
    convs = [Conversation(f"c{i}", started_ns=i) for i in range(20)]
    flagged = {"c1", "c2", "c3"}
    a = select_for_labeling(convs, 6, flagged=flagged, flagged_share=0.5)
    assert a == select_for_labeling(convs, 6, flagged=flagged, flagged_share=0.5)
    assert len(a) == 6 and sum(c.conversation_id in flagged for c in a) == 3
    assert [c.started_ns for c in a] == sorted(c.started_ns for c in a)
    allf = select_for_labeling(convs, 6, flagged=flagged, flagged_share=1.0)
    assert len(allf) == 6 and flagged <= {c.conversation_id for c in allf}
    assert len(select_for_labeling(convs, 50)) == 20
