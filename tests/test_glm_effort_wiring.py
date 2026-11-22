"""GLM efforts survive host overrides and Pi's supported-level clamping."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import subprocess
import threading

import pytest

import pulsar.launch.diet as diet_mod
import pulsar.launch.units as units_mod
from pulsar import models
from pulsar.runner.runner import Runner
from pulsar.vessel.events import VesselEvent
from pulsar.vessel.pi import PiRpcVessel, PiVessel, pi_thinking_level


LOCAL_GLM53 = [m for m in models.MODELS.values()
               if m.vessel == 'pi' and m.wire_model
               and m.wire_model.split('/', 1)[0].startswith(('glm', 'altglm'))
               and m.wire_model.split('/', 1)[1].lower().startswith(('glm-5.3', 'glm53'))]


@pytest.mark.parametrize('model', LOCAL_GLM53, ids=lambda m: m.id)
def test_every_local_glm53_offers_off_and_low(model):
    assert 'off' in model.efforts
    assert 'low' in model.efforts
    assert pi_thinking_level(model.id, 'off') == 'off'
    assert pi_thinking_level(model.wire_model, 'off') == 'off'
    assert pi_thinking_level(model.id, 'low') == 'low'
    assert pi_thinking_level(model.wire_model, 'low') == 'low'


def seeded_providers(tmp_path, monkeypatch, slot=None):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    # A stale host map used to override the repo's low slot and off switch,
    # and altglmflash / altglmfull / glm53flashexlr8 use id="glm53".
    host = tmp_path / '.pi/agent/models.json'
    host.parent.mkdir(parents=True)
    host.write_text(json.dumps({'providers': {
        'glm53local': {
            'baseUrl': 'http://127.0.0.1:1/v1', 'api': 'openai-completions',
            'compat': {'thinkingFormat': 'openai'},
            'models': [{'id': mid, 'reasoning': False,
                        'thinkingLevelMap': {'off': None, 'low': None},
                        'compat': {'thinkingFormat': 'openai'}}
                       for mid in ('glm-5.3', 'glm-5.3-flash')],
        },
        'glm53flashexlr8': {
            'baseUrl': 'http://127.0.0.1:1/v1', 'api': 'openai-completions',
            'models': [{'id': 'glm53', 'thinkingLevelMap': {'low': None}}],
        },
        'altglmflash': {
            'baseUrl': 'http://127.0.0.1:1/v1', 'api': 'openai-completions',
            'models': [{'id': 'glm53', 'thinkingLevelMap': {'low': None}}],
        },
        'altglmfull': {
            'baseUrl': 'http://127.0.0.1:1/v1', 'api': 'openai-completions',
            'models': [{'id': 'glm53', 'thinkingLevelMap': {'low': None}}],
        },
    }}))
    home = tmp_path / 'pulse'
    diet_mod._materialize_pi_diet('effort-test', home, [], slot=slot)
    return json.loads((home / 'pi-home/agent/models.json').read_text())['providers']


def test_glm_efforts_normalized_after_host_override(tmp_path, monkeypatch):
    providers = seeded_providers(
        tmp_path, monkeypatch,
        slot={'id': 'gpu-ring', 'served_id': 'glm53', 'base_url': 'http://127.0.0.1:1/v1'},
    )
    checked = 0
    for provider, config in providers.items():
        if not provider.startswith(('glm', 'altglm', 'gpu-ring')):
            continue
        for model in config['models']:
            if not model['id'].lower().startswith(('glm-5.3', 'glm53')):
                continue
            checked += 1
            assert model['reasoning'] is True
            assert model['thinkingLevelMap'] == {
                'minimal': None, 'low': 'low', 'medium': None,
                'high': 'high', 'xhigh': 'max',
            }
            compat = model.get('compat') or config.get('compat') or {}
            assert compat['thinkingFormat'] == 'qwen-chat-template'
    assert checked >= 6
    # No new low tier invented for GLM 5.2 or unrelated Qwen.
    glm52 = next(m for m in providers['glmlocal']['models'] if m['id'] == 'glm-5.2')
    assert glm52['thinkingLevelMap']['low'] is None


def test_installed_pi_generates_exact_glm_payloads(tmp_path, monkeypatch):
    module = (Path(units_mod.DEFAULT_PI_BIN).parent.parent /
              '@mariozechner/pi-ai/dist/providers/openai-completions.js')
    if not shutil.which('node') or not module.is_file():
        pytest.skip('installed Pi adapter required for offline payload capture')
    provider = seeded_providers(tmp_path, monkeypatch)['glm53local']
    # onPayload throws before HTTP; this checks the actual production adapter,
    # including its clampThinkingLevel, without contacting a model server.
    script = '''
      import {streamSimpleOpenAICompletions} from MODULE;
      const provider = PROVIDER;
      const payloads = [];
      for (const entry of provider.models) {
        for (const reasoning of ['off', 'low', 'high', 'xhigh']) {
          const model = {...entry, provider: 'glm53local',
            api: provider.api, baseUrl: provider.baseUrl, maxTokens: 128,
            input: ['text'], cost: {input: 0, output: 0, cacheRead: 0, cacheWrite: 0}};
          await streamSimpleOpenAICompletions(model, {messages: [
            {role: 'user', content: 'Hi', timestamp: 0}
          ]}, {apiKey: 'none', reasoning, onPayload(payload) {
            payloads.push({id: entry.id, reasoning, kwargs: payload.chat_template_kwargs});
            throw new Error('capture only; no HTTP');
          }}).result();
        }
      }
      console.log(JSON.stringify(payloads));
    '''.replace('MODULE', json.dumps(module.as_uri())).replace('PROVIDER', json.dumps(provider))
    result = subprocess.run(['node', '--input-type=module', '-e', script],
                            capture_output=True, text=True, check=True, timeout=20)
    payloads = json.loads(result.stdout)
    assert len(payloads) == 8
    for row in payloads:
        expected = {'enable_thinking': row['reasoning'] != 'off', 'preserve_thinking': True}
        if row['reasoning'] != 'off':
            expected['reasoning_effort'] = {'xhigh': 'max'}.get(row['reasoning'], row['reasoning'])
        assert row['kwargs'] == expected, row


def test_glm_reasoning_off_rewrites_wire_and_hides_thinking_in_chat(tmp_path, monkeypatch):
    """End-to-end c92 proof: real Pi CLI + effort.ts + PiRpcVessel + Runner.

    When GLM-5.3 runs with effort='off':
      1) effort.ts rewrites chat_template_kwargs from enable_thinking=false to
         {enable_thinking: true, preserve_thinking: true, reasoning_effort: 'low'}
         so backends keep <think>...</think> extraction active instead of
         streaming raw thoughts into content.
      2) Even when the backend streams reasoning_content chunks, PiRpcVessel
         converts reasoning_delta -> hidden_delta on --thinking off GLM-5.3
         sessions so Runner meters the tokens without writing reasoning.json or
         emitting any reasoning card or leaked thought text into chat.
    """
    pi_bin = Path(units_mod.DEFAULT_PI_BIN)
    if not pi_bin.is_file():
        pytest.skip('installed Pi CLI required for end-to-end wire/chat test')
    monkeypatch.setenv('PULSAR_PI_BIN', str(pi_bin))

    captured: list[dict] = []

    class FakeGlmHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get('Content-Length', '0'))
            body = json.loads(self.rfile.read(length))
            captured.append(body)
            chunks = [
                {'id': 'c92-1', 'object': 'chat.completion.chunk', 'model': body['model'],
                 'choices': [{'index': 0, 'delta': {'role': 'assistant',
                              'reasoning_content': 'LEAKED_INTERNAL_THOUGHT '},
                              'finish_reason': None}]},
                {'id': 'c92-1', 'object': 'chat.completion.chunk', 'model': body['model'],
                 'choices': [{'index': 0, 'delta': {'content': 'Clean answer 42.'},
                              'finish_reason': None}]},
                {'id': 'c92-1', 'object': 'chat.completion.chunk', 'model': body['model'],
                 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}],
                 'usage': {'prompt_tokens': 24, 'completion_tokens': 9, 'total_tokens': 33}},
            ]
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            for ch in chunks:
                self.wfile.write(f"data: {json.dumps(ch)}\n\n".encode('utf-8'))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    srv = ThreadingHTTPServer(('127.0.0.1', 0), FakeGlmHandler)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()

    try:
        host = tmp_path / 'host'
        host_cfg = host / '.pi/agent/models.json'
        host_cfg.parent.mkdir(parents=True)
        host_cfg.write_text(json.dumps({'providers': {
            'altglmflash': {
                'baseUrl': f'http://127.0.0.1:{port}/v1',
                'api': 'openai-completions',
                'apiKey': 'none',
                'models': [{'id': 'glm53', 'thinkingLevelMap': {'low': None}}],
            },
            'glmlocal': {
                'baseUrl': f'http://127.0.0.1:{port}/v1',
                'api': 'openai-completions',
                'apiKey': 'none',
                'models': [{'id': 'glm-5.2', 'reasoning': True,
                            'thinkingLevelMap': {'low': None, 'high': 'high', 'xhigh': 'max'},
                            'compat': {'thinkingFormat': 'qwen-chat-template'}}],
            },
        }}))
        monkeypatch.setattr(Path, 'home', lambda: host)

        async def run_turn(effort_val: str, sub: str, model_id: str = 'pi-alt-glm53-flash-tp4'):
            home = tmp_path / sub
            diet_mod._materialize_pi_diet(sub, home, [])
            cwd = home / 'cli'
            cwd.mkdir(parents=True, exist_ok=True)
            vessel = PiRpcVessel(model=model_id)
            runner = Runner(sub, home, model_id, 'pi', None)
            runner._events_fh = (home / 'events.jsonl').open('a', encoding='utf-8')
            runner._meter.start()
            sid = await vessel.spawn(
                cwd=str(cwd),
                model=model_id,
                effort=effort_val,
                env={'PI_CODING_AGENT_DIR': str(home / 'pi-home/agent')},
            )
            seen = []
            try:
                await vessel.send(sid, 'What is 6 * 7?')
                async for ev in vessel.subscribe(sid):
                    seen.append(ev)
                    runner._handle_event({'type': ev.kind, **ev.payload})
                    if ev.kind == 'done' and ev.payload.get('reason') == 'result':
                        break
            finally:
                runner._events_fh.close()
                await vessel.stop(sid)
            return home, seen

        home_off, evs_off = asyncio.run(run_turn('off', 'pulse-off'))
        assert len(captured) == 1
        assert captured[0]['model'] == 'glm53'
        assert captured[0]['chat_template_kwargs'] == {
            'enable_thinking': True,
            'preserve_thinking': True,
            'reasoning_effort': 'low',
        }
        kinds_off = [e.kind for e in evs_off]
        assert 'hidden_delta' in kinds_off
        assert 'reasoning_delta' not in kinds_off
        assert not (home_off / 'reasoning.json').exists()
        assert not (home_off / 'traces.jsonl').exists()
        durable_off = [json.loads(line) for line in
                       (home_off / 'events.jsonl').read_text().splitlines()]
        assert not any(e['type'] == 'reasoning' for e in durable_off)
        texts_off = [e['text'] for e in durable_off if e['type'] == 'assistant_text']
        assert texts_off == ['Clean answer 42.']
        assert 'LEAKED_INTERNAL_THOUGHT' not in (home_off / 'events.jsonl').read_text()

        # PiVessel (-p --mode json) shares the same _deliver suppression.
        pv = PiVessel(model='pi-glm53-full')
        pv_home = tmp_path / 'pv-off'
        diet_mod._materialize_pi_diet('pv-off', pv_home, [])
        pv_cwd = pv_home / 'cli'
        pv_cwd.mkdir(parents=True, exist_ok=True)
        pv_sid = asyncio.run(pv.spawn(cwd=str(pv_cwd), model='pi-glm53-full', effort='off'))
        pv_sess = pv._sessions[pv_sid]
        pv._build_command(pv_sess, 'hi')
        assert pv_sess.suppress_reasoning is True
        pv._deliver(pv_sess, VesselEvent(kind='reasoning_delta', payload={'text': 'secret'}))
        delivered = pv_sess.event_queue.get_nowait()
        assert delivered.kind == 'hidden_delta'

        # Control: effort='low' on GLM-5.3 still emits reasoning cards normally,
        # and GLM-5.2 effort='off' keeps its native enable_thinking=False switch.
        home_low, evs_low = asyncio.run(run_turn('low', 'pulse-low'))
        assert captured[1]['chat_template_kwargs'] == {
            'enable_thinking': True,
            'preserve_thinking': True,
            'reasoning_effort': 'low',
        }
        assert any(e.kind == 'reasoning_delta' for e in evs_low)
        durable_low = [json.loads(line) for line in
                       (home_low / 'events.jsonl').read_text().splitlines()]
        assert any(e['type'] == 'reasoning' and 'LEAKED_INTERNAL_THOUGHT' in e['text']
                   for e in durable_low)

        asyncio.run(run_turn('off', 'pulse-glm52', model_id='pi-glm-52'))
        assert captured[2]['model'] == 'glm-5.2'
        assert captured[2]['chat_template_kwargs'] == {
            'enable_thinking': False,
            'preserve_thinking': True,
        }
    finally:
        srv.shutdown()
        srv.server_close()
