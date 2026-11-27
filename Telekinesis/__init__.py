"""Telekinesis — Siren telekinesis replacing vanilla Phaselock.

Lifts enemies and loot toward the crosshair using spring physics.
Blocks ServerStartActionSkill entirely for Siren — zero vanilla Phaselock
code runs. We do our own target detection (via the target already resolved
by StartActionSkill's GetPreferredTarget call), stagger, spring physics,
charge, animation, particles, and tattoo glow.

Cooldown is managed via the vanilla SkillCooldownPool (we set its value
manually, it drains at its configured rate for HUD display).

Hooks:
  1. ServerStartActionSkill PRE — block vanilla, activate/release
  2. PlayerTick PRE — spring physics, charge drain, FX upkeep, orbit input
  3. PrevWeapon/NextWeapon PRE — scroll wheel push/pull while holding
  4. StartFire PRE — M1 throw, block ADS while holding
  5. EquipWeaponFromSlot PRE — block weapon switch (number keys) while holding
  6. MeleeAttack PRE — block melee while holding
  7. ThrowGrenade PRE — block grenade throws while holding
  8. ShowStatusMenu PRE — block inventory/menus while holding
"""

from __future__ import annotations

import math
from typing import Any

from mods_base import ENGINE, get_pc, build_mod, BoolOption, SliderOption, NestedOption, hook
import unrealsdk
from uemath import Rotator, Vector
from unrealsdk.hooks import Block, Type
from unrealsdk.unreal import UObject, WrappedStruct, BoundFunction, WeakPointer

from . import fx
from . import object_grab


# ─── State ───────────────────────────────────────────────────────────────────

_active: bool = False
_target_wp: WeakPointer | None = None  # WeakPointer to held WillowAIPawn
_target_uid: str = ""
_capture_dist: float = 0.0
_charge: float = 0.0  # 0.0-1.0
_activation_charge: float = 1.0  # charge fraction for next activation
_force_vec = None  # reusable Vector struct
_grab_blend_node = None  # WeaponAction AnimNodeSpecialMoveBlend on Arms mesh
_grab_bone: str = ""  # bone name to apply force to ('' = whole mesh)
_grab_bone_idx: int = -1  # index into PhysicsAssetInstance.Bodies
_grab_bone_mass: float = 1.0  # mass of just the grab bone body
_grab_bone_chain: list = []  # indices of grab bone + parents in Bodies array
_grab_orig_mass_scales: dict = {}  # body_idx -> original InstanceMassScale
_pid_integral_x: float = 0.0  # PID integral accumulator
_pid_integral_y: float = 0.0
_pid_integral_z: float = 0.0
_grab_mass: float = 1.0  # total PhysX ragdoll mass
_grab_body_count: int = 1  # number of physics bodies in ragdoll
_duration_mult: float = 1.0  # drain rate multiplier from Phaselock duration skills
_hand_fx_delay = 0.0  # TimeSeconds when hand FX should spawn (0 = not pending)
_grab_pending = False  # True during windup — anim plays but no forces yet
_saved_stagger_settings = None  # original (body_class, rest_vel,
# give_up, unstagger_give_up)
_windup_sound_time = 0.0  # TimeSeconds when windup AkEvent
# should fire (0 = not pending)
_windup_sound_played = False  # True once the windup sound has been played this activation
_target_dead: bool = False  # True when held target dies (hold-dead mode)
_throwing: bool = False  # True during throw — applies throw force instead of spring
_throw_pending: float = 0.0  # TimeSeconds when throw force
# should start (0 = not pending)
_throw_windup_start: float = 0.0  # TimeSeconds when throw windup began
_throw_dir: Vector = Vector((0.0, 0.0, 0.0))  # unit direction for throw
_throw_elapsed: float = 0.0  # seconds elapsed since throw started
_throw_sound_played: bool = False  # True once throw release sound has fired
_throw_sound_time: float = 0.0  # TimeSeconds when throw release
# sound should fire (0 = not scheduled)
_crushing: bool = False  # True during crush windup — bones converge
_crush_start: float = 0.0  # TimeSeconds when crush started


# ─── Options ─────────────────────────────────────────────────────────────────

enabled = BoolOption(
    "Telekinesis Enabled",
    True,
    description="Master toggle. When disabled, Phaselock works normally.",
)

spring_strength = SliderOption(
    "Spring Strength",
    50,
    10,
    500,
    5,
    True,
    description=(
        "How strongly the enemy is pulled toward your crosshair. "
        "Higher = snappier tracking, lower = floaty. Default: 50"
    ),
)

bone_kp = SliderOption(
    "Bone Kp",
    20,
    1,
    100,
    1,
    True,
    description=(
        "Proportional gain for bone grab. Controls pull strength toward crosshair. "
        "Higher = snappier, lower = floaty. Default: 20"
    ),
)

bone_ki = SliderOption(
    "Bone Ki",
    0,
    0,
    50,
    1,
    True,
    description=(
        "Integral gain for bone grab. Accumulates error over time to ensure "
        "heavy pawns reach the crosshair. Higher = faster correction, too high = overshoot. "
        "0 = disabled. Default: 0"
    ),
)

bone_kd = SliderOption(
    "Bone Kd",
    50,
    0,
    100,
    1,
    True,
    description=(
        "Derivative gain for bone grab. Controls braking/damping. "
        ">10 = overdamped (no overshoot), <10 = underdamped (bouncy). Default: 50"
    ),
)

hold_gravity = SliderOption(
    "Hold Gravity %",
    100,
    0,
    100,
    5,
    True,
    description=(
        "Gravity while held (CustomGravityScaling). "
        "0 = weightless float, 100 = full gravity. Default: 100"
    ),
)

min_hold_distance = SliderOption(
    "Min Hold Distance",
    200,
    100,
    1000,
    50,
    True,
    description="Minimum hold distance — prevents clipping into player. Default: 200",
)

max_hold_distance = SliderOption(
    "Max Hold Distance",
    4000,
    500,
    5000,
    100,
    True,
    description="Maximum capture distance. Default: 4000",
)

hold_distance_override = SliderOption(
    "Hold Distance Override",
    0,
    0,
    3000,
    50,
    True,
    description="Override capture distance. 0 = use actual distance at capture. Default: 0",
)

scroll_step = SliderOption(
    "Scroll Distance Step",
    100,
    25,
    500,
    25,
    True,
    description="How much each scroll wheel notch pushes/pulls the held enemy (UU). Default: 100",
)

charge_drain_rate = SliderOption(
    "Charge Drain Rate",
    0,
    0,
    20,
    1,
    True,
    description=(
        "How fast charge drains while holding (hundredths per second). "
        "0 = no passive drain, 3 = ~33s hold time, 10 = 10s. Default: 0"
    ),
)

force_drain_rate = SliderOption(
    "Force Drain Rate",
    10,
    0,
    200,
    5,
    True,
    description=(
        "Extra charge drain from work done (force x distance). "
        "Heavier enemies and violent acceleration cost more charge. "
        "Smooth orbiting is nearly free. 0 = disabled. Default: 10"
    ),
)

min_activation_pct = SliderOption(
    "Min Activation Charge %",
    5,
    1,
    100,
    1,
    True,
    description=(
        "Minimum cooldown progress required to activate early. "
        "5 = can activate after 5%% of cooldown has elapsed. "
        "100 = must be fully recharged. Default: 5"
    ),
)

activation_cost = SliderOption(
    "Activation Cost %",
    20,
    0,
    100,
    5,
    True,
    description=(
        "Charge consumed on each grab (percentage of max). "
        "Prevents infinite re-grab by costing charge on activation. "
        "0 = free, 50 = half charge per grab. Default: 20"
    ),
)

grab_windup_delay = SliderOption(
    "Grab Windup Delay",
    35,
    0,
    100,
    1,
    True,
    description=(
        "Delay before the grab fires (hundredths of a second). "
        "The hand animation plays during this time. At 0 the grab is instant. "
        "Default: 35 (0.35s — syncs with the hand reaching out)"
    ),
)

grab_anim_time = SliderOption(
    "Grab Anim Freeze Point",
    40,
    0,
    87,
    1,
    True,
    description=(
        "Where in the grab animation the hand freezes (hundredths of a second). "
        "The animation plays normally from 0 to this point, then holds. "
        "0 = instant freeze, 40 = hand extended pose, 87 = end. Default: 40"
    ),
)

resume_anim_on_release = BoolOption(
    "Resume Hand Anim on Release",
    True,
    description=(
        "Play the rest of the grab animation when releasing. If off, the hand snaps back instantly."
    ),
)

sound_peak_offset = SliderOption(
    "Sound Peak Offset",
    55,
    0,
    100,
    1,
    True,
    description=(
        "How far into the Activate sound the 'peak' is (hundredths of a second). "
        "The windup sound is timed so the peak aligns with the grab moment. "
        "Default: 55 (0.55s). Adjust if the whoosh feels early/late."
    ),
)

throw_sound_offset = SliderOption(
    "Throw Sound Offset",
    1000,
    100,
    2000,
    10,
    True,
    description=(
        "How early the release sound plays before the throw apex (milliseconds). "
        "Time it so the crackle lands at the moment of release. Default: 1000 (1.0s)"
    ),
)

fx_settings = NestedOption(
    "FX Settings",
    children=[
        fx.enable_sounds,
        sound_peak_offset,
        throw_sound_offset,
        grab_anim_time,
        resume_anim_on_release,
        fx.orb_min_scale,
        fx.orb_max_scale,
        fx.orb_responsiveness,
        fx.orb_drain_sensitivity,
        fx.pulse_frequency,
        fx.pulse_magnitude,
        fx.pulse_tattoo_offset,
    ],
    description="Sound effects, hand animation, and orb visual settings.",
)

