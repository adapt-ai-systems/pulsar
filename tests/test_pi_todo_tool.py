"""pi gets the todo_write tool: the bundled extension + the prompt line reach
both pi launch modes (json and rpc)."""
import asyncio
import json

import pytest

from pulsar.vessel.pi import (PiVessel, PiRpcVessel, PI_TODO_EXTENSION,
                              PI_TODO_PROMPT, pi_todo_args)
from tests.test_vessel_pi import _shim_env as json_shim, _collect_turn
from tests.test_vessel_pi_rpc import _shim_env as rpc_shim


def test_todo_extension_is_bundled():
    assert PI_TODO_EXTENSION.is_file()
    src = PI_TODO_EXTENSION.read_text()
    assert 'name: "todo_write"' in src
    assert pi_todo_args()[:2] == ['-e', str(PI_TODO_EXTENSION)]


def test_missing_extension_degrades_to_no_args(monkeypatch, tmp_path):
    import pulsar.vessel.pi as pi
    monkeypatch.setattr(pi, 'PI_TODO_EXTENSION', tmp_path / 'nope.ts')
    assert pi_todo_args() == []


@pytest.mark.parametrize('rpc', [False, True])
def test_todo_tool_reaches_both_pi_launch_modes(tmp_path, monkeypatch, rpc):
    ws = tmp_path / 'pulse' / 'cli'
    ws.mkdir(parents=True)
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
    assert args[args.index('-e') + 1] == str(PI_TODO_EXTENSION)
    appends = [args[i + 1] for i, a in enumerate(args) if a == '--append-system-prompt']
    assert PI_TODO_PROMPT in appends
