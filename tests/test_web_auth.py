"""Dashboard sign-in (OIDC with a fake provider, trusted proxy headers), roles, team scoping, CSRF, approvals."""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import pytest

from ambientq.auth import (
    OIDC,
    PENDING_COOKIE,
    SESSION_COOKIE,
    Auth,
    AuthError,
    Identity,
    Signer,
    parse_cookies,
    resolve_levels,
    same_origin,
    validate_claims,
)
from ambientq.models import Insight, InsightStatus
from ambientq.store import SQLStore
from ambientq.web import App, Request, serve_background

ISSUER = "https://idp.example.test/realms/demo"
ROLES = {"admin": ["root@example.com"], "approver": ["group:sre", {"group": "blue-leads", "teams": ["blue"]}],
         "viewer": ["*"]}


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def jwt(claims: dict[str, Any], alg: str = "RS256", sign: Any = None, kid: str = "k1") -> str:
    head = b64(json.dumps({"alg": alg, "kid": kid}).encode())
    body = b64(json.dumps(claims).encode())
    sig = sign(f"{head}.{body}".encode()) if sign else b"fake-signature"
    return f"{head}.{body}.{b64(sig)}"


class FakeIdP:
    """An OpenID Provider in-process: discovery, token endpoint, optional JWKS."""

    def __init__(self, claims: dict[str, Any] | None = None, jwks: dict[str, Any] | None = None,
                 sign: Any = None) -> None:
        self.claims = claims or {}
        self.jwks = jwks
        self.sign = sign
        self.nonce = ""
        self.token_requests: list[dict[str, str]] = []

    def __call__(self, method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, Any]:
        if url.endswith("/.well-known/openid-configuration"):
            meta = {"issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/auth",
                    "token_endpoint": f"{ISSUER}/token"}
            if self.jwks is not None:
                meta["jwks_uri"] = f"{ISSUER}/certs"
            return 200, meta
        if url.endswith("/certs"):
            return 200, self.jwks
        if url.endswith("/token"):
            form = {k: v[0] for k, v in urllib.parse.parse_qs((body or b"").decode()).items()}
            self.token_requests.append(form)
            now = int(time.time())
            claims = {"iss": ISSUER, "aud": "ambientq", "sub": "u1", "exp": now + 300, "iat": now,
                      "nonce": self.nonce, **self.claims}
            return 200, {"id_token": jwt(claims, sign=self.sign), "access_token": "x"}
        return 404, {}


def oidc_auth(idp: FakeIdP, roles: dict[str, Any] = ROLES) -> Auth:
    cfg = {"mode": "oidc", "roles": roles, "public_url": "https://aq.example.test",
           "oidc": {"issuer": ISSUER, "client_id": "ambientq"}}
    return Auth(cfg, secret=b"s" * 32, oidc=OIDC(cfg["oidc"], "client-secret", http=idp))


def proxy_auth(roles: dict[str, Any] = ROLES) -> Auth:
    return Auth({"mode": "proxy", "roles": roles, "proxy": {"trusted_proxies": ["127.0.0.1/32", "10.0.0.0/8"]}},
                secret=b"p" * 32)


@pytest.fixture
def store(tmp_path):
    s = SQLStore(f"sqlite:///{tmp_path / 'aq.db'}")
    s.migrate()
    for iid, team in (("ins_blue", "blue"), ("ins_red", "red")):
        s.save_insight(Insight(insight_id=iid, fingerprint=iid, title=f"{team} insight", category="tool_use",
                               rubric_id="R", agent_name="bot", status=InsightStatus.NEW, severity="high",
                               first_seen_run="r", last_seen_run="r", created_at=time.time(), team=team))
        s.save_proposal({"proposal_id": f"prp_{team}", "insight_id": iid, "status": "PENDING_APPROVAL",
                         "team": team, "kind": "tool_description", "file": "agent.yaml", "start_line": 3,
                         "end_line": 4, "diff": "--- a/agent.yaml\n+++ b/agent.yaml\n@@ -3 +3 @@\n-old\n+new <b>\n",
                         "rationale": "say MANDATORY", "replay": {
                             "fixed": 2, "still_failing": 0, "regressions": 0, "mode": "multi", "unmatched_calls": 1,
                             "rows": [{"conversation_id": "c1", "kind": "failing", "before_fail": True,
                                       "after_fail": False, "unmatched_calls": [{"tool": "x"}],
                                       "before_reply": "I scaled it", "after_reply": "Confirm first?"}]},
                         "shadow": {"verdict": "better", "conversations": 4, "live_findings": 3,
                                    "patched_findings": 0, "wins": 3, "losses": 0, "ran_at": time.time()}})
    return s


