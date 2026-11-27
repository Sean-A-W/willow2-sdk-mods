"""FX module — hand orb, tattoo glow, target light, sounds, and fade management.

All visual and audio effects for Telekinesis. Zero gameplay logic.
Called from __init__.py at activation, grab moment, tick, and cleanup.
"""

from __future__ import annotations

import math

import unrealsdk
from mods_base import ENGINE, BoolOption, SliderOption


# ─── Options ────────────────────────────────────────────────────────────────

enable_sounds = BoolOption(
    "Enable Sounds",
    True,
    description="Play Phaselock sound effects on grab and release.",
)

orb_min_scale = SliderOption(
    "Orb Min Scale %",
    50,
    0,
    100,
    5,
    True,
    description=(
        "Orb size when idle (percentage of base). "
        "0 = invisible when idle, 100 = no shrinking. Default: 50"
    ),
)

orb_max_scale = SliderOption(
    "Orb Max Scale %",
    150,
    100,
    300,
    10,
    True,
    description=(
        "Orb size at maximum drain intensity (percentage of base). "
        "100 = no growth, 200 = double size at peak. Default: 150"
    ),
)

orb_responsiveness = SliderOption(
    "Orb Responsiveness",
    80,
    10,
    200,
    10,
    True,
    description=(
        "How quickly the orb reacts to drain changes (tenths). "
        "Higher = snappier, lower = smoother. Default: 80"
    ),
)

orb_drain_sensitivity = SliderOption(
    "Orb Drain Sensitivity",
    5,
    1,
    50,
    1,
    True,
    description=(
        "How much drain is needed to reach full orb size (ten-thousandths per tick). "
        "Lower = orb grows more easily, higher = needs more violent movement. Default: 5"
    ),
)

pulse_frequency = SliderOption(
    "Pulse Frequency",
    15,
    1,
    50,
    1,
    True,
    description="Pulse speed in tenths of Hz. 15 = 1.5 Hz. Default: 15",
)

pulse_magnitude = SliderOption(
    "Pulse Magnitude %",
    8,
    0,
    30,
    1,
    True,
    description="Pulse amplitude as percentage. 8 = ±8% scale/brightness. 0 = no pulse. Default: 8",
)

pulse_tattoo_offset = SliderOption(
    "Tattoo Phase Offset",
    0,
    0,
    180,
    15,
    True,
    description=(
        "Phase offset between orb and tattoo pulse in degrees. "
        "0 = in sync, 180 = opposite. Default: 0"
    ),
)


# ─── State ──────────────────────────────────────────────────────────────────

_hand_psc = None  # ParticleSystemComponent for hand orb
_hand_particle_template = None  # cached FirstPersonParticleSystem
_target_light = None  # PointLightComponent on held target
_light_start_time: float = 0.0  # TimeSeconds when light was created (for lerp)
_fading_fx: list = []  # (kind, fx_obj, end_time, ...) entries to fade out
_orb_intensity: float = 0.0  # smoothed 0-1 drain intensity for orb scale


# ─── Constants ──────────────────────────────────────────────────────────────

_ORB_BASE_SCALE = 0.35  # from skill.FirstPersonScale

_ORB_LOOP_EMITTERS = (0, 1, 2, 6, 7)  # firefly trails + center orb + blue smoke
_ORB_FLATTEN_SIZE = (6,)  # flatten SizeMultiplyLife only (keep looping)

_FADE_DURATION = 0.5  # seconds for FX to fade out
_THROW_BURST_DURATION = 0.3  # how long throw burst FX lives before fade
_LIGHT_LERP_DURATION = 1.3  # seconds for light to fade in (vanilla LiftDuration)
_LIGHT_MAX_BRIGHTNESS = 4.0


# ─── Emitter State ──────────────────────────────────────────────────────────

_saved_emitter_state = None  # list of per-emitter saved dicts
_original_emitter_state = None  # saved ONCE on first use, never overwritten


