"""Regression tests for release hygiene fixes (HYG-01..HYG-11, INST-1)."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from pulsar.launch import diet as diet_mod

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_daemon_main_sets_077_umask(monkeypatch):
    import pulsar.daemon.__main__ as daemon_main

    seen = {}
    old_umask = os.umask(0o022)
    try:
        def fake_create_app():
            cur = os.umask(0o077)
            os.umask(cur)
            seen["umask"] = cur
            return object()

        monkeypatch.setattr(daemon_main, "create_app", fake_create_app)
        monkeypatch.setattr(daemon_main.uvicorn, "run", lambda *a, **kw: None)
        daemon_main.main()
        assert seen["umask"] == 0o077
    finally:
        os.umask(old_umask)


def test_runner_main_keeps_umask_and_makes_own_files_private(tmp_path, monkeypatch):
    # The agent tool is a child of the runner. Files it writes in the user's
    # projects must keep the user's umask, so the runner must not change it.
    # The runner's own directory and log are still owner-only.
    from pulsar.runner import runner as runner_mod

    seen = {}
    old_umask = os.umask(0o022)
    try:
        async def fake_run(self):
            cur = os.umask(0o077)
            os.umask(cur)
            seen["umask"] = cur
            return 0

        monkeypatch.setattr(runner_mod.Runner, "run", fake_run)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "pulsar.runner",
                "--pulse-id",
                "p1",
                "--home",
                str(tmp_path / "pulse-home"),
                "--model",
                "claude-sonnet-5-5",
            ],
        )
        with pytest.raises(SystemExit) as exc:
            runner_mod.main()
        assert exc.value.code == 0
        assert seen["umask"] == 0o022
        assert stat.S_IMODE((tmp_path / "pulse-home").stat().st_mode) == 0o700
        assert stat.S_IMODE((tmp_path / "pulse-home" / "runner.log").stat().st_mode) == 0o600
    finally:
        os.umask(old_umask)


def test_pi_and_grok_diet_modes_owner_only(tmp_path, monkeypatch):
    old_umask = os.umask(0o022)
    try:
        fake_home = tmp_path / "user"
        (fake_home / ".pi" / "agent").mkdir(parents=True)
        (fake_home / ".pi" / "agent" / "models.json").write_text(
            json.dumps({"providers": {"demo": {"apiKey": "dummy-token"}}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

        pulse_home = tmp_path / "pulse"
        diet_mod._materialize_pi_diet("p1", pulse_home, [])
        pi_dir = pulse_home / "pi-home"
        agent_dir = pi_dir / "agent"
        models_file = agent_dir / "models.json"
        assert stat.S_IMODE(pi_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(agent_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(models_file.stat().st_mode) == 0o600

        diet_mod._materialize_grok_diet("p1", pulse_home, [])
        grok_dir = pulse_home / "grok-home"
        grok_cfg = grok_dir / "config.toml"
        assert stat.S_IMODE(grok_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(grok_cfg.stat().st_mode) == 0o600
    finally:
        os.umask(old_umask)


def test_systemd_unit_templates_handle_spaces_and_percent(tmp_path):
    install_text = (REPO_ROOT / "install.sh").read_text(encoding="utf-8")
    assert "@REPO_DIR_QUOTED@" in install_text
    assert 'replace("%", "%%")' in install_text

    def esc_spec(val: str) -> str:
        return val.replace("%", "%%")

    def esc_quoted(val: str) -> str:
        return val.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")

    base = tmp_path / "my pulsar 100%u"
    (base / "venv" / "bin").mkdir(parents=True)
    (base / "web" / "scripts").mkdir(parents=True)
    py_exe = base / "venv" / "bin" / "python"
    py_exe.write_text("#!/bin/sh\nexit 0\n")
    py_exe.chmod(0o755)
    build_sh = base / "web" / "scripts" / "build-live.sh"
    build_sh.write_text("#!/bin/sh\nexit 0\n")
    build_sh.chmod(0o755)
    node_exe = base / "node"
    node_exe.write_text("#!/bin/sh\nexit 0\n")
    node_exe.chmod(0o755)

    data_dir = str(base / "data dir")
    cfg_dir = str(base / "cfg dir")
    service_path = f"{base}/bin:/usr/bin:/bin"
    replacements = {
        "@REPO_DIR_QUOTED@": esc_quoted(str(base)),
        "@VENV_DIR_QUOTED@": esc_quoted(str(base / "venv")),
        "@DATA_DIR_QUOTED@": esc_quoted(data_dir),
        "@CONFIG_DIR_QUOTED@": esc_quoted(cfg_dir),
        "@NODE_BIN_QUOTED@": esc_quoted(str(node_exe)),
        "@SERVICE_PATH_QUOTED@": esc_quoted(service_path),
        "@REPO_DIR@": esc_spec(str(base)),
        "@VENV_DIR@": esc_spec(str(base / "venv")),
        "@DATA_DIR@": esc_spec(data_dir),
        "@CONFIG_DIR@": esc_spec(cfg_dir),
        "@DAEMON_PORT@": "8726",
        "@WEB_PORT@": "5292",
        "@NODE_BIN@": esc_spec(str(node_exe)),
        "@SERVICE_PATH@": esc_spec(service_path),
    }

    rendered = []
    for unit in ("pulsar-daemon.service", "pulsar-web.service"):
        text = (REPO_ROOT / "ops" / "systemd" / f"{unit}.in").read_text(encoding="utf-8")
        for k, v in replacements.items():
            text = text.replace(k, v)
        assert "@" not in text
        out = tmp_path / unit
        out.write_text(text, encoding="utf-8")
        rendered.append(str(out))

    if shutil.which("systemd-analyze"):
        proc = subprocess.run(
            ["systemd-analyze", "verify", *rendered],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr


def test_build_live_uses_user_lock_and_checks_cd():
    text = (REPO_ROOT / "web" / "scripts" / "build-live.sh").read_text(encoding="utf-8")
    assert "/tmp/pulsar-web-build.lock" not in text
    assert 'cd "$(dirname "$0")/.." || exit 1' in text
    assert "XDG_RUNTIME_DIR" in text


def test_requirements_txt_pins_runtime_deps_and_used_by_installer():
    req = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    lines = [
        line.strip()
        for line in req.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert lines, "requirements.txt must not be empty"
    names = set()
    for line in lines:
        assert "==" in line, f"unpinned requirement: {line}"
        names.add(line.split("==", 1)[0].lower())
    for required in ("fastapi", "uvicorn", "websockets", "python-multipart", "httpx"):
        assert required in names
    install_text = (REPO_ROOT / "install.sh").read_text(encoding="utf-8")
    assert 'requirements.txt' in install_text
    assert '--upgrade pip' not in install_text


def test_pi_tuned_installer_disables_third_party_scripts():
    text = (REPO_ROOT / "addons" / "pi-tuned" / "install.sh").read_text(encoding="utf-8")
    assert "npm ci --ignore-scripts" in text
    assert "--ignore-scripts=false" not in text


def test_pnpm_workspace_config_for_pnpm9_and_latest():
    ws = (REPO_ROOT / "web" / "pnpm-workspace.yaml").read_text(encoding="utf-8")
    assert "packages:" in ws
    assert "onlyBuiltDependencies:" in ws
    assert "allowBuilds:" in ws
    assert "esbuild" in ws
    pkg = json.loads((REPO_ROOT / "web" / "package.json").read_text(encoding="utf-8"))
    assert "pnpm" not in pkg


def test_install_sh_checks_all_prerequisites_in_one_pass(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()

    def make_stub(name: str, body: str) -> None:
        p = fake_bin / name
        p.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        p.chmod(0o755)

    # Link essential shell tools so bash and the script run, while omitting
    # systemctl, systemd-run, pnpm, and npm, and providing old python3 + node.
    for tool in ("bash", "uname", "id", "sed", "cut", "awk", "getent", "flock", "dirname", "pwd"):
        src = shutil.which(tool)
        if src:
            os.symlink(src, fake_bin / tool)

    make_stub("python3", "echo 3.10.12")
    make_stub("node", "echo v12.22.9")

    env = {
        "PATH": str(fake_bin),
        "HOME": str(tmp_path / "home"),
    }
    (tmp_path / "home").mkdir()

    proc = subprocess.run(
        [str(fake_bin / "bash"), str(REPO_ROOT / "install.sh"), "--with-pi-tuned"],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    assert proc.returncode == 1
    out = proc.stdout
    assert "[OK]   OS: Linux" in out
    assert "[FAIL] systemd:" in out
    assert "[FAIL] Python: found 3.10.12" in out
    assert "[FAIL] Node.js: found v12.22.9" in out
    assert "need >= 20.6.0" in out
    assert "[FAIL] pnpm: not found" in out
    assert "[FAIL] npm: not found" in out
    assert "prerequisites check failed" in proc.stderr


def test_build_live_rebuilds_when_symlinked_models_json_changes(tmp_path):
    # Mimic web/ layout with src/lib/models.json -> ../../pulsar/models.json
    pulsar_dir = tmp_path / "pulsar"
    web_dir = tmp_path / "web"
    (pulsar_dir).mkdir()
    (web_dir / "src" / "lib").mkdir(parents=True)
    (web_dir / "static").mkdir(parents=True)
    (web_dir / "scripts").mkdir(parents=True)
    (web_dir / "node_modules" / ".bin").mkdir(parents=True)

    models_target = pulsar_dir / "models.json"
    models_target.write_text('{"models": []}\n', encoding="utf-8")
    os.symlink(os.path.relpath(models_target, web_dir / "src" / "lib"), web_dir / "src" / "lib" / "models.json")

    for name in ("svelte.config.js", "vite.config.js", "server.js", "package.json", "pnpm-lock.yaml"):
        (web_dir / name).write_text("// stub\n", encoding="utf-8")

    build_script = web_dir / "scripts" / "build-live.sh"
    shutil.copy2(REPO_ROOT / "web" / "scripts" / "build-live.sh", build_script)

    count_file = tmp_path / "vite-runs"
    vite = web_dir / "node_modules" / ".bin" / "vite"
    vite.write_text(
        f"#!/bin/sh\necho run >> '{count_file}'\nmkdir -p live-next\ntouch live-next/index.js\n",
        encoding="utf-8",
    )
    vite.chmod(0o755)

    env = {**os.environ, "XDG_RUNTIME_DIR": str(tmp_path / "run")}
    (tmp_path / "run").mkdir()

    def run_build() -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(build_script)],
            cwd=web_dir,
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )

    # First build creates web/live
    r1 = run_build()
    assert r1.returncode == 0, r1.stderr
    assert "live copy updated" in r1.stdout
    assert len(count_file.read_text().splitlines()) == 1

    # Immediate rerun with no changes must skip rebuild
    r2 = run_build()
    assert r2.returncode == 0, r2.stderr
    assert "live copy is current" in r2.stdout
    assert len(count_file.read_text().splitlines()) == 1

    # Backdate live/.built (and the symlink itself) to simulate an earlier build,
    # then atomically replace the symlink target at the current time (as pulsar-models add does).
    past = (web_dir / "live" / ".built").stat().st_mtime - 5.0
    os.utime(web_dir / "live" / ".built", (past, past))
    os.utime(web_dir / "src" / "lib" / "models.json", (past, past), follow_symlinks=False)

    new_tmp = pulsar_dir / ".tmp-models.json"
    new_tmp.write_text('{"models": [{"id": "local-test"}]}\n', encoding="utf-8")
    os.replace(new_tmp, models_target)

    r3 = run_build()
    assert r3.returncode == 0, r3.stderr
    assert "live copy updated" in r3.stdout
    assert len(count_file.read_text().splitlines()) == 2

    # Rerun with no further changes must skip rebuild again
    r4 = run_build()
    assert r4.returncode == 0, r4.stderr
    assert "live copy is current" in r4.stdout
    assert len(count_file.read_text().splitlines()) == 2
