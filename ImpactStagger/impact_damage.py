from __future__ import annotations

from time import perf_counter

from uemath import Vector

from mods_base import BoolOption, SliderOption


impact_damage_scale = SliderOption(
    "Impact Damage %",
    75,
    0,
    100,
    1,
    True,
    description=(
        "Max-health % dealt per impact, scaled by speed. "
        "At reference speed (10000), this is the exact % of max health dealt. "
        "Slower impacts deal proportionally less. "
        "0 = disabled, 75 = 75% max HP at full speed. "
        "Default: 75"
    ),
)

# Speed at which the slider value equals the exact % of max health dealt.
SPEED_REFERENCE = 10000.0

min_impact_velocity = SliderOption(
    "Min Impact Velocity",
    1200,
    0,
    10000,
    100,
    True,
    description=(
        "Minimum impact speed to trigger collision damage. "
        "Prevents damage from gentle landings or resting contacts. "
        "Default: 1200"
    ),
)

impact_cooldown = SliderOption(
    "Impact Cooldown",
    0,
    0,
    50,
    1,
    True,
    description=(
        "Minimum time between impact damage ticks per enemy (in tenths of a second). "
        "Prevents damage spam from multiple bone contacts per frame. "
        "0 = no cooldown. 5 = 0.5s, 10 = 1s. Default: 0"
    ),
)

launch_grace = SliderOption(
    "Launch Grace",
    2,
    0,
    20,
    1,
    True,
    description=(
        "Grace period after launching a grounded enemy (in hundredths of a second). "
        "Ignores collisions right after launch to prevent false damage from "
        "feet still touching the ground. "
        "0 = disabled, 2 = 20ms. Default: 2"
    ),
)

launch_speed_threshold = SliderOption(
    "Launch Speed Threshold",
    200,
    0,
    2000,
    50,
    True,
    description=(
        "Max ragdoll speed to count as 'grounded' for launch grace. "
        "Only ragdolls moving slower than this get the grace period. "
        "Higher = more forgiving, 0 = grace applies always. "
        "Default: 200"
    ),
)

crit_bones_only = BoolOption(
    "Crit Bones Only",
    False,
    description=(
        "Only deal impact damage when a crit-zone bone hits a surface. "
        "Uses BL2's native hit regions per enemy type (e.g. head for bandits, "
        "eye for loaders, mouth for skags). Non-crit bones are controlled by "
        "the sliders below."
    ),
)

non_crit_min_velocity = SliderOption(
    "Non-Crit Min Velocity",
    1800,
    0,
    10000,
    100,
    True,
    description=(
        "Minimum impact speed for non-crit bones to deal damage. "
        "Higher than Min Impact Velocity to filter out ground dragging on limbs. "
        "Only used when Crit Bones Only is off. "
        "Default: 1800"
    ),
)

non_crit_damage_scale = SliderOption(
    "Non-Crit Damage %",
    50,
    0,
    100,
    5,
    True,
    description=(
        "Damage multiplier for non-crit bone impacts as % of full damage. "
        "0 = non-crit bones deal no damage, 100 = same as crit bones. "
        "Default: 50"
    ),
)

# Per-pawn cooldown: pawn id -> last impact timestamp
LAST_IMPACT: dict[str, float] = {}

# Per-pawn launch grace: pawn id -> last time impulse was applied.
LAST_LAUNCH: dict[str, float] = {}

max_impacts_per_frame = SliderOption(
    "Max Impacts Per Frame",
    10,
    1,
    50,
    1,
    True,
    description=(
        "Maximum impact damage events processed per frame. "
        "Prevents lag spikes when many ragdolls collide simultaneously. "
        "Lower = smoother but some impacts get skipped. "
        "Higher = more damage events but heavier frames. "
        "0 would disable impact damage entirely (use 1 minimum). "
        "Default: 10"
    ),
)

# Wall-clock time window for the per-frame cap (~1 frame at 60fps).
_frame_window: float = 0.016
_frame_start: float = 0.0
_frame_count: int = 0


