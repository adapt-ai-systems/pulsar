"""pulsar/models.json is the one model registry — both sides read it."""
import copy
import json
import re
from pathlib import Path

from pulsar import models as reg

EXPECTED_LAUNCH_IDS = [
    "claude-opus-5-5",
    "claude-sonnet-5-5",
    "claude-fable-5-2",
    "gpt-6.2-astra",
    "gpt-6-sol",
    "gpt-6-luna",
    "agy-gemini-4-neon",
    "agy-gemini-4-carbon",
    "agy-gemini-4-argon",
    "grok-4.7",
]

PRESETS_PATH = reg.SHIPPED_REGISTRY_PATH.with_name("models.presets.json")
SHIPPED_PI_PROVIDERS_PATH = (
    reg.SHIPPED_REGISTRY_PATH.parent / "vessel" / "pi_providers.json"
)


def test_registry_loads_and_every_model_has_a_known_vessel():
    assert reg.MODELS
    for m in reg.MODELS.values():
        assert m.vessel in reg.VESSELS, m.id
        assert m.default_handoff > 0, m.id
        if m.vessel == "pi" and not m.slot:
            assert m.wire_model, f"{m.id}: pi models need wire_model"
            assert m.thinking in {
                "glm", "glm52", "qwen38", "ds41", "passthrough", "required-low"
            }, f"{m.id}: pi models need a valid thinking profile"


def test_slots_are_registry_entries_with_base_urls():
    ids = {m.id for m in reg.MODELS.values() if m.slot}
    assert {"gpu-ring", "gpu-pair", "gpu-local"} <= ids
    for m in reg.MODELS.values():
        if m.slot:
            assert m.base_url and m.base_url.startswith("http"), m.id
            assert not m.wire_model, m.id


def test_picker_default_is_a_visible_model():
    m = reg.model_by_id(reg.DEFAULT_MODEL_ID)
    assert m is not None and not m.hidden


def test_general_thinking_default_is_medium():
    assert reg.DEFAULT_EFFORT == "medium"


def test_default_model_effort_is_supported():
    m = reg.model_by_id(reg.DEFAULT_MODEL_ID)
    effort = m.default_effort or reg.DEFAULT_EFFORT
    assert effort in reg.efforts_for(reg.DEFAULT_MODEL_ID, m.vessel)


def test_vessel_for_registry_then_prefix_then_claude():
    assert reg.vessel_for("gpt-6-sol") == "codex"
    assert reg.vessel_for("pi-nemo") == "pi"
    assert reg.vessel_for("pi-nex-mini") == "pi"
    assert reg.vessel_for("grok-9-future") == "grok"      # prefix fallback
    assert reg.vessel_for("mystery") == "claude-code"


def test_default_handoff_registry_then_family():
    assert reg.default_handoff_for("pi-qwen38-flash") == 175_000
    assert reg.default_handoff_for("gpt-5.5-codex") == 225_000
    assert reg.default_handoff_for("qwen3-coder") == 190_000
    assert reg.default_handoff_for("mystery") is None


def test_efforts_model_override_beats_vessel_dial():
    assert reg.efforts_for("pi-glm-flash", "pi") == ("off", "low", "high", "max")
    assert reg.efforts_for("pi-glm53-full", "pi") == ("off", "low", "high", "max")
    assert reg.efforts_for("pi-alt-glm53-flash-tp4", "pi") == ("off", "low", "high", "max")
    assert reg.efforts_for("pi-alt-glm53-full-tp4", "pi") == ("off", "low", "high", "max")
    assert reg.efforts_for("pi-v4-flash-b", "pi") == ()
    assert reg.efforts_for("gpt-6-sol", "codex")[0] == "none"
    assert reg.fast_vessels() == ("codex",)


def test_web_symlink_points_at_the_registry():
    web = reg.SHIPPED_REGISTRY_PATH.parent.parent / "web" / "src" / "lib" / "models.json"
    assert web.is_symlink()
    assert json.loads(web.read_text(encoding="utf-8")) == json.loads(
        reg.SHIPPED_REGISTRY_PATH.read_text(encoding="utf-8")
    )


