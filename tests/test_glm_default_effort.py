from pulsar import models
from pulsar.vessel.pi import pi_thinking_level


def test_tested_glm_defaults_and_honors_explicit_choices():
    # glm flash defaults low; glm full / EXL3 stay high
    assert models.model_by_id('pi-glm-flash').default_effort == 'low'
    assert models.model_by_id('pi-glm-exl3').default_effort == 'high'
    assert models.model_by_id('pi-glm53-full').default_effort == 'high'
    for name, default in [('pi-glm-flash', 'low'), ('glm53local/glm-5.3-flash', 'low'),
                          ('glm-5.3-flash', 'low'),
                          ('pi-glm53-full', 'high'), ('glm53local/glm-5.3', 'high'),
                          ('glm-5.3', 'high'), ('pi-glm-exl3', 'high'),
                          ('glm53exl3/GLM-5.3-Flash-EXL3', 'high')]:
        assert pi_thinking_level(name, None) == default
        assert pi_thinking_level(name, '') == default
        assert pi_thinking_level(name, 'off') == 'off'
        assert pi_thinking_level(name, 'low') == 'low'
        assert pi_thinking_level(name, 'high') == 'high'
        assert pi_thinking_level(name, 'max') == 'xhigh'


def test_unrelated_defaults_unchanged():
    for name in ['pi-glm-uncensored', 'glm53pair/glm-5.3-flash-nvfp4',
                 'pi-glm-52', 'pi-qwen38-flash-tp4', 'pi-v4-flash']:
        assert pi_thinking_level(name, None) == 'off'


def test_qwen_flash_defaults_low():
    # qwen flash defaults to low reasoning; explicit off still wins
    assert models.model_by_id('pi-qwen38-flash').default_effort == 'low'
    assert pi_thinking_level('pi-qwen38-flash', None) == 'low'
    assert pi_thinking_level('pi-qwen38-flash', '') == 'low'
    assert pi_thinking_level('glmlocal/Qwen3.8-Flash-Next-NVFP4', None) == 'low'
    assert pi_thinking_level('pi-qwen38-flash', 'off') == 'off'