def get_impact_speed(collision_data) -> float:
    """Extract normal-component impact speed from collision data.

    Uses dot(ContactVelocity, ContactNormal) to get the speed INTO the surface.
    Slope sliding (mostly tangential) produces a near-zero normal component,
    while wall slams produce a high normal component.
    Falls back to TotalNormalForceVector magnitude.
    """
    try:
        contacts = collision_data.ContactInfos
        if contacts is not None and len(contacts) > 0:
            contact = contacts[0]
            vel = None
            try:
                vel = contact.ContactVelocity[0]
            except Exception:
                pass
            if vel is None:
                try:
                    vel = contact.ContactVelocity_0
                except Exception:
                    pass
            if vel is not None:
                # Get contact normal for angle-aware calculation
                normal = None
                try:
                    normal = contact.ContactNormal
                except Exception:
                    pass
                if normal is not None:
                    # Normal component = abs(dot(velocity, normal))
                    normal_speed = abs(Vector(vel) @ Vector(normal))
                    if normal_speed > 0:
                        return normal_speed
                # Fallback: raw speed if normal unavailable
                speed = Vector(vel).magnitude
                if speed > 0:
                    return speed
    except Exception:
        pass

    # Fallback: total normal force magnitude
    try:
        fv = collision_data.TotalNormalForceVector
        return Vector(fv).magnitude
    except Exception:
        return 0.0


def on_collision(
    pawn_id: str,
    collision_data,
    world_time: float,
    max_health: float,
    is_crit: bool = True,
) -> float | None:
    """Process a ragdoll collision event.

    Returns damage to deal (as fraction of max health scaled by speed),
    or None if the collision should be ignored.

    is_crit: whether the colliding bone is in a crit zone.
    Crit bones use min_impact_velocity and deal full damage.
    Non-crit bones use non_crit_min_velocity and deal scaled damage.
    """
    if impact_damage_scale.value <= 0:
        return None

    # Per-frame cap: prevent game freeze from mass ragdoll collisions.
    # Uses wall-clock time because game TimeSeconds can advance mid-collision batch.
    global _frame_start, _frame_count
    now = perf_counter()
    if now - _frame_start > _frame_window:
        _frame_start = now
        _frame_count = 0
    if _frame_count >= max_impacts_per_frame.value:
        return None

    # Grace period after impulse — ignore collisions from the launch itself
    grace = launch_grace.value / 100.0
    last_launch = LAST_LAUNCH.get(pawn_id, 0.0)
    if grace > 0 and world_time - last_launch < grace:
        return None

    cooldown = impact_cooldown.value / 10.0
    last = LAST_IMPACT.get(pawn_id, 0.0)
    if cooldown > 0 and world_time - last < cooldown:
        return None

    speed = get_impact_speed(collision_data)

    # Crit bones use the base threshold; non-crit bones use the higher threshold
    if is_crit:
        if speed < min_impact_velocity.value:
            return None
    else:
        if speed < non_crit_min_velocity.value:
            return None

    damage = max_health * (speed / SPEED_REFERENCE) * (impact_damage_scale.value / 100.0)

    # Non-crit bones deal reduced damage
    if not is_crit:
        damage *= non_crit_damage_scale.value / 100.0
        if damage <= 0:
            return None

    _frame_count += 1
    LAST_IMPACT[pawn_id] = world_time
    return damage


def record_launch(pawn_id: str, world_time: float) -> None:
    """Record that an impulse was just applied — starts the launch grace period."""
    LAST_LAUNCH[pawn_id] = world_time


def clear_pawn(pawn_id: str) -> None:
    """Remove tracking for a pawn (on death/cleanup)."""
    LAST_IMPACT.pop(pawn_id, None)
    LAST_LAUNCH.pop(pawn_id, None)


def is_crit_bone(pawn, bone_name: str) -> bool:
    """Check if a bone is in a crit zone using BL2's native hit region system.

    Uses BodyClassDefinition.HitRegionList to find which region the bone belongs to,
    then checks bCriticalHit. Works per-enemy-type (loaders have eye crit, skags
    have mouth crit, bandits have head crit, etc.).
    """
    bc = pawn.BodyClass
    if bc is None:
        return False
    regions = bc.HitRegionList
    if regions is None:
        return False
    for i in range(len(regions)):
        region = regions[i]
        if not region.bCriticalHit:
            continue
        bones = region.RegionBoneNames
        if bones is not None:
            for j in range(len(bones)):
                if str(bones[j]) == bone_name:
                    return True
    return False


def get_options() -> list:
    """Return impact damage slider options for build_mod."""
    return [
        impact_damage_scale,
        min_impact_velocity,
        impact_cooldown,
        launch_grace,
        launch_speed_threshold,
        non_crit_min_velocity,
        non_crit_damage_scale,
        max_impacts_per_frame,
    ]