def req(method: str, path: str, headers: dict[str, str] | None = None, form: dict[str, str] | None = None,
        ip: str = "127.0.0.1") -> Request:
    u = urllib.parse.urlsplit(path)
    return Request(method, u.path, {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}, headers or {}, ip,
                   form or {}, "https://aq.example.test")


def proxy_headers(email: str, groups: str = "") -> dict[str, str]:
    return {"X-Forwarded-User": email.split("@")[0], "X-Forwarded-Email": email, "X-Forwarded-Groups": groups}


# -- roles ---------------------------------------------------------------------------------

def test_roles_resolve_with_team_scope():
    lv = resolve_levels(ROLES, "ann", "ann@example.com", ["blue-leads"])
    ident = Identity("ann", "ann@example.com", levels=lv)
    assert ident.can("viewer") and ident.can("approver", "blue") and not ident.can("approver", "red")
    assert not ident.can("admin") and ident.role("blue") == "approver" and ident.role("red") == "viewer"
    root = Identity("r", "ROOT@example.com", levels=resolve_levels(ROLES, "r", "ROOT@example.com", []))
    assert root.can("admin", "red")
    scoped = Identity("v", levels=resolve_levels({"viewer": [{"user": "v", "teams": ["red"]}]}, "v", "", []))
    assert scoped.visible_teams() == {"red"}


def test_signer_rejects_tampering_and_expiry():
    s = Signer(b"k" * 32)
    tok = s.dumps({"u": "a"}, 60)
    assert s.loads(tok)["u"] == "a"
    assert s.loads(tok[:-2] + ("A" if tok[-2] != "A" else "B") + tok[-1]) is None
    assert Signer(b"other" * 8).loads(tok) is None
    assert s.loads(s.dumps({"u": "a"}, -10)) is None


def test_claims_validation():
    now = time.time()
    good = {"iss": ISSUER, "aud": "ambientq", "exp": now + 60, "iat": now, "nonce": "n"}
    validate_claims(good, issuer=ISSUER, client_id="ambientq", nonce="n")
    for bad in ({"iss": "https://evil"}, {"aud": "other"}, {"exp": now - 600}, {"nonce": "x"},
                {"aud": ["ambientq", "other"], "azp": "other"}, {"iat": now + 3600}):
        with pytest.raises(AuthError):
            validate_claims({**good, **bad}, issuer=ISSUER, client_id="ambientq", nonce="n")


def test_same_origin():
    assert same_origin({"Origin": "https://aq.example.test"}, "https://aq.example.test", None)
    assert same_origin({"Referer": "https://aq.example.test/proposals/x"}, "http://internal:8080",
                       "https://aq.example.test")
    assert not same_origin({"Origin": "https://evil.test"}, "https://aq.example.test", None)
    assert not same_origin({}, "https://aq.example.test", None)


# -- read-only when auth is off ----------------------------------------------------------------

def test_auth_off_is_read_only(store):
    app = App(store)
    page = app.handle(req("GET", "/proposals/prp_blue"))
    assert page.code == 200 and "Read-only" in str(page.body) and "method=post" not in str(page.body)
    assert "new &lt;b&gt;" in str(page.body) and "Confirm first?" in str(page.body)
    r = app.handle(req("POST", "/proposals/prp_blue/decision", form={"decision": "approve"}))
    assert r.code == 404
    assert store.get_proposal("prp_blue")["status"] == "PENDING_APPROVAL"


# -- OIDC ------------------------------------------------------------------------------------

def _sign_in(app: App, idp: FakeIdP, next_path: str = "/proposals") -> tuple[str, Any]:
    r = app.handle(req("GET", "/proposals"))
    assert r.code == 303 and r.headers[0][1].startswith("/login?")
    r = app.handle(req("GET", f"/login?next={urllib.parse.quote(next_path)}"))
    assert r.code == 303
    loc = dict(r.headers)["Location"]
    q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(loc).query).items()}
    assert loc.startswith(f"{ISSUER}/auth?") and q["code_challenge_method"] == "S256"
    assert q["redirect_uri"] == "https://aq.example.test/auth/callback"
    pending = dict(r.headers)["Set-Cookie"].split(";")[0].split("=", 1)[1]
    idp.nonce = q["nonce"]
    cb = app.handle(req("GET", f"/auth/callback?code=abc&state={q['state']}",
                        {"Cookie": f"{PENDING_COOKIE}={pending}"}))
    return q["code_challenge"], cb


