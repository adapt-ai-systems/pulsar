"""Distinct stock/uncensored routes and offline serving prerequisites."""
import json
from pathlib import Path
import pulsar.launch.diet as diet_mod
from pulsar import models
from pulsar.vessel.pi import _CLI, pi_thinking_level


ID = 'pi-glm-exl3-uncensored-tp4'
WIRE = 'glm53exl3tp4uncensored/GLM-5.3-Flash-EXL3-Uncensored'


def test_registered_uncensored_model_keeps_reasoning_and_rpc():
    m = models.model_by_id(ID)
    assert m.wire_model == WIRE and m.pi_rpc and m.vessel == 'pi'
    assert m.default_effort == 'high' and m.default_handoff == 225000
    assert _CLI[ID] == WIRE
    for key in (ID, WIRE):
        assert pi_thinking_level(key, None) == 'high'
        assert pi_thinking_level(key, 'off') == 'off'
        assert pi_thinking_level(key, 'max') == 'xhigh'


def test_seeded_provider_is_distinct_without_host_config(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    home = tmp_path / 'pulse'
    diet_mod._materialize_pi_diet('uncensored-test', home, [])
    p = json.loads((home / 'pi-home/agent/models.json').read_text())['providers']['glm53exl3tp4uncensored']
    assert p['baseUrl'] == 'http://192.0.2.10:8888/v1'
    assert p['models'][0]['id'] == WIRE.split('/', 1)[1]
    assert p['models'][0]['contextWindow'] == 850000
    assert p['models'][0]['reasoning'] is True