# ─── Internal: Emitter Helpers ──────────────────────────────────────────────


def _find_module(lod, class_name):
    """Find a module by class name in a LOD's module list."""
    for mod in lod.Modules:
        if mod is not None and mod.Class.Name == class_name:
            return mod
    return None


def _set_emitter_loops(template, indices, loops, log) -> None:
    """Set EmitterLoops on specific emitters in the particle template."""
    try:
        emitters = template.Emitters
        for i in indices:
            rm = emitters[i].LODLevels[0].RequiredModule
            rm.EmitterLoops = loops
    except Exception as e:
        log(f"Set emitter loops failed: {e}")


def _flatten_emitter_curves(template, log) -> None:
    """Flatten SizeMultiplyLife, color curves, and extend particle lifetime on
    selected emitters so the orb stays rock-solid (no pulsing or fading).
    Particle lifetime is extended to 999s so the particle never dies/respawns.
    Original values are saved ONCE on first call and reused for all restores."""
    global _saved_emitter_state, _original_emitter_state
    first_time = _original_emitter_state is None
    saved = []
    try:
        emitters = template.Emitters
        for i in _ORB_FLATTEN_SIZE:
            lod = emitters[i].LODLevels[0]
            entry = {"idx": i}

            # Flatten SizeMultiplyLife
            size_mod = _find_module(lod, "ParticleModuleSizeMultiplyLife")
            if size_mod is not None:
                table = size_mod.LifeMultiplier.LookupTable
                if first_time:
                    entry["orig_size"] = list(table)
                peak = max(table[j] for j in range(len(table)))
                if peak < 1.0:
                    peak = 1.0
                for j in range(len(table)):
                    table[j] = peak

            # Flatten ColorScaleOverLife
            color_mod = _find_module(lod, "ParticleModuleColorScaleOverLife")
            if color_mod is not None:
                ct = color_mod.ColorScaleOverLife.LookupTable
                if first_time:
                    entry["orig_color"] = list(ct)
                cpeak = max(ct[j] for j in range(len(ct)))
                if cpeak < 1.0:
                    cpeak = 1.0
                for j in range(len(ct)):
                    ct[j] = cpeak

                at = color_mod.AlphaScaleOverLife.LookupTable
                if first_time:
                    entry["orig_alpha"] = list(at)
                apeak = max(at[j] for j in range(len(at)))
                if apeak < 1.0:
                    apeak = 1.0
                for j in range(len(at)):
                    at[j] = apeak
                pass

            # Flatten ColorOverLife
            color_ol = _find_module(lod, "ParticleModuleColorOverLife")
            if color_ol is not None:
                ct = color_ol.ColorOverLife.LookupTable
                if first_time:
                    entry["orig_col_ol"] = list(ct)
                cpeak = max(ct[j] for j in range(len(ct)))
                if cpeak < 1.0:
                    cpeak = 1.0
                for j in range(len(ct)):
                    ct[j] = cpeak

                at = color_ol.AlphaOverLife.LookupTable
                if first_time:
                    entry["orig_alpha_ol"] = list(at)
                apeak = max(at[j] for j in range(len(at)))
                if apeak < 1.0:
                    apeak = 1.0
                for j in range(len(at)):
                    at[j] = apeak
                pass

            # Extend particle lifetime so it never dies/respawns
            lifetime_mod = _find_module(lod, "ParticleModuleLifetime")
            if lifetime_mod is not None:
                lt = lifetime_mod.Lifetime.LookupTable
                if first_time:
                    entry["orig_lifetime"] = list(lt)
                for j in range(len(lt)):
                    lt[j] = 999.0

            # Extend emitter duration and disable bKillOnCompleted
            rm = lod.RequiredModule
            if first_time:
                entry["orig_duration"] = rm.EmitterDuration
                entry["orig_kill_on_completed"] = rm.bKillOnCompleted
            rm.EmitterDuration = 999.0
            rm.bKillOnCompleted = False
            pass

            saved.append(entry)
    except Exception as e:
        log(f"Flatten emitter curves failed: {e}")

    if first_time:
        _original_emitter_state = saved
        pass
    _saved_emitter_state = _original_emitter_state


