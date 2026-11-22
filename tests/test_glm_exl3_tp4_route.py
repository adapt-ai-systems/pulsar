"""A dedicated TP4 picker keeps TP2 routing and GLM reasoning behavior."""
from pulsar import models
from pulsar.vessel.pi import _CLI, pi_thinking_level


def test_exl3_tp4_route_and_reasoning_preserve_tp2():
    model = models.model_by_id('pi-glm-exl3-tp4')
    assert model.label == 'GLM 5.3 Flash EXL3 (TP4)'
    assert model.vessel == 'pi' and model.pi_rpc
    assert model.wire_model == 'glm53exl3tp4/GLM-5.3-Flash-EXL3'
    assert model.default_handoff == 225000
    assert model.default_effort == 'high'
    assert models.efforts_for(model.id, 'pi') == ('off', 'low', 'high', 'max')
    assert _CLI[model.id] == model.wire_model
    for key in (model.id, model.wire_model):
        assert pi_thinking_level(key, None) == 'high'
        assert pi_thinking_level(key, 'low') == 'low'
        assert pi_thinking_level(key, 'high') == 'high'
        assert pi_thinking_level(key, 'off') == 'off'
        assert pi_thinking_level(key, 'max') == 'xhigh'
    assert model.wire_model != 'glm53exl3/GLM-5.3-Flash-EXL3'
    if 'pi-glm-exl3' in _CLI:
        assert _CLI['pi-glm-exl3'] == 'glm53exl3/GLM-5.3-Flash-EXL3'
