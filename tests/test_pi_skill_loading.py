"""Selected Pi skills must reach the prompt, not merely a catalog label."""
import asyncio
import json
from pathlib import Path

import pytest

from pulsar.vessel.pi import PiVessel, PiRpcVessel, VesselError
from tests.test_vessel_pi import _shim_env as json_shim, _collect_turn
from tests.test_vessel_pi_rpc import _shim_env as rpc_shim


def workspace(tmp_path):
    ws = tmp_path / 'pulse' / 'cli'
    ws.mkdir(parents=True)
    (ws / 'PULSAR.md').write_text('PULSE_IDENTITY\nselected [loaded]')
    for name in ('selected', 'ambient'):
        skill = ws / '.claude' / 'skills' / name
        skill.mkdir(parents=True)
        (skill / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: short\n---\n{name.upper()}_FULL_BODY_SENTINEL\n')
    (ws.parent / 'diet.json').write_text(json.dumps({'clean': True, 'skills': ['selected']}))
    return ws


@pytest.mark.parametrize('rpc', [False, True])
def test_selected_full_body_reaches_both_pi_launch_modes(tmp_path, monkeypatch, rpc):
    ws = workspace(tmp_path)
    dump = tmp_path / 'argv.json'
    (rpc_shim if rpc else json_shim)(monkeypatch, tmp_path)
    monkeypatch.setenv('FAKE_PI_RPC_ARGV_DUMP' if rpc else 'FAKE_PI_ARGV_DUMP', str(dump))

    async def run():
        vessel = (PiRpcVessel if rpc else PiVessel)()
        sid = await vessel.spawn(cwd=str(ws), initial_prompt='hi')
        try:
            await asyncio.wait_for(_collect_turn(vessel.subscribe(sid)), timeout=15)
        finally:
            await vessel.stop(sid)
    asyncio.run(run())
    args = json.loads(dump.read_text())
    appends = [args[i + 1] for i, arg in enumerate(args) if arg == '--append-system-prompt']
    delivered = '\n'.join(Path(a).read_text() if Path(a).is_file() else a for a in appends)
    assert 'PULSE_IDENTITY' in delivered
    assert 'SELECTED_FULL_BODY_SENTINEL' in delivered
    assert 'AMBIENT_FULL_BODY_SENTINEL' not in delivered
    assert str(ws / '.claude' / 'skills' / 'selected') in delivered  # resource base
    assert '--no-skills' in args and '-nc' in args  # ambient discovery still off


def test_refresh_removes_deselected_skill_and_deduplicates(tmp_path):
    from pulsar.vessel.pi import pi_skill_args
    ws = workspace(tmp_path)
    diet = ws.parent / 'diet.json'
    diet.write_text(json.dumps({'clean': True, 'skills': ['selected', 'selected']}))
    args = pi_skill_args(ws)
    assert args.count(str(ws / '.claude' / 'skills' / 'selected' / 'SKILL.md')) == 1
    diet.write_text(json.dumps({'clean': True, 'skills': []}))
    assert pi_skill_args(ws) == []  # stale link does not count as selected
    diet.unlink()
    assert pi_skill_args(ws) == []


@pytest.mark.parametrize('skills', [['../escape'], ['missing'], ['selected']])
def test_bad_selected_skill_fails_visibly(tmp_path, skills, caplog):
    import logging
    from pulsar.vessel.pi import pi_skill_args
    ws = workspace(tmp_path)
    if skills == ['selected']:
        (ws / '.claude' / 'skills' / 'selected' / 'SKILL.md').unlink()
    (ws.parent / 'diet.json').write_text(json.dumps({'clean': True, 'skills': skills}))
    if skills == ['missing']:
        # The spawner skips a skill whose directory does not exist (logged
        # ERROR), so the vessel must skip it too instead of refusing to spawn
        # the whole pulse.
        with caplog.at_level(logging.WARNING, logger='pulsar.vessel.pi'):
            assert pi_skill_args(ws) == []
        assert 'missing' in caplog.text
        return
    with pytest.raises(VesselError, match='skill'):
        pi_skill_args(ws)


def test_full_fat_diet_unchanged(tmp_path):
    from pulsar.vessel.pi import pi_skill_args
    ws = workspace(tmp_path)
    (ws.parent / 'diet.json').write_text(json.dumps({'clean': False, 'skills': ['selected']}))
    assert pi_skill_args(ws) == []


@pytest.mark.parametrize('rpc', [False, True])
def test_real_pi_receives_bodies_before_any_model_call(tmp_path, monkeypatch, rpc):
    """Real CLI builds its prompt; exit in before_agent_start, no inference/API."""
    import os
    import shutil
    from pulsar.launch.units import DEFAULT_PI_BIN
    binary = (os.environ.get('PULSAR_PI_BIN') or shutil.which('pi')
              or DEFAULT_PI_BIN)
    if not Path(binary).is_file():
        pytest.skip('real Pi CLI not installed')
    ws = workspace(tmp_path)
    agent = tmp_path / 'private-agent'
    agent.mkdir()
    (agent / 'models.json').write_text(json.dumps({'providers': {'probe': {
        'baseUrl': 'http://127.0.0.1:1/v1', 'api': 'openai-completions',
        'apiKey': 'none', 'models': [{'id': 'probe', 'name': 'probe'}]}}}))
    output = tmp_path / 'actual-system-prompt.txt'
    extension = tmp_path / 'capture.ts'
    extension.write_text('import { writeFileSync } from "node:fs";\n'
        'export default function(pi) { pi.on("before_agent_start", (event) => {\n'
        f'writeFileSync({json.dumps(str(output))}, event.systemPrompt);\n'
        'process.exit(0); }); }\n')
    (ws.parent / 'pi-args.json').write_text(json.dumps(['-e', str(extension), '--no-builtin-tools']))
    monkeypatch.setenv('PULSAR_PI_BIN', str(binary))
    monkeypatch.setenv('PI_CODING_AGENT_DIR', str(agent))

    async def run():
        vessel = (PiRpcVessel if rpc else PiVessel)(model='probe/probe')
        sid = await vessel.spawn(cwd=str(ws), initial_prompt='Check loaded instructions.')
        try:
            await asyncio.wait_for(_collect_turn(vessel.subscribe(sid)), timeout=30)
        finally:
            await vessel.stop(sid)
    asyncio.run(run())
    prompt = output.read_text()
    assert 'PULSE_IDENTITY' in prompt
    assert prompt.count('SELECTED_FULL_BODY_SENTINEL') == 1
    assert 'AMBIENT_FULL_BODY_SENTINEL' not in prompt
    assert str(ws / '.claude' / 'skills' / 'selected') in prompt


# ---- skill delivery arms ---------------------------------------------------

def _appends(args):
    return [args[i + 1] for i, a in enumerate(args) if a == '--append-system-prompt']


def test_arm_a_default_appends_every_selected_body(tmp_path):
    from pulsar.vessel.pi import pi_skill_args
    ws = workspace(tmp_path)
    (ws.parent / 'diet.json').write_text(json.dumps({'clean': True, 'skills': ['selected', 'ambient']}))
    bodies = [a for a in _appends(pi_skill_args(ws)) if a.endswith('SKILL.md')]
    assert [Path(b).parent.name for b in bodies] == ['selected', 'ambient']


def test_arm_b_empty_list_sends_read_lines_not_bodies(tmp_path):
    from pulsar.vessel.pi import pi_skill_args
    ws = workspace(tmp_path)
    (ws.parent / 'diet.json').write_text(json.dumps({'clean': True, 'skills': ['selected', 'ambient']}))
    (ws.parent / 'pi-skill-bodies.json').write_text('[]')
    appends = _appends(pi_skill_args(ws))
    assert not any(a.endswith('SKILL.md') for a in appends)
    for name in ('selected', 'ambient'):
        path = ws / '.claude' / 'skills' / name / 'SKILL.md'
        assert any(f'NOT loaded: read {path} before working' in a for a in appends), name


def test_arm_c_named_bodies_only(tmp_path):
    from pulsar.vessel.pi import pi_skill_args
    ws = workspace(tmp_path)
    (ws.parent / 'diet.json').write_text(json.dumps({'clean': True, 'skills': ['selected', 'ambient']}))
    (ws.parent / 'pi-skill-bodies.json').write_text('["ambient"]')
    appends = _appends(pi_skill_args(ws))
    assert [Path(a).parent.name for a in appends if a.endswith('SKILL.md')] == ['ambient']
    assert any('Selected Pulsar skill: selected. Its SKILL.md is NOT loaded' in a for a in appends)


@pytest.mark.parametrize('content', ['{"bodies": []}', 'not json', '["ok", 3]'])
def test_malformed_bodies_file_falls_back_to_arm_a(tmp_path, content):
    from pulsar.vessel.pi import pi_skill_args
    ws = workspace(tmp_path)
    (ws.parent / 'pi-skill-bodies.json').write_text(content)
    assert any(a.endswith('SKILL.md') for a in _appends(pi_skill_args(ws)))