def _restore_emitter_curves(template, log) -> None:
    """Restore original size, color, and lifetime curves."""
    global _saved_emitter_state
    if _saved_emitter_state is None:
        return
    try:
        emitters = template.Emitters
        for entry in _saved_emitter_state:
            idx = entry["idx"]
            lod = emitters[idx].LODLevels[0]

            if "orig_size" in entry:
                size_mod = _find_module(lod, "ParticleModuleSizeMultiplyLife")
                if size_mod is not None:
                    for j, v in enumerate(entry["orig_size"]):
                        size_mod.LifeMultiplier.LookupTable[j] = v

            if "orig_color" in entry:
                color_mod = _find_module(lod, "ParticleModuleColorScaleOverLife")
                if color_mod is not None:
                    for j, v in enumerate(entry["orig_color"]):
                        color_mod.ColorScaleOverLife.LookupTable[j] = v
                    for j, v in enumerate(entry["orig_alpha"]):
                        color_mod.AlphaScaleOverLife.LookupTable[j] = v

            if "orig_col_ol" in entry:
                color_ol = _find_module(lod, "ParticleModuleColorOverLife")
                if color_ol is not None:
                    for j, v in enumerate(entry["orig_col_ol"]):
                        color_ol.ColorOverLife.LookupTable[j] = v
                    for j, v in enumerate(entry["orig_alpha_ol"]):
                        color_ol.AlphaOverLife.LookupTable[j] = v

            if "orig_lifetime" in entry:
                lifetime_mod = _find_module(lod, "ParticleModuleLifetime")
                if lifetime_mod is not None:
                    for j, v in enumerate(entry["orig_lifetime"]):
                        lifetime_mod.Lifetime.LookupTable[j] = v

            if "orig_duration" in entry:
                lod.RequiredModule.EmitterDuration = entry["orig_duration"]
            if "orig_kill_on_completed" in entry:
                lod.RequiredModule.bKillOnCompleted = entry["orig_kill_on_completed"]
    except Exception as e:
        log(f"Restore emitter curves failed: {e}")
    _saved_emitter_state = None


# ─── Internal: Skill Defaults ──────────────────────────────────────────────


def get_lift_skill_defaults():
    """Get a LiftActionSkill object (CDO or instance) for reading default properties."""
    try:
        for s in unrealsdk.find_all("LiftActionSkill"):
            try:
                ps = s.FirstPersonParticleSystem
                if ps is not None:
                    return s
            except Exception:
                continue
    except Exception:
        pass
    return None


# ─── Internal: Sound ────────────────────────────────────────────────────────

_AK_ACTIVATE = "Ake_FX_Player_Siren.Ak_Play_FX_Player_Phaselock_Activate"
_AK_LOOP = "Ake_FX_Player_Siren.Ak_Play_FX_Player_Phaselock_Loop"
_AK_COLLAPSE = "Ake_FX_Player_Siren.Ak_Play_FX_Player_Phaselock_Collapse"
_AK_MISS = "Ake_FX_Player_Siren.Ak_Play_FX_Maya_Phaselock_False_Start"
_AK_VOICE = "Ake_VOBD.VOBD_PL_Siren.Ak_Play_VOBD_Siren_PL_Start_Action_Skill"

_cached_ak_activate = None
_cached_ak_loop = None
_cached_ak_collapse = None
_cached_ak_miss = None
_cached_ak_voice = None


def _resolve_ak(path: str):
    """Find an AkEvent by path, returns the object or None."""
    try:
        return unrealsdk.find_object("AkEvent", path)
    except Exception:
        return None