hold_dead = BoolOption(
    "Hold Dead Targets",
    True,
    description=(
        "Keep holding an enemy's ragdoll after they die. Release with F or when charge runs out."
    ),
)

fallback_phaselock = BoolOption(
    "Fallback to Phaselock",
    False,
    description=(
        "Use vanilla Phaselock on targets that can't be ragdolled (Goliaths, Constructors, etc.)."
    ),
)

throw_force = SliderOption(
    "Throw Force",
    15000,
    500,
    50000,
    500,
    True,
    description=(
        "Acceleration applied when throwing with M1 (left click). "
        "Applied for a short burst. Higher = faster throw. Default: 15000"
    ),
)

throw_duration = SliderOption(
    "Throw Duration",
    28,
    1,
    200,
    1,
    True,
    description=(
        "How long throw force is applied (hundredths of a second). "
        "The animation auto-scales to fit windup + force + recovery. "
        "28 = 1.0x anim speed with default windup/recovery. Default: 28"
    ),
)

throw_windup_pct = SliderOption(
    "Throw Windup %",
    20,
    0,
    80,
    1,
    True,
    description=(
        "Percentage of throw duration spent winding up before force is applied. "
        "The melee animation plays during this time. 0 = instant force. Default: 30"
    ),
)

throw_recovery_pct = SliderOption(
    "Throw Recovery %",
    50,
    0,
    80,
    1,
    True,
    description=(
        "Percentage of throw duration at the end where force stops (hand returning to neutral). "
        "Enemy is released, animation finishes naturally. 0 = force until end. Default: 30"
    ),
)

crush_duration = SliderOption(
    "Crush Duration",
    30,
    5,
    100,
    1,
    True,
    description=(
        "How long the crush windup takes before the kill fires (hundredths of a second). "
        "Bones converge during this time. Default: 30 (0.3s)"
    ),
)

crush_anim_duration = SliderOption(
    "Crush Anim Duration",
    25,
    5,
    100,
    1,
    True,
    description=(
        "How long the hand animation takes to reach the crush pose (hundredths of a second). "
        "Separate from crush duration so you can offset the kill timing from the animation. "
        "Default: 25 (0.25s)"
    ),
)

crush_strength = SliderOption(
    "Crush Strength",
    30,
    10,
    100,
    1,
    True,
    description=(
        "Peak crush force (tenths). Ramps up from 0 over the crush duration. "
        "10 = 1x, 30 = 3x, 100 = 10x. Default: 30"
    ),
)

crush_anim_time = SliderOption(
    "Crush Anim Pose",
    87,
    0,
    87,
    1,
    True,
    description=(
        "Where in the grab animation the hand moves to during crush (hundredths of a second). "
        "87 = fully closed fist. Default: 87"
    ),
)

invert_scroll = BoolOption(
    "Invert Scroll",
    True,
    description=(
        "Swap scroll direction for push/pull. Default: scroll up = pull, scroll down = push."
    ),
)

log_to_console = BoolOption(
    "Log to Console",
    False,
    description="Print debug messages to console.",
)


# ─── Helpers ─────────────────────────────────────────────────────────────────


def log(msg: str) -> None:
    if log_to_console.value:
        print(f"[TK] {msg}")


def _pawn_uid(pawn) -> str:
    try:
        return pawn.PathName(pawn)
    except Exception:
        return ""


def _get_target():
    """Dereference target WeakPointer. Returns pawn or None."""
    if _target_wp is None:
        return None
    try:
        return _target_wp()
    except Exception:
        return None


def _is_siren(pc) -> bool:
    """Check if the player's action skill is Phaselock (Siren)."""
    try:
        skill_def = pc.PlayerSkillTree.GetActionSkill()
        path = skill_def.PathName(skill_def)
        return "Siren" in path or "Phaselock" in path
    except Exception:
        return False


# ─── Animation ───────────────────────────────────────────────────────────────

_GRAB_ANIM_NAME = "Phase_Lock_Lift"  # 1P animation on Arms mesh (0.87s)
_grab_anim_playing = False  # True while intro is still playing to freeze point


def _find_arms_weapon_action_node(pawn) -> object | None:
    """Find the WeaponAction AnimNodeSpecialMoveBlend on the Arms mesh."""
    try:
        arms = pawn.Arms
        if arms is None:
            return None
        arms_path = arms.PathName(arms)
    except Exception:
        return None
    try:
        nodes = unrealsdk.find_all("AnimNodeSpecialMoveBlend")
        for n in nodes:
            try:
                np = n.PathName(n)
                if arms_path in np and "Default__" not in np and str(n.NodeName) == "WeaponAction":
                    return n
            except Exception:
                continue
    except Exception:
        pass
    return None


def _start_grab_anim() -> None:
    """Play the grab animation at normal speed from the start.
    Tick will freeze it at the slider-controlled point once reached."""
    global _grab_anim_playing
    if _grab_blend_node is None:
        return
    freeze = grab_anim_time.value / 100.0
    try:
        anim_len = _grab_blend_node.PlayCustomAnim(
            _GRAB_ANIM_NAME,
            1.0,  # Rate — normal speed
            0.15,  # BlendInTime
            99.0,  # BlendOutTime — huge so it never blends out
            True,  # bLooping — must be True or node deactivates
            True,  # bOverride
        )
        _grab_anim_playing = True
        if anim_len <= 0:
            log("Grab anim returned 0 — animation not found on Arms skeleton")
            _grab_anim_playing = False
            return
        log(f"Grab anim started: {_GRAB_ANIM_NAME} playing to freeze={freeze:.2f}s")
    except Exception as e:
        log(f"Grab anim start failed: {e}")


def _get_lift_skill_instance():
    """Get the live LiftActionSkill instance (not CDO)."""
    try:
        for s in unrealsdk.find_all("LiftActionSkill"):
            try:
                if "Default__" in s.PathName(s):
                    continue
                return s
            except Exception:
                continue
    except Exception:
        pass
    return None


_FIZZLE_DURATION = 0.53  # Phase_Lock_Fail animation length


def _play_fizzle(player) -> None:
    """Play fizzle feedback: 1P fail animation + fizzle particle FX + miss sound."""
    # 1P fail animation on arms
    try:
        node = _find_arms_weapon_action_node(player)
        if node is not None:
            node.PlayCustomAnim(
                "Phase_Lock_Fail",
                1.0,
                0.15,
                0.1,  # BlendOutTime
                False,
                True,
            )
            log("Fizzle: Phase_Lock_Fail anim")
    except Exception as e:
        log(f"Fizzle anim failed: {e}")

    # Fizzle FX + sound
    fx.play_fizzle(player, log)


_MELEE_ANIM_DURATION = 0.7  # 1P Siren melee: 21 frames @ 30fps (Anim_Siren.Siren_1st)


def _play_throw_anim() -> None:
    """Play a quick push gesture for the throw, scaled so the animation
    fills the full throw timeline (windup + force + recovery)."""
    if _grab_blend_node is None:
        return
    try:
        # Throw duration slider = force application time
        # Total animation = force_time / force_fraction
        force_time = throw_duration.value * 0.01
        windup = throw_windup_pct.value * 0.01
        recovery = throw_recovery_pct.value * 0.01
        force_frac = max(1.0 - windup - recovery, 0.05)
        total_time = force_time / force_frac
        rate = _MELEE_ANIM_DURATION / total_time if total_time > 0 else 1.0
        # Blend-in matches windup so the hold→throw transition is seamless
        windup_time = total_time * throw_windup_pct.value * 0.01
        blend_in = max(windup_time, 0.05)  # at least 50ms to avoid pop
        recovery_time = total_time * throw_recovery_pct.value * 0.01
        blend_out = max(recovery_time, 0.1)
        # Melee is a forward punch/thrust — looks like a force push
        _grab_blend_node.PlayCustomAnim(
            "Melee",
            rate,
            blend_in,
            blend_out,
            False,  # bLooping
            True,  # bOverride
        )
        # Suppress sound notifies on the melee anim sequence
        seq = _grab_blend_node.GetCustomAnimNodeSeq()
        if seq is not None:
            seq.bNoNotifies = True
        log("Throw anim: Melee thrust")
    except Exception as e:
        log(f"Throw anim failed: {e}")


def _stop_grab_anim(fade_only: bool = False) -> None:
    """Stop the grab animation.

    fade_only=False (default): reverse to start (hand opens — normal release).
    fade_only=True: just blend out from current pose (crush kill — hand stays closed).
    """
    global _grab_anim_reversing
    if _grab_blend_node is None:
        return
    try:
        if fade_only:
            # Re-seek to crush pose, then play forward very slowly.
            # The remaining anim time at this rate = hold time before blend-out.
            end_pos = crush_anim_time.value / 100.0
            _grab_blend_node.PlayCustomAnim(
                _GRAB_ANIM_NAME,
                0.5,  # slow — holds the pose briefly before blend-out
                0.0,  # no blend in — already at this pose
                0.1,  # blend out when anim ends
                False,  # non-looping
                True,  # override
                end_pos,  # start at crush pose
            )
            seq = _grab_blend_node.GetCustomAnimNodeSeq()
            if seq is not None:
                seq.bNoNotifies = True
            log(f"Grab anim: hold at crush pose {end_pos:.2f}s, slow forward + blend out")
        elif resume_anim_on_release.value:
            # Read where the animation currently is
            current_pos = grab_anim_time.value / 100.0  # fallback: hold freeze point
            try:
                seq = _grab_blend_node.GetCustomAnimNodeSeq()
                if seq is not None:
                    current_pos = seq.CurrentTime
            except Exception:
                pass
            # Play non-looping from current position in reverse
            _grab_blend_node.PlayCustomAnim(
                _GRAB_ANIM_NAME,
                0.001,  # near-zero rate (we'll override below)
                0.0,  # BlendInTime — already at this pose
                0.2,  # BlendOutTime — smooth blend when it reaches 0
                False,  # bLooping — play once
                True,  # bOverride
                current_pos,  # StartTime
            )
            seq = _grab_blend_node.GetCustomAnimNodeSeq()
            if seq is not None:
                seq.Rate = -1.0
                seq.SetPosition(current_pos, False)
                log(f"Grab anim reversing from {current_pos:.2f}s")
            else:
                _grab_blend_node.StopCustomAnim(0.2)
                log("Grab anim: no seq, falling back to StopCustomAnim")
        else:
            _grab_blend_node.StopCustomAnim(0.2)
    except Exception as e:
        log(f"Grab anim stop failed: {e}")