def test_oidc_sign_in_and_approve(store):
    idp = FakeIdP({"preferred_username": "sam", "email": "sam@example.com", "groups": ["sre"]})
    app = App(store, auth=oidc_auth(idp))
    challenge, cb = _sign_in(app, idp)
    assert cb.code == 303 and dict(cb.headers)["Location"] == "/proposals"
    # the PKCE verifier sent to the token endpoint matches the challenge
    import hashlib
    verifier = idp.token_requests[0]["code_verifier"]
    assert b64(hashlib.sha256(verifier.encode()).digest()) == challenge
    assert idp.token_requests[0]["client_secret"] == "client-secret"
    cookies = [v for k, v in cb.headers if k == "Set-Cookie"]
    session = next(c for c in cookies if c.startswith(SESSION_COOKIE)).split(";")[0].split("=", 1)[1]
    assert "HttpOnly" in cookies[0] and "Secure" in cookies[0] and "SameSite=Lax" in cookies[0]
    hdrs = {"Cookie": f"{SESSION_COOKIE}={session}", "Origin": "https://aq.example.test"}
    page = app.handle(req("GET", "/proposals/prp_red", hdrs))
    assert page.code == 200 and "Approve" in str(page.body) and "sam@example.com" in str(page.body)
    csrf = str(page.body).split("name=csrf value='")[1].split("'")[0]
    # wrong token, then no Origin: refused and audited
    assert app.handle(req("POST", "/proposals/prp_red/decision", hdrs, {"csrf": "x", "decision": "approve"})).code == 403
    no_origin = {"Cookie": hdrs["Cookie"]}
    assert app.handle(req("POST", "/proposals/prp_red/decision", no_origin,
                          {"csrf": csrf, "decision": "approve"})).code == 403
    r = app.handle(req("POST", "/proposals/prp_red/decision", hdrs,
                       {"csrf": csrf, "decision": "approve", "note": "CHG-1"}))
    assert r.code == 303 and dict(r.headers)["Location"].endswith("m=approved")
    p = store.get_proposal("prp_red")
    assert p["status"] == "APPROVED" and p["approved_by"] == "sam@example.com" and p["decision_note"] == "CHG-1"
    actions = [a["action"] for a in store.audit_log(50)]
    assert {"sign_in", "approved", "decision_refused"} <= set(actions)
    approved = next(a for a in store.audit_log(50) if a["action"] == "approved")
    assert "dashboard" in str(approved["detail"])
    # a second decision is refused
    r = app.handle(req("POST", "/proposals/prp_red/decision", hdrs, {"csrf": csrf, "decision": "reject"}))
    assert dict(r.headers)["Location"].endswith("m=decided")
    out = app.handle(req("GET", "/logout", hdrs))
    assert out.code == 303 and "Max-Age=0" in dict(out.headers)["Set-Cookie"]


def test_oidc_rejects_bad_state_and_tampered_session(store):
    idp = FakeIdP({"email": "x@example.com"})
    app = App(store, auth=oidc_auth(idp))
    r = app.handle(req("GET", "/login"))
    pending = dict(r.headers)["Set-Cookie"].split(";")[0].split("=", 1)[1]
    bad = app.handle(req("GET", "/auth/callback?code=abc&state=wrong", {"Cookie": f"{PENDING_COOKIE}={pending}"}))
    assert bad.code == 401 and "state mismatch" in str(bad.body)
    assert app.handle(req("GET", "/auth/callback?code=abc&state=x")).code == 401
    assert app.handle(req("GET", "/", {"Cookie": f"{SESSION_COOKIE}=forged.token"})).code == 303
    assert app.handle(req("GET", "/api/insights")).code == 401
    assert "sign_in_failed" in [a["action"] for a in store.audit_log(20)]


def test_oidc_nonce_mismatch_is_refused(store):
    idp = FakeIdP({"email": "x@example.com"})
    app = App(store, auth=oidc_auth(idp))
    r = app.handle(req("GET", "/login"))
    loc = dict(r.headers)["Location"]
    q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(loc).query).items()}
    pending = dict(r.headers)["Set-Cookie"].split(";")[0].split("=", 1)[1]
    idp.nonce = "replayed"
    cb = app.handle(req("GET", f"/auth/callback?code=abc&state={q['state']}", {"Cookie": f"{PENDING_COOKIE}={pending}"}))
    assert cb.code == 401 and "nonce" in str(cb.body)