def _ensure_ak_cache():
    """Lazily resolve AkEvent objects on first use."""
    global _cached_ak_activate, _cached_ak_loop, _cached_ak_collapse
    global _cached_ak_miss, _cached_ak_voice
    if _cached_ak_activate is None:
        _cached_ak_activate = _resolve_ak(_AK_ACTIVATE)
    if _cached_ak_loop is None:
        _cached_ak_loop = _resolve_ak(_AK_LOOP)
    if _cached_ak_collapse is None:
        _cached_ak_collapse = _resolve_ak(_AK_COLLAPSE)
    if _cached_ak_miss is None:
        _cached_ak_miss = _resolve_ak(_AK_MISS)
    if _cached_ak_voice is None:
        _cached_ak_voice = _resolve_ak(_AK_VOICE)


def _play_ak(pawn, ak_obj, log) -> None:
    """Play an AkEvent on a pawn."""
    if ak_obj is None or pawn is None:
        return
    try:
        pawn.PlayAkEvent(ak_obj)
    except Exception as e:
        log(f"PlayAkEvent failed: {e}")


# ─── Public: Hand Orb ──────────────────────────────────────────────────────


def start_hand_fx(pawn, log) -> None:
    """Spawn the blue orb particle on the left hand."""
    global _hand_psc, _hand_particle_template
    if _hand_psc is not None:
        return  # already active

    skill = get_lift_skill_defaults()
    if skill is None:
        return

    # Modify the vanilla template in-place (cached after first find).
    # Original values are saved by _flatten_emitter_curves and restored
    # via restore_vanilla_template() when the mod is disabled.
    if _hand_particle_template is None:
        _hand_particle_template = skill.FirstPersonParticleSystem
        if _hand_particle_template is None:
            return
        _set_emitter_loops(_hand_particle_template, _ORB_LOOP_EMITTERS, 0, log)
        _flatten_emitter_curves(_hand_particle_template, log)

    try:
        arms = pawn.Arms
        psc = unrealsdk.construct_object("ParticleSystemComponent", pawn)
        arms.AttachComponentToSocket(psc, "LilithMeleeFX")
        psc.SetDepthPriorityGroup(1)
        psc.SetInWorldForeground(True)
        psc.SetOnlyOwnerSee(True)
        psc.SetTranslation(skill.FirstPersonTranslation)
        psc.SetScale(skill.FirstPersonScale)
        psc.SetTemplate(_hand_particle_template)
        psc.ActivateSystem()
        _hand_psc = psc
    except Exception as e:
        log(f"Hand FX spawn failed: {e}")


def stop_hand_fx(log, instant: bool = False) -> None:
    """Stop the blue orb PSC.

    instant=False (default): fade out over _FADE_DURATION (normal release).
    instant=True: deactivate immediately (crush kill — avoids hollow orb during forward anim).
    """
    global _hand_psc
    if _hand_psc is None:
        return
    if instant:
        try:
            _hand_psc.DeactivateSystem()
            _hand_psc.DetachFromAny()
        except Exception:
            pass
    else:
        try:
            now = ENGINE.GetCurrentWorldInfo().TimeSeconds
            cur_scale = _ORB_BASE_SCALE * (
                orb_min_scale.value * 0.01
                + (1.0 - orb_min_scale.value * 0.01) * _orb_intensity * orb_max_scale.value * 0.01
            )
            _fading_fx.append(("psc", _hand_psc, now + _FADE_DURATION, cur_scale))
        except Exception:
            try:
                _hand_psc.DeactivateSystem()
                _hand_psc.DetachFromAny()
            except Exception:
                pass
    _hand_psc = None