# ─── Physics Helpers ─────────────────────────────────────────────────────────


def _get_total_phys_mass(target) -> tuple[float, int]:
    """Get the total PhysX ragdoll mass and body count.
    Only works when ragdolled (PhysicsAssetInstance populated).
    Falls back to gameplay Mass if unavailable."""
    try:
        phys_inst = target.Mesh.PhysicsAssetInstance
        if phys_inst is not None:
            bodies = phys_inst.Bodies
            if bodies is not None and len(bodies) > 0:
                n = len(bodies)
                total = sum(bodies[i].GetBodyMass() for i in range(n))
                if total > 0:
                    log(f"PhysX mass: {total:.1f} ({n} bodies)")
                    return total, n
    except Exception as e:
        log(f"PhysX mass read failed: {e}")

    # Fallback: gameplay mass, assume 1 body
    try:
        m = target.Mass
        if m is not None and m > 0:
            log(f"PhysX mass FALLBACK (gameplay): {m:.1f}")
            return float(m), 1
    except Exception:
        pass
    log("PhysX mass FALLBACK: 100.0")
    return 100.0, 1


def _set_gravity(pawn, scale: float) -> None:
    """Set gravity scaling on a ragdolled pawn."""
    try:
        pawn.CustomGravityScaling = scale
    except Exception as e:
        log(f"CustomGravityScaling failed: {e}")
    try:
        phys_inst = pawn.Mesh.PhysicsAssetInstance
        if phys_inst is not None:
            bodies = phys_inst.Bodies
            if bodies is not None:
                for i in range(len(bodies)):
                    bodies[i].CustomGravityFactor = scale
    except Exception as e:
        log(f"Per-body gravity failed: {e}")


def _find_grab_bone(mesh, pc) -> str:
    """Pick the physics bone closest to the player's aim ray.

    Iterates all physics bodies in the asset, gets each bone's world
    position, and returns the one with the smallest perpendicular
    distance from the crosshair ray. Works for humanoids and non-humanoids.
    """
    phys_asset = None
    try:
        phys_asset = mesh.PhysicsAsset
    except Exception:
        pass
    if phys_asset is None:
        return ""

    try:
        setups = phys_asset.BodySetup
        if setups is None or len(setups) == 0:
            return ""
    except Exception:
        return ""

    # Build aim ray
    try:
        view_loc = pc.CalcViewLocation
        view_rot = pc.CalcViewRotation
        eye_x, eye_y, eye_z = view_loc.X, view_loc.Y, view_loc.Z
        fwd = Rotator(view_rot).to_vector()
        fwd_x, fwd_y, fwd_z = fwd.x, fwd.y, fwd.z
    except Exception:
        return ""

    best_name = ""
    best_perp = float("inf")
    n = len(setups)

    for i in range(n):
        try:
            name = str(setups[i].BoneName)
            bone_loc = mesh.GetBoneLocation(name, 0)
            dx = bone_loc.X - eye_x
            dy = bone_loc.Y - eye_y
            dz = bone_loc.Z - eye_z

            # Project onto aim ray
            along = dx * fwd_x + dy * fwd_y + dz * fwd_z
            if along < 0:
                continue  # behind camera

            # Perpendicular distance from aim ray
            px = dx - fwd_x * along
            py = dy - fwd_y * along
            pz = dz - fwd_z * along
            perp = math.sqrt(px * px + py * py + pz * pz)

            if perp < best_perp:
                best_perp = perp
                best_name = name
        except Exception:
            continue

    if best_name:
        log(f"Grab bone: {best_name} (perp={best_perp:.0f})")
    return best_name


# ─── Cooldown ────────────────────────────────────────────────────────────────


def _check_cooldown(pc) -> float:
    """Check cooldown progress. Returns 1.0 if ready, 0.0-1.0 if cooling down."""
    try:
        pool = pc.SkillCooldownPool
        if pool is None or pool.Data is None:
            return 1.0
        current = pool.Data.GetCurrentValue()
        if current <= 0:
            return 1.0  # ready
        max_val = pool.Data.GetMaxValue()
        if max_val <= 0:
            return 1.0
        return (max_val - current) / max_val  # 0.0 = just started, 1.0 = ready
    except Exception:
        return 1.0


# ─── Activate / Release ─────────────────────────────────────────────────────


def _cleanup() -> None:
    """Clean up Telekinesis state — restore gravity, stop anim/FX, clear refs."""
    global _active, _target_wp, _target_uid, _hand_fx_delay, _grab_anim_playing, _grab_pending
    global _grab_bone, _grab_bone_idx, _grab_bone_mass, _grab_bone_chain, _grab_orig_mass_scales
    global _pid_integral_x, _pid_integral_y, _pid_integral_z
    global _grab_mass, _grab_blend_node, _duration_mult
    global _windup_sound_time, _windup_sound_played, _target_dead, _throwing, _throw_pending
    global _throw_sound_time, _throw_sound_played, _crushing

    fx.reset_orb_intensity()
    _throw_pending = 0.0
    _throw_sound_time = 0.0
    _throw_sound_played = False
    _duration_mult = 1.0
    log("_cleanup: enter")
    global _saved_stagger_settings
    target = _get_target()
    if target is not None:
        if object_grab.target_is_object:
            object_grab.cleanup(target)
        else:
            _set_gravity(target, 1.0)
            # Restore bone mass scales
            if _grab_orig_mass_scales:
                try:
                    mesh = target.Mesh
                    bodies = mesh.PhysicsAssetInstance.Bodies
                    setups = mesh.PhysicsAsset.BodySetup
                    for i, orig_scale in _grab_orig_mass_scales.items():
                        bodies[i].InstanceMassScale = orig_scale
                        if i < len(setups):
                            bodies[i].UpdateMassProperties(setups[i])
                    pass  # masses restored
                except Exception:
                    pass
            try:
                now = ENGINE.GetCurrentWorldInfo().TimeSeconds
                target.StaggerState.StaggerEndRagdollTime = now + 2.0
            except Exception:
                pass

    # Restore stagger settings (pawn only, skip if target is gone — objects are destroyed)
    if not object_grab.target_is_object and _saved_stagger_settings is not None:
        if target is not None:
            try:
                body_class, rest_vel, give_up, unstagger_give_up = _saved_stagger_settings
                ss = body_class.StaggerSettings
                ss.StaggeredRestVelocity = rest_vel
                ss.StaggeredRestGiveUpTime = give_up
                ss.UnstaggerGiveUpTime = unstagger_give_up
            except Exception as e:
                log(f"Stagger restore failed: {e}")
        _saved_stagger_settings = None

    _hand_fx_delay = 0.0
    log("_cleanup: stopping anim")
    _stop_grab_anim(fade_only=_crushing)
    fx.stop_hand_fx(log, instant=_crushing)
    fx.stop_target_light(log)
    try:
        player = get_pc().Pawn
        if player is not None:
            fx.queue_tattoo_fade(player, log)
            # Throw/crush pre-fire the release sound — don't double-play
            if not _throwing and not _crushing:
                fx.play_release_sound(player, log)
    except Exception:
        pass

    _active = False
    _target_wp = None
    _target_uid = ""
    _grab_bone = ""
    _grab_bone_idx = -1
    _grab_bone_mass = 1.0
    _grab_bone_chain = []
    _grab_orig_mass_scales = {}
    _pid_integral_x = _pid_integral_y = _pid_integral_z = 0.0
    _grab_mass = 1.0
    _grab_body_count = 1
    _grab_blend_node = None
    _grab_anim_playing = False
    _grab_pending = False
    _windup_sound_time = 0.0
    _windup_sound_played = False
    _target_dead = False
    _throwing = False
    _crushing = False
    object_grab.reset_state()
    log("_cleanup: done")


def _release() -> None:
    """Release the held target. Pool is already at the right value from tick —
    vanilla drain handles the cooldown automatically."""
    _cleanup()


