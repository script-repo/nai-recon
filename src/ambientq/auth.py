"""Dashboard sign-in: OIDC (authorization code + PKCE) or trusted proxy headers, roles, sessions, CSRF.

Standard library only. ``auth.mode``:

* ``none``: no sign-in; the dashboard is read-only and has no write endpoints.
* ``oidc``: authorization code flow with PKCE (S256) against any OpenID
  Provider (Keycloak, Entra ID, Okta, Dex, ...). The ID token comes straight
  from the token endpoint over TLS, which OIDC Core 3.1.3.7 accepts in place
  of a signature check; when the optional ``cryptography`` package is installed
  (``pip install 'ambientq[oidc]'``) the RS256/ES256 signature is also checked
  against the provider's JWKS.
* ``proxy``: an authenticating reverse proxy (oauth2-proxy, Pomerium, an
  ingress with external auth) sets identity headers; they are trusted only
  from ``auth.proxy.trusted_proxies``.

Roles are ``viewer`` < ``approver`` < ``admin``, granted in ``auth.roles`` to
``*``, a user name, an email, ``group:<name>``, or a mapping that scopes the
grant to teams: ``{group: sre, teams: [platform]}``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import ssl
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

ROLES = {"viewer": 1, "approver": 2, "admin": 3}
SESSION_COOKIE = "ambientq_session"
PENDING_COOKIE = "ambientq_oidc"
ALL = "*"


class AuthError(Exception):
    """A sign-in that must fail (bad state, bad token, untrusted proxy...)."""


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# -- identities and roles --------------------------------------------------------------

@dataclass
class Identity:
    user: str
    email: str = ""
    groups: list[str] = field(default_factory=list)
    # role level per team; the key "*" holds the level granted for every team
    levels: dict[str, int] = field(default_factory=dict)
    csrf: str = ""

    @property
    def name(self) -> str:
        return self.email or self.user

    def level(self, team: str | None = None) -> int:
        lv = self.levels.get(ALL, 0)
        if team:
            lv = max(lv, self.levels.get(team, 0))
        return lv

    def can(self, role: str, team: str | None = None) -> bool:
        return self.level(team) >= ROLES[role]

    def role(self, team: str | None = None) -> str:
        lv = self.level(team)
        return next((r for r, n in sorted(ROLES.items(), key=lambda x: -x[1]) if lv >= n), "none")

    def visible_teams(self) -> set[str] | None:
        """Teams this identity may view; ``None`` means every team."""
        if self.levels.get(ALL, 0) >= ROLES["viewer"]:
            return None
        return {t for t, lv in self.levels.items() if lv >= ROLES["viewer"]}


def _matches(entry: str, user: str, email: str, groups: list[str]) -> bool:
    entry = entry.strip()
    if entry == ALL:
        return True
    if entry.startswith("group:"):
        return entry[6:] in groups
    return entry.lower() in (user.lower(), email.lower()) and entry != ""


def resolve_levels(roles_cfg: dict[str, Any], user: str, email: str, groups: list[str]) -> dict[str, int]:
    levels: dict[str, int] = {}
    for role, entries in (roles_cfg or {}).items():
        if role not in ROLES:
            continue
        for e in entries or []:
            if isinstance(e, dict):
                who = [f"group:{e['group']}"] if e.get("group") else []
                who += [str(e[k]) for k in ("user", "email") if e.get(k)]
                teams = [str(t) for t in e.get("teams") or []] or [ALL]
            else:
                who, teams = [str(e)], [ALL]
            if any(_matches(w, user, email, groups) for w in who):
                for t in teams:
                    levels[t] = max(levels.get(t, 0), ROLES[role])
    return levels


# -- signed cookies --------------------------------------------------------------------

class Signer:
    """HMAC-SHA256 signed, expiring JSON tokens for cookies."""

    def __init__(self, secret: bytes) -> None:
        self.secret = secret

    def dumps(self, data: dict[str, Any], ttl_s: int) -> str:
        body = b64url(json.dumps({**data, "exp": int(time.time()) + ttl_s}, separators=(",", ":")).encode())
        sig = b64url(hmac.new(self.secret, body.encode(), hashlib.sha256).digest())
        return f"{body}.{sig}"

    def loads(self, token: str) -> dict[str, Any] | None:
        try:
            body, sig = token.split(".", 1)
        except ValueError:
            return None
        good = b64url(hmac.new(self.secret, body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, good):
            return None
        try:
            data = json.loads(b64url_decode(body))
        except (ValueError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or int(data.get("exp", 0)) < time.time():
            return None
        return data


def parse_cookies(header: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (header or "").split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            out[k] = v
    return out


# -- OIDC ------------------------------------------------------------------------------

def pkce_pair() -> tuple[str, str]:
    verifier = b64url(secrets.token_bytes(48))
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


HttpFn = Callable[[str, str, dict[str, str], bytes | None], tuple[int, dict[str, Any]]]


def urllib_http(ca_bundle: str | None = None, timeout: float = 15) -> HttpFn:
    ctx = ssl.create_default_context(cafile=ca_bundle) if ca_bundle else ssl.create_default_context()

    def call(method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, dict[str, Any]]:
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:  # noqa: S310
                return r.status, json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode() or "{}")
            except json.JSONDecodeError:
                return e.code, {}

    return call


def decode_jwt(token: str) -> tuple[dict[str, Any], dict[str, Any], bytes, bytes]:
    try:
        h, p, s = token.split(".")
        return json.loads(b64url_decode(h)), json.loads(b64url_decode(p)), f"{h}.{p}".encode(), b64url_decode(s)
    except (ValueError, json.JSONDecodeError) as exc:
        raise AuthError("malformed ID token") from exc


def verify_signature(header: dict[str, Any], signing_input: bytes, sig: bytes, jwks: dict[str, Any]) -> bool:
    """Verify an RS256/ES256 JWT signature with ``cryptography`` if it is installed.

    Returns False when the library is missing (the caller then relies on the TLS
    back-channel); raises AuthError when the signature is wrong.
    """
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
        from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
    except ImportError:
        return False
    key = next((k for k in jwks.get("keys", []) if k.get("kid") == header.get("kid")), None)
    if key is None and len(jwks.get("keys", [])) == 1:
        key = jwks["keys"][0]
    if key is None:
        raise AuthError("no JWKS key for the ID token's kid")

    def n(v: str) -> int:
        return int.from_bytes(b64url_decode(v), "big")

    try:
        if header.get("alg") == "RS256" and key.get("kty") == "RSA":
            pub = rsa.RSAPublicNumbers(n(key["e"]), n(key["n"])).public_key()
            pub.verify(sig, signing_input, padding.PKCS1v15(), hashes.SHA256())
        elif header.get("alg") == "ES256" and key.get("kty") == "EC":
            pub_ec = ec.EllipticCurvePublicNumbers(n(key["x"]), n(key["y"]), ec.SECP256R1()).public_key()
            half = len(sig) // 2
            der = encode_dss_signature(int.from_bytes(sig[:half], "big"), int.from_bytes(sig[half:], "big"))
            pub_ec.verify(der, signing_input, ec.ECDSA(hashes.SHA256()))
        else:
            raise AuthError(f"unsupported ID token algorithm {header.get('alg')!r}")
    except AuthError:
        raise
    except Exception as exc:  # InvalidSignature and key errors
        raise AuthError("ID token signature is invalid") from exc
    return True


def validate_claims(claims: dict[str, Any], *, issuer: str, client_id: str, nonce: str,
                    leeway_s: int = 120) -> None:
    now = time.time()
    if str(claims.get("iss", "")).rstrip("/") != issuer.rstrip("/"):
        raise AuthError("ID token issuer mismatch")
    aud = claims.get("aud")
    if client_id not in (aud if isinstance(aud, list) else [aud]):
        raise AuthError("ID token audience mismatch")
    if isinstance(aud, list) and len(aud) > 1 and claims.get("azp") not in (None, client_id):
        raise AuthError("ID token authorized party mismatch")
    if float(claims.get("exp", 0)) + leeway_s < now:
        raise AuthError("ID token expired")
    if float(claims.get("iat", now)) - leeway_s > now:
        raise AuthError("ID token issued in the future")
    if not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
        raise AuthError("ID token nonce mismatch")


class OIDC:
    def __init__(self, cfg: dict[str, Any], client_secret: str | None, http: HttpFn | None = None) -> None:
        if not cfg.get("issuer") or not cfg.get("client_id"):
            raise ValueError("auth.oidc.issuer and auth.oidc.client_id are required for auth.mode=oidc")
        self.issuer = str(cfg["issuer"]).rstrip("/")
        self.client_id = str(cfg["client_id"])
        self.client_secret = client_secret
        self.scopes = str(cfg.get("scopes") or "openid email profile")
        self.groups_claim = str(cfg.get("groups_claim") or "groups")
        self.http = http or urllib_http(cfg.get("ca_bundle"))
        self._meta: dict[str, Any] | None = None
        self._jwks: dict[str, Any] | None = None
        self._warned = False

    def meta(self) -> dict[str, Any]:
        if self._meta is None:
            code, data = self.http("GET", f"{self.issuer}/.well-known/openid-configuration",
                                   {"Accept": "application/json"}, None)
            if code != 200 or "authorization_endpoint" not in data:
                raise AuthError(f"OIDC discovery failed (HTTP {code})")
            self._meta = data
        return self._meta

    def authorize_url(self, redirect_uri: str, state: str, nonce: str, challenge: str) -> str:
        q = {"response_type": "code", "client_id": self.client_id, "redirect_uri": redirect_uri,
             "scope": self.scopes, "state": state, "nonce": nonce, "code_challenge": challenge,
             "code_challenge_method": "S256"}
        ep = self.meta()["authorization_endpoint"]
        return f"{ep}{'&' if '?' in ep else '?'}{urllib.parse.urlencode(q)}"

    def exchange(self, code: str, redirect_uri: str, verifier: str, nonce: str) -> dict[str, Any]:
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                "client_id": self.client_id, "code_verifier": verifier}
        if self.client_secret:
            form["client_secret"] = self.client_secret
        status, data = self.http("POST", self.meta()["token_endpoint"],
                                 {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
                                 urllib.parse.urlencode(form).encode())
        if status != 200 or not data.get("id_token"):
            raise AuthError(f"token exchange failed (HTTP {status}: {data.get('error', '')})")
        header, claims, signing_input, sig = decode_jwt(str(data["id_token"]))
        if str(header.get("alg", "none")).lower() == "none":
            raise AuthError("unsigned ID token rejected")
        verified = False
        if self.meta().get("jwks_uri"):
            if self._jwks is None:
                _, self._jwks = self.http("GET", self.meta()["jwks_uri"], {"Accept": "application/json"}, None)
            verified = verify_signature(header, signing_input, sig, self._jwks or {})
        if not verified and not self._warned:
            log.warning("ID token signatures are not checked (install 'ambientq[oidc]'); relying on the TLS "
                        "token-endpoint exchange (OIDC Core 3.1.3.7)")
            self._warned = True
        validate_claims(claims, issuer=self.issuer, client_id=self.client_id, nonce=nonce)
        return claims


# -- the authenticator the web app uses -------------------------------------------------

class Auth:
    """Resolves the identity behind a request, and runs the OIDC sign-in dance."""

    def __init__(self, cfg: dict[str, Any], *, secret: bytes | None = None, oidc: OIDC | None = None,
                 client_secret: str | None = None) -> None:
        self.cfg = cfg
        self.mode = str(cfg.get("mode") or "none")
        if self.mode not in ("none", "oidc", "proxy"):
            raise ValueError(f"auth.mode must be none, oidc or proxy, got {self.mode!r}")
        if secret is None:
            env = os.environ.get(str(cfg.get("session_secret_env") or "AMBIENTQ_SESSION_SECRET"), "")
            if env:
                secret = env.encode()
            else:
                if self.mode != "none":
                    log.warning("no session secret set (auth.session_secret_env); using a random one: "
                                "sessions end when ambientq restarts and don't work across replicas")
                secret = secrets.token_bytes(32)
        self.signer = Signer(secret)
        self.ttl = int(cfg.get("session_ttl_s") or 28800)
        self.secure_cookie = not bool(cfg.get("cookie_insecure"))
        self.public_url = (cfg.get("public_url") or "").rstrip("/") or None
        self.roles = cfg.get("roles") or {}
        proxy = cfg.get("proxy") or {}
        self.proxy = proxy
        self.trusted = [ipaddress.ip_network(c, strict=False) for c in proxy.get("trusted_proxies") or []]
        self.oidc = oidc
        if self.mode == "oidc" and self.oidc is None:
            self.oidc = OIDC(cfg.get("oidc") or {}, client_secret)

    @classmethod
    def from_config(cls, config: Any) -> Auth:
        a = config.section("auth")
        return cls(a, client_secret=_oidc_secret(a))

    @property
    def enabled(self) -> bool:
        return self.mode != "none"

    # -- identities --
    def identity(self, headers: Any, client_ip: str) -> Identity | None:
        if self.mode == "proxy":
            return self._proxy_identity(headers, client_ip)
        if self.mode == "oidc":
            data = self.signer.loads(parse_cookies(headers.get("Cookie")).get(SESSION_COOKIE, ""))
            if not data or data.get("k") != "s":
                return None
            return self.make_identity(data.get("u", ""), data.get("e", ""), data.get("g") or [], data.get("c", ""))
        return None

    def make_identity(self, user: str, email: str, groups: list[str], csrf: str = "") -> Identity:
        return Identity(user=user, email=email, groups=list(groups),
                        levels=resolve_levels(self.roles, user, email, list(groups)), csrf=csrf)

    def _trusted(self, ip: str) -> bool:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(addr in net for net in self.trusted)

    def _proxy_identity(self, headers: Any, client_ip: str) -> Identity | None:
        user = headers.get(self.proxy.get("user_header") or "X-Forwarded-User") or ""
        email = headers.get(self.proxy.get("email_header") or "X-Forwarded-Email") or ""
        if not (user or email):
            return None
        if not self._trusted(client_ip):
            raise AuthError(f"identity headers from untrusted address {client_ip}")
        raw = headers.get(self.proxy.get("groups_header") or "X-Forwarded-Groups") or ""
        groups = [g.strip() for g in raw.replace(";", ",").split(",") if g.strip()]
        ident = self.make_identity(user, email, groups)
        # proxy mode has no session cookie of its own: derive a stable CSRF token from the identity
        ident.csrf = b64url(hmac.new(self.signer.secret, f"csrf|{ident.name}".encode(), hashlib.sha256).digest())
        return ident

    # -- OIDC flow --
    def redirect_uri(self, host_url: str) -> str:
        return f"{self.public_url or host_url}/auth/callback"

    def begin(self, host_url: str, next_path: str = "/") -> tuple[str, str]:
        """Start sign-in: returns (provider URL to redirect to, pending cookie value)."""
        assert self.oidc is not None
        state, nonce = b64url(secrets.token_bytes(24)), b64url(secrets.token_bytes(24))
        verifier, challenge = pkce_pair()
        pending = self.signer.dumps({"k": "p", "st": state, "n": nonce, "v": verifier,
                                     "next": next_path if next_path.startswith("/") else "/"}, 600)
        return self.oidc.authorize_url(self.redirect_uri(host_url), state, nonce, challenge), pending

    def finish(self, host_url: str, query: dict[str, str], pending_cookie: str) -> tuple[Identity, str, str]:
        """Complete sign-in: returns (identity, session cookie value, where to go next)."""
        assert self.oidc is not None
        if query.get("error"):
            raise AuthError(f"provider returned {query['error']}")
        pending = self.signer.loads(pending_cookie)
        if not pending or pending.get("k") != "p":
            raise AuthError("sign-in expired or was started elsewhere")
        if not hmac.compare_digest(query.get("state", ""), str(pending.get("st", ""))):
            raise AuthError("state mismatch")
        if not query.get("code"):
            raise AuthError("no authorization code")
        claims = self.oidc.exchange(query["code"], self.redirect_uri(host_url), str(pending["v"]),
                                    str(pending["n"]))
        user = str(claims.get("preferred_username") or claims.get("sub") or "")
        email = str(claims.get("email") or "")
        groups = claims.get(self.oidc.groups_claim) or []
        groups = [str(g) for g in groups] if isinstance(groups, list) else [str(groups)]
        csrf = b64url(secrets.token_bytes(24))
        ident = self.make_identity(user, email, groups, csrf)
        session = self.signer.dumps({"k": "s", "u": user, "e": email, "g": groups[:50], "c": csrf}, self.ttl)
        return ident, session, str(pending.get("next") or "/")

    def cookie(self, name: str, value: str, max_age: int) -> str:
        parts = [f"{name}={value}", "Path=/", "HttpOnly", "SameSite=Lax", f"Max-Age={max_age}"]
        if self.secure_cookie:
            parts.append("Secure")
        return "; ".join(parts)


def _oidc_secret(auth_cfg: dict[str, Any]) -> str | None:
    env = (auth_cfg.get("oidc") or {}).get("client_secret_env")
    return os.environ.get(env) if env else None


def check_csrf(ident: Identity | None, form_token: str) -> bool:
    return bool(ident and ident.csrf and form_token and hmac.compare_digest(ident.csrf, form_token))


def same_origin(headers: Any, host_url: str, public_url: str | None) -> bool:
    """The request's Origin (or Referer) must be this dashboard."""
    origin = headers.get("Origin") or ""
    if not origin:
        ref = headers.get("Referer") or ""
        if not ref:
            return False
        u = urllib.parse.urlsplit(ref)
        origin = f"{u.scheme}://{u.netloc}"
    allowed = {host_url.rstrip("/")}
    if public_url:
        allowed.add(public_url.rstrip("/"))
    return origin.rstrip("/") in allowed