def play_throw_burst(pawn, log) -> None:
    """Spawn a short-lived burst FX on the hand at throw apex (suction + flash)."""
    if _hand_particle_template is None:
        return
    try:
        arms = pawn.Arms
        psc = unrealsdk.construct_object("ParticleSystemComponent", pawn)
        arms.AttachComponentToSocket(psc, "LilithMeleeFX")
        psc.SetDepthPriorityGroup(1)
        psc.SetInWorldForeground(True)
        psc.SetOnlyOwnerSee(True)
        skill = get_lift_skill_defaults()
        if skill is not None:
            psc.SetTranslation(skill.FirstPersonTranslation)
            psc.SetScale(skill.FirstPersonScale)
        psc.SetTemplate(_hand_particle_template)
        psc.ActivateSystem()
        now = ENGINE.GetCurrentWorldInfo().TimeSeconds
        _fading_fx.append(
            (
                "psc",
                psc,
                now + _THROW_BURST_DURATION,
                skill.FirstPersonScale if skill else _ORB_BASE_SCALE,
            )
        )
        log("Throw burst FX spawned")
    except Exception as e:
        log(f"Throw burst FX failed: {e}")


def update_orb_scale(work_drain: float, dt: float, player, log) -> None:
    """Smoothly scale the hand orb and tattoo glow based on drain intensity."""
    global _orb_intensity
    drain_ref = orb_drain_sensitivity.value * 0.0001
    target_i = min(work_drain / drain_ref, 1.0) if drain_ref > 0 else 0.0
    smooth = orb_responsiveness.value * 0.1
    _orb_intensity += (target_i - _orb_intensity) * min(smooth * dt, 1.0)

    # Sine pulse for orb and tattoo
    try:
        t = ENGINE.GetCurrentWorldInfo().TimeSeconds
    except Exception:
        t = 0.0
    freq = pulse_frequency.value * 0.1  # tenths of Hz → Hz
    mag = pulse_magnitude.value * 0.01  # percentage → fraction
    omega = t * freq * math.tau  # 2π * freq * t
    orb_pulse = 1.0 + mag * math.sin(omega)
    tattoo_phase = math.radians(pulse_tattoo_offset.value)  # degrees → radians
    tattoo_pulse = 1.0 + mag * math.sin(omega + tattoo_phase)

    # Scale orb
    if _hand_psc is not None:
        min_frac = orb_min_scale.value * 0.01
        max_mult = orb_max_scale.value * 0.01
        scale = _ORB_BASE_SCALE * (min_frac + (1.0 - min_frac) * _orb_intensity * max_mult)
        _hand_psc.SetScale(scale * orb_pulse)

    # Pulse tattoo brightness: 0.4 base + 0.6 from intensity
    if player is not None:
        try:
            glow = (0.4 + 0.6 * _orb_intensity) * tattoo_pulse
            mat0 = player.Arms.GetMaterial(0)
            if mat0 is not None:
                mat0.SetScalarParameterValue("p_EnablePowerEmissive", glow)
            ami = player.ArmsMaterialInstance
            if ami is not None:
                ami.SetScalarParameterValue("p_EnablePowerEmissive", glow)
        except Exception:
            pass


def reset_orb_intensity() -> None:
    """Reset orb intensity for next grab."""
    global _orb_intensity
    _orb_intensity = 0.0


def reset_all() -> None:
    """Clear all cached UE3 references on map transition.

    All UE3 objects (PSCs, lights, particle templates, AkEvents) are destroyed
    on map unload — stale references cause engine crashes.
    """
    global _orb_intensity, _hand_particle_template, _original_emitter_state
    global _saved_emitter_state, _hand_psc, _target_light, _light_start_time
    global _cached_ak_activate, _cached_ak_loop, _cached_ak_collapse
    global _cached_ak_miss, _cached_ak_voice
    _orb_intensity = 0.0
    _hand_particle_template = None
    _original_emitter_state = None
    _saved_emitter_state = None
    _hand_psc = None
    _target_light = None
    _light_start_time = 0.0
    _fading_fx.clear()
    _cached_ak_activate = None
    _cached_ak_loop = None
    _cached_ak_collapse = None
    _cached_ak_miss = None
    _cached_ak_voice = None