def _grab_moment(pc) -> None:
    """The 'grab' fires — stagger the target, disable gravity, start forces and FX.
    Called when the hand FX delay fires (synced with the initial flash)."""
    global _grab_pending, _saved_stagger_settings
    _grab_pending = False

    target = _get_target()
    if target is None:
        log("Grab moment: target gone")
        _cleanup()
        return

    player = pc.Pawn
    if player is None:
        _cleanup()
        return

    # Stagger into ragdoll
    try:
        already_ragdolled = target.IsStaggered() and int(target.StaggerState.State) == 0
    except Exception:
        already_ragdolled = False

    if not already_ragdolled:
        log("Grab moment: staggering target")
        target.Stagger(None)

    # Read PhysX mass + body count now that ragdoll is active
    global _grab_mass, _grab_body_count, _grab_bone_mass, _duration_mult
    _grab_mass, _grab_body_count = _get_total_phys_mass(target)

    # Redistribute mass: make grab bone heavy so bVelChange=False impulses
    # are naturally damped by inertia. Floor underweight bones for joint stability.
    global _grab_orig_mass_scales
    _grab_orig_mass_scales = {}
    if _grab_bone and _grab_bone_idx >= 0:
        try:
            mesh = target.Mesh
            phys_asset = mesh.PhysicsAsset
            phys_inst = mesh.PhysicsAssetInstance
            bodies = phys_inst.Bodies
            setups = phys_asset.BodySetup
            n = len(bodies)
            ns = len(setups)

            grab_scale = 10.0
            min_bone_mass = _grab_mass / (2.0 * n)

            # Save originals and read current masses (single read pass)
            orig_scales = []
            orig_masses = []
            for i in range(n):
                orig_scales.append(bodies[i].InstanceMassScale)
                orig_masses.append(bodies[i].GetBodyMass())
            _grab_orig_mass_scales = dict(enumerate(orig_scales))

            # Log any zero-mass bodies
            zero_bones = [
                str(setups[i].BoneName) for i in range(min(n, ns)) if orig_masses[i] <= 0.0
            ]
            if zero_bones:
                log(f"Zero-mass bodies: {', '.join(zero_bones)}")

            # Check if ImpactStagger already baked — if any scale != 1.0, it ran.
            # We still need to apply grab bone boost and floor on top.
            is_baked = any(abs(s - 1.0) > 0.01 for s in orig_scales)
            if is_baked:
                log("ImpactStagger already baked masses — applying grab bone boost on top")

            # Compute target masses: grab bone 10x, others floored
            target_masses = []
            for i in range(n):
                if i == _grab_bone_idx:
                    target_masses.append(orig_masses[i] * grab_scale)
                else:
                    m = orig_masses[i]
                    target_masses.append(max(m, min_bone_mass) if m > 0.0 else m)

            # Single write pass: compute per-bone scale and apply
            boosted = 0
            for i in range(n):
                if orig_masses[i] > 0.0:
                    bone_scale = orig_scales[i] * target_masses[i] / orig_masses[i]
                else:
                    bone_scale = orig_scales[i]
                bodies[i].InstanceMassScale = bone_scale
                if i < ns:
                    bodies[i].UpdateMassProperties(setups[i])
                if i != _grab_bone_idx and target_masses[i] > orig_masses[i]:
                    boosted += 1

            _grab_bone_mass = bodies[_grab_bone_idx].GetBodyMass()
            _grab_mass = sum(bodies[i].GetBodyMass() for i in range(n))

            log(
                f"Grab bone: {_grab_bone} idx={_grab_bone_idx} "
                f"bone_mass={_grab_bone_mass:.2f} total={_grab_mass:.2f} "
                f"min_bone={min_bone_mass:.2f} boosted={boosted}"
            )
        except Exception as e:
            _grab_bone_mass = _grab_mass / _grab_body_count
            log(f"Mass redistrib failed: {e}")
    else:
        _grab_bone_mass = _grab_mass / _grab_body_count

    # Read Phaselock duration from skill tree (includes all skill/mod bonuses)
    # Scale drain inversely: more duration skill points → slower drain
    _BASE_PHASELOCK_DURATION = 6.0
    try:
        dur_attr = unrealsdk.find_object(
            "AttributeDefinition",
            "GD_Siren_Skills.Misc.Att_Phaselock_Duration",
        )
        if dur_attr is not None:
            effective_dur = dur_attr.GetValue(player)[0]
            if effective_dur > 0:
                _duration_mult = _BASE_PHASELOCK_DURATION / effective_dur
                log(
                    f"Duration scaling: {effective_dur:.1f}s "
                    f"(base {_BASE_PHASELOCK_DURATION}s) "
                    f"-> drain mult {_duration_mult:.3f}"
                )
            else:
                _duration_mult = 1.0
        else:
            _duration_mult = 1.0
    except Exception as e:
        log(f"Duration attribute read failed: {e}")
        _duration_mult = 1.0

    # Set gravity
    grav = hold_gravity.value / 100.0
    _set_gravity(target, grav)

    # Stagger recovery already disabled in _activate — just extend the ragdoll timer.

    # Extend ragdoll timer
    try:
        now = ENGINE.GetCurrentWorldInfo().TimeSeconds
        target.StaggerState.StaggerEndRagdollTime = now + 300.0
    except Exception:
        pass

    # Start hand FX, target light, bubble sphere, and hold sound
    fx.start_hand_fx(player, log)
    fx.start_target_light(target, log)
    fx.play_hold_sound(player, log)

    log("Grab moment: target grabbed")


def _activate(pc, player, target) -> None:
    """Activate force hold on a target."""
    global _active, _target_wp, _target_uid, _capture_dist, _charge
    global _grab_bone, _grab_mass, _grab_blend_node, _saved_stagger_settings
    global _hand_fx_delay, _grab_pending, _windup_sound_time, _windup_sound_played

    # Capture distance from camera to target
    try:
        view_loc = pc.CalcViewLocation
        eye_x, eye_y, eye_z = view_loc.X, view_loc.Y, view_loc.Z
    except Exception:
        eye_x = player.Location.X
        eye_y = player.Location.Y
        eye_z = player.Location.Z + player.EyeHeight

    tl = target.Location
    dist = Vector(tl).distance(Vector((eye_x, eye_y, eye_z)))

    override = hold_distance_override.value
    if override > 0:
        dist = float(override)
    _capture_dist = max(float(min_hold_distance.value), dist)

    _target_wp = WeakPointer(target)
    _target_uid = _pawn_uid(target)
    _charge = _activation_charge - (activation_cost.value / 100.0)
    if _charge <= 0.0:
        _charge = 0.01  # tiny amount so it activates but drains almost immediately
    _active = True
    _grab_pending = True  # stagger + forces deferred until flash

    # Find the best bone to grab (precompute for when stagger fires)
    global _grab_bone_idx, _grab_bone_mass
    try:
        _grab_bone = _find_grab_bone(target.Mesh, pc)
        if _grab_bone:
            # Cache bone body index and mass for spring physics
            phys_asset = target.Mesh.PhysicsAsset
            _grab_bone_idx = phys_asset.FindBodyIndex(_grab_bone) if phys_asset else -1
            log(f"Grab bone: {_grab_bone} (body idx={_grab_bone_idx})")
        else:
            _grab_bone_idx = -1
            log("No grab bone found — using whole mesh")
    except Exception:
        _grab_bone = ""
        _grab_bone_idx = -1
    _grab_bone_mass = 1.0  # updated in _grab_moment when ragdoll is ready

    # Mass is read in _grab_moment after stagger creates the ragdoll
    _grab_mass = 100.0  # placeholder until ragdolled

    # Disable ragdoll recovery immediately — can't wait for _grab_moment
    # because IS may have already staggered the pawn and the recovery timer
    # could expire during the windup delay.
    try:
        bc = target.BodyClass
        ss = bc.StaggerSettings
        _saved_stagger_settings = (
            bc,
            ss.StaggeredRestVelocity,
            ss.StaggeredRestGiveUpTime,
            ss.UnstaggerGiveUpTime,
        )
        ss.StaggeredRestVelocity = 0.0
        ss.StaggeredRestGiveUpTime = 0.0
        ss.UnstaggerGiveUpTime = 0.0
    except Exception as e:
        log(f"Stagger settings override failed: {e}")
        _saved_stagger_settings = None

    try:
        ai_name = target.AIClass.Name if target.AIClass is not None else "?"
        bc_name = target.BodyClass.Name if target.BodyClass is not None else "?"
        log(f"Grabbed: ai={ai_name} body={bc_name} bone={_grab_bone}")
    except Exception:
        pass

    # 1P grab animation — starts playing immediately (hand reaches out)
    try:
        _grab_blend_node = _find_arms_weapon_action_node(player)
        if _grab_blend_node is not None:
            _start_grab_anim()
        else:
            log("WeaponAction node not found on Arms")
    except Exception as e:
        log(f"Grab anim setup failed: {e}")
        _grab_blend_node = None

    # Tattoo glow starts immediately (it's on the player, not target-dependent)
    fx.start_tattoo_glow(player, log)

    # Maya voice line on cast
    fx.play_voice_line(player, log)

    # Schedule the "grab moment" — stagger, gravity, forces, FX all fire together
    delay = grab_windup_delay.value / 100.0
    if delay <= 0.0:
        # Instant grab — no windup, skip windup sound
        _hand_fx_delay = 0.0
        _windup_sound_time = 0.0
        _windup_sound_played = True
        _grab_moment(pc)
    else:
        try:
            now = ENGINE.GetCurrentWorldInfo().TimeSeconds
            _hand_fx_delay = now + delay
            # Schedule windup sound so its peak aligns with the grab moment.
            # peak_offset = how far into the sound the peak is (e.g. 0.55s).
            # If windup >= peak_offset: delay sound start so peak hits at grab.
            # If windup < peak_offset: play immediately (can't start earlier).
            peak = sound_peak_offset.value / 100.0
            if delay >= peak:
                _windup_sound_time = now + (delay - peak)
            else:
                # Windup too short for full buildup — play immediately
                _windup_sound_time = now
            _windup_sound_played = False
            log(
                f"Sound scheduled: peak_offset={peak:.2f}s, windup={delay:.2f}s, "
                f"sound_delay={(delay - peak) if delay >= peak else 0:.2f}s"
            )
        except Exception:
            _hand_fx_delay = 0.0
            _windup_sound_time = 0.0
            _windup_sound_played = True
            _grab_moment(pc)

    try:
        ename = target.AIClass.Name if target.AIClass is not None else "?"
    except Exception:
        ename = "?"
    log(f"Windup on {ename} dist={_capture_dist:.0f} charge={_charge * 100:.0f}%")