def test_ds41_fixture_entry_keeps_pi_rpc_id():
    model = reg.model_by_id("pi-ds41-flash")
    assert model.label == "DeepSeek V4.1 Flash (TP4 ring · 1M ctx)"
    assert model.hidden
    assert model.vessel == "pi" and model.pi_rpc
    assert model.wire_model == "ds41local/deepseek-v4.1-flash"
    assert model.default_effort == "none"


def test_retired_qwen38_tp4_keeps_dedicated_provider():
    model = reg.model_by_id("pi-qwen38-flash-tp4")
    assert model.hidden
    assert model.label == "Qwen 3.8 Flash (TP4)"
    assert model.vessel == "pi" and model.pi_rpc
    assert model.wire_model == "qwen38tp4/Qwen3.8-Flash-Next-NVFP4"


def test_glm_flash_routes_separately_from_full():
    # Full and Flash share a serving slot, not a wire identity.
    glm = reg.model_by_id("pi-glm-flash")
    full = reg.model_by_id("pi-glm53-full")
    qwen = reg.model_by_id("pi-qwen38-flash")
    assert glm.hidden
    assert not full.hidden
    assert glm.wire_model == "glm53local/glm-5.3-flash"
    assert full.wire_model == "glm53local/glm-5.3"
    assert qwen.hidden
    assert qwen.wire_model == "glmlocal/Qwen3.8-Flash-Next-NVFP4"
    assert reg.model_by_id("pi-glm-uncensored").hidden


def test_grok_entries_use_grok_vessel_dial():
    for mid in ("grok-4.7", "grok-4.7-build-fast", "grok-4.6"):
        model = reg.model_by_id(mid)
        assert model is not None and not model.hidden, mid
        assert model.vessel == "grok", mid
        assert reg.efforts_for(mid, "grok") == ("low", "medium", "high", "xhigh")
        assert reg.default_handoff_for(mid) == 300_000, mid


def test_grok_46_defaults_to_low_effort():
    assert reg.model_by_id("grok-4.6").default_effort == "low"


def test_hidden_fixture_models_stay_resolvable():
    visible = {m.id for m in reg.MODELS.values() if not m.hidden}
    gone = ["pi-v4-flash-tp2", "pi-glm-exl3",
            "pi-grok46", "pi-qwen27-interface"]
    for mid in gone:
        model = reg.model_by_id(mid)
        assert model is not None and model.hidden, mid
    assert not visible & set(gone)
    assert "pi-qwen27" not in visible
    assert reg.model_by_id("pi-qwen38-flash").label == "Qwen 3.8 Flash (TP2 pair)"
    assert reg.model_by_id("pi-glm-uncensored").label == "GLM 5.3 Flash uncensored (TP2 pair)"


def test_retired_local_recipes_are_hidden_but_ids_still_resolve():
    retired = ('pi-qwen38-flash-tp4', 'pi-glm-exl3-uncensored-tp4',
               'pi-glm-exl3-tp4', 'pi-glm-uncensored')
    for mid in retired:
        model = reg.model_by_id(mid)
        assert model is not None and model.hidden, mid
        assert model.wire_model and reg.vessel_for(mid) == 'pi'
    for mid in ('pi-glm53-full', 'pi-glm53-flash-exlr8', 'pi-tuned-flash'):
        assert not reg.model_by_id(mid).hidden, mid


def test_prompt_notes_flags_match_old_sniffing():
    """The prompt_notes flags reproduce the former ds4*/qwen*/VISION_MODELS rules."""
    from pulsar.vessel.vision_sidecar import VISION_MODELS
    flagged_vision = {m.id for m in reg.MODELS.values()
                      if "vision" in m.prompt_notes}
    assert flagged_vision == set(VISION_MODELS)
    for m in reg.MODELS.values():
        assert ("ds4_clarity" in m.prompt_notes) == m.id.startswith(("ds4", "codex-ds4")), m.id
        assert ("qwen" in m.prompt_notes) == m.id.startswith("qwen"), m.id
        assert set(m.prompt_notes) <= {"ds4_clarity", "vision", "qwen"}, m.id


