from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from ambientq.config import Config
from ambientq.demo.mock_llm import MockLLM
from ambientq.harness import Harness
from ambientq.llm import LLMClient
from ambientq.store import SQLStore

DEMO_HARNESS = Path(__file__).resolve().parents[1] / "src" / "ambientq" / "demo" / "harness"


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout


@pytest.fixture
def harness_dir(tmp_path: Path) -> Path:
    repo = tmp_path / "agent-repo"
    shutil.copytree(DEMO_HARNESS, repo / "harness")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    return repo / "harness"


@pytest.fixture
def harness(harness_dir: Path) -> Harness:
    return Harness.load(harness_dir)


@pytest.fixture
def store() -> SQLStore:
    s = SQLStore("sqlite://:memory:")
    yield s
    s.close()


@pytest.fixture
def cfg(harness_dir: Path) -> Config:
    return Config.load(overrides={
        "store": {"url": "sqlite://:memory:"},
        "harness": {"path": str(harness_dir)},
        "llm": {"base_url": "http://mock/v1", "model": "mock-judge"},
        "sampling": {"rate": 1.0},
    })


@pytest.fixture
def mock() -> MockLLM:
    return MockLLM()


@pytest.fixture
def llm(mock: MockLLM) -> LLMClient:
    return LLMClient("http://mock/v1", "mock-judge", transport=mock.transport)