# ─── Object Grab Activation ─────────────────────────────────────────────────


def _do_object_grab_moment(pc) -> None:
    """Fire the object grab moment — object-specific setup + common FX."""
    global _grab_pending, _grab_mass, _grab_body_count, _duration_mult
    _grab_pending = False

    target = _get_target()
    if target is None:
        log("Object grab moment: target gone")
        _cleanup()
        return

    player = pc.Pawn
    if player is None:
        _cleanup()
        return

    _grab_mass, _grab_body_count, _duration_mult = object_grab.grab_moment(target, log)

    fx.start_hand_fx(player, log)
    fx.start_target_light(target, log)
    fx.play_hold_sound(player, log)
    log("Object grab moment: object grabbed")


def _activate_object(pc, player, obj) -> None:
    """Activate force hold on a non-pawn object."""
    global _active, _target_wp, _target_uid, _capture_dist, _charge
    global _grab_bone, _grab_mass, _grab_blend_node, _saved_stagger_settings
    global _hand_fx_delay, _grab_pending, _windup_sound_time, _windup_sound_played

    result = object_grab.activate(
        pc,
        player,
        obj,
        {
            "hold_distance_override": hold_distance_override.value,
            "min_hold_distance": min_hold_distance.value,
            "activation_charge": _activation_charge,
            "activation_cost": activation_cost.value,
        },
        log,
    )
    if result is None:
        return

    _target_wp = result["target_wp"]
    _target_uid = result["target_uid"]
    _capture_dist = result["capture_dist"]
    _charge = result["charge"]
    _grab_bone = result["grab_bone"]
    _grab_mass = result["grab_mass"]
    _saved_stagger_settings = result["saved_stagger_settings"]
    _active = True
    _grab_pending = True

    # 1P grab animation
    try:
        _grab_blend_node = _find_arms_weapon_action_node(player)
        if _grab_blend_node is not None:
            _start_grab_anim()
    except Exception:
        _grab_blend_node = None

    fx.start_tattoo_glow(player, log)
    fx.play_voice_line(player, log)

    # Schedule grab moment
    delay = grab_windup_delay.value / 100.0
    if delay <= 0.0:
        _hand_fx_delay = 0.0
        _windup_sound_time = 0.0
        _windup_sound_played = True
        _do_object_grab_moment(pc)
    else:
        try:
            now = ENGINE.GetCurrentWorldInfo().TimeSeconds
            _hand_fx_delay = now + delay
            peak = sound_peak_offset.value / 100.0
            if delay >= peak:
                _windup_sound_time = now + (delay - peak)
            else:
                _windup_sound_time = now
            _windup_sound_played = False
        except Exception:
            _hand_fx_delay = 0.0
            _windup_sound_time = 0.0
            _windup_sound_played = True
            _do_object_grab_moment(pc)

    log(f"Object windup: dist={_capture_dist:.0f}")


# ─── Hook 1: ServerStartActionSkill (PRE) ────────────────────────────────────
# Blocks ALL vanilla Phaselock code. StartActionSkill (exec, client-side)
# already called GetPreferredTarget and passes the result as SkillTarget.
# We use that target directly — no vanilla skill activation occurs.


@hook("WillowGame.WillowPlayerController:ServerStartActionSkill", Type.PRE)
def _on_server_start(
    obj: UObject,
    args: WrappedStruct,
    _ret: Any,
    _func: BoundFunction,
) -> type[Block] | None:
    global _activation_charge

    if not enabled.value:
        return None

    pc = obj

    # Only intercept Siren (LiftActionSkill)
    if not _is_siren(pc):
        return None

    player = pc.Pawn
    if player is None:
        return Block

    # Block during FFYL
    try:
        if player.IsInjured():
            return Block
    except Exception:
        pass

    # If already holding — release on re-press
    if _active:
        log("Re-press detected — releasing")
        _release()
        return Block

    # Cooldown check via pool
    progress = _check_cooldown(pc)
    if progress < 1.0:
        threshold = min_activation_pct.value / 100.0
        if progress < threshold:
            log(f"On cooldown ({progress * 100:.0f}%)")
            return Block
        _activation_charge = progress
        log(f"Early activation — {progress * 100:.0f}% charged")
    else:
        _activation_charge = 1.0

    # Target from args — already resolved by client's GetPreferredTarget
    target = args.SkillTarget
    if target is None:
        # No pawn target — try grabbing a non-pawn object
        if object_grab.enable_object_grab.value:
            obj = object_grab.trace_for_object(pc, player, max_hold_distance.value, log)
            if obj is not None:
                _activate_object(pc, player, obj)
                log("ServerStartActionSkill blocked — object grab active")
                return Block
        log("No target — vanilla miss/fizzle")
        return None  # let vanilla handle miss animation

    # Non-AIPawn targets (teammate revive via Res, etc.) — let vanilla handle
    try:
        if target.Class.Name != "WillowAIPawn":
            log(f"Not WillowAIPawn ({target.Class.Name}) — vanilla Phaselock")
            return None
    except Exception:
        return None

    # Validate: alive
    try:
        if not target.IsAliveAndWell():
            log("Target not alive")
            return Block
    except Exception:
        return Block

    # Validate: not unstaggerable
    try:
        bc = target.BodyClass
        if bc is not None and (bc.bVehicleBlockingActor or bc.IgnoreMomentumTransfer):
            if fallback_phaselock.value:
                log("Can't ragdoll target — falling back to vanilla Phaselock")
                return None  # let vanilla run
            log("Can't ragdoll target — fizzle")
            _play_fizzle(player)
            return Block
    except Exception:
        pass

    # Validate: not scripted/matinee
    try:
        if target.bUsedByMatinee:
            log("Matinee pawn")
            return Block
    except Exception:
        pass

    # Range check
    try:
        pl = player.Location
        tl = target.Location
        dist = Vector(tl).distance(Vector(pl))
        if dist > max_hold_distance.value:
            log(f"Too far: {dist:.0f}")
            return Block
    except Exception:
        return Block

    _activate(pc, player, target)
    log("ServerStartActionSkill blocked — Telekinesis active")
    return Block


# ─── Hook 2: PlayerTick (PRE) ────────────────────────────────────────────────
# Spring physics, charge drain, FX upkeep.