def test_oidc_signature_checked_with_jwks(store):
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = key.public_key().public_numbers()

    def num(n: int) -> str:
        return b64(n.to_bytes((n.bit_length() + 7) // 8, "big"))

    jwks = {"keys": [{"kty": "RSA", "kid": "k1", "n": num(pub.n), "e": num(pub.e)}]}
    good = FakeIdP({"email": "a@example.com"}, jwks=jwks,
                   sign=lambda m: key.sign(m, padding.PKCS1v15(), hashes.SHA256()))
    _, cb = _sign_in(App(store, auth=oidc_auth(good)), good)
    assert cb.code == 303
    forged = FakeIdP({"email": "a@example.com"}, jwks=jwks)  # signature bytes are junk
    _, cb = _sign_in(App(store, auth=oidc_auth(forged)), forged)
    assert cb.code == 401 and "signature" in str(cb.body)


# -- trusted proxy --------------------------------------------------------------------------------

def test_proxy_headers_roles_and_team_scope(store):
    app = App(store, auth=proxy_auth({"approver": [{"group": "blue-leads", "teams": ["blue"]}],
                                      "viewer": [{"group": "blue-leads", "teams": ["blue"]}],
                                      "admin": ["root@example.com"]}))
    lead = proxy_headers("ann@example.com", "blue-leads")
    assert app.handle(req("GET", "/")).code == 401
    # headers from an untrusted address are not believed
    assert app.handle(req("GET", "/", lead, ip="203.0.113.9")).code == 401
    home = app.handle(req("GET", "/", lead, ip="10.1.2.3"))
    assert home.code == 200 and "blue insight" in str(home.body) and "red insight" not in str(home.body)
    assert app.handle(req("GET", "/proposals/prp_red", lead)).code == 404
    assert app.handle(req("GET", "/api/proposals/prp_red", lead)).code == 404
    assert [p["proposal_id"] for p in json.loads(app.handle(req("GET", "/api/proposals", lead)).body)] == ["prp_blue"]
    assert app.handle(req("GET", "/audit", lead)).code == 403
    assert app.handle(req("GET", "/audit", proxy_headers("root@example.com"))).code == 200
    stranger = proxy_headers("eve@example.com")
    assert app.handle(req("GET", "/", stranger)).code == 403
    assert "access_denied" in [a["action"] for a in store.audit_log(20)]


def test_viewer_cannot_approve(store):
    app = App(store, auth=proxy_auth())
    viewer = {**proxy_headers("val@example.com"), "Origin": "https://aq.example.test"}
    page = app.handle(req("GET", "/proposals/prp_blue", viewer))
    assert page.code == 200 and "method=post" not in str(page.body) and "approvers decide" in str(page.body)
    csrf = app.auth.identity(viewer, "127.0.0.1").csrf  # type: ignore[union-attr]
    r = app.handle(req("POST", "/proposals/prp_blue/decision", viewer, {"csrf": csrf, "decision": "approve"}))
    assert dict(r.headers)["Location"].endswith("m=forbidden")
    assert store.get_proposal("prp_blue")["status"] == "PENDING_APPROVAL"
    assert any(a["action"] == "decision_refused" and "approver" in str(a["detail"]) for a in store.audit_log(20))


def test_approve_over_http_in_proxy_mode(store):
    """End to end over a real socket: proxy headers, CSRF token from the page, security headers."""
    app = App(store, auth=proxy_auth())
    srv = serve_background(app, port=0)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        hdrs = proxy_headers("sam@example.com", "sre")
        with urllib.request.urlopen(urllib.request.Request(f"{base}/proposals/prp_blue", headers=hdrs)) as r:
            html = r.read().decode()
            assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
            assert r.headers["X-Frame-Options"] == "DENY"
        csrf = html.split("name=csrf value='")[1].split("'")[0]
        body = urllib.parse.urlencode({"csrf": csrf, "decision": "reject", "note": "too broad"}).encode()

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a: Any, **k: Any) -> None:
                return None

        opener = urllib.request.build_opener(NoRedirect)
        post = urllib.request.Request(f"{base}/proposals/prp_blue/decision", data=body, method="POST",
                                      headers={**hdrs, "Origin": base})
        with pytest.raises(urllib.error.HTTPError) as exc:
            opener.open(post)
        assert exc.value.code == 303
        p = store.get_proposal("prp_blue")
        assert p["status"] == "REJECTED" and p["approved_by"] == "sam@example.com"
    finally:
        srv.shutdown()


def test_pages_render_with_auth_off(store):
    app = App(store)
    for path in ("/", "/proposals", "/runs", "/usage", "/usage?since=7d", "/reports", "/audit", "/insights/ins_blue",
                 "/?team=red"):
        r = app.handle(req("GET", path))
        assert r.code == 200, path
        assert "ambientq" in str(r.body)
    assert "red insight" in str(app.handle(req("GET", "/?team=red")).body)
    assert "blue insight" not in str(app.handle(req("GET", "/?team=red")).body)
    assert app.handle(req("GET", "/reports/notamonth")).code == 404
    assert json.loads(app.handle(req("GET", "/api/usage")).body)["source"]["totals"]["calls"] == 0


def test_cookie_parsing():
    assert parse_cookies("a=1; aq_session=x.y; b") == {"a": "1", "aq_session": "x.y"}
    assert parse_cookies(None) == {}
