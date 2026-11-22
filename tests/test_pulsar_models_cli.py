"""Tests for ops/bin/pulsar-models (presets, add, merge, and probing).

All filesystem writes go exclusively to tmp_path via env overrides; the real
~/.pi and repository files are never touched.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from pulsar import models as reg

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "ops" / "bin" / "pulsar-models"
SHIPPED_REGISTRY = reg.SHIPPED_REGISTRY_PATH
SHIPPED_PRESETS = SHIPPED_REGISTRY.with_name("models.presets.json")


def _run_cli(args: list[str], *, reg_path: Path, pi_path: Path,
             presets_path: Path = SHIPPED_PRESETS) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "PULSAR_REPO": str(ROOT),
        "PULSAR_MODELS_PATH": str(reg_path),
        "PULSAR_PRESETS_PATH": str(presets_path),
        "PULSAR_PI_MODELS_PATH": str(pi_path),
    }
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        capture_output=True, text=True, env=env, timeout=30,
    )


def test_presets_subcommand_lists_all_eight_presets(tmp_path):
    reg_file = tmp_path / "models.json"
    reg_file.write_text(SHIPPED_REGISTRY.read_text(encoding="utf-8"), encoding="utf-8")
    pi_file = tmp_path / "pi-models.json"

    r = _run_cli(["presets", "--json"], reg_path=reg_file, pi_path=pi_file)
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout)
    assert set(data) == {
        "pi-glm53-full", "pi-glm-flash", "pi-qwen38-flash", "pi-qwen27",
        "pi-ds41-flash", "codex-ds4-flash", "local-openai", "local-slot",
    }

    r_txt = _run_cli(["presets"], reg_path=reg_file, pi_path=pi_file)
    assert r_txt.returncode == 0
    assert "pi-glm53-full" in r_txt.stdout
    assert "codex-ds4-flash" in r_txt.stdout


def test_add_pi_preset_writes_registry_and_merges_pi_config(tmp_path):
    reg_file = tmp_path / "models.json"
    reg_file.write_text(SHIPPED_REGISTRY.read_text(encoding="utf-8"), encoding="utf-8")
    pi_file = tmp_path / "pi-agent" / "models.json"
    pi_file.parent.mkdir(parents=True)
    # Pre-existing unrelated provider + top-level key must survive the merge.
    pi_file.write_text(json.dumps({
        "customTop": "keep-me",
        "providers": {
            "other": {
                "baseUrl": "https://api.example.com/v1",
                "api": "openai-completions",
                "apiKey": "secret-stays",
                "models": [{"id": "m1", "name": "Other"}],
            }
        },
    }), encoding="utf-8")

    # 1. Add GLM-5.3 Full
    r1 = _run_cli(
        ["add", "pi-glm53-full", "--url", "http://192.0.2.50:8000/v1"],
        reg_path=reg_file, pi_path=pi_file,
    )
    assert r1.returncode == 0, r1.stderr
    assert "./scripts/build-live.sh" in r1.stdout
    assert "systemctl --user restart pulsar-daemon.service pulsar-web.service" in r1.stdout
    assert '"apiKey"' in r1.stdout

    # 2. Add GLM-5.3 Flash (shares provider glm53local) — both models must coexist
    r2 = _run_cli(
        ["add", "pi-glm-flash", "--url", "http://192.0.2.50:8000/v1"],
        reg_path=reg_file, pi_path=pi_file,
    )
    assert r2.returncode == 0, r2.stderr

    loaded = reg._load(reg_file)
    assert "pi-glm53-full" in loaded["models"]
    assert "pi-glm-flash" in loaded["models"]
    assert loaded["models"]["pi-glm53-full"].max_concurrent == 2
    assert loaded["models"]["pi-glm53-full"].prompt_notes == ("vision",)
    assert loaded["models"]["pi-glm-flash"].default_effort == "low"

    pi_doc = json.loads(pi_file.read_text(encoding="utf-8"))
    assert pi_doc["customTop"] == "keep-me"
    assert pi_doc["providers"]["other"]["apiKey"] == "secret-stays"
    glm_prov = pi_doc["providers"]["glm53local"]
    assert glm_prov["baseUrl"] == "http://192.0.2.50:8000/v1"
    assert {m["id"] for m in glm_prov["models"]} == {"glm-5.3", "glm-5.3-flash"}


def test_add_respects_custom_id_served_id_and_duplicate_guard(tmp_path):
    reg_file = tmp_path / "models.json"
    reg_file.write_text(SHIPPED_REGISTRY.read_text(encoding="utf-8"), encoding="utf-8")
    pi_file = tmp_path / "pi-models.json"

    r = _run_cli(
        ["add", "local-openai", "--url", "http://192.0.2.60:9000/v1",
         "--id", "my-local", "--served-id", "llama-4-70b", "--label", "My Local"],
        reg_path=reg_file, pi_path=pi_file,
    )
    assert r.returncode == 0, r.stderr

    loaded = reg._load(reg_file)
    m = loaded["models"]["my-local"]
    assert m.label == "My Local"
    assert m.wire_model == "local/llama-4-70b"
    assert m.thinking == "passthrough"

    pi_doc = json.loads(pi_file.read_text(encoding="utf-8"))
    assert pi_doc["providers"]["local"]["baseUrl"] == "http://192.0.2.60:9000/v1"
    assert pi_doc["providers"]["local"]["models"][0]["id"] == "llama-4-70b"

    # Duplicate without --force fails cleanly
    dup = _run_cli(
        ["add", "local-openai", "--url", "http://192.0.2.60:9000/v1", "--id", "my-local"],
        reg_path=reg_file, pi_path=pi_file,
    )
    assert dup.returncode != 0
    assert "already exists" in dup.stderr


def test_add_codex_and_slot_presets_do_not_touch_pi_config(tmp_path):
    reg_file = tmp_path / "models.json"
    reg_file.write_text(SHIPPED_REGISTRY.read_text(encoding="utf-8"), encoding="utf-8")
    pi_file = tmp_path / "pi-models.json"

    r_codex = _run_cli(
        ["add", "codex-ds4-flash", "--url", "http://192.0.2.70:8000/v1",
         "--served-id", "deepseek-v4-pro"],
        reg_path=reg_file, pi_path=pi_file,
    )
    assert r_codex.returncode == 0, r_codex.stderr

    r_slot = _run_cli(
        ["add", "local-slot", "--url", "http://192.0.2.71:8000/v1"],
        reg_path=reg_file, pi_path=pi_file,
    )
    assert r_slot.returncode == 0, r_slot.stderr

    assert not pi_file.exists()
    loaded = reg._load(reg_file)
    ds4 = loaded["models"]["codex-ds4-flash"]
    assert ds4.vessel == "codex"
    assert ds4.base_url == "http://192.0.2.70:8000/v1"
    assert ds4.wire_model == "deepseek-v4-pro"
    assert ds4.prompt_notes == ("ds4_clarity",)

    slot = loaded["models"]["local-slot"]
    assert slot.slot is True
    assert slot.base_url == "http://192.0.2.71:8000/v1"


def test_status_probe_sends_provider_api_key(tmp_path):
    """A keyed local server must read LIVE, not DOWN: the probe sends the pi apiKey."""
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.headers.get("Authorization") != "Bearer test-key-123":
                self.send_response(401)
                self.end_headers()
                return
            body = json.dumps({"data": [{"id": "my-model"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/v1"
        reg_file = tmp_path / "models.json"
        reg_file.write_text(SHIPPED_REGISTRY.read_text(encoding="utf-8"), encoding="utf-8")
        pi_file = tmp_path / "pi-models.json"
        r_add = _run_cli(["add", "local-openai", "--url", url, "--served-id", "my-model"],
                         reg_path=reg_file, pi_path=pi_file)
        assert r_add.returncode == 0, r_add.stderr

        r_nokey = _run_cli(["--check"], reg_path=reg_file, pi_path=pi_file)
        assert r_nokey.returncode == 1 and "DOWN" in r_nokey.stdout

        doc = json.loads(pi_file.read_text(encoding="utf-8"))
        for prov in doc["providers"].values():
            prov["apiKey"] = "test-key-123"
        pi_file.write_text(json.dumps(doc), encoding="utf-8")
        r_key = _run_cli(["--check"], reg_path=reg_file, pi_path=pi_file)
        assert r_key.returncode == 0, r_key.stdout
        assert "LIVE" in r_key.stdout and "DOWN" not in r_key.stdout
    finally:
        srv.shutdown()


def test_help_and_removed_write_skill_flag(tmp_path):
    reg_file = tmp_path / "models.json"
    reg_file.write_text(SHIPPED_REGISTRY.read_text(encoding="utf-8"), encoding="utf-8")
    pi_file = tmp_path / "pi-models.json"

    r_help = _run_cli(["--help"], reg_path=reg_file, pi_path=pi_file)
    assert r_help.returncode == 0, r_help.stderr
    assert "--write-skill" not in r_help.stdout

    r_bad = _run_cli(["--write-skill"], reg_path=reg_file, pi_path=pi_file)
    assert r_bad.returncode != 0