@hook("WillowGame.WillowPlayerController:PlayerTick", Type.PRE)
def _on_tick(
    obj: UObject,
    args: WrappedStruct,
    _ret: Any,
    _func: BoundFunction,
) -> None:
    global _charge, _force_vec, _hand_fx_delay, _windup_sound_time
    global _windup_sound_played, _target_dead, _throwing, _throw_pending
    global _throw_elapsed, _throw_sound_played, _throw_sound_time

    # Process fading FX — runs even when not active
    fx.tick_fading(log)

    if not enabled.value or not _active:
        return

    dt = args.DeltaTime
    if dt <= 0:
        return

    # Lerp target light brightness
    fx.tick_target_light()

    pc = obj
    target = _get_target()

    # Target gone (died, destroyed, menu exit)
    if target is None:
        log("Target lost (WeakPointer expired)")
        _release()
        return

    # Validate object still exists (check bDeleteMe for pending-kill actors)
    if object_grab.target_is_object:
        if not object_grab.validate(target, log):
            _release()
            return

    # Validate target alive (or allow dead hold) — skip for objects
    if not object_grab.target_is_object and not _target_dead:
        try:
            if not target.IsAliveAndWell():
                if hold_dead.value:
                    _target_dead = True
                    log("Target died — holding ragdoll")
                else:
                    log("Target died")
                    _release()
                    return
        except Exception:
            log("Target invalid")
            _release()
            return

    # Player downed (FFYL)
    try:
        if pc.Pawn is None or pc.Pawn.IsInjured():
            log("Player downed")
            _cleanup()  # no cooldown on FFYL
            return
    except Exception:
        pass

    try:
        now = ENGINE.GetCurrentWorldInfo().TimeSeconds
    except Exception:
        _cleanup()
        return

    # Base charge drain — scaled by Phaselock duration skills
    base_drain = (charge_drain_rate.value / 100.0) * _duration_mult
    if object_grab.target_is_object:
        base_drain *= object_grab.drain_mult()
    _charge -= base_drain * dt
    if _charge <= 0.0:
        _charge = 0.0
        log("Charge depleted")
        _release()
        return

    if object_grab.target_is_object:
        pass  # Gravity zeroed on grab, velocity-based movement handles positioning
    else:
        # Keep ragdolled
        try:
            target.StaggerState.StaggerEndRagdollTime = now + 2.0
        except Exception:
            pass

    # Deferred windup sound — timed so peak aligns with grab moment
    if not _windup_sound_played and _windup_sound_time > 0.0:
        try:
            if now >= _windup_sound_time:
                _windup_sound_played = True
                fx.play_grab_sound(pc.Pawn, log)
        except Exception:
            _windup_sound_played = True

    # Deferred grab moment — stagger, FX, and forces all start together
    if _hand_fx_delay > 0.0:
        try:
            if now >= _hand_fx_delay:
                _hand_fx_delay = 0.0
                if object_grab.target_is_object:
                    _do_object_grab_moment(pc)
                else:
                    _grab_moment(pc)
        except Exception:
            pass

    # Animation: play intro then freeze at slider point
    global _grab_anim_playing
    if _grab_blend_node is not None:
        freeze = grab_anim_time.value / 100.0
        try:
            seq = _grab_blend_node.GetCustomAnimNodeSeq()
            if seq is not None:
                if _grab_anim_playing:
                    # Still playing intro — check if we've reached the freeze point
                    if seq.CurrentTime >= freeze:
                        # Switch to frozen: replay at near-zero rate, 0 blend-in
                        _grab_blend_node.PlayCustomAnim(
                            _GRAB_ANIM_NAME,
                            0.001,
                            0.0,
                            99.0,
                            True,
                            True,
                            freeze,
                        )
                        seq = _grab_blend_node.GetCustomAnimNodeSeq()
                        if seq is not None:
                            seq.SetPosition(freeze, False)
                        _grab_anim_playing = False
                        log(f"Grab anim frozen at {freeze:.2f}s")
                else:
                    # Already frozen — re-seek to prevent drift
                    seq.SetPosition(freeze, False)
        except Exception:
            pass

    # Update HUD — show charge level on cooldown pool icon
    try:
        pool = pc.SkillCooldownPool
        if pool is not None and pool.Data is not None:
            max_val = pool.Data.GetMaxValue()
            # current=0 means icon full (ready), current=max means icon empty
            pool.Data.SetCurrentValue(max_val * (1.0 - _charge))
    except Exception:
        pass

    # During windup, only play animation — no forces or charge drain yet
    if _grab_pending:
        fx.update_orb_scale(0.0, dt, pc.Pawn, log)
        return

    # ── Compute desired hold position (used by crush and spring) ──

    # Use actual camera position/rotation (matches crosshair trace)
    try:
        view_loc = pc.CalcViewLocation
        view_rot = pc.CalcViewRotation
        eye_x = view_loc.X
        eye_y = view_loc.Y
        eye_z = view_loc.Z
        fwd = Rotator(view_rot).to_vector()
        fwd_x, fwd_y, fwd_z = fwd.x, fwd.y, fwd.z
    except Exception as e:
        log(f"CalcView fallback: {e}")
        player = pc.Pawn
        try:
            eye_x = player.Location.X
            eye_y = player.Location.Y
            eye_z = player.Location.Z + player.EyeHeight
        except Exception:
            return
        try:
            rot = pc.Rotation
            fwd = Rotator(rot).to_vector()
            fwd_x, fwd_y, fwd_z = fwd.x, fwd.y, fwd.z
        except Exception:
            return

    des_x = eye_x + fwd_x * _capture_dist
    des_y = eye_y + fwd_y * _capture_dist
    des_z = eye_z + fwd_z * _capture_dist

    # ── Crush: converge bones, finalize when duration reached ──

    if _crushing:
        if target is not None:
            _crush_tick(target, des_x, des_y, des_z, dt)
        try:
            now_cr = ENGINE.GetCurrentWorldInfo().TimeSeconds
        except Exception:
            now_cr = _crush_start

        # Play release sound early (uses throw_sound_offset slider)
        if not _throw_sound_played and _throw_sound_time > 0.0:
            if now_cr >= _throw_sound_time:
                _throw_sound_played = True
                _throw_sound_time = 0.0
                try:
                    player = pc.Pawn
                    if player is not None:
                        fx.play_release_sound(player, log)
                except Exception:
                    pass

        dur = crush_duration.value * 0.01
        elapsed_cr = now_cr - _crush_start
        if elapsed_cr >= dur:
            _finalize_crush(pc)
        else:
            # Lerp hand animation from hold freeze to crush pose
            anim_dur = crush_anim_duration.value * 0.01
            anim_progress = min(elapsed_cr / anim_dur, 1.0) if anim_dur > 0 else 1.0
            start_pos = grab_anim_time.value * 0.01
            end_pos = crush_anim_time.value * 0.01
            anim_pos = start_pos + (end_pos - start_pos) * anim_progress
            try:
                seq = _grab_blend_node.GetCustomAnimNodeSeq()
                if seq is not None:
                    seq.SetPosition(anim_pos, False)
            except Exception:
                pass
            fx.update_orb_scale(0.0, dt, pc.Pawn, log)
        return  # skip spring physics during crush

    # ── Throw release sound — can fire during windup or force phase ──

    if not _throw_sound_played and _throw_sound_time > 0.0:
        try:
            now_ts = ENGINE.GetCurrentWorldInfo().TimeSeconds
        except Exception:
            now_ts = _throw_sound_time
        if now_ts >= _throw_sound_time:
            _throw_sound_played = True
            _throw_sound_time = 0.0
            try:
                player = pc.Pawn
                if player is not None:
                    fx.play_release_sound(player, log)
            except Exception:
                pass

    # ── Throw windup: anim plays, spring still holds, no throw force yet ──

    if _throw_pending > 0.0:
        try:
            now_tp = ENGINE.GetCurrentWorldInfo().TimeSeconds
        except Exception:
            now_tp = _throw_pending  # trigger immediately on failure
        if now_tp >= _throw_pending:
            _throw_pending = 0.0
            _throwing = True
            _throw_elapsed = 0.0
            # Zero gravity during throw so the target doesn't sag
            if target is not None:
                if object_grab.target_is_object:
                    object_grab.set_gravity(0.0)
                else:
                    _set_gravity(target, 0.0)
            # Sound moved to apex (force→recovery transition)
            log(
                f"Throw force started: dir="
                f"({_throw_dir[0]:.2f}, {_throw_dir[1]:.2f}, "
                f"{_throw_dir[2]:.2f})"
            )

    # ── Throw: apply directional force instead of spring, then release ──

    if _throwing:
        _throw_elapsed += dt
        # Throw duration slider directly controls force application time
        force_dur = throw_duration.value * 0.01
        if _throw_elapsed >= force_dur:
            # Burst FX at throw apex (force→recovery transition)
            try:
                player = pc.Pawn
                if player is not None:
                    fx.play_throw_burst(player, log)
            except Exception:
                pass
            log(f"Throw complete (duration {_throw_elapsed:.3f}s)")
            _release()
            return

        # Force scales with remaining charge — starts strong, tapers off
        # Minimum 30% force so low-charge throws still feel punchy
        force = float(throw_force.value) * max(_charge, 0.3)

        if object_grab.target_is_object:
            # Single impulse then release — physics handles the arc
            impulse = force * 0.1
            object_grab.throw_tick(target, _throw_dir, impulse, log)
            try:
                player = pc.Pawn
                if player is not None:
                    fx.play_throw_burst(player, log)
            except Exception:
                pass
            log("Pickup thrown (impulse)")
            _release()
            return

        m = _grab_mass
        n = _grab_body_count
        scale = m / n if n > 0 else m
        tx = _throw_dir[0] * force * scale
        ty = _throw_dir[1] * force * scale
        tz = _throw_dir[2] * force * scale
        try:
            mesh = target.Mesh
            if _force_vec is None:
                _force_vec = unrealsdk.make_struct("Vector")
            _force_vec.X = tx
            _force_vec.Y = ty
            _force_vec.Z = tz
            mesh.AddForce(_force_vec)
        except Exception as e:
            log(f"Throw force failed: {e}")
            _release()
            return

        # Work-based throw drain: power = throw_accel · velocity (dot product)
        # Uses target Velocity (single property read) instead of per-body average
        try:
            vel = target.Velocity
            vx, vy, vz = vel.X, vel.Y, vel.Z
        except Exception:
            vx = vy = vz = 0.0
        throw_accel = force
        power = throw_accel * (_throw_dir @ Vector((vx, vy, vz)))
        work_drain = m * abs(power) * (force_drain_rate.value * 1e-10) * _duration_mult * dt
        fx.update_orb_scale(work_drain, dt, pc.Pawn, log)
        _charge -= work_drain
        if _charge <= 0.0:
            _charge = 0.0
            log("Throw complete (charge spent)")
            _release()
        return

    # ── Spring physics (des_x/y/z already computed above) ──

    # During throw windup, pull the pawn back toward the player (opposite throw dir)
    if _throw_pending > 0.0:
        windup_dur = _throw_pending - _throw_windup_start
        if windup_dur > 0:
            try:
                now_pb = ENGINE.GetCurrentWorldInfo().TimeSeconds
            except Exception:
                now_pb = _throw_windup_start
            progress = min((now_pb - _throw_windup_start) / windup_dur, 1.0)
            # Pull back up to 30% of capture distance
            # Sine ease-out: fast initial pull, decelerates to a hang at peak
            pullback = _capture_dist * 0.3 * math.sin(progress * math.pi * 0.5)
            des_x -= _throw_dir[0] * pullback
            des_y -= _throw_dir[1] * pullback
            des_z -= _throw_dir[2] * pullback

    # Offset upward so the visual center aligns with crosshair
    if object_grab.target_is_object:
        des_z += object_grab.height_offset()
    elif not _grab_bone:
        # Only offset when using whole-mesh (no grab bone) — with bone targeting
        # the grab bone itself should track the crosshair directly
        try:
            des_z += target.CylinderComponent.CollisionHeight
        except Exception:
            des_z += 80.0

    # ── Object hold: SetLocation lerp ──
    if object_grab.target_is_object and object_grab.object_mesh is not None:
        alpha = min(float(spring_strength.value) * 0.1 * dt, 1.0)
        work_drain = object_grab.hold_tick(
            target,
            des_x,
            des_y,
            des_z,
            alpha,
            dt,
            force_drain_rate.value,
            _duration_mult,
            log,
        )
        if work_drain < 0:
            _release()
            return
        fx.update_orb_scale(work_drain, dt, pc.Pawn, log)
        _charge -= work_drain
        if _charge <= 0.0:
            _charge = 0.0
            log("Charge depleted (force drain)")
            _release()
        return

    # ── Pawn hold: spring-damper physics ──

    try:
        cur_x = target.Location.X
        cur_y = target.Location.Y
        cur_z = target.Location.Z
    except Exception:
        _release()
        return

    disp_x = des_x - cur_x
    disp_y = des_y - cur_y
    disp_z = des_z - cur_z
    ax = ay = az = 0.0

    try:
        mesh = target.Mesh
    except Exception:
        _release()
        return

    if _force_vec is None:
        _force_vec = unrealsdk.make_struct("Vector")

    try:
        if _grab_bone and _grab_bone_idx >= 0:
            # ── PID bone tracking via AddImpulse(bVelChange=True).
            # spring_strength is NOT used here — Kp/Ki/Kd control everything.
            bodies = mesh.PhysicsAssetInstance.Bodies
            grab_body = bodies[_grab_bone_idx]
            max(grab_body.GetBodyMass(), 5.0)  # floor to 5kg to prevent light-bone explosion

            bv = grab_body.GetUnrealWorldVelocity()
            bone_vx, bone_vy, bone_vz = bv.X, bv.Y, bv.Z

            bone_name = str(mesh.PhysicsAsset.BodySetup[_grab_bone_idx].BoneName)
            bone_loc = mesh.GetBoneLocation(bone_name, 0)
            bdx = des_x - bone_loc.X
            bdy = des_y - bone_loc.Y
            bdz = des_z - bone_loc.Z

            # Velocity-based bone tracking via AddImpulse(bVelChange=True).
            # AddForce is unusable — PhysX substeps multiply it ~60x.
            # Instead, directly set bone velocity toward target each frame.

            # PID controller: P pulls toward target, I accumulates error
            # for heavy pawns, D brakes on velocity.
            # Output is a velocity impulse applied via AddImpulse(bVelChange).
            global _pid_integral_x, _pid_integral_y, _pid_integral_z
            # Scale PID gains by total mass so heavy/light pawns feel the same
            mass_scale = _grab_mass / 80.0  # normalized to ~80kg reference
            Kp = float(bone_kp.value) * mass_scale
            Ki = bone_ki.value * 0.00001 * mass_scale
            Kd = bone_kd.value * 0.1 * mass_scale

            # Accumulate integral (with anti-windup clamp)
            _pid_integral_x += bdx * dt
            _pid_integral_y += bdy * dt
            _pid_integral_z += bdz * dt
            _INT_MAX = 500.0
            _pid_integral_x = max(-_INT_MAX, min(_INT_MAX, _pid_integral_x))
            _pid_integral_y = max(-_INT_MAX, min(_INT_MAX, _pid_integral_y))
            _pid_integral_z = max(-_INT_MAX, min(_INT_MAX, _pid_integral_z))

            # PID impulse with bVelChange=False — engine divides by mass.
            # Heavy grab bone (10x redistributed) naturally resists acceleration,
            # providing built-in stability. No velocity reversal possible.
            _force_vec.X = Kp * bdx + Ki * _pid_integral_x - Kd * bone_vx
            _force_vec.Y = Kp * bdy + Ki * _pid_integral_y - Kd * bone_vy
            _force_vec.Z = Kp * bdz + Ki * _pid_integral_z - Kd * bone_vz
            mesh.AddImpulse(_force_vec, bone_loc, bone_name, False)

            # Drain calc
            ax = bdx * Kp
            ay = bdy * Kp
            az = bdz * Kp
        else:
            # ── Whole-mesh spring: same as before
            m = _grab_mass
            n_bodies = _grab_body_count
            k = float(spring_strength.value)
            c = 2.0 * math.sqrt(k)

            ax = disp_x * k
            ay = disp_y * k
            az = disp_z * k

            # Average velocity across all bodies for damping
            try:
                phys_inst = mesh.PhysicsAssetInstance
                if phys_inst is not None:
                    bodies = phys_inst.Bodies
                    if bodies is not None and len(bodies) > 0:
                        avg_vx = avg_vy = avg_vz = 0.0
                        for bi in range(len(bodies)):
                            bv = bodies[bi].GetUnrealWorldVelocity()
                            avg_vx += bv.X
                            avg_vy += bv.Y
                            avg_vz += bv.Z
                        n = len(bodies)
                        ax -= (avg_vx / n) * c
                        ay -= (avg_vy / n) * c
                        az -= (avg_vz / n) * c
            except Exception:
                pass

            scale = m / n_bodies if n_bodies > 0 else m
            _force_vec.X = ax * scale
            _force_vec.Y = ay * scale
            _force_vec.Z = az * scale
            mesh.AddForce(_force_vec)
    except Exception as e:
        log(f"AddForce failed: {e}")
        _release()
        return

    # Work-based charge drain: proportional to displacement x velocity
    # For bone mode ax/ay/az are velocity-scale (disp*gain), for whole-mesh they're acceleration
    try:
        vel = target.Velocity
        drain_vx, drain_vy, drain_vz = vel.X, vel.Y, vel.Z
    except Exception:
        drain_vx = drain_vy = drain_vz = 0.0
    power = ax * drain_vx + ay * drain_vy + az * drain_vz
    if _grab_bone and _grab_bone_idx >= 0:
        # Bone mode: ax/ay/az are disp*gain (velocity-scale, not force-scale)
        work_drain = (
            _grab_mass * abs(power) * (force_drain_rate.value * 1e-11) * _duration_mult * dt
        )
    else:
        work_drain = (
            _grab_mass * abs(power) * (force_drain_rate.value * 1e-10) * _duration_mult * dt
        )
    fx.update_orb_scale(work_drain, dt, pc.Pawn, log)
    _charge -= work_drain
    if _charge <= 0.0:
        _charge = 0.0
        log("Charge depleted (force drain)")
        _release()
        return


