"""The Helm chart renders a config ambientq loads, and keeps secrets in Secrets.

Skipped unless a ``helm`` binary is on PATH (or named by ``AMBIENTQ_HELM``);
the CI ``helm`` job runs it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from ambientq.config import Config

CHART = Path(__file__).resolve().parents[1] / "deploy" / "helm" / "ambientq"
HELM = os.environ.get("AMBIENTQ_HELM") or shutil.which("helm")

pytestmark = pytest.mark.skipif(not HELM, reason="helm is not installed")

SECRET_ENVS = {"NAI_API_KEY", "AMBIENTQ_STORE_URL", "AMBIENTQ_INGEST_TOKEN", "AMBIENTQ_SESSION_SECRET",
               "AMBIENTQ_OIDC_CLIENT_SECRET", "FORGE_TOKEN"}


def render(*args: str) -> list[dict[str, Any]]:
    out = subprocess.run([str(HELM), "template", "t", str(CHART), *args], capture_output=True, text=True,
                         check=True).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def render_error(*args: str) -> str:
    res = subprocess.run([str(HELM), "template", "t", str(CHART), *args], capture_output=True, text=True)
    assert res.returncode != 0
    return res.stderr


def kind(docs: list[dict[str, Any]], k: str) -> dict[str, Any]:
    return next(d for d in docs if d["kind"] == k)


def load_config(docs: list[dict[str, Any]], tmp_path: Path) -> Config:
    p = tmp_path / "ambientq.yaml"
    p.write_text(kind(docs, "ConfigMap")["data"]["ambientq.yaml"])
    return Config.load(p)  # also rejects any inline secret


def pod_specs(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    specs = [kind(docs, "Deployment")["spec"]["template"]["spec"]]
    specs += [d["spec"]["jobTemplate"]["spec"]["template"]["spec"] for d in docs if d["kind"] == "CronJob"]
    return specs


def test_defaults(tmp_path):
    docs = render()
    cfg = load_config(docs, tmp_path)
    assert cfg.get("auth", "mode") == "none" and cfg.get("replay", "mode") == "multi"
    assert cfg.get("llm", "verifier_model") is None and cfg.data["alerts"]["webhooks"] == []
    dep = kind(docs, "Deployment")
    c = dep["spec"]["template"]["spec"]["containers"][0]
    assert c["image"] == "ghcr.io/script-repo/ambientq:0.2.0"
    assert c["livenessProbe"]["httpGet"]["path"] == "/healthz" == c["readinessProbe"]["httpGet"]["path"]
    assert dep["spec"]["replicas"] == 1 and dep["spec"]["strategy"]["type"] == "Recreate"
    assert {d["kind"] for d in docs} == {"NetworkPolicy", "ConfigMap", "PersistentVolumeClaim", "Service",
                                         "Deployment", "CronJob"}


def test_security_context_everywhere():
    for spec in pod_specs(render("-f", str(CHART / "ci" / "full-values.yaml"))):
        assert spec["securityContext"]["runAsNonRoot"] is True
        assert spec["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
        assert spec["automountServiceAccountToken"] is False
        for c in spec["containers"] + spec.get("initContainers", []):
            sc = c["securityContext"]
            assert sc["readOnlyRootFilesystem"] is True and sc["allowPrivilegeEscalation"] is False
            assert sc["runAsUser"] == 10001 and sc["capabilities"] == {"drop": ["ALL"]}


def test_everything_enabled(tmp_path):
    docs = render("-f", str(CHART / "ci" / "full-values.yaml"))
    cfg = load_config(docs, tmp_path)
    assert cfg.get("auth", "mode") == "oidc"
    assert cfg.get("auth", "public_url") == "https://ambientq.example.invalid"
    assert cfg.data["auth"]["oidc"]["client_id"] == "ambientq"
    assert [w["url_env"] for w in cfg.data["alerts"]["webhooks"]] == ["AMBIENTQ_ALERT_WEBHOOK_0",
                                                                      "AMBIENTQ_ALERT_WEBHOOK_1"]
    assert cfg.agents()[0] == {"name": "nkp-ops-agent", "harness": "/harness/repo/agents/nkp-ops",
                               "team": "platform"}
    assert cfg.team_for("docs-agent") == "docs" and cfg.get("shadow", "enabled") is True
    assert cfg.data["pricing"]["models"]["llama-3-1-70b-judge"]["output_per_1k"] == 0.004
    for spec in pod_specs(docs):
        env = {e["name"]: e for e in spec["containers"][0]["env"]}
        for name in SECRET_ENVS | {"AMBIENTQ_ALERT_WEBHOOK_0", "AMBIENTQ_ALERT_WEBHOOK_1"}:
            assert "secretKeyRef" in env[name]["valueFrom"], name
            assert "value" not in env[name]
        assert env["AMBIENTQ_ALERT_WEBHOOK_1"]["valueFrom"]["secretKeyRef"] == {"name": "ambientq-alerts",
                                                                                "key": "teamsUrl"}
        assert not any(v.get("persistentVolumeClaim") for v in spec["volumes"])  # Postgres: no PVC
        assert "affinity" not in spec
    assert "PersistentVolumeClaim" not in {d["kind"] for d in docs}
    ing = kind(docs, "Ingress")
    assert ing["spec"]["tls"][0]["secretName"] == "ambientq-tls" and ing["spec"]["ingressClassName"] == "nginx"
    assert kind(docs, "PodDisruptionBudget")["spec"]["minAvailable"] == 1
    assert kind(docs, "Deployment")["spec"]["replicas"] == 2


def test_proxy_mode_behind_oauth2_proxy(tmp_path):
    docs = render("-f", str(CHART / "ci" / "proxy-values.yaml"))
    cfg = load_config(docs, tmp_path)
    assert cfg.get("auth", "mode") == "proxy"
    assert cfg.data["auth"]["proxy"]["trusted_proxies"] == ["10.42.0.0/16"]
    assert cfg.data["auth"]["proxy"]["user_header"] == "X-Auth-Request-User"  # what ingress-nginx overwrites
    assert cfg.get("auth", "public_url") == "http://ambientq.example.invalid"
    assert "auth-url" in " ".join(kind(docs, "Ingress")["metadata"]["annotations"])


@pytest.mark.parametrize("args, message", [
    (["--set", "auth.mode=oidc"], "sessionSecret"),
    (["--set", "auth.mode=oidc", "--set", "auth.sessionSecret.secretName=s"], "issuer"),
    (["--set", "auth.mode=oidc", "--set", "auth.sessionSecret.secretName=s",
      "--set", "auth.oidc.issuer=https://i", "--set", "auth.oidc.clientId=c"], "publicUrl"),
    (["--set", "replicaCount=3"], "postgresUrlSecret"),
    (["--set", "auth.mode=magic"], r"auth[./]mode"),  # helm versions word schema errors differently
    (["--set", "alerts.webhooks[0].kind=slack"], "secretName"),
])
def test_invalid_values_fail(args, message):
    assert re.search(message, render_error(*args))