def test_prompt_notes_for_unregistered_ids_use_prefix_rules():
    assert reg.prompt_notes_for("ds4-new") == ("ds4_clarity",)
    assert reg.prompt_notes_for("qwen3.8-27b") == ("qwen",)
    assert reg.prompt_notes_for("claude-x") == ()
    assert reg.prompt_notes_for(None) == ()
    assert reg.prompt_notes_for("pi-glm53-full") == ("vision",)


def test_every_model_declares_explicit_locality():
    raw = json.loads(reg.REGISTRY_PATH.read_text(encoding="utf-8"))
    for entry in raw["models"]:
        assert isinstance(entry.get("local"), bool), f"{entry['id']}: missing boolean local flag"
    # Cloud pi models are NOT local:
    for mid in ("pi-glm-53", "pi-grok46"):
        m = reg.model_by_id(mid)
        assert m is not None and m.vessel == "pi", mid
        assert m.local is False and reg.is_local(mid) is False, mid
    # Cloud models on other vessels are NOT local:
    for mid in ("claude-opus-5-5", "claude-fable-5-1", "gpt-6-sol",
                "agy-gemini-4-argon", "grok-4.7"):
        assert reg.is_local(mid) is False, mid
    # Self-hosted models ARE local:
    for mid in ("pi-qwen38-flash", "pi-ds41-flash", "pi-glm53-full",
                "pi-glm-flash", "pi-qwen27", "gpu-ring",
                "gpu-pair", "gpu-local", "codex-ds4-flash"):
        assert reg.is_local(mid) is True, mid
    # Unknown ids fail closed:
    assert reg.is_local("unknown-model") is False
    assert reg.is_local(None) is False


def test_missing_local_flag_defaults_false_with_warning(tmp_path, caplog, monkeypatch):
    import logging
    monkeypatch.setattr(reg, "_warned_missing_local", set())
    custom = {
        "default_model": "hand-added-pi",
        "default_effort": "medium",
        "vessels": {"pi": {"label": "Pi", "efforts": [], "fast": False}},
        "models": [
            {
                "id": "hand-added-pi",
                "label": "Hand Added Pi",
                "vessel": "pi",
                "default_handoff": 150000,
                "wire_model": "prov/model",
                "thinking": "passthrough",
            },
            {
                "id": "bad-type-local",
                "label": "Bad Type Local",
                "vessel": "pi",
                "local": "yes",
                "default_handoff": 150000,
                "wire_model": "prov/model2",
                "thinking": "passthrough",
            },
        ],
    }
    p = tmp_path / "models.json"
    p.write_text(json.dumps(custom), encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="pulsar.models"):
        loaded = reg._load(p)
        # Re-loading does not spam duplicate warnings for the same model id
        reg._load(p)
    assert loaded["models"]["hand-added-pi"].local is False
    assert loaded["models"]["bad-type-local"].local is False
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert any("'hand-added-pi'" in w and "local=False" in w for w in warnings)
    assert any("'bad-type-local'" in w and "local=False" in w for w in warnings)


def test_ds41_dial_is_numeric_and_old_names_alias():
    for mid in ("pi-alt-ds41", "pi-ds41-flash"):
        assert reg.efforts_for(mid, "pi") == ("80", "70", "50", "30", "none")
        assert reg.canonical_effort(mid, "low") == "50"
        assert reg.canonical_effort(mid, "off") == "none"
        assert reg.canonical_effort(mid, "max") == "80"
        assert reg.canonical_effort(mid, "70") == "70"
    assert reg.slot_efforts("deepseek-v4.1-flash") == reg.DS41_EFFORTS
    assert reg.canonical_effort("gpu-ring", "high", "deepseek-v4.1-flash") == "80"
    assert reg.canonical_effort("gpu-ring", "high", "glm-5.3-flash") == "high"
    assert reg.canonical_effort("pi-glm-flash", "low") == "low"