# ─── Throw (M1) ─────────────────────────────────────────────────────────────


def _throw_target(pc) -> None:
    """Start throwing — applies force for a short burst, then releases."""
    global _throwing, _throw_dir, _throw_elapsed, _throw_pending, _throw_windup_start

    if _throwing or _throw_pending > 0.0:
        return

    target = _get_target()
    if target is None:
        _release()
        return

    try:
        rot = pc.CalcViewRotation
        _throw_dir = Rotator(rot).to_vector()
    except Exception:
        _release()
        return

    # Throw uses all remaining charge — more charge = stronger throw
    global _charge
    if _charge <= 0.05:
        log("Not enough charge to throw")
        _release()
        return

    # Fade the hand orb out — energy is "released" into the throw
    fx.stop_hand_fx(log)

    # Play push animation and clear node so cleanup doesn't play reverse on top
    _play_throw_anim()
    global _grab_blend_node
    _grab_blend_node = None

    # Throw duration slider = force time; derive total from windup/recovery %
    global _throw_sound_time, _throw_sound_played
    force_time = throw_duration.value * 0.01
    force_frac = max(1.0 - throw_windup_pct.value * 0.01 - throw_recovery_pct.value * 0.01, 0.05)
    total_time = force_time / force_frac
    windup = total_time * throw_windup_pct.value * 0.01

    # Schedule release sound: apex_time - offset, clamped to now (M1 press)
    sound_lead = throw_sound_offset.value * 0.001
    try:
        now_w = ENGINE.GetCurrentWorldInfo().TimeSeconds
    except Exception:
        now_w = 0.0
    apex_time = now_w + windup + force_time
    _throw_sound_time = max(apex_time - sound_lead, now_w)
    _throw_sound_played = False

    if windup > 0:
        if now_w > 0:
            _throw_windup_start = now_w
            _throw_pending = now_w + windup
        else:
            _throw_pending = 0.0
            _throwing = True
            _throw_elapsed = 0.0
        log(f"Throw windup: {windup:.3f}s before force")
    else:
        _throwing = True
        _throw_elapsed = 0.0
        # Zero gravity during throw so the target doesn't sag
        target = _get_target()
        if target is not None:
            if object_grab.target_is_object:
                object_grab.set_gravity(0.0)
            else:
                _set_gravity(target, 0.0)
        # Sound moved to apex (force→recovery transition)
        log(f"Throw started: dir=({_throw_dir[0]:.2f}, {_throw_dir[1]:.2f}, {_throw_dir[2]:.2f})")


# ─── Kill (M2) ──────────────────────────────────────────────────────────────

_kill_dmg_class = None
_kill_dmg_type_def = None


def _kill_target(pc) -> None:
    """Right-click — start crush windup. Bones converge, then gib fires."""
    global _crushing, _crush_start

    if _throwing or _throw_pending > 0.0 or _crushing:
        return

    target = _get_target()
    if target is None:
        _release()
        return

    if object_grab.target_is_object:
        return

    if _target_dead:
        _release()
        return

    # Cache damage type references
    global _kill_dmg_class, _kill_dmg_type_def
    if _kill_dmg_class is None:
        _kill_dmg_class = unrealsdk.find_class("DamageType")
    if _kill_dmg_type_def is None:
        try:
            _kill_dmg_type_def = unrealsdk.find_object(
                "WillowDamageTypeDefinition",
                "GD_Explosive.DamageType.DmgType_Explosive_Impact",
            )
        except Exception:
            pass

    _crushing = True
    try:
        _crush_start = ENGINE.GetCurrentWorldInfo().TimeSeconds
    except Exception:
        _crush_start = 0.0

    # Schedule release sound before the kill fires
    global _throw_sound_time, _throw_sound_played
    sound_lead = throw_sound_offset.value * 0.001
    dur = crush_duration.value * 0.01
    kill_time = _crush_start + dur
    _throw_sound_time = max(kill_time - sound_lead, _crush_start)
    _throw_sound_played = False

    # Animation will be driven per-tick via SetPosition in the crush tick section
    log(f"Crush windup started: dur={dur:.2f}s sound_at={_throw_sound_time - _crush_start:.2f}s")