def restore_vanilla_template(log) -> None:
    """Restore the vanilla particle template to its original state.

    Call when the mod is disabled so vanilla Phaselock FX are clean.
    """
    global _hand_particle_template
    if _hand_particle_template is not None:
        _set_emitter_loops(_hand_particle_template, _ORB_LOOP_EMITTERS, 1, log)
        _restore_emitter_curves(_hand_particle_template, log)
        _hand_particle_template = None


# ─── Public: Tattoo Glow ───────────────────────────────────────────────────


def start_tattoo_glow(pawn, log) -> None:
    """Enable Siren tattoo glow on the 1P arms."""
    try:
        mat0 = pawn.Arms.GetMaterial(0)
        if mat0 is not None:
            mat0.SetScalarParameterValue("p_EnablePowerEmissive", 1.0)
        ami = pawn.ArmsMaterialInstance
        if ami is not None:
            ami.SetScalarParameterValue("p_EnablePowerEmissive", 1.0)
        pass
    except Exception as e:
        log(f"Tattoo glow start failed: {e}")


def stop_tattoo_glow(pawn, log) -> None:
    """Disable Siren tattoo glow on the 1P arms."""
    try:
        mat0 = pawn.Arms.GetMaterial(0)
        if mat0 is not None:
            mat0.SetScalarParameterValue("p_EnablePowerEmissive", 0.0)
        ami = pawn.ArmsMaterialInstance
        if ami is not None:
            ami.SetScalarParameterValue("p_EnablePowerEmissive", 0.0)
        pass
    except Exception as e:
        log(f"Tattoo glow stop failed: {e}")


def queue_tattoo_fade(player, log) -> None:
    """Queue the tattoo glow for gradual fade-out."""
    try:
        now = ENGINE.GetCurrentWorldInfo().TimeSeconds
        _fading_fx.append(("tattoo", player, now + _FADE_DURATION))
    except Exception as e:
        log(f"Tattoo fade queue failed: {e}")


# ─── Public: Sound ──────────────────────────────────────────────────────────


def play_grab_sound(player, log) -> None:
    """Play the Activate (windup) sound on the player pawn."""
    if not enable_sounds.value:
        return
    _ensure_ak_cache()
    _play_ak(player, _cached_ak_activate, log)
    pass


def play_hold_sound(player, log) -> None:
    """Play the Loop (sustained hum) sound at grab moment."""
    if not enable_sounds.value:
        return
    _ensure_ak_cache()
    _play_ak(player, _cached_ak_loop, log)
    pass


def play_release_sound(player, log) -> None:
    """Play the Collapse (release) sound — also stops the Loop."""
    if not enable_sounds.value:
        return
    _ensure_ak_cache()
    _play_ak(player, _cached_ak_collapse, log)
    pass


def play_fizzle_sound(player, log) -> None:
    """Play the Miss (fizzle) sound."""
    if not enable_sounds.value:
        return
    _ensure_ak_cache()
    _play_ak(player, _cached_ak_miss, log)
    pass


def play_voice_line(player, log) -> None:
    """Play Maya's action skill voice line."""
    if not enable_sounds.value:
        return
    _ensure_ak_cache()
    _play_ak(player, _cached_ak_voice, log)
    pass


# ─── Public: Target Point Light ─────────────────────────────────────────────


def start_target_light(target, log) -> None:
    """Attach a purple/blue point light to the held pawn. Starts at brightness 0, lerps up."""
    global _target_light, _light_start_time
    if _target_light is not None:
        return
    try:
        plc = unrealsdk.construct_object("PointLightComponent", target)
        plc.Radius = 500.0
        plc.FalloffExponent = 0.5
        plc.Brightness = 0.0  # start dark, lerp up in tick
        plc.LightColor.R = 96
        plc.LightColor.G = 128
        plc.LightColor.B = 255
        plc.LightColor.A = 0
        plc.CastShadows = False
        plc.CastStaticShadows = False
        plc.CastDynamicShadows = False
        target.AttachComponent(plc)
        _target_light = plc
        _light_start_time = ENGINE.GetCurrentWorldInfo().TimeSeconds
    except Exception as e:
        log(f"Target light start failed: {e}")


