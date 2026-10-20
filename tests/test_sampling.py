from ambientq.models import Conversation
from ambientq.sampling import hash_fraction, select


def convs(n):
    return [Conversation(f"c{i}") for i in range(n)]


def test_deterministic_and_rate():
    a = [c.conversation_id for c in select(convs(200), rate=0.25, seed="s")]
    b = [c.conversation_id for c in select(convs(200), rate=0.25, seed="s")]
    assert a == b and 25 < len(a) < 75
    assert select(convs(10), rate=0.0) == []
    assert len(select(convs(10), rate=1.0)) == 10
    assert 0 <= hash_fraction("x", "y") < 1


def test_priority_first_and_caps():
    cs = convs(50)
    cs[7].has_error = True
    out = select(cs, rate=0.0, priority={"c3"}, max_priority=5)
    assert {c.conversation_id for c in out} == {"c3", "c7"}
    out = select(cs, rate=1.0, priority={"c9"}, max_total=4)
    assert len(out) == 4 and out[0].conversation_id in ("c7", "c9")
