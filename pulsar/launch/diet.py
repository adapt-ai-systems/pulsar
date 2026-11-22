"""Diet materialization: the per-vessel private homes (codex, grok, pi),
the opted-in skill links, and the context files, written
before every spawn.

Pi provider map merge rule (``_materialize_pi_diet``):
  1. Seed ``models["providers"]`` from ``pulsar/vessel/pi_providers.json``
     (the complete no-host baseline).
  2. If ``~/.pi/agent/models.json`` exists, merge ``providers.update(host)``
     so host-only providers and keys are available.
  3. Apply the documented post-merge normalization rules so stale host
     copies cannot drop multimodal input, context windows, output caps, or
     thinking wire formats.

Daemon-side (pulsar.launch): runners never import this module.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
import tomllib
from pathlib import Path

from pulsar.fsutil import atomic_write_json, atomic_write_text
from pulsar.launch import skills as skills_mod
from pulsar.launch.cages import _materialize_cages
from pulsar.launch.context import materialize_context
from pulsar.vessel import profiles as profiles_mod
from pulsar.vessel.profiles import (
    PROFILES,
    codex_empty_agents,
    codex_home,
    grok_empty_agents,
    grok_empty_claude_plugins,
    grok_home,
    pi_home,
)

log = logging.getLogger("pulsar.spawn")


def _relink(link: Path, target: Path) -> None:
    """Point ``link`` at ``target``, replacing whatever is there."""
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(target)


def _materialize_codex_diet(pulse_id: str, home: Path,
                            linked: list[str]) -> None:
    """Clean-diet enforcement for the codex CLI: give it a private CODEX_HOME.

    A fresh CODEX_HOME is the whole game — no ~/.codex/config.toml means no
    ``[plugins."superpowers@openai-curated"]`` and no connector set, no
    ~/.codex/AGENTS.md means no global "always weave" mandate, and
    CODEX_HOME/skills holds EXACTLY the opted-in skills (codex reads them
    natively, with their SKILL.md front-matter descriptions — no AGENTS.md
    index needed). Bundled skills are disabled as a group, including future
    additions (verified with codex-cli 0.153.3, 2026-09-04).

    ``auth.json`` is a SYMLINK to the real login, relinked every spawn: one
    canonical token file means a refresh rotation can't strand the user's
    interactive codex, and if the CLI ever replaces the link with a real file
    the next spawn heals it.
    """
    ch = codex_home(home)
    skills = ch / "skills"
    skills.mkdir(parents=True, exist_ok=True)
    for entry in skills.iterdir():
        if entry.is_symlink():
            entry.unlink()
        elif entry.name != ".system":  # codex materializes its built-ins here
            log.warning("diet pulse=%s non-symlink %r in codex skills — left "
                        "alone", pulse_id, entry.name)
    for name in linked:
        (skills / name).symlink_to(skills_mod.SKILLS_ROOT / name)

    cfg = "\n".join([
        "# pulsar: generated, regenerated every spawn. A clean codex pulse",
        "# reads THIS file, not ~/.codex/config.toml — no plugins, no",
        "# connectors, no global AGENTS.md.",
        f'[projects."{home / "cli"}"]',
        'trust_level = "trusted"',
        "",
        "[notice]",
        "hide_full_access_warning = true",
        "",
        "[skills.bundled]",
        "enabled = false",
        "",
    ])
    # A caller-owned workload may need a private MCP server or a stricter
    # sandbox than Pulsar's generic clean Codex home. Keep that contract in
    # the pulse workspace so the workload owns it, while Pulsar still owns
    # the isolation/login base above. This is deliberately append-only TOML:
    # callers may add new tables, but cannot replace the generated project
    # trust or re-enable ambient plugins/connectors.
    fragment = home / "cli" / ".codex" / "pulsar-config.toml"
    if fragment.is_file():
        extra = fragment.read_text(encoding="utf-8").strip()
        if extra:
            cfg += "\n# caller-owned workload config\n" + extra + "\n"
            log.info("diet pulse=%s appended caller codex config %s",
                     pulse_id, fragment)
    atomic_write_text(ch / "config.toml", cfg)

    auth = profiles_mod.CODEX_REAL_HOME / "auth.json"
    if auth.exists():
        _relink(ch / "auth.json", auth)
    else:
        log.error("diet pulse=%s %s missing — the codex pulse has no login",
                  pulse_id, auth)

    codex_empty_agents(home).mkdir(parents=True, exist_ok=True)
    log.info("diet pulse=%s codex home %s built: skills=%s, builtins disabled, "
             "~/.agents hidden by the unit", pulse_id, ch, linked or "none")


def _materialize_grok_diet(pulse_id: str, home: Path,
                           linked: list[str]) -> None:
    """Give a clean Grok pulse only its explicitly selected Pulsar skills.

    Grok discovers several user-level compatibility trees and its own bundled
    skills automatically.  A private GROK_HOME removes user config, plugins,
    MCP servers, and ~/.grok skills; config disables Claude/Cursor discovery
    and hides bundled skill roots.  Tools honor .gitignore so ``data/`` and
    ``venv/`` stay out of searches.  ~/.agents is additionally bind-hidden by
    ``spawn_runner`` because Grok has no compatibility switch for it.
    """
    gh = grok_home(home)
    gh.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(gh, 0o700)
    # Grok 1.0.3 advertises [skills].paths but does not load those paths in
    # agent/inspect mode. Its native project directory is reliable.
    skills = home / "cli" / ".grok" / "skills"
    skills.mkdir(parents=True, exist_ok=True)
    for entry in skills.iterdir():
        if entry.is_symlink():
            entry.unlink()
        elif entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    for name in linked:
        # Grok's configured skill walker does not follow directory symlinks;
        # copy the selected packages into this generated private home.
        shutil.copytree(skills_mod.SKILLS_ROOT / name, skills / name, symlinks=True)

    ignored = [
        profiles_mod.GROK_REAL_HOME / "skills",
        profiles_mod.GROK_REAL_HOME / "bundled" / "skills",
        gh / "bundled" / "skills",
        Path.home() / ".agents" / "skills",
        Path.home() / ".cursor" / "skills",
    ]
    quoted_ignored = ", ".join(json.dumps(str(p)) for p in ignored)
    plugin_names: list[str] = []
    try:
        installed = json.loads(profiles_mod.GROK_CLAUDE_PLUGINS_FILE.read_text())
        plugin_names = sorted({key.split("@", 1)[0]
                               for key in installed.get("plugins", {})})
    except (OSError, ValueError, AttributeError):
        pass
    quoted_plugins = ", ".join(json.dumps(name) for name in plugin_names)
    cfg = "\n".join([
        "# pulsar: generated, regenerated every spawn. Clean Grok pulses",
        "# see only this private home and explicitly linked Pulsar skills.",
        "[skills]",
        f"ignore = [{quoted_ignored}]",
        "",
        "[compat.claude]",
        "skills = false",
        "rules = false",
        "agents = false",
        "mcps = false",
        "hooks = false",
        "sessions = false",
        "",
        "[compat.cursor]",
        "skills = false",
        "rules = false",
        "agents = false",
        "mcps = false",
        "hooks = false",
        "sessions = false",
        "",
        "[plugins]",
        f"disabled = [{quoted_plugins}]",
        "",
        "[marketplace]",
        "default_skills_installs_purged = true",
        "",
        "[tools]",
        "respect_gitignore = false",
        "",
    ])
    # Explicit workload MCP servers belong to the pulse, not to ambient
    # user config. Grok's clean mode disables Claude compatibility discovery,
    # so translate the caller-owned .mcp.json into its native config.
    mcp_file = home / "cli" / ".mcp.json"
    if mcp_file.is_file():
        servers = json.loads(mcp_file.read_text()).get("mcpServers", {})
        for name, server in servers.items():
            cfg += f"\n[mcp_servers.{json.dumps(name)}]\n"
            for key in ("command", "args", "url", "env", "headers"):
                if key not in server:
                    continue
                value = server[key]
                if isinstance(value, dict):
                    value_text = "{ " + ", ".join(
                        f"{json.dumps(k)} = {json.dumps(v)}"
                        for k, v in value.items()) + " }"
                else:
                    value_text = json.dumps(value)
                cfg += f"{key} = {value_text}\n"
        # Fail before writing a broken native config or consuming a message.
        tomllib.loads(cfg)

    atomic_write_text(gh / "config.toml", cfg)

    auth = profiles_mod.GROK_REAL_HOME / "auth.json"
    if auth.exists():
        _relink(gh / "auth.json", auth)
    else:
        log.warning("diet pulse=%s Grok auth missing at %s", pulse_id, auth)

    grok_empty_agents(home).mkdir(parents=True, exist_ok=True)
    grok_empty_claude_plugins(home).mkdir(parents=True, exist_ok=True)
    log.info("diet pulse=%s Grok private home written: skills=%s",
             pulse_id, linked or "none")


_PI_PROVIDERS_PATH = Path(__file__).resolve().parents[1] / "vessel" / "pi_providers.json"


def _pi_providers_path() -> Path:
    env = os.environ.get("PULSAR_PI_PROVIDERS_PATH")
    return Path(env) if env else _PI_PROVIDERS_PATH


# Served max_model_len per served model id, as /v1/models reported it.
# Add an id here only from a /v1/models record, never from a launch flag guess.
_SERVED_MAX_MODEL_LEN = {
    "Qwen3.8-Flash-Next-NVFP4": 262144,
    "deepseek-v4.1-flash": 1048576,
    "glm53": 163840,
}


def _materialize_pi_diet(pulse_id: str, home: Path,
                          linked: list[str],
                          slot: dict | None = None) -> None:
    """Give a clean pi (OMP) pulse a private PI_HOME with its own provider map.

    pi reads providers/models from ``$PI_CODING_AGENT_DIR/models.json`` — NOT
    ``PI_HOME``, which pi 0.73.1 ignores entirely (config.js getAgentDir reads
    ENV_AGENT_DIR = "<APP>_CODING_AGENT_DIR"). The bridge exports
    PI_CODING_AGENT_DIR=<pi-home>/agent.
    A clean pulse gets a private home so it never shares ~/.pi (sessions,
    models, provider config) with other pulses. Only the repo-seeded providers
    in ``pulsar/vessel/pi_providers.json`` are used when no host map exists;
    when ``~/.pi/agent/models.json`` is present, ``providers.update(host)``
    merges the host providers and the post-merge rules below normalize fields
    that older host configs may leave stale. pi skills (--no-skills
    in the vessel) and context files (-nc) are disabled headlessly, so only
    the provider map is needed here.
    """
    from pulsar.vessel.pi import PI_PROVIDER_THINKING_MAPS
    ph = pi_home(home)
    agent = ph / "agent"
    ph.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(ph, 0o700)
    agent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(agent, 0o700)
    models = json.loads(_pi_providers_path().read_text(encoding="utf-8"))
    # A clean pulse still needs every provider the host pi config exposes
    # (e.g. local or cloud endpoints in ~/.pi/agent/models.json). Merge the
    # host provider map so keys and endpoints stay outside the repo.
    host_cfg = Path.home() / ".pi" / "agent" / "models.json"
    if host_cfg.is_file():
        try:
            host = json.loads(host_cfg.read_text()).get("providers") or {}
            models["providers"].update(host)
        except json.JSONDecodeError:
            log.warning("diet pulse=%s host pi models.json unreadable; host providers not seeded",
                        pulse_id)
    else:
        log.warning("diet pulse=%s no host pi models.json; using repo defaults only", pulse_id)
    # GLM Flash is multimodal. Older host maps mark it text-only, which makes
    # Pi's Read tool omit images before the serving endpoint ever sees them.
    for provider, model_id in (("glm53local", "glm-5.3-flash"),
                               ("glm53pair", "glm-5.3-flash-nvfp4"),
                               ("glm53exl3", "GLM-5.3-Flash-EXL3")):
        for model in models["providers"].get(provider, {}).get("models", []):
            if model.get("id") == model_id:
                model["input"] = ["text", "image"]
    # The host map replaces glm53local wholesale and older copies may carry
    # stale limits. Keep the declared 128k output cap so long xhigh turns do
    # not stop on ``length`` (see pi._output_cap_payload).
    for model in models["providers"].get("glm53local", {}).get("models", []):
        if model.get("id") == "glm-5.3-flash":
            model["contextWindow"] = 1048576
            model["maxTokens"] = 131072
    # Qwen 27B EXL3 reads thinking control ONLY from
    # chat_template_kwargs.enable_thinking — with thinkingFormat "openai",
    # `--thinking off` sends nothing and default server thinking can exhaust
    # the output budget (see pi._output_cap_payload). Normalize after the
    # host merge with qwen-chat-template and a 32k maxTokens cap. Scope to
    # 27B; do not change the legacy 35B provider behavior.
    for model in models["providers"].get("localqwen", {}).get("models", []):
        if model.get("id") == "qwen3.8-27b":
            model.update(name="Qwen3.8 27B EXL3",
                         reasoning=True, input=["text", "image"],
                         contextWindow=196608, maxTokens=32768)
            model["thinkingLevelMap"] = dict(PI_PROVIDER_THINKING_MAPS["qwen38"])
            model["compat"] = {
                **model.get("compat", {}),
                "thinkingFormat": "qwen-chat-template",
                "supportsReasoningEffort": True,
                "supportsDeveloperRole": False,
                "maxTokensField": "max_tokens",
            }
    # The host provider map replaces the built-in glmlocal provider wholesale,
    # so normalize these fields after the merge. Pi calls its top thinking
    # level ``high`` internally; the Qwen template accepts only xhigh/medium/low.
    for model in models["providers"].get("glmlocal", {}).get("models", []):
        if model.get("id") == "Qwen3.8-Flash-Next-NVFP4":
            model["thinkingLevelMap"] = dict(PI_PROVIDER_THINKING_MAPS["qwen38_flash"])
    # SGLang's dsv41 custom encoder reads `thinking`, not Qwen's
    # `enable_thinking`. Pi's nested-reasoning wire format (named openrouter)
    # is normalized by SGLang into thinking + top-level reasoning_effort;
    # it also sends effort=none for off. No OpenRouter endpoint is involved.
    # Normalize after the host merge so older provider configs work too.
    for model in models["providers"].get("ds41local", {}).get("models", []):
        if model.get("id") == "deepseek-v4.1-flash":
            model["name"] = "DeepSeek V4.1 Flash (1M ctx)"
            model["contextWindow"] = 1048576
            model["reasoning"] = True
            model["input"] = ["text", "image"]
            model["thinkingLevelMap"] = dict(PI_PROVIDER_THINKING_MAPS["ds41"])
            model["compat"] = {
                **model.get("compat", {}),
                "thinkingFormat": "openrouter",
                "supportsReasoningEffort": True,
                "supportsDeveloperRole": False,
                "maxTokensField": "max_tokens",
            }
    # Providers carrying a legacy DS41 name map are normalized to numeric effort.
    for model in models["providers"].get("altds", {}).get("models", []):
        if model.get("id") == "deepseek-v4.1-flash":
            model["thinkingLevelMap"] = dict(PI_PROVIDER_THINKING_MAPS["ds41"])
    for model in models["providers"].get("glm53exl3", {}).get("models", []):
        if model.get("id") == "GLM-5.3-Flash-EXL3":
            model["name"] = "GLM 5.3 Flash EXL3 (850k ctx)"
            model["contextWindow"] = 850000
            model["reasoning"] = True
            model["input"] = ["text", "image"]
    # Normalize local GLM 5.3 *after* host overrides (including glm53flashexlr8
    # id="glm53" and altglmflash / altglmfull). A null low slot makes Pi clamp
    # --thinking low upward to high before constructing the request, and
    # missing reasoning/qwen-chat-template breaks effort + off handling.
    for provider, config in models["providers"].items():
        if not provider.startswith(("glm", "altglm")):
            continue
        for model in config.get("models", []):
            if not model.get("id", "").lower().startswith(("glm-5.3", "glm53")):
                continue
            model["reasoning"] = True
            model["thinkingLevelMap"] = dict(PI_PROVIDER_THINKING_MAPS["glm"])
            model["compat"] = {
                **config.get("compat", {}), **model.get("compat", {}),
                "thinkingFormat": "qwen-chat-template",
                "supportsReasoningEffort": True,
                "supportsDeveloperRole": False,
                "maxTokensField": "max_tokens",
            }
    # contextWindow: vLLM counts prompt + max_tokens against max_model_len
    # EXCLUSIVELY (prompt + max_tokens == max_model_len passes, +1 400s).
    # pi treats contextWindow as the max allowed TOTAL, so every model with a
    # recorded served max_model_len gets at most that length minus one, in
    # whichever provider (repo or host) lists it. A smaller window is kept.
    for config in models["providers"].values():
        for model in config.get("models", []):
            served = _SERVED_MAX_MODEL_LEN.get(model.get("id"))
            window = model.get("contextWindow")
            if served and isinstance(window, int) and window >= served:
                model["contextWindow"] = served - 1
    if isinstance(slot, dict):
        slot_id = slot.get("id")
        served_id = slot.get("served_id")
        base_url = slot.get("base_url")
        if (isinstance(slot_id, str) and isinstance(served_id, str)
                and isinstance(base_url, str)):
            models["providers"][slot_id] = {
                "baseUrl": base_url,
                "api": "openai-completions",
                "apiKey": "none",
                "compat": {"supportsDeveloperRole": False,
                           "supportsReasoningEffort": True,
                           "maxTokensField": "max_tokens",
                           "thinkingFormat": "qwen-chat-template"},
                "models": [{"id": served_id,
                            "name": f"{slot_id}: {served_id}",
                            "reasoning": True,
                            "input": ["text", "image"]}],
            }
            if served_id.lower().startswith("deepseek"):
                models["providers"][slot_id]["models"][0]["thinkingLevelMap"] = \
                    dict(PI_PROVIDER_THINKING_MAPS["ds41"])
            elif served_id.lower().startswith(("glm-5.3", "glm53")):
                models["providers"][slot_id]["models"][0]["thinkingLevelMap"] = \
                    dict(PI_PROVIDER_THINKING_MAPS["glm"])
    atomic_write_json(agent / "models.json", models, indent=2)
    log.info("diet pulse=%s pi home %s built (providers %s)",
             pulse_id, ph, ",".join(sorted(models["providers"])))


_DIET_BUILDERS = {
    "codex": lambda pulse_id, home, wanted, linked, slot: _materialize_codex_diet(
        pulse_id, home, linked
    ),
    "grok": lambda pulse_id, home, wanted, linked, slot: _materialize_grok_diet(
        pulse_id, home, linked
    ),
    "pi": lambda pulse_id, home, wanted, linked, slot: _materialize_pi_diet(
        pulse_id, home, linked, slot
    ),
}


def materialize_diet(pulse_id: str, home: Path, diet: dict | None,
                     vessel: str = "claude-code",
                     title: str | None = None,
                     model: str | None = None) -> dict:
    """Write ``<home>/diet.json`` + skill symlinks before every spawn.

    The daemon decides the diet; the runner just reads the file at boot.
    ``None`` means clean-by-default — ``{"clean": true}`` is written so the
    runner never has to guess. The write is atomic (tmp + replace) so a
    booting runner can't read a half-written file.

    Skill symlinks land in ``<home>/cli/.claude/skills/`` (the CLI's cwd) —
    stale links are cleared first; a missing or invalid skill name logs
    ERROR and is skipped, never blocks the spawn. Returns the diet written.
    """
    if diet is None:
        diet = {"clean": True}
    home.mkdir(parents=True, exist_ok=True)
    atomic_write_json(home / "diet.json", diet)

    links_dir = home / "cli" / ".claude" / "skills"
    links_dir.mkdir(parents=True, exist_ok=True)
    for entry in links_dir.iterdir():
        if entry.is_symlink():
            entry.unlink()
        else:
            log.warning("diet pulse=%s non-symlink %r in skills dir — left "
                        "alone", pulse_id, entry.name)
    # Full-fat (clean=False) loads skills the normal way — no links needed.
    wanted = list(diet.get("skills") or []) if diet.get("clean") else []
    linked = []
    for name in wanted:
        if "/" in name or name in (".", ".."):
            log.error("diet pulse=%s skill name %r invalid — skipped",
                      pulse_id, name)
            continue
        src = skills_mod.SKILLS_ROOT / name
        if not src.is_dir():
            log.error("diet pulse=%s skill %r missing at %s — skipped",
                      pulse_id, name, src)
            continue
        (links_dir / name).symlink_to(src)
        linked.append(name)
    log.info("diet pulse=%s written clean=%s memory=%s skills=%s linked=%s "
             "vessel=%s", pulse_id, diet.get("clean"),
             bool(diet.get("memory")), wanted, linked, vessel)
    # Per-vessel skill delivery (skills are NOT claude-only, 2026-07-27):
    # claude reads the .claude/skills symlinks above via --setting-sources
    # project; qwen and codex each need their native mechanism fed.
    prof = PROFILES.get(vessel)
    # A slot pi pulse needs its private provider map even without the clean
    # diet (its dynamic slot is registered there).
    if prof is not None and prof.diet_builder and (
            diet.get("clean")
            or (prof.diet_builder == "pi" and diet.get("slot"))):
        _DIET_BUILDERS[prof.diet_builder](pulse_id, home, wanted, linked,
                                          diet.get("slot"))
    _materialize_cages(pulse_id, home, list(diet.get("cages") or []), vessel)
    # Harness overrides (an experiment): the validated pi-vessel knobs
    # travel in the diet and land in <home>/harness-overrides.json for the
    # vessel to read at every launch. No overrides -> the file is REMOVED so
    # a stale one can never leak into the next spawn. This sidecar is
    # OPTIONAL: an I/O failure (e.g. a directory sitting at that path) must
    # never block a spawn (2026-10-23) — log it and
    # move the stale override aside so a failed update cannot silently keep
    # applying it. Never rmtree: an unexpected dir is renamed, not walked.
    _ho_path = home / "harness-overrides.json"
    _ho = diet.get("harness_overrides")
    try:
        if isinstance(_ho, dict) and _ho:
            atomic_write_json(_ho_path, _ho)
            log.info("diet pulse=%s harness_overrides=%s", pulse_id,
                     sorted(_ho))
        elif _ho_path.exists() or _ho_path.is_symlink():
            _ho_path.unlink()
            log.info("diet pulse=%s removed stale harness-overrides.json",
                     pulse_id)
    except OSError as exc:
        log.warning("diet pulse=%s could not update harness-overrides.json "
                    "(%s) — moving any stale override aside", pulse_id, exc)
        _stale = None
        for _name in (_ho_path.name + ".stale",
                      "%s.stale-%d-%d" % (_ho_path.name, os.getpid(),
                                          time.monotonic_ns())):
            _target = _ho_path.with_name(_name)
            try:
                _ho_path.rename(_target)
                _stale = _target
                break
            except OSError:
                # .stale already occupied (e.g. a directory left by an
                # earlier fault) — try the collision-free name next.
                continue
        if _stale is not None:
            log.warning("diet pulse=%s stale override quarantined at %s",
                        pulse_id, _stale)
        else:
            # Quarantine impossible: drop the stale REGULAR sidecar (or
            # symlink) so a failed update can never leave an old override
            # still applied. An unexpected directory stays in place —
            # never rmtree — and is logged loudly.
            try:
                if _ho_path.is_symlink() or _ho_path.is_file():
                    _ho_path.unlink()
                    log.error("diet pulse=%s harness-overrides.json could "
                              "not be quarantined — stale file removed so "
                              "it cannot apply", pulse_id)
                else:
                    log.error("diet pulse=%s stale harness-overrides.json "
                              "could not be quarantined or removed at %s "
                              "(unexpected directory) — it may still apply",
                              pulse_id, _ho_path)
            except OSError as exc2:
                log.error("diet pulse=%s stale harness-overrides.json could "
                          "not be quarantined or removed at %s (%s) — it "
                          "may still apply", pulse_id, _ho_path, exc2)
    # The context-render knobs ride the same harness_overrides object:
    # prompt_parts from harness_overrides wins over the diet's when both are set
    # (ONE resolution, shared with the default-skills merge and the pi
    # todo extension); part_text/skill_catalog
    # reshape the rendered PULSAR.md. Absent keys render today's
    # document, byte-for-byte.
    _ho = _ho if isinstance(_ho, dict) else {}
    from pulsar.harness_overrides import effective_prompt_parts
    parts = effective_prompt_parts(_ho, diet)
    # Every pulse learns its own id + the pulse-messaging recipe.
    # The skill catalog is PULSAR content, not vessel clutter: clean means
    # "no ambient config from the vessel's own home", never "no index"
    # (turning it off leaves clean codex/grok pulses unable to discover skills).
    materialize_context(pulse_id, home / "cli", vessel, title, linked,
                        model=model, parts=parts,
                        part_text=_ho.get("part_text"),
                        skill_catalog=_ho.get("skill_catalog"))
    return diet