def _crush_tick(target, cx: float, cy: float, cz: float, dt: float) -> None:
    """Per-frame crush: pull all bones toward the hold point using AddRadialForce.

    Same approach as ImpactStagger's singularity gravity wells — negative
    strength pulls bones inward. Origin is the crosshair hold point (des_x/y/z).
    """
    try:
        mesh = target.Mesh
        if mesh is None:
            return

        # Crush progress (0→1)
        elapsed = 0.0
        try:
            elapsed = ENGINE.GetCurrentWorldInfo().TimeSeconds - _crush_start
        except Exception:
            pass
        dur = crush_duration.value * 0.01
        progress = min(elapsed / dur, 1.0) if dur > 0 else 1.0

        # Ramp from 0 to peak over crush duration (progress² for acceleration)
        mult = crush_strength.value / 10.0
        pull_strength = 500.0 * progress * progress * (_grab_mass / 80.0) * mult

        origin = _force_vec
        if origin is None:
            return
        origin.X = cx
        origin.Y = cy
        origin.Z = cz

        # AddRadialForce: negative strength = pull inward, falloff=0 = constant
        mesh.AddRadialForce(origin, 200.0, -pull_strength, 0)
    except Exception:
        pass


def _finalize_crush(pc) -> None:
    """Crush complete — gib the pawn and clean up."""
    global _charge, _crushing

    target = _get_target()
    if target is None:
        _crushing = False
        _release()
        return

    try:
        max_hp = target.GetMaxHealth()
    except Exception:
        max_hp = 10000.0
    overkill_damage = max(int(max_hp * 10), 100000)

    try:
        ename = target.AIClass.Name if target.AIClass is not None else "?"
    except Exception:
        ename = "?"
    log(f"Crush kill: {ename} overkill={overkill_damage}")

    # Force gib: set the flag and fire the gib behavior event manually
    try:
        target.bGibbed = True
        death_def = target.BodyClass.DeathDef
        if death_def is not None:
            death_def.OnDeathGib(target.ConsumerHandle, pc)
    except Exception as e:
        log(f"Crush: gib setup failed: {e}")

    # Kill via TakeDamage
    momentum = unrealsdk.make_struct("Vector")
    hit_info = unrealsdk.make_struct("TraceHitInfo")
    causer = pc.Pawn if pc.Pawn is not None else pc
    with unrealsdk.hooks.prevent_hooking_direct_calls():
        target.TakeDamage(
            overkill_damage,
            pc,
            target.Location,
            momentum,
            _kill_dmg_class,
            hit_info,
            causer,
        )

    # Hide the pawn — gib path skips normal destroy
    try:
        target.SetHidden(True)
        target.SetCollision(False, False)
        mesh = target.Mesh
        if mesh is not None:
            mesh.SetHidden(True)
            mesh.SetTraceBlocking(False, False)
            mesh.SetActorCollision(False, False)
        target.LifeSpan = 1.0
    except Exception:
        pass

    _charge = 0.0
    # _crushing stays True until _cleanup reads it for the forward animation

    try:
        player = pc.Pawn
        if player is not None:
            fx.play_throw_burst(player, log)
    except Exception:
        pass

    _release()


# ─── Public API ──────────────────────────────────────────────────────────────


def is_held(uid: str) -> bool:
    """Check if a pawn is currently held by Telekinesis."""
    return _active and uid == _target_uid


# ─── Scroll Wheel: Push / Pull ──────────────────────────────────────────────


def _scroll_adjust(pull: bool) -> None:
    """Adjust capture distance. pull=True brings closer, pull=False pushes further."""
    global _capture_dist
    step = float(scroll_step.value)
    if pull:
        _capture_dist = max(float(min_hold_distance.value), _capture_dist - step)
        pass
    else:
        _capture_dist = min(float(max_hold_distance.value), _capture_dist + step)


@hook("WillowGame.WillowInventoryManager:PrevWeapon", Type.PRE)
def _on_prev_weapon(
    obj: UObject,
    args: WrappedStruct,
    _ret: Any,
    _func: BoundFunction,
) -> type[Block] | None:
    if not _active:
        return None
    _scroll_adjust(pull=not invert_scroll.value)
    return Block


@hook("WillowGame.WillowInventoryManager:NextWeapon", Type.PRE)
def _on_next_weapon(
    obj: UObject,
    args: WrappedStruct,
    _ret: Any,
    _func: BoundFunction,
) -> type[Block] | None:
    if not _active:
        return None
    _scroll_adjust(pull=invert_scroll.value)
    return Block


# ─── Block player actions while holding ──────────────────────────────────────


@hook("WillowGame.WillowPlayerController:StartFire", Type.PRE)
def _on_start_fire(
    obj: UObject,
    args: WrappedStruct,
    _ret: Any,
    _func: BoundFunction,
) -> type[Block] | None:
    if not _active:
        return None
    try:
        if args.FireModeNum == 0:
            # M1 — throw held pawn
            _throw_target(obj)
        elif args.FireModeNum == 1:
            # M2 — kill held pawn
            _kill_target(obj)
    except Exception:
        pass
    return Block


@hook("WillowGame.WillowPlayerController:EquipWeaponFromSlot", Type.PRE)
def _on_equip_slot(
    obj: UObject,
    args: WrappedStruct,
    _ret: Any,
    _func: BoundFunction,
) -> type[Block] | None:
    if not _active:
        return None
    return Block


@hook("WillowGame.WillowPlayerController:MeleeAttack", Type.PRE)
def _on_melee(
    obj: UObject,
    args: WrappedStruct,
    _ret: Any,
    _func: BoundFunction,
) -> type[Block] | None:
    if not _active:
        return None
    return Block


@hook("WillowGame.WillowPlayerController:ThrowGrenade", Type.PRE)
def _on_throw_grenade(
    obj: UObject,
    args: WrappedStruct,
    _ret: Any,
    _func: BoundFunction,
) -> type[Block] | None:
    if not _active:
        return None
    return Block


@hook("WillowGame.WillowPlayerController:ShowStatusMenu", Type.PRE)
def _on_show_menu(
    obj: UObject,
    args: WrappedStruct,
    _ret: Any,
    _func: BoundFunction,
) -> type[Block] | None:
    if not _active:
        return None
    return Block


# ─── Mod Registration ───────────────────────────────────────────────────────

settings = NestedOption(
    "Telekinesis Settings",
    children=[
        spring_strength,
        bone_kp,
        bone_ki,
        bone_kd,
        hold_gravity,
        min_hold_distance,
        max_hold_distance,
        hold_distance_override,
        scroll_step,
        invert_scroll,
        throw_force,
        throw_duration,
        throw_windup_pct,
        throw_recovery_pct,
        crush_duration,
        crush_anim_duration,
        crush_strength,
        crush_anim_time,
        charge_drain_rate,
        force_drain_rate,
        min_activation_pct,
        activation_cost,
        grab_windup_delay,
        hold_dead,
        fallback_phaselock,
        object_grab.enable_object_grab,
        object_grab.object_drain_mult,
        object_grab.skip_mission_items,
        fx_settings,
    ],
    description="Tune force hold physics, gravity, distance, and charge.",
)

# ─── Map transition: force cleanup to prevent stale UE3 references ──────────


def _force_reset() -> None:
    """Reset all state without touching UE3 objects (they may be destroyed)."""
    global _active, _target_wp, _target_uid, _grab_blend_node, _grab_pending
    global _throwing, _crushing, _target_dead, _throw_pending
    global _charge, _grab_mass, _grab_body_count, _grab_bone, _grab_bone_idx
    global _pid_integral_x, _pid_integral_y, _pid_integral_z
    global _saved_stagger_settings, _grab_orig_mass_scales
    global _throw_sound_time, _throw_sound_played, _crush_start
    if _active:
        log("Force reset — clearing all state")
    _active = False
    _target_wp = None
    _target_uid = ""
    _grab_blend_node = None
    _grab_pending = False
    _throwing = False
    _crushing = False
    _target_dead = False
    _throw_pending = 0.0
    _throw_sound_time = 0.0
    _throw_sound_played = False
    _crush_start = 0.0
    _charge = 0.0
    _grab_mass = 1.0
    _grab_body_count = 1
    _grab_bone = ""
    _grab_bone_idx = -1
    _grab_orig_mass_scales = {}
    _pid_integral_x = _pid_integral_y = _pid_integral_z = 0.0
    _saved_stagger_settings = None
    fx.reset_all()
    object_grab.reset_state()


@hook(
    "WillowGame.WillowPlayerController:WillowClientShowLoadingMovie",
    Type.PRE,
)
def _on_loading_movie_show(
    obj: UObject,
    args: WrappedStruct,
    _ret: Any,
    _func: BoundFunction,
) -> None:
    """Fires BEFORE map unload — objects still exist but we must stop touching them."""
    _force_reset()


@hook(
    "WillowGame.WillowPlayerController:WillowClientDisableLoadingMovie",
    Type.PRE,
)
def _on_map_load(
    obj: UObject,
    args: WrappedStruct,
    _ret: Any,
    _func: BoundFunction,
) -> None:
    """Fires after map load — ensure clean state for new session."""
    _force_reset()


mod = build_mod(
    options=[
        enabled,
        settings,
        log_to_console,
    ],
    keybinds=[],
)