def test_neon_offers_xhigh_effort_while_other_agy_models_stop_at_high():
    assert reg.efforts_for("agy-gemini-4-neon", "antigravity") == (
        "low", "medium", "high", "xhigh")
    assert reg.efforts_for("agy-gemini-4-carbon", "antigravity") == (
        "low", "medium", "high")
    assert reg.efforts_for("agy-gemini-4-argon", "antigravity") == (
        "low", "medium", "high")


# --------------------------------------------------------------------------- #
# Shipping starter files (pulsar/models.json, pi_providers.json, presets)
# --------------------------------------------------------------------------- #

_PRIVATE_ADDR_RE = re.compile(
    r"\b100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.\d{1,3}\.\d{1,3}\b"
    r"|\b192\.168\.\d{1,3}\.\d{1,3}\b"
    r"|\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"
    r"|/home/[a-z0-9._-]+/|/Users/[a-z0-9._-]+/",
    re.I,
)


def test_shipped_starter_registry_loads_and_has_exact_ten_launch_models():
    raw_text = reg.SHIPPED_REGISTRY_PATH.read_text(encoding="utf-8")
    assert not _PRIVATE_ADDR_RE.search(raw_text)
    loaded = reg._load(reg.SHIPPED_REGISTRY_PATH)
    assert set(loaded["vessels"]) == {
        "claude-code", "codex", "grok", "pi", "antigravity"
    }
    assert loaded["raw"]["default_model"] == "claude-opus-5-5"
    assert loaded["raw"]["default_effort"] == "medium"
    visible = [m.id for m in loaded["models"].values() if not m.hidden]
    assert visible == EXPECTED_LAUNCH_IDS
    assert list(loaded["models"]) == EXPECTED_LAUNCH_IDS
    for m in loaded["models"].values():
        assert m.local is False, m.id
        assert m.slot is False, m.id
        assert m.base_url is None, m.id
        assert m.default_handoff > 0, m.id
        assert m.vessel in loaded["vessels"], m.id


def test_shipped_pi_providers_is_empty():
    raw = json.loads(SHIPPED_PI_PROVIDERS_PATH.read_text(encoding="utf-8"))
    assert raw == {"providers": {}}


def test_every_preset_applied_to_starter_loads_and_validates(tmp_path):
    presets_text = PRESETS_PATH.read_text(encoding="utf-8")
    assert not _PRIVATE_ADDR_RE.search(presets_text)
    presets_doc = json.loads(presets_text)
    presets = presets_doc["presets"]
    assert set(presets) == {
        "pi-glm53-full",
        "pi-glm-flash",
        "pi-qwen38-flash",
        "pi-qwen27",
        "pi-ds41-flash",
        "codex-ds4-flash",
        "local-openai",
        "local-slot",
    }
    starter_raw = json.loads(reg.SHIPPED_REGISTRY_PATH.read_text(encoding="utf-8"))
    for name, preset in presets.items():
        entry = copy.deepcopy(preset["model"])
        assert entry["id"] == name
        assert entry["local"] is True
        if entry["vessel"] == "pi" and not entry.get("slot"):
            assert entry.get("wire_model"), name
            assert entry.get("thinking") in {
                "glm", "glm52", "qwen38", "ds41", "passthrough", "required-low"
            }, name
            prov = preset["pi_provider"]
            assert prov["config"]["baseUrl"] == "http://127.0.0.1:8000/v1"
            wire_prov, _, wire_id = entry["wire_model"].partition("/")
            assert prov["name"] == wire_prov
            assert any(m["id"] == wire_id for m in prov["config"]["models"])
        else:
            assert entry.get("base_url") == "http://127.0.0.1:8000/v1", name

        combined = copy.deepcopy(starter_raw)
        combined["models"].append(entry)
        p = tmp_path / f"{name}.json"
        p.write_text(json.dumps(combined), encoding="utf-8")
        loaded = reg._load(p)
        m = loaded["models"][entry["id"]]
        assert m.local is True
        assert m.vessel in loaded["vessels"]
        assert m.default_handoff > 0