def tick_target_light() -> None:
    """Lerp target light brightness from 0 to max over _LIGHT_LERP_DURATION."""
    if _target_light is None:
        return
    try:
        elapsed = ENGINE.GetCurrentWorldInfo().TimeSeconds - _light_start_time
        t = min(elapsed / _LIGHT_LERP_DURATION, 1.0)
        _target_light.SetLightProperties(_LIGHT_MAX_BRIGHTNESS * t)
    except Exception:
        pass


def stop_target_light(log) -> None:
    """Queue the target light for fade-out."""
    global _target_light
    if _target_light is None:
        return
    try:
        now = ENGINE.GetCurrentWorldInfo().TimeSeconds
        _fading_fx.append(("light", _target_light, now + _FADE_DURATION))
        pass
    except Exception as e:
        try:
            _target_light.DetachFromAny()
        except Exception:
            pass
        log(f"Target light: instant cleanup (fade queue failed: {e})")
    _target_light = None


# ─── Public: Fizzle (FX + Sound) ───────────────────────────────────────────


def play_fizzle(player, log) -> None:
    """Spawn fizzle hand FX (fire-and-forget PSC) + play fizzle sound."""
    try:
        skill = get_lift_skill_defaults()
        if skill is not None:
            fizzle_template = skill.FirstPersonParticleSystem_Fizzled
            if fizzle_template is not None:
                psc = unrealsdk.construct_object("ParticleSystemComponent", player)
                player.Arms.AttachComponentToSocket(psc, "LilithMeleeFX")
                psc.SetDepthPriorityGroup(1)
                psc.SetInWorldForeground(True)
                psc.SetOnlyOwnerSee(True)
                psc.SetTranslation(skill.FirstPersonTranslation)
                psc.SetScale(skill.FirstPersonScale)
                psc.SetTemplate(fizzle_template)
                psc.ActivateSystem()
                log("Fizzle: spawned fizzle PSC")
    except Exception as e:
        log(f"Fizzle FX failed: {e}")

    play_fizzle_sound(player, log)


# ─── Public: Fade Tick ──────────────────────────────────────────────────────


def tick_fading(log) -> None:
    """Process all fading FX entries. Call every tick (runs even when not active)."""
    if not _fading_fx:
        return
    try:
        now_fade = ENGINE.GetCurrentWorldInfo().TimeSeconds
    except Exception:
        now_fade = 0.0
    still_fading = []
    for entry in _fading_fx:
        kind, fx_obj, end_time = entry[0], entry[1], entry[2]
        try:
            remaining = end_time - now_fade
            if remaining <= 0:
                # Fade complete — clean up
                if kind == "psc":
                    fx_obj.DeactivateSystem()
                    fx_obj.DetachFromAny()
                elif kind == "light":
                    fx_obj.DetachFromAny()
                elif kind == "tattoo":
                    stop_tattoo_glow(fx_obj, log)
            else:
                # Fade in progress — interpolate
                t = remaining / _FADE_DURATION  # 1.0 → 0.0
                if kind == "psc":
                    start_scale = entry[3] if len(entry) > 3 else _ORB_BASE_SCALE
                    fx_obj.SetScale(start_scale * t)
                elif kind == "light":
                    fx_obj.Brightness = _LIGHT_MAX_BRIGHTNESS * t
                elif kind == "tattoo":
                    try:
                        mat0 = fx_obj.Arms.GetMaterial(0)
                        if mat0 is not None:
                            mat0.SetScalarParameterValue("p_EnablePowerEmissive", t)
                        ami = fx_obj.ArmsMaterialInstance
                        if ami is not None:
                            ami.SetScalarParameterValue("p_EnablePowerEmissive", t)
                    except Exception:
                        pass
                still_fading.append(entry)
        except Exception:
            # Object gone — skip it
            pass
    _fading_fx.clear()
    _fading_fx.extend(still_fading)
