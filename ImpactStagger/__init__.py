from __future__ import annotations

import json
import time
from math import sqrt
from pathlib import Path
from typing import Any

from mods_base import ENGINE, hook, build_mod, BoolOption, SliderOption, NestedOption, ButtonOption
import unrealsdk
from unrealsdk import unreal
from unrealsdk.hooks import Block, Type
from unrealsdk.unreal import WeakPointer

from . import bowling
from . import enemy_masses
from . import grenades
from . import impact_damage
from . import multiplayer
from . import object_physics
from . import rockets
from . import singularity


def _iter_ai_pawns() -> list:
    """Walk WorldInfo.PawnList instead of find_all — skips CDOs/archetypes."""
    result = []
    try:
        pawn = ENGINE.GetCurrentWorldInfo().PawnList
    except Exception:
        return result
    while pawn is not None:
        try:
            if pawn.Class.Name == "WillowAIPawn":
                result.append(pawn)
            pawn = pawn.NextPawn
        except Exception:
            break
    return result


# Cached DamageType class for impact damage TakeDamage calls.
_impact_dmg_type = None


def _get_impact_damage_type():
    global _impact_dmg_type
    if _impact_dmg_type is None:
        try:
            _impact_dmg_type = unrealsdk.find_class("DamageType")
        except Exception:
            pass
    return _impact_dmg_type


def _get_damage_pipeline(pawn):
    """Acquire a DamagePipeline from the engine pool. Must be released after use."""
    # note to self: this may not even be needed its not a required argument
    try:
        return pawn.GetDamagePipeline()
    except Exception:
        return None


def _release_damage_pipeline(pawn, pipeline):
    """Return a DamagePipeline to the engine pool."""
    if pipeline is not None:
        try:
            pawn.ReleaseDamagePipeline(pipeline)
        except Exception:
            pass


log_to_console = BoolOption(
    "Log to Console",
    False,
    description="Print debug messages to the in-game console.",
)

log_perf = BoolOption(
    "Log Performance",
    False,
    description="Log per-function timing stats to console every 5 seconds.",
)


def log(msg: str) -> None:
    """Print debug messages to the in-game console."""
    if log_to_console.value:
        print(msg)


# --- Lightweight profiler ---

_PERF: dict[str, list[float]] = {}  # name -> [total_ms, calls, max_ms]
_perf_last_dump: float = 0.0


def _perf_start() -> float:
    """Start a timing measurement. Returns start time (0 if profiling disabled)."""
    if not log_perf.value:
        return 0.0
    return time.perf_counter()


def _perf_end(name: str, start: float) -> None:
    """Record elapsed time for a named section."""
    if not log_perf.value:
        return
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    entry = _PERF.get(name)
    if entry is None:
        _PERF[name] = [elapsed_ms, 1, elapsed_ms]
    else:
        entry[0] += elapsed_ms
        entry[1] += 1
        if elapsed_ms > entry[2]:
            entry[2] = elapsed_ms


def _perf_dump() -> None:
    """Dump and reset profiler stats (called periodically from PlayerTick)."""
    global _perf_last_dump
    if not log_perf.value or not _PERF:
        return
    now = time.perf_counter()
    if now - _perf_last_dump < 5.0:
        return
    _perf_last_dump = now
    log("[IS-PERF] --- 5s profiler summary ---")
    for name, (total, calls, peak) in sorted(_PERF.items(), key=lambda x: -x[1][0]):
        avg = total / calls if calls > 0 else 0
        log(
            f"[IS-PERF] {name}: {total:.2f}ms total, "
            f"{calls} calls, {avg:.3f}ms avg, {peak:.3f}ms peak"
        )
    _PERF.clear()


# --- Tracking dicts ---

# Pellet accumulation:
# pawn id -> (timestamp, accumulated_damage, acc_mx, acc_my, acc_mz, applied_mag)
RECENT_DAMAGE: dict[str, tuple[float, float, float, float, float, float]] = {}
accumulation_window = SliderOption(
    "Accumulation Window",
    10,
    0,
    100,
    1,
    True,
    description=(
        "Time window (in ms) to accumulate momentum from multiple projectiles "
        "(e.g. shotgun pellets, Badaboom rockets). Hits within this window combine "
        "their force. 0 = no accumulation. Default: 10"
    ),
)

# Deferred impulse for ragdoll init:
# pawn id -> (ix, iy, iz, hit_loc, bone, vel_change[, compensate_mass])
PENDING_IMPULSE: dict[int, tuple] = {}

# Killing blow impulse: pawn id -> (ix, iy, iz, hit_loc, bone, vel_change[, compensate_mass])
DEATH_IMPULSE: dict[int, tuple] = {}

# Pawns whose impulse was already handled by TakeDamage - HandleMomentum should just block
IMPULSE_HANDLED: set[str] = set()

# All currently ragdolled pawns — used by singularity gravity wells for per-tick pull
# Keyed by PathName (stable UObject identifier) — id(pawn) is unstable because
# WeakPointer doesn't prevent Python wrapper GC, so id() can be reused by different pawns.
RAGDOLLED_PAWNS: dict[str, tuple[WeakPointer, float]] = {}  # uid -> (wp, cached_mass)


def _pawn_uid(pawn) -> str:
    """Stable UObject identifier (PathName). Unlike id(pawn), survives wrapper GC."""
    try:
        return pawn.PathName(pawn)
    except Exception:
        return ""


# Deferred launch velocity logging: read actual velocity next tick after PhysX simulates
# Each entry: (WeakPointer, enemy_name, force, mass, bone)
_PENDING_LAUNCH_LOG: list = []

# Throttle for standing-enemy well scan
_well_scan_time: float = 0.0

well_scan_enabled = BoolOption(
    "Well Scan",
    True,
    description=(
        "Enable scanning for standing enemies near singularity wells. "
        "When on, enemies that wander into an active gravity well get pulled in. "
        "Turn off to save performance if you don't use singularity grenades."
    ),
)

disable_ragdoll_collision = BoolOption(
    "Disable Ragdoll-Ragdoll Collision",
    False,
    description=(
        "Ragdolled pawns pass through each other instead of colliding. "
        "Massively reduces PhysX load when many ragdolls are crammed together "
        "(e.g. singularity wells). They still collide with the world and vehicles."
    ),
)

max_well_ragdolls = SliderOption(
    "Max Ragdolls Per Well",
    0,
    0,
    20,
    1,
    True,
    description=(
        "Cap how many ragdolls a single gravity well can track. "
        "0 = unlimited. Prevents PhysX overload from too many bodies in one spot. "
        "Default: 0"
    ),
)

# Cached true physics mass per (PhysicsAsset.Name, DrawScale): actual total mass
# Keyed by (name, ds) because physics mass doesn't scale predictably with DrawScale.
PHYSICS_MASS_CACHE: dict[tuple[str, float], float] = {}

BULLET_DMG_NAMES: set[str] = {
    "WillowDmgSource_Bullet",
    "WillowDmgSource_Shotgun",
    "WillowDmgSource_Pistol",
    "WillowDmgSource_Sniper",
    "WillowDmgSource_SubMachineGun",
    "WillowDmgSource_MachineGun",
}

# Explosion-type damage that should scatter nearby objects (loot, props)
EXPLOSION_DMG_NAMES: set[str] = {
    "WillowDmgSource_Grenade",
    "WillowDmgSource_Rocket",
}


# --- Options ---

min_ragdoll_velocity = SliderOption(
    "Min Ragdoll Velocity",
    1232,
    1,
    10000,
    1,
    True,
    description=(
        "Minimum estimated velocity (force/mass) to allow ragdoll. "
        "Prevents heavy enemies from ragdolling when they'd just flop. "
        "Higher = only ragdoll when the hit would visibly move them. "
        "Default: 1232"
    ),
)

damage_severity_threshold = SliderOption(
    "Damage Severity %",
    0,
    0,
    50,
    1,
    True,
    description=(
        "Minimum damage as % of enemy max health to allow ragdoll. "
        "0 = disabled (any hit can ragdoll). Set higher for only heavy hits. "
        "Default: 0"
    ),
)

low_health_threshold = SliderOption(
    "Low Health %",
    30,
    5,
    75,
    5,
    True,
    description=(
        "Below this health %, enemies can be ragdolled regardless of damage severity. Default: 30"
    ),
)

filter_bosses = BoolOption(
    "Filter Bosses",
    True,
    description=(
        "Prevent boss enemies from being staggered. "
        "Bosses have scripted sequences that can break "
        "when ragdolled. Default: On"
    ),
)
filter_champions = BoolOption(
    "Filter Champions",
    False,
    description="Prevent champion (mini-boss) enemies from being staggered. Default: Off",
)
stagger_filters_menu = NestedOption(
    "Stagger Filters",
    children=[filter_bosses, filter_champions],
    description="Control which enemy types can be staggered.",
)

impulse_scale = SliderOption(
    "Impulse Scale %",
    100,
    1,
    1000,
    10,
    True,
    description=(
        "How much force is applied to ragdolled enemies. "
        "100% = natural momentum. 200% = double force. "
        "Default: 100"
    ),
)

ragdoll_hit_scale_bullet = SliderOption(
    "Ragdoll Hit Scale % (Bullet)",
    100,
    0,
    400,
    5,
    True,
    description=(
        "Force on already-ragdolled enemies from bullet follow-up hits,"
        " as % of Impulse Scale. Default: 100"
    ),
)


ragdoll_hit_scale_other = SliderOption(
    "Ragdoll Hit Scale % (Other)",
    100,
    0,
    400,
    5,
    True,
    description=(
        "Force on already-ragdolled enemies from other damage follow-up hits,"
        " as % of Impulse Scale. Default: 100"
    ),
)

recovery_time = SliderOption(
    "Recovery Time",
    3,
    1,
    50,
    1,
    True,
    description=(
        "How long (in tenths of a second) enemies stay ragdolled. "
        "10 = 1s, 30 = 3s. Vanilla: random 5-30. "
        "Default: 3"
    ),
)

rest_velocity = SliderOption(
    "Rest Velocity",
    200,
    0,
    500,
    1,
    True,
    description=(
        "How slow the ragdoll must be moving before recovery can start. "
        "Higher = enemies start getting up while still sliding. "
        "Vanilla: 8. Default: 300"
    ),
)

ragdoll_friction = SliderOption(
    "Ragdoll Friction",
    12,
    0,
    100,
    1,
    True,
    description=(
        "Friction of ragdolled enemies (in tenths). "
        "Higher = more tumble, less slide. Vanilla: 7. "
        "Default: 12"
    ),
)

ragdoll_bounciness = SliderOption(
    "Ragdoll Bounciness",
    5,
    0,
    100,
    1,
    True,
    description=("Bounciness of ragdolled enemies (in tenths). Vanilla: 3. Default: 5"),
)

bullet_force_mult = SliderOption(
    "Bullet Force %",
    300,
    0,
    1000,
    5,
    True,
    description=(
        "Force % for all bullets (pistols, shotguns, snipers, SMGs, ARs). "
        "0 = no force, 100 = normal, 200 = 2x. Default: 300"
    ),
)


melee_force_mult = SliderOption(
    "Melee Force %",
    100,
    0,
    1000,
    5,
    True,
    description=(
        "Force % for melee hits (including bladed). "
        "0 = no force, 100 = normal, 200 = 2x. Default: 100"
    ),
)

skill_force_mult = SliderOption(
    "Skill Force %",
    100,
    0,
    1000,
    5,
    True,
    description=(
        "Force % for action skill damage. 0 = no force, 100 = normal, 200 = 2x. Default: 100"
    ),
)

vehicle_force_mult = SliderOption(
    "Vehicle Force %",
    100,
    0,
    1000,
    5,
    True,
    description="Force % for vehicle impacts. 0 = no force, 100 = normal, 200 = 2x. Default: 100",
)

shield_force_mult = SliderOption(
    "Shield Force %",
    100,
    0,
    1000,
    5,
    True,
    description=(
        "Force % for shield effects (novas, spikes). "
        "0 = no force, 100 = normal, 200 = 2x. Default: 100"
    ),
)

other_force_mult = SliderOption(
    "Other Force %",
    100,
    0,
    1000,
    5,
    True,
    description=(
        "Force % for any damage not covered above. "
        "0 = no force, 100 = normal, 200 = 2x. Default: 100"
    ),
)

max_bone_force = SliderOption(
    "Max Per-Bone Force",
    500000,
    100,
    500000,
    100,
    True,
    description=(
        "Maximum force applied to any single bone during per-bone explosions. Default: 500000"
    ),
)

min_explosion_force = SliderOption(
    "Min Explosion Force",
    0,
    0,
    100000,
    500,
    True,
    description=(
        "Minimum total force for explosion ragdolls (grenades, rockets, barrels). "
        "0 = no floor. "
        "Default: 0"
    ),
)

terminal_velocity = SliderOption(
    "Terminal Velocity",
    0,
    0,
    10000,
    100,
    True,
    description=(
        "When set, each additional hit applies less force the closer the enemy "
        "is to this speed. At the cap, further hits have no effect. "
        "Default: 0"
    ),
)

# --- Helpers ---


def get_mass(pawn) -> float:
    if pawn.BodyClass is not None and pawn.BodyClass.Mass > 0:
        return pawn.BodyClass.Mass
    return 100.0


# TODO: remove for prod — tracks unknown enemies during playtesting
_UNKNOWN_ENEMIES_FILE = Path(__file__).parent / "unknown_enemies.json"
_UNKNOWN_ENEMIES_SEEN: set[str] = set()  # AIClass names already recorded this session

# TODO: remove for prod — tracks all damage source names during playtesting
_DAMAGE_SOURCES_FILE = Path(__file__).parent / "damage_sources.json"
_DAMAGE_SOURCES_SEEN: set[str] = set()  # DamageType names already recorded this session


def _record_unknown_enemy(pawn, ai_name: str) -> None:
    """Log and record an enemy not in the mass dict. Appends to unknown_enemies.json."""
    if ai_name in _UNKNOWN_ENEMIES_SEEN:
        return
    _UNKNOWN_ENEMIES_SEEN.add(ai_name)

    # Gather useful info for defining the mass later
    ds = pawn.DrawScale if pawn.DrawScale > 0 else 1.0
    body_mass = 100.0
    phys_asset_name = "?"
    true_mass = None
    try:
        if pawn.BodyClass is not None:
            body_mass = pawn.BodyClass.Mass
    except Exception:
        pass
    try:
        pa = pawn.Mesh.PhysicsAsset
        if pa is not None:
            phys_asset_name = pa.Name
    except Exception:
        pass
    true_mass = get_true_physics_mass(pawn)

    log(
        f"[IS] UNKNOWN ENEMY: {ai_name} "
        f"(BodyClass.Mass={body_mass}, ds={ds}, "
        f"phys_asset={phys_asset_name}, true_mass={true_mass})"
    )

    entry = {
        "ai_name": ai_name,
        "body_mass": body_mass,
        "draw_scale": ds,
        "phys_asset": phys_asset_name,
        "true_physics_mass": true_mass,
    }

    # Load existing, append, save
    try:
        existing = []
        if _UNKNOWN_ENEMIES_FILE.exists():
            existing = json.loads(_UNKNOWN_ENEMIES_FILE.read_text())
        # Skip if already in file from a previous session
        if not any(e.get("ai_name") == ai_name for e in existing):
            existing.append(entry)
            _UNKNOWN_ENEMIES_FILE.write_text(json.dumps(existing, indent=2))
    except Exception as e:
        log(f"[IS] failed to write unknown_enemies.json: {e}")


def _record_damage_source(dmg_name: str, mag: float, enemy_name: str) -> None:
    """Log and record a damage source name. Appends to damage_sources.json."""
    if dmg_name in _DAMAGE_SOURCES_SEEN:
        return
    _DAMAGE_SOURCES_SEEN.add(dmg_name)

    log(f"[IS] NEW DAMAGE SOURCE: {dmg_name} (first seen on {enemy_name} mag={mag:.0f})")

    entry = {
        "dmg_name": dmg_name,
        "first_enemy": enemy_name,
        "first_mag": mag,
    }

    try:
        existing = []
        if _DAMAGE_SOURCES_FILE.exists():
            existing = json.loads(_DAMAGE_SOURCES_FILE.read_text())
        if not any(e.get("dmg_name") == dmg_name for e in existing):
            existing.append(entry)
            _DAMAGE_SOURCES_FILE.write_text(json.dumps(existing, indent=2))
    except Exception as e:
        log(f"[IS] failed to write damage_sources.json: {e}")


def _bake_bone_masses(pawn, mesh) -> None:
    """Set InstanceMassScale on each ragdoll bone so total mass equals ENEMY_MASS[name] * ds³.

    Also sets pawn.Mass (per-instance) for any native code that reads it.
    Must be called AFTER get_true_physics_mass has cached the original mass.
    """
    try:
        ai_name = pawn.AIClass.Name if pawn.AIClass is not None else None
    except Exception:
        ai_name = None

    base_mass = enemy_masses.get_base_mass(ai_name)
    if base_mass is None:
        # TODO: remove for prod — accumulates unknown enemies to JSON for playtesting
        if ai_name is not None:
            _record_unknown_enemy(pawn, ai_name)
        return

    # Cache original mass before we modify anything (idempotent if already cached)
    true_mass = get_true_physics_mass(pawn)
    if true_mass is None or true_mass <= 0:
        return

    ds = pawn.DrawScale if pawn.DrawScale > 0 else 1.0
    desired = base_mass * ds**3
    scale = desired / true_mass

    # Set per-instance pawn mass for any native code that reads it
    pawn.Mass = desired

    phys_inst = mesh.PhysicsAssetInstance
    phys_asset = mesh.PhysicsAsset
    if phys_inst is None or phys_asset is None:
        return

    try:
        bodies = phys_inst.Bodies
        setups = phys_asset.BodySetup
        if bodies is None or setups is None:
            return
        n = len(bodies)
        ns = len(setups)
        min_bone_mass = desired / (2.0 * n)  # floor: half of average bone mass

        # Read original masses and compute per-bone scale in one pass
        orig_masses = [bodies[i].GetBodyMass() for i in range(n)]
        # Log any zero-mass bodies
        zero_bones = [str(setups[i].BoneName) for i in range(min(n, ns)) if orig_masses[i] <= 0.0]
        if zero_bones:
            log(f"[IS] Zero-mass bodies on {ai_name}: {', '.join(zero_bones)}")
        # Apply floor: each bone at least min_bone_mass
        floored = [max(m, min_bone_mass) if m > 0.0 else m for m in orig_masses]
        # Uniform scale so floored total hits desired
        floored_total = sum(floored)
        if floored_total < 0.01:
            return
        final_scale = desired / floored_total
        boosted = 0
        for i in range(n):
            bone_scale = (
                (floored[i] / orig_masses[i] * final_scale) if orig_masses[i] > 0.0 else scale
            )
            bodies[i].InstanceMassScale = bone_scale
            if i < ns:
                bodies[i].UpdateMassProperties(setups[i])
            if floored[i] > orig_masses[i]:
                boosted += 1

        log(
            f"[IS] baked bone masses: {ai_name} desired={desired:.1f} original={true_mass:.1f} "
            f"min_bone={min_bone_mass:.2f} boosted={boosted}"
        )
    except Exception as e:
        log(f"[IS] bake_bone_masses FAILED: {e}")


def get_desired_mass(pawn) -> float:
    """Get the desired total mass for velocity estimation.

    Dict enemies: enemy_masses.get_base_mass(name) * ds³
    Unknown: true_physics_mass (if cached) or BodyClass.Mass * ds³ (fallback)
    """
    try:
        ai_name = pawn.AIClass.Name if pawn.AIClass is not None else None
    except Exception:
        ai_name = None

    ds = pawn.DrawScale if pawn.DrawScale > 0 else 1.0

    base_mass = enemy_masses.get_base_mass(ai_name)
    if base_mass is not None:
        return base_mass * ds**3

    true_mass = get_true_physics_mass(pawn)
    if true_mass is not None and true_mass > 0:
        return true_mass

    return get_mass(pawn) * ds**3


def get_true_physics_mass(pawn) -> float | None:
    """Get the actual total physics mass from bone bodies, cached per PhysicsAsset + DrawScale.

    Returns the true total mass, or None if unavailable.
    In case physics mass doesn't scale predictably with DrawScale,
    each (asset, ds) combination is cached separately.
    """
    mesh = pawn.Mesh
    if mesh is None:
        return None
    phys_asset = mesh.PhysicsAsset
    if phys_asset is None:
        return None

    ds = pawn.DrawScale if pawn.DrawScale > 0 else 1.0
    cache_key = (phys_asset.Name, ds)

    cached = PHYSICS_MASS_CACHE.get(cache_key)
    if cached is not None:
        log(f"[IS] mass cache hit: {cache_key[0]} ds={cache_key[1]:.2f} total={cached:.1f}")
        return cached

    # Compute from PhysicsAssetInstance (only available when ragdolled)
    phys_inst = mesh.PhysicsAssetInstance
    if phys_inst is None:
        return None
    try:
        bodies = phys_inst.Bodies
        if bodies is None or len(bodies) == 0:
            return None
        total = sum(bodies[i].GetBodyMass() for i in range(len(bodies)))
        if total <= 0:
            return None
        PHYSICS_MASS_CACHE[cache_key] = total
        try:
            name = pawn.AIClass.Name if pawn.AIClass is not None else pawn.Name
        except Exception:
            name = "?"
        log(f"[IS] cached physics mass: {name} total={total:.1f} ds={ds:.2f}")
        return total
    except Exception:
        return None


# PhysicsAssets that should never be ragdolled
# (too large, stationary, or extreme mass like Threshers at mass=100,000)
EXCLUDED_ASSETS: set[str] = {
    "Phat_Thresher",
    "Phat_ThresherArm",
    "Phat_ConstructorBot",
    "Phat_HyperionTurret_Gun",
}


def can_ragdoll(pawn) -> bool:
    """Check if a pawn is eligible for our ragdoll effects."""
    if pawn.bTearOff or pawn.bHardAttach:
        return False
    bc = pawn.BodyClass
    if bc is not None:
        if bc.IgnoreMomentumTransfer:
            return False
        if bc.bVehicleBlockingActor:
            return False
    # Exclude large/stationary enemies by PhysicsAsset
    try:
        mesh = pawn.Mesh
        if mesh is not None:
            phys_asset = mesh.PhysicsAsset
            if phys_asset is not None and phys_asset.Name in EXCLUDED_ASSETS:
                return False
    except Exception:
        pass
    # Exclude pawns in cinematics - ragdolling them can hang the game
    # by interrupting matinee sequences the game is waiting on.
    try:
        if pawn.bUsedByMatinee:
            return False
    except Exception:
        pass
    # Exclude boss/champion enemies based on filter options
    if filter_bosses.value:
        try:
            if pawn.IsBoss():
                return False
        except Exception:
            pass
    if filter_champions.value:
        try:
            if pawn.IsChampion():
                return False
        except Exception:
            pass
    return True


def get_ragdoll_hit_scale(dmg_name: str) -> float:
    """Return the per-source ragdoll hit scale (0-1 range) based on damage type name."""
    # the game doesnt have the most realistic magnitudes baked in,
    # - well not that there is much realism anyways
    if singularity.is_singularity(dmg_name):
        return ragdoll_hit_scale_other.value / 100.0
    if dmg_name in BULLET_DMG_NAMES:
        return ragdoll_hit_scale_bullet.value / 100.0
    if grenades.is_grenade(dmg_name):
        return grenades.get_ragdoll_hit_scale()
    if rockets.is_rocket(dmg_name):
        return rockets.get_ragdoll_hit_scale()
    return ragdoll_hit_scale_other.value / 100.0


SOURCE_SCALE_MAP: dict[str, SliderOption] = {
    "WillowDmgSource_Bullet": bullet_force_mult,
    "WillowDmgSource_Pistol": bullet_force_mult,
    "WillowDmgSource_Shotgun": bullet_force_mult,
    "WillowDmgSource_Sniper": bullet_force_mult,
    "WillowDmgSource_SubMachineGun": bullet_force_mult,
    "WillowDmgSource_MachineGun": bullet_force_mult,
    "WillowDmgSource_Grenade": grenades.grenade_force_mult,
    "WillowDmgSource_Rocket": rockets.rocket_force_mult,
    "WillowDmgSource_Melee": melee_force_mult,
    "WillowDmgSource_MeleeWithBlade": melee_force_mult,
    "WillowDmgSource_Skill": skill_force_mult,
    "WillowDmgSource_VehicleRanInto": vehicle_force_mult,
    "WillowDmgSource_VehicleRanOver": vehicle_force_mult,
    "WillowDmgSource_Shield": shield_force_mult,
    "WillowDmgSource_ShieldNova": shield_force_mult,
    "WillowDmgSource_ShieldSpike": shield_force_mult,
}


def get_source_scale(dmg_name: str) -> float:
    """Return the per-source force multiplier based on damage type name."""
    if singularity.is_singularity(dmg_name):
        return singularity.get_source_scale()
    slider = SOURCE_SCALE_MAP.get(dmg_name)
    if slider is not None:
        return slider.value / 100.0
    return other_force_mult.value / 100.0


def compute_impulse(pawn, args, mag, scale):
    """Compute impulse direction and magnitude from TakeDamage args.

    Handles all damage sources including singularity (sqrt dampening + force floor).
    """
    momentum = args.Momentum

    # At close range, bullet traces can have misleading directions - use player-to-enemy instead
    # The gun is not centered on the player pawn, when you are close to an enemy sometimes the
    # projectiles fly towards you i think to hit them where you are looking at them.
    instigator = args.InstigatedBy
    damage_type = args.DamageType
    use_momentum_dir = True
    dmg_name = damage_type.Name if damage_type is not None else "None"
    has_instigator = instigator is not None and instigator.Pawn is not None
    if damage_type is not None and dmg_name in BULLET_DMG_NAMES and has_instigator:
        src = instigator.Pawn.Location
        dst = pawn.Location
        dx, dy, dz = dst.X - src.X, dst.Y - src.Y, dst.Z - src.Z
        dist = sqrt(dx * dx + dy * dy + dz * dz)
        if 0 < dist < 300:
            use_momentum_dir = False
            dir_x, dir_y, dir_z = dx / dist, dy / dist, dz / dist

    if use_momentum_dir:
        if mag <= 0:
            return None
        dir_x, dir_y, dir_z = momentum.X / mag, momentum.Y / mag, momentum.Z / mag

    source_mult = get_source_scale(dmg_name)
    force = mag * scale * source_mult

    # Explosions: minimum force floor
    if dmg_name not in BULLET_DMG_NAMES:
        floor = float(min_explosion_force.value)
        if 0 < force < floor:
            force = floor

    ix, iy, iz = dir_x * force, dir_y * force, dir_z * force

    return ix, iy, iz


def compute_impulse_from_momentum(momentum, mag, scale, dmg_name=""):
    """Compute impulse from a raw momentum vector.

    For HandleMomentum which has no TakeDamage args.
    """
    if mag <= 0:
        return None
    source_mult = get_source_scale(dmg_name)
    force = mag * scale * source_mult

    # Singularity: sqrt dampening with minimum floor
    if singularity.is_singularity(dmg_name):
        force = singularity.compute_force(force)
    # Explosions: minimum force floor
    elif dmg_name not in BULLET_DMG_NAMES:
        floor = float(min_explosion_force.value)
        if 0 < force < floor:
            force = floor

    ix = momentum.X / mag * force
    iy = momentum.Y / mag * force
    iz = momentum.Z / mag * force

    return ix, iy, iz


def apply_per_bone_impulse(
    pawn,
    ix,
    iy,
    iz,
    center,
    min_dist=50.0,
    vel_change=False,
    compensate_mass=False,
):
    """Apply impulse to each physics bone - original direction, magnitude weighted by 1/dist²."""
    mesh = pawn.Mesh
    phys_asset = mesh.PhysicsAsset
    if phys_asset is None:
        return False

    body_setups = phys_asset.BodySetup
    if body_setups is None or len(body_setups) == 0:
        return False

    total_force = sqrt(ix * ix + iy * iy + iz * iz)
    if total_force <= 0:
        return False
    dir_x, dir_y, dir_z = ix / total_force, iy / total_force, iz / total_force

    # Mass compensation: for singularity (mass-equalized pull), multiply by current total
    # mass so AddImpulse's bone mass division cancels out -> uniform velocity for all enemies.
    # Normal hits need no correction — bone masses baked in on_ragdoll_ready.
    if compensate_mass:
        current_mass = get_desired_mass(pawn)
        if current_mass > 0:
            ratio = current_mass / singularity.MASS_REFERENCE
            total_force *= ratio
            log(
                f"[IS] mass compensate: mass={current_mass:.1f} "
                f"ratio={ratio:.1f} force={total_force:.0f}"
            )
        else:
            log(f"[IS] mass compensate FAILED: mass={current_mass}")

    cx, cy, cz = center.X, center.Y, center.Z

    # Gather bone distances from center
    bones = []
    for i in range(len(body_setups)):
        bone_name = body_setups[i].BoneName
        try:
            bone_loc = mesh.GetBoneLocation(bone_name, 0)
            bx, by, bz = bone_loc.X, bone_loc.Y, bone_loc.Z
        except Exception:
            return False
        dx, dy, dz = bx - cx, by - cy, bz - cz
        dist = sqrt(dx * dx + dy * dy + dz * dz)
        if dist < min_dist:
            dist = min_dist
        bones.append((bone_name, dist))

    if not bones:
        return False

    # Normalize weights so total impulse across all bones = total_force
    # One way to make this more realistic would be to calc the frontal area
    # with respect to the explosion but we dont even get the coordinates of
    # the explosion that way an enemy laying laying on the floor next to a
    # detonation roughly on ground level will experience a lot less force
    # than someone standing next to it
    total_weight = sum(1.0 / (d**2) for _, d in bones)
    if total_weight <= 0:
        return False

    cap = float(max_bone_force.value)

    # Get per-bone masses for velocity-aware capping (prevents light bones from
    # reaching extreme velocities even when force cap is reasonable)
    bone_masses = None
    if not vel_change:
        phys_inst = mesh.PhysicsAssetInstance
        if phys_inst is not None:
            try:
                bodies = phys_inst.Bodies
                if bodies is not None and len(bodies) == len(bones):
                    bone_masses = [bodies[i].GetBodyMass() for i in range(len(bodies))]
            except Exception:
                pass

    vel = pawn.Velocity
    for i, (bone_name, dist) in enumerate(bones):
        weight = (1.0 / (dist**2)) / total_weight
        force = total_force * weight
        if force > cap:
            force = cap
        # Cap per-bone velocity: force/bone_mass must not exceed max_bone_force
        # This prevents light bones (mass 0.1) from reaching 50x the velocity of
        # heavy bones (mass 5.0) with the same force cap
        if bone_masses is not None and bone_masses[i] > 0:
            vel_cap = cap * bone_masses[i]
            if force > vel_cap:
                force = vel_cap
        vel.X, vel.Y, vel.Z = dir_x * force, dir_y * force, dir_z * force
        mesh.AddImpulse(vel, center, bone_name, vel_change)

    vc_label = "speed" if vel_change else "force"
    log(f"[IS] {vc_label}={total_force:.0f} -> {len(bones)} bones")
    return True


def apply_impulse(
    pawn,
    ix,
    iy,
    iz,
    hit_location,
    bone_name,
    vel_change=False,
    compensate_mass=False,
):
    """Apply impulse - bone-specific AddImpulse if bone known, else per-bone."""
    # Record launch time so impact damage ignores the initial ground contact.
    # Only when ragdoll is near-stationary (on the ground) - if already airborne,
    # a follow-up hit shouldn't delay wall-impact damage.
    grace_threshold = impact_damage.launch_speed_threshold.value
    try:
        root_body = pawn.Mesh.GetRootBodyInstance()
        if root_body is not None:
            rb_vel = root_body.GetUnrealWorldVelocity()
            speed = sqrt(rb_vel.X**2 + rb_vel.Y**2 + rb_vel.Z**2)
            if speed < grace_threshold or grace_threshold == 0:
                world_time = ENGINE.GetCurrentWorldInfo().TimeSeconds
                impact_damage.record_launch(_pawn_uid(pawn), world_time)
    except Exception:
        pass

    # Terminal velocity: reduce force based on how fast pawn is already moving
    # also means forces that would slow them down are reduced (needs tuning)
    cap = float(terminal_velocity.value)
    if cap > 0:
        try:
            root_body = pawn.Mesh.GetRootBodyInstance()
            if root_body is not None:
                rb_vel = root_body.GetUnrealWorldVelocity()
                current_speed = sqrt(rb_vel.X**2 + rb_vel.Y**2 + rb_vel.Z**2)
                headroom = max(0.0, 1.0 - current_speed / cap)
                if headroom < 1.0:
                    ix, iy, iz = ix * headroom, iy * headroom, iz * headroom
                    log(f"[IS] terminal vel: {current_speed:.0f}/{cap:.0f} headroom={headroom:.0%}")
        except Exception:
            pass  # Non-ragdolled or no physics - skip velocity check

    mesh = pawn.Mesh
    mesh.WakeRigidBody()
    has_bone = bone_name != "" and bone_name != "None"

    if has_bone:
        # Walk up the bone hierarchy to find the nearest bone with a physics body
        phys_asset = mesh.PhysicsAsset
        if phys_asset is not None:
            resolved = bone_name
            while phys_asset.FindBodyIndex(resolved) < 0:
                parent = mesh.GetParentBone(resolved)
                if parent == "" or parent == "None" or parent == resolved:
                    resolved = ""
                    break
                resolved = parent
            if resolved == "":
                # No physics bone found at all - fall back to per-bone
                apply_per_bone_impulse(
                    pawn,
                    ix,
                    iy,
                    iz,
                    hit_location,
                    vel_change=vel_change,
                    compensate_mass=compensate_mass,
                )
                return
            # Use the nearest physics bone (may be the original or a parent)
            bone_name = resolved

        # Singularity mass compensation: multiply by current total mass so AddImpulse's
        # bone mass division cancels out -> uniform velocity for all enemies.
        # Normal hits don't need correction — bone masses baked in on_ragdoll_ready.
        if compensate_mass:
            current_mass = get_desired_mass(pawn)
            if current_mass > 0:
                ratio = current_mass / singularity.MASS_REFERENCE
                ix, iy, iz = ix * ratio, iy * ratio, iz * ratio

        total = sqrt(ix * ix + iy * iy + iz * iz)
        vc_label = "speed" if vel_change else "force"
        log(f"[IS] {vc_label}={total:.0f} -> {bone_name}")
        vel = pawn.Velocity
        vel.X, vel.Y, vel.Z = ix, iy, iz
        mesh.AddImpulse(vel, hit_location, bone_name, vel_change)
    else:
        apply_per_bone_impulse(
            pawn,
            ix,
            iy,
            iz,
            hit_location,
            vel_change=vel_change,
            compensate_mass=compensate_mass,
        )


# --- Hooks ---


@hook("WillowGame.WillowAIPawn:TakeDamage", Type.PRE)
def on_take_damage(
    obj: unreal.UObject,
    args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    pawn = obj

    # Client guard: let engine handle native damage, don't compute impulses
    if multiplayer.is_client():
        return

    pid = id(pawn)
    uid = _pawn_uid(pawn)
    momentum = args.Momentum
    damage = args.Damage
    mag = sqrt(momentum.X**2 + momentum.Y**2 + momentum.Z**2)
    # Skip zero-momentum damage (DoT ticks, status effects, Tediore throws)
    if mag < 1.0:
        return

    if not can_ragdoll(pawn):
        if log_to_console.value:
            try:
                _dn = pawn.AIClass.Name if pawn.AIClass is not None else "?"
                _bc = pawn.BodyClass
                _vba = _bc.bVehicleBlockingActor if _bc is not None else "?"
                _imt = _bc.IgnoreMomentumTransfer if _bc is not None else "?"
                _mat = pawn.bUsedByMatinee
                _smt = (
                    pawn.Controller.ScriptedMoveTarget is not None
                    if pawn.Controller is not None
                    else False
                )
            except Exception:
                _dn, _vba, _imt, _mat, _smt = "?", "?", "?", "?", "?"
            log(
                f"[IS] can_ragdoll FALSE: {_dn} "
                f"tearOff={pawn.bTearOff} hardAttach={pawn.bHardAttach} "
                f"vba={_vba} imt={_imt} matinee={_mat} scripted={_smt}"
            )
        return
    # Clear stale flag - each TakeDamage starts fresh so later non-TakeDamage
    # HandleMomentum calls (singularity, knockback grenades) aren't wrongly blocked
    IMPULSE_HANDLED.discard(uid)

    dmg_type = args.DamageType
    dmg_name = dmg_type.Name if dmg_type is not None else ""

    ds = pawn.DrawScale if pawn.DrawScale > 0 else 1.0
    try:
        enemy_name = pawn.AIClass.Name if pawn.AIClass is not None else pawn.Name
    except Exception:
        enemy_name = "?"

    # TODO: remove for prod — record all damage source names
    if dmg_name:
        _record_damage_source(dmg_name, mag, enemy_name)

    # Singularity pull: arrives as TakeDamage with DamageType=None and raw momentum.
    # Detected via DamageCauser projectile definition (e.g. Grenade_SingularityChild).
    # No threshold/severity checks - singularity pulls everything.
    # Mass equalization (compensate_mass) ensures equal pull speed across enemy types.
    # No sqrt dampening - TakeDamage raw momentum is already reasonable pull force.
    _is_sing_pull = False
    if dmg_name == "":
        try:
            _causer = args.DamageCauser
            if _causer is not None:
                _defn = _causer.Definition
                if _defn is not None and "Singularity" in _defn.Name:
                    _is_sing_pull = True
        except Exception:
            pass
    if _is_sing_pull:
        log(f"[IS] singularity pull: {enemy_name} mag={mag:.0f} ds={ds:.2f}")
        # --- Radius diagnostics (TODO: remove for prod) ---
        if log_to_console.value:
            try:
                log(f"[IS-SING-RADIUS] DamageRadius={_causer.DamageRadius}")
            except Exception as e:
                log(f"[IS-SING-RADIUS] DamageRadius err: {e}")
            try:
                log(f"[IS-SING-RADIUS] Def.DamageRadius={_causer.Definition.DamageRadius}")
            except Exception as e:
                log(f"[IS-SING-RADIUS] Def.DamageRadius err: {e}")
            try:
                _gm = _causer.InstigatorController.Pawn.GetActiveOrBestGrenadeMod()
                log(f"[IS-SING-RADIUS] GrenadeMod.BlastRadius={_gm.BlastRadius}")
            except Exception as e:
                log(f"[IS-SING-RADIUS] GrenadeMod err: {e}")
        # --- End radius diagnostics ---
        # Register gravity well at the singularity projectile's location.
        # Multiple enemies hit by the same singularity all fire TakeDamage — deduplicate
        # by location proximity (same grenade = same position within 1 unit).
        # Register this pawn with the well so only originally-pulled pawns get the
        # continuous gravity effect.
        _well_x, _well_y, _well_z = 0.0, 0.0, 0.0
        try:
            causer_loc = _causer.Location
            cx, cy, cz = causer_loc.X, causer_loc.Y, causer_loc.Z
            _well_x, _well_y, _well_z = cx, cy, cz
            current_time = ENGINE.GetCurrentWorldInfo().TimeSeconds
            is_dup = False
            for well in singularity.ACTIVE_WELLS:
                if abs(cx - well[0]) < 1 and abs(cy - well[1]) < 1 and abs(cz - well[2]) < 1:
                    is_dup = True
                    break
            if not is_dup:
                singularity.add_well(cx, cy, cz, current_time)
                log(f"[IS] gravity well added at ({cx:.0f}, {cy:.0f}, {cz:.0f})")
        except Exception:
            pass
        # Skip enemies beyond catch radius
        _sing_catch_r = float(singularity.singularity_catch_radius.value)
        try:
            _px, _py, _pz = pawn.Location.X, pawn.Location.Y, pawn.Location.Z
            _ddx, _ddy, _ddz = _px - _well_x, _py - _well_y, _pz - _well_z
            _dist = (_ddx * _ddx + _ddy * _ddy + _ddz * _ddz) ** 0.5
            log(
                f"[IS-SING-RADIUS] {enemy_name} dist={_dist:.0f} "
                f"from well (max={_sing_catch_r:.0f})"
            )
            if _dist > _sing_catch_r:
                log(
                    f"[IS] singularity skip: {enemy_name} "
                    f"too far ({_dist:.0f} > {_sing_catch_r:.0f})"
                )
                IMPULSE_HANDLED.add(uid)
                return Block
        except Exception:
            pass
        # Register this pawn with the well (existing or just-created)
        _puid = _pawn_uid(pawn)
        singularity.register_pawn_at(
            _puid,
            _well_x,
            _well_y,
            _well_z,
            max_per_well=max_well_ragdolls.value,
        )
        log(f"[IS-SING] registered {enemy_name} uid={_puid} with well")

        scale = impulse_scale.value / 100.0
        source_mult = singularity.get_source_scale()
        force = mag * scale * source_mult
        # Min floor for edge-of-range pulls
        floor = float(singularity.min_singularity_force.value)
        if 0 < force < floor:
            force = floor
        dir_x = momentum.X / mag
        dir_y = momentum.Y / mag
        dir_z = momentum.Z / mag
        already_staggered = pawn.StaggerState.bIsStaggered

        # Killing blow - defer to PlayDying
        if pawn.GetHealth() - damage <= 0:
            ix, iy, iz = dir_x * force, dir_y * force, dir_z * force
            DEATH_IMPULSE[pid] = (ix, iy, iz, args.HitLocation, "", False, True)
            multiplayer.broadcast_death(pawn, ix, iy, iz, args.HitLocation, "", False, True)
            IMPULSE_HANDLED.add(uid)
            return

        # Ensure ragdoll lasts at least as long as the gravity well
        well_dur = singularity.singularity_well_duration.value / 10.0
        ragdoll_dur = max(recovery_time.value / 10.0, well_dur)

        if already_staggered:
            state = int(pawn.StaggerState.State)
            hit_scale = get_ragdoll_hit_scale("Singularity")
            f = force * hit_scale
            ix, iy, iz = dir_x * f, dir_y * f, dir_z * f
            if state in (1, 2, 3):
                PENDING_IMPULSE[pid] = (ix, iy, iz, args.HitLocation, "", False, True)
                pawn.Stagger(None)
                pawn.StaggerState.StaggerEndRagdollTime = (
                    ENGINE.GetCurrentWorldInfo().TimeSeconds + ragdoll_dur
                )
                leftover = PENDING_IMPULSE.pop(pid, None)
                if leftover is not None:
                    apply_impulse(pawn, *leftover)
                multiplayer.broadcast_impulse(pawn, ix, iy, iz, args.HitLocation, "", False, True)
            else:
                # Extend ragdoll time for gravity well
                pawn.StaggerState.StaggerEndRagdollTime = (
                    ENGINE.GetCurrentWorldInfo().TimeSeconds + ragdoll_dur
                )
                apply_impulse(pawn, ix, iy, iz, args.HitLocation, "", False, True)
                multiplayer.broadcast_impulse(pawn, ix, iy, iz, args.HitLocation, "", False, True)
        else:
            ix, iy, iz = dir_x * force, dir_y * force, dir_z * force
            PENDING_IMPULSE[pid] = (ix, iy, iz, args.HitLocation, "", False, True)
            pawn.Stagger(None)
            # Extend ragdoll time for gravity well (on_ragdoll_ready already set 0.3s default,
            # override it here so the pawn stays ragdolled for the full well duration)
            pawn.StaggerState.StaggerEndRagdollTime = (
                ENGINE.GetCurrentWorldInfo().TimeSeconds + ragdoll_dur
            )
            multiplayer.broadcast_impulse(pawn, ix, iy, iz, args.HitLocation, "", False, True)

        IMPULSE_HANDLED.add(uid)
        return

    # Named DamageType singularity idk if this code has ever run
    if singularity.is_singularity(dmg_name):
        return

    log(f"[IS] hit: {enemy_name} src={dmg_name} mag={mag:.0f} ds={ds:.2f}")

    # Accumulate damage over a short window for multi-pellet weapons (shotguns,
    # badaboom(though rockets dont appear to be exploding at the same time?), etc)
    # Applies to both fresh and already-staggered enemies!
    world_time = ENGINE.GetCurrentWorldInfo().TimeSeconds
    prev = RECENT_DAMAGE.get(uid)
    if prev is not None and (world_time - prev[0]) < (accumulation_window.value / 1000.0):
        accumulated = prev[1] + damage
        acc_mx, acc_my, acc_mz = prev[2] + momentum.X, prev[3] + momentum.Y, prev[4] + momentum.Z
        applied_mag = prev[5]
    else:
        accumulated = damage
        acc_mx, acc_my, acc_mz = momentum.X, momentum.Y, momentum.Z
        applied_mag = 0.0
    RECENT_DAMAGE[uid] = (world_time, accumulated, acc_mx, acc_my, acc_mz, applied_mag)

    acc_mag = sqrt(acc_mx**2 + acc_my**2 + acc_mz**2)

    already_staggered = pawn.StaggerState.bIsStaggered

    if not already_staggered:
        max_health = pawn.GetMaxHealth()
        if max_health <= 0:
            return
        health_pct = pawn.GetHealth() / max_health
        is_low_health = health_pct <= low_health_threshold.value / 100.0

        if not is_low_health:
            sev = float(damage_severity_threshold.value)
            sev_pct = accumulated / max_health * 100.0
            if sev > 0 and sev_pct < sev:
                log(f"[IS] severity={sev_pct:.0f}%, FAILED (need {sev:.1f}%)")
                IMPULSE_HANDLED.add(uid)
                return
            if sev > 0:
                log(f"[IS] severity={sev_pct:.0f}%, passed ({sev:.1f}%)")

    # Use delta magnitude: only apply the incremental force since last application
    # Prevents each pellet from applying the FULL accumulated force
    delta_mag = acc_mag - applied_mag
    if delta_mag < 1.0:
        IMPULSE_HANDLED.add(uid)
        return
    mag = delta_mag

    scale = impulse_scale.value / 100.0
    source_mult = get_source_scale(dmg_name)
    hit_location = args.HitLocation
    hit_info = args.HitInfo
    bone_name = hit_info.BoneName if hit_info is not None else ""

    # Rockets: always per-bone for consistent full-body impact across all enemy types
    if rockets.is_rocket(dmg_name):
        bone_name = ""

    # Estimate resulting velocity - skip ragdoll if it wouldn't visibly move the enemy
    # Check if the bone will actually get single-bone treatment or fall back to per-bone
    has_bone = bone_name != "" and bone_name != "None"
    will_use_single_bone = False
    if has_bone:
        mesh = pawn.Mesh
        phys_asset = mesh.PhysicsAsset if mesh is not None else None
        if phys_asset is not None:
            resolved = bone_name
            while phys_asset.FindBodyIndex(resolved) < 0:
                parent = mesh.GetParentBone(resolved)
                if parent == "" or parent == "None" or parent == resolved:
                    resolved = ""
                    break
                resolved = parent
            # Single-bone only if bone has its own physics body (didn't walk to parent)
            will_use_single_bone = resolved == bone_name
    # Estimate velocity using desired mass from enemy_masses dict.
    # For per-bone: 1/dist² concentrates most force on central bones (spine/hips),
    # so effective mass resisting visible motion is ~1/4 of total, not total.
    # For single-bone: force goes to one bone (~1/16 of total).
    desired_mass = get_desired_mass(pawn)
    if will_use_single_bone:
        effective_mass = desired_mass / 16.0
    else:
        effective_mass = desired_mass / 4.0
    est_force = mag * scale * source_mult
    if dmg_name not in BULLET_DMG_NAMES:
        floor = float(min_explosion_force.value)
        if 0 < est_force < floor:
            est_force = floor
    # Use accumulated magnitude for stagger decision (can we tip them over?)
    # but delta magnitude for actual force application (don't double-apply)
    acc_est_force = acc_mag * scale * source_mult
    if dmg_name not in BULLET_DMG_NAMES:
        if 0 < acc_est_force < floor:
            acc_est_force = floor
    estimated_vel = acc_est_force / effective_mass
    if not already_staggered and estimated_vel < min_ragdoll_velocity.value:
        log(f"[IS] est. speed={estimated_vel:.0f}, FAILED (need {min_ragdoll_velocity.value})")
        IMPULSE_HANDLED.add(uid)
        return
    if not already_staggered:
        log(f"[IS] est. speed={estimated_vel:.0f}, passed ({min_ragdoll_velocity.value})")

    # Killing blow - defer to PlayDying hook
    if pawn.GetHealth() - damage <= 0:
        log(
            f"[IS] killing blow: {enemy_name} hp={pawn.GetHealth():.0f} "
            f"dmg={damage:.0f} staggered={already_staggered}"
        )
        impulse = compute_impulse(pawn, args, mag, scale)
        if impulse is not None:
            DEATH_IMPULSE[pid] = (*impulse, hit_location, bone_name, False)
            multiplayer.broadcast_death(pawn, *impulse, hit_location, bone_name, False)
        IMPULSE_HANDLED.add(uid)
        return

    if already_staggered:
        state = int(pawn.StaggerState.State)
        hit_scale = scale * get_ragdoll_hit_scale(dmg_name)

        if state in (1, 2, 3):
            # Recovering - re-stagger them
            impulse = compute_impulse(pawn, args, mag, hit_scale)
            if impulse is not None:
                PENDING_IMPULSE[pid] = (*impulse, hit_location, bone_name, False)
            pawn.Stagger(None)
            pawn.StaggerState.StaggerEndRagdollTime = (
                ENGINE.GetCurrentWorldInfo().TimeSeconds + recovery_time.value / 10.0
            )
            # SetupRagdollPhysics only fires for states 2,3 - for state 1, apply directly
            leftover = PENDING_IMPULSE.pop(pid, None)
            if leftover is not None:
                apply_impulse(pawn, *leftover)
            if impulse is not None:
                multiplayer.broadcast_impulse(pawn, *impulse, hit_location, bone_name, False)
        else:
            # Still ragdolled (state 0) - hit them while down
            impulse = compute_impulse(pawn, args, mag, hit_scale)
            if impulse is not None:
                apply_impulse(pawn, *impulse, hit_location, bone_name, False)
                multiplayer.broadcast_impulse(pawn, *impulse, hit_location, bone_name, False)
    else:
        # Fresh stagger - full force, no mass division
        impulse = compute_impulse(pawn, args, mag, scale)
        if impulse is not None:
            PENDING_IMPULSE[pid] = (*impulse, hit_location, bone_name, False)
            multiplayer.broadcast_impulse(pawn, *impulse, hit_location, bone_name, False)
        pawn.Stagger(None)

    # Mark how much force we've applied in this accumulation window
    prev = RECENT_DAMAGE.get(uid)
    if prev is not None:
        RECENT_DAMAGE[uid] = (prev[0], prev[1], prev[2], prev[3], prev[4], acc_mag)

    IMPULSE_HANDLED.add(uid)


@hook("WillowGame.WillowAIPawn:HandleMomentum", Type.PRE)
def on_knockback(
    obj: unreal.UObject,
    args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
):
    """Replace knockback animations with ragdoll."""
    pawn = obj

    # Client guard: if already staggered, block to prevent double impulse.
    # Otherwise let native flinch/stagger play (host's Stagger() replicates and overrides).
    if multiplayer.is_client():
        try:
            if pawn.StaggerState.bIsStaggered:
                return Block
        except Exception:
            pass
        return

    if not can_ragdoll(pawn):
        return

    pid = id(pawn)
    uid = _pawn_uid(pawn)

    damage_type = args.DamageType
    dmg_name = damage_type.Name if damage_type is not None else ""

    # If TakeDamage already handled impulse for this pawn, block vanilla knockback.
    if uid in IMPULSE_HANDLED:
        return Block

    # HandleMomentum receives already mass-divided momentum
    momentum = args.Momentum
    mag = sqrt(momentum.X**2 + momentum.Y**2 + momentum.Z**2)
    if mag <= 0:
        return

    scale = impulse_scale.value / 100.0

    if pawn.StaggerState.bIsStaggered:
        # Already ragdolled by an external source (e.g. singularity) - apply impulse
        hit_scale = scale * get_ragdoll_hit_scale(dmg_name)
        impulse = compute_impulse_from_momentum(momentum, mag, hit_scale, dmg_name)
        if impulse is not None:
            state = int(pawn.StaggerState.State)
            if state in (1, 2, 3):
                # Recovering - re-stagger
                PENDING_IMPULSE[pid] = (*impulse, args.HitLocation, "", False)
                pawn.Stagger(None)
                pawn.StaggerState.StaggerEndRagdollTime = (
                    ENGINE.GetCurrentWorldInfo().TimeSeconds + recovery_time.value / 10.0
                )
                leftover = PENDING_IMPULSE.pop(pid, None)
                if leftover is not None:
                    apply_impulse(pawn, *leftover)
                multiplayer.broadcast_impulse(pawn, *impulse, args.HitLocation, "", False)
            else:
                # State 0 - push the ragdoll
                apply_impulse(pawn, *impulse, args.HitLocation, "", False)
                multiplayer.broadcast_impulse(pawn, *impulse, args.HitLocation, "", False)
        return Block

    # Not staggered - replace knockback animation with ragdoll
    # Skip bullets: their stagger should come from TakeDamage
    # (with accumulation and close-range fix)
    if dmg_name in BULLET_DMG_NAMES:
        return

    # HandleMomentum momentum is already mass-divided, so mag IS the estimated velocity
    source_mult = get_source_scale(dmg_name)
    estimated_vel = mag * scale * source_mult
    if estimated_vel < min_ragdoll_velocity.value:
        return

    impulse = compute_impulse_from_momentum(momentum, mag, scale, dmg_name)
    if impulse is not None:
        PENDING_IMPULSE[pid] = (*impulse, args.HitLocation, "", False)
        multiplayer.broadcast_impulse(pawn, *impulse, args.HitLocation, "", False)
    pawn.Stagger(None)
    return Block


@hook("WillowGame.WillowAIPawn:SetupRagdollPhysics", Type.POST)
def on_ragdoll_ready(
    obj: unreal.UObject,
    args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """Apply physics settings to all staggers and deferred impulse if pending."""
    pawn = obj

    pawn.StaggerState.StaggerEndRagdollTime = (
        ENGINE.GetCurrentWorldInfo().TimeSeconds + recovery_time.value / 10.0
    )
    phys_mat = pawn.Mesh.PhysMaterialOverride
    if phys_mat is not None:
        phys_mat.Friction = ragdoll_friction.value / 10.0
        phys_mat.Restitution = ragdoll_bounciness.value / 10.0

    # Disable vanilla failsafes
    bc = pawn.BodyClass
    if bc is not None and bc.StaggerSettings is not None:
        bc.StaggerSettings.StaggeredRestGiveUpTime = 0.0
        bc.StaggerSettings.UnstaggerGiveUpTime = 0.0
        bc.StaggerSettings.StaggeredRestVelocity = float(rest_velocity.value)

    # Enable collision notifications for impact damage (host-only)
    mesh = pawn.Mesh
    if not multiplayer.is_client() and mesh is not None:
        mesh.ScriptRigidBodyCollisionThresholdCompiled = 10
        mesh.SetNotifyRigidBodyCollision(True)

    # Bake corrected masses into ragdoll bones so AddImpulse works
    # without runtime correction. Must cache original physics mass FIRST
    # (get_true_physics_mass) before modifying InstanceMassScale.
    if mesh is not None:
        _bake_bone_masses(pawn, mesh)

    # Track ragdolled pawns for singularity gravity well tick
    uid = _pawn_uid(pawn)
    try:
        pawn_mass = pawn.Mass if pawn.Mass > 0 else 100.0
    except Exception:
        pawn_mass = 100.0
    RAGDOLLED_PAWNS[uid] = (WeakPointer(pawn), pawn_mass)
    log(f"[IS] ragdoll tracked: {uid} (total: {len(RAGDOLLED_PAWNS)})")

    if multiplayer.is_client():
        # Client: drain queued impulse from host broadcast
        multiplayer.drain_client_pending(pawn)
        return

    stored = PENDING_IMPULSE.pop(id(pawn), None)
    if stored is not None:
        apply_impulse(pawn, *stored)
        # Queue velocity read for next tick (PhysX hasn't simulated yet this frame)
        if log_to_console.value:
            force = sqrt(stored[0] ** 2 + stored[1] ** 2 + stored[2] ** 2)
            mass = get_desired_mass(pawn)
            try:
                ename = pawn.AIClass.Name if pawn.AIClass is not None else "?"
            except Exception:
                ename = "?"
            bone = stored[4] if len(stored) > 4 else ""
            _PENDING_LAUNCH_LOG.append((WeakPointer(pawn), ename, force, mass, bone))

    # Disable ragdoll-to-ragdoll collisions — must run AFTER all other physics setup
    # (bake_bone_masses, apply_impulse) to avoid being overwritten.
    # Changes RBChannel from Pawn(2) to GameplayPhysics(5) so ragdolls don't share
    # a channel. Still collides with Default(0/world) and Vehicle(3).
    if disable_ragdoll_collision.value and mesh is not None:
        mesh.SetRBChannel(5)  # RBCC_GameplayPhysics — not used by anything else
        mesh.SetRBCollidesWithChannel(0, True)  # world
        mesh.SetRBCollidesWithChannel(2, False)  # no pawn-to-pawn
        mesh.SetRBCollidesWithChannel(3, True)  # vehicles
        mesh.SetRBCollidesWithChannel(5, False)  # no self-channel (ragdoll-to-ragdoll)
        log(f"[IS] disabled ragdoll collision: {uid} -> RBChannel=5")


@hook("WillowGame.WillowStaggerAnimNodeBlend:PlayStaggerAnimation", Type.POST)
def on_play_recovery(
    obj: unreal.UObject,
    args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """Scale recovery animation speed by enemy HP %."""
    # Navigate to pawn: AnimNode.SkelComponent -> mesh.Outer
    pawn = None
    try:
        mesh = obj.SkelComponent
        if mesh is not None:
            pawn = mesh.Outer
    except Exception:
        pass
    if pawn is None:
        try:
            outer = obj.Outer
            if outer is not None and hasattr(outer, "GetHealth"):
                pawn = outer
            elif outer is not None:
                outer2 = outer.Outer
                if outer2 is not None and hasattr(outer2, "GetHealth"):
                    pawn = outer2
        except Exception:
            pass
    if pawn is None:
        return

    # Pawn is recovering from ragdoll — remove from gravity well tracking
    RAGDOLLED_PAWNS.pop(_pawn_uid(pawn), None)

    hp = pawn.GetHealth()
    max_hp = pawn.GetMaxHealth()
    if max_hp <= 0:
        return
    hp_pct = max(0.0, min(1.0, hp / max_hp))
    rate = 1.0 + 2.0 * hp_pct

    try:
        children = obj.Children
        if children is not None:
            for i in range(len(children)):
                anim_node = children[i].Anim
                if anim_node is not None:
                    try:
                        anim_node.Rate = rate
                    except Exception:
                        pass
    except Exception:
        pass


def _scan_standing_enemies_near_wells(current_time: float, all_pawns: list | None = None) -> None:
    """Find standing enemies that wandered into an active gravity well and stagger them in."""
    _t0 = _perf_start()
    catch_r = float(singularity.singularity_catch_radius.value)
    catch_r_sq = catch_r * catch_r
    well_dur = singularity.singularity_well_duration.value / 10.0
    ragdoll_dur = max(recovery_time.value / 10.0, well_dur)

    # Collect all UIDs already tracked by any well or already ragdolled
    already_known: set[str] = set(RAGDOLLED_PAWNS.keys())
    for well in singularity.ACTIVE_WELLS:
        already_known.update(well[4])

    if all_pawns is None:
        all_pawns = _iter_ai_pawns()
        if not all_pawns:
            _perf_end("well_scan", _t0)
            return

    for pawn in all_pawns:
        try:
            # Skip already-known pawns early (cheap set lookup before engine calls)
            uid = _pawn_uid(pawn)
            if not uid or uid in already_known:
                continue
            if pawn.bTearOff or pawn.bHardAttach:
                continue
            if pawn.HealthPool is None or pawn.HealthPool.Data is None:
                continue
            if pawn.GetHealth() <= 0:
                continue
            if not can_ragdoll(pawn):
                continue
            # Check if already staggered
            try:
                if pawn.StaggerState.bIsStaggered:
                    continue
            except Exception:
                continue

            px, py, pz = pawn.Location.X, pawn.Location.Y, pawn.Location.Z
        except Exception:
            continue

        # Check against all active wells
        cap = max_well_ragdolls.value
        for well in singularity.ACTIVE_WELLS:
            if cap > 0 and len(well[4]) >= cap:
                continue
            wx, wy, wz = well[0], well[1], well[2]
            dx, dy, dz = px - wx, py - wy, pz - wz
            dist_sq = dx * dx + dy * dy + dz * dz
            if dist_sq > catch_r_sq:
                continue

            # This standing enemy is inside the well — pull them in!
            dist = sqrt(dist_sq) if dist_sq > 0 else 1.0
            try:
                ename = pawn.AIClass.Name if pawn.AIClass is not None else "?"
            except Exception:
                ename = "?"
            log(f"[IS-SING] well capture: {ename} uid={uid} dist={dist:.0f}")

            # Small pull impulse toward well center
            pull_force = 2000.0
            dir_x, dir_y, dir_z = -dx / dist, -dy / dist, -dz / dist
            ix = dir_x * pull_force
            iy = dir_y * pull_force
            iz = dir_z * pull_force

            pid = id(pawn)
            PENDING_IMPULSE[pid] = (ix, iy, iz, pawn.Location, "", False, True)
            pawn.Stagger(None)
            pawn.StaggerState.StaggerEndRagdollTime = current_time + ragdoll_dur
            well[4].add(uid)
            already_known.add(uid)
            break  # Only capture by one well
    _perf_end("well_scan", _t0)


@hook("WillowGame.WillowPlayerController:PlayerTick", Type.PRE)
def on_player_tick(
    obj: unreal.UObject,
    args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """Per-frame tick: singularity gravity wells, bowling, cleanup."""
    _t0 = _perf_start()

    # Process deferred launch velocity logging (PhysX needs one tick to simulate)
    if _PENDING_LAUNCH_LOG:
        for wp, ename, force, mass, _bone in _PENDING_LAUNCH_LOG:
            pawn = wp()
            if pawn is None:
                continue
            actual_vel = 0.0
            try:
                root_body = pawn.Mesh.GetRootBodyInstance()
                if root_body is not None:
                    rb_vel = root_body.GetUnrealWorldVelocity()
                    actual_vel = sqrt(rb_vel.X**2 + rb_vel.Y**2 + rb_vel.Z**2)
            except Exception:
                pass
            est_vel = force / mass if mass > 0 else 0
            log(
                f"launch: {ename} force={force:.0f} mass={mass:.0f} "
                f"est_vel={est_vel:.0f} actual_vel={actual_vel:.0f}"
            )
        _PENDING_LAUNCH_LOG.clear()

    # Dump profiler stats periodically
    _perf_dump()

    # Fetch current_time early — used by singularity wells
    need_time = singularity.ACTIVE_WELLS
    if not need_time:
        _perf_end("PlayerTick", _t0)
        return
    try:
        current_time = ENGINE.GetCurrentWorldInfo().TimeSeconds
    except Exception:
        _perf_end("PlayerTick", _t0)
        return

    # Singularity gravity wells — continuous pull
    if singularity.ACTIVE_WELLS:
        _tw0 = _perf_start()
        singularity.tick_wells(
            current_time,
            RAGDOLLED_PAWNS,
            log,
            max_per_well=max_well_ragdolls.value,
        )
        _perf_end("tick_wells", _tw0)

    # PawnList walk for well_scan (standing enemy capture).
    global _well_scan_time
    well_scan_due = (
        singularity.ACTIVE_WELLS
        and well_scan_enabled.value
        and current_time - _well_scan_time >= 0.25
    )
    if well_scan_due:
        _well_scan_time = current_time
        _scan_standing_enemies_near_wells(current_time, all_pawns=_iter_ai_pawns())

    # Clean up stale bowling hit pairs periodically
    if RAGDOLLED_PAWNS and bowling.bowling_enabled.value:
        bowling.cleanup_hit_pairs(RAGDOLLED_PAWNS)

    _perf_end("PlayerTick", _t0)


def _apply_bowling_damage(target, speed: float) -> None:
    """Deal impact damage to a bowling target based on bowler speed.

    Used when ragdoll-ragdoll collisions are disabled, so RigidBodyCollision
    won't fire between bowler and target.
    """
    if impact_damage.impact_damage_scale.value <= 0:
        return
    if speed < impact_damage.min_impact_velocity.value:
        return
    try:
        max_health = target.GetMaxHealth()
    except Exception:
        return
    damage = (
        max_health
        * (speed / impact_damage.SPEED_REFERENCE)
        * (impact_damage.impact_damage_scale.value / 100.0)
    )
    if damage <= 0:
        return
    try:
        pc = ENGINE.GetCurrentWorldInfo().GetALocalPlayerController()
    except Exception:
        return
    dmg_type = _get_impact_damage_type()
    causer = pc.Pawn if pc.Pawn is not None else pc
    momentum = unrealsdk.make_struct("Vector")
    hit_info = unrealsdk.make_struct("TraceHitInfo")
    int_damage = max(1, int(damage))
    try:
        ename = str(target.AIClass.Name) if target.AIClass is not None else "?"
    except Exception:
        ename = "?"
    log(f"[IS-BOWL] impact: {ename} dmg={int_damage} hp={target.GetHealth():.0f} speed={speed:.0f}")
    with unrealsdk.hooks.prevent_hooking_direct_calls():
        target.TakeDamage(int_damage, pc, target.Location, momentum, dmg_type, hit_info, causer)


@hook("WillowGame.WillowAIPawn:Touch", Type.PRE)
def on_pawn_touch(
    obj: unreal.UObject,
    args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """Event-driven bowling: ragdolled pawn overlaps standing pawn."""
    if not bowling.bowling_enabled.value:
        return
    other = args.Other
    if other is None:
        return
    other_class = other.Class.Name
    if other_class != "WillowAIPawn":
        return
    # Only process when obj is the ragdolled bowler
    bowler_uid = _pawn_uid(obj)
    if not bowler_uid or bowler_uid not in RAGDOLLED_PAWNS:
        return
    ragdoll_col_off = disable_ragdoll_collision.value
    bowling.handle_touch(
        obj,
        bowler_uid,
        other,
        RAGDOLLED_PAWNS,
        log,
        can_ragdoll,
        _pawn_uid,
        PENDING_IMPULSE,
        get_target_mass_fn=get_desired_mass,
        apply_damage_fn=_apply_bowling_damage if ragdoll_col_off else None,
        ragdoll_collision_disabled=ragdoll_col_off,
    )


@hook("WillowGame.WillowPawn:RigidBodyCollision", Type.PRE)
def on_ragdoll_collision(
    obj: unreal.UObject,
    args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """Deal damage to ragdolled enemies on high-speed surface impacts."""
    _t0 = _perf_start()
    pawn = obj
    # Impact damage is host-only - TakeDamage replicates via engine natively
    if multiplayer.is_client():
        return
    if pawn.bTearOff:
        return
    try:
        if not pawn.StaggerState.bIsStaggered:
            return
    except Exception:
        return

    # Determine which bone collided and whether it's a crit zone
    bone_name = ""
    try:
        body_idx = args.ContactIndex
        mesh = pawn.Mesh
        bone_name = str(mesh.PhysicsAsset.BodySetup[body_idx].BoneName)
    except Exception:
        # Fallback: FindClosestBone from contact position
        try:
            mesh = pawn.Mesh
            contacts = args.RigidCollisionData.ContactInfos
            if contacts is not None and len(contacts) > 0:
                pos = contacts[0].ContactPosition
                bone_name = str(mesh.FindClosestBone(pos))
        except Exception:
            pass

    is_crit = impact_damage.is_crit_bone(pawn, bone_name) if bone_name else False

    # Crit bones only mode: skip non-crit bones entirely
    if impact_damage.crit_bones_only.value and not is_crit:
        return

    uid = _pawn_uid(pawn)
    max_health = pawn.GetMaxHealth()
    if max_health <= 0:
        return
    world_time = ENGINE.GetCurrentWorldInfo().TimeSeconds
    damage = impact_damage.on_collision(
        uid,
        args.RigidCollisionData,
        world_time,
        max_health,
        is_crit,
    )
    if damage is None or damage <= 0:
        return

    # Apply impact damage directly from the collision callback.
    # Previously queued to PlayerTick, but the actual hang was caused by
    # WillowDmgSource_Bullet — plain DamageType works fine here.
    try:
        pc = ENGINE.GetCurrentWorldInfo().GetALocalPlayerController()
    except Exception:
        _perf_end("RigidBodyCollision", _t0)
        return
    dmg_type = _get_impact_damage_type()
    causer = pc.Pawn if pc.Pawn is not None else pc
    momentum = unrealsdk.make_struct("Vector")
    hit_info = unrealsdk.make_struct("TraceHitInfo")
    if bone_name:
        hit_info.BoneName = bone_name
    int_damage = max(1, int(damage))
    try:
        ename = str(pawn.AIClass.Name) if pawn.AIClass is not None else "?"
    except Exception:
        ename = "?"
    log(f"[IS] impact: {ename} dmg={int_damage} hp={pawn.GetHealth():.0f} bone={bone_name}")
    with unrealsdk.hooks.prevent_hooking_direct_calls():
        pawn.TakeDamage(int_damage, pc, pawn.Location, momentum, dmg_type, hit_info, causer)
    _perf_end("RigidBodyCollision", _t0)


@hook("WillowGame.WillowPawn:PlayDying", Type.PRE)
def on_play_dying_pre(
    obj: unreal.UObject,
    args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
):
    """Override PlayDying for EXP Loaders dying while ragdolled.

    EXP Loaders' PlayDying triggers OnPlayDeathPizazz (self-destruct explosion)
    then PlayTechDeath - a native final C++ function that hangs when the pawn is
    ragdolled.  native final functions can't be hooked, and OnPlayDeathPizazz
    hangs when called from Python.  So we skip both entirely: just set up the
    ragdoll death state and let the body despawn.  Other enemy types use the
    normal native PlayDying path.
    """
    pawn = obj
    # Client: don't override EXP Loader death - host handles it
    if multiplayer.is_client():
        return
    try:
        if not pawn.StaggerState.bIsStaggered:
            return
    except Exception:
        return

    try:
        name = pawn.AIClass.Name if pawn.AIClass is not None else "?"
    except Exception:
        name = "?"

    # Override enemies with special death sequences that hang when ragdolled.
    # EXP Loaders: PlayTechDeath (self-destruct) hangs in ragdoll state.
    # BoomBoom: Kismet death script hangs if killed before vehicle entrance.
    _DEATH_OVERRIDE_CLASSES = {"CharClass_LoaderEXP", "CharClass_BoomBoom"}
    if name not in _DEATH_OVERRIDE_CLASSES:
        return

    uid = _pawn_uid(pawn)
    log(f"[IS] PlayDying PRE: ragdoll death override {name}")

    # Clean up tracking state (POST hook won't fire since we Block)
    impact_damage.clear_pawn(uid)

    # --- Minimal PlayDying replacement ---
    # Death flags - prevent re-entry via HandleHealthDepleted
    pawn.bPlayedDeath = True
    pawn.bTearOff = True

    # TermRagdoll restores animation state so InitRagdoll can re-enter ragdoll
    # cleanly for the death pose.  Without this, InitRagdoll may fail or
    # produce broken physics state.
    try:
        pawn.TermRagdoll()
    except Exception as e:
        log(f"[IS] PlayDying PRE: {name} TermRagdoll error: {e}")

    # Match the RB channel setup that native PlayDying does before
    # OnPlayDeathPizazz (channel 2 = dead body, collides with world + dead).
    mesh = pawn.Mesh
    if mesh is not None:
        mesh.SetRBChannel(2)
        mesh.SetRBCollidesWithChannel(0, True)
        mesh.SetRBCollidesWithChannel(2, True)

    # Skip OnPlayDeathPizazz (EXP Loader self-destruct, etc.) - calling it from
    # Python is unreliable and can hang the engine.  The trade-off: enemies that
    # die while ragdolled won't fire their death effects (explosion, etc.).

    # Stop dialog and sounds
    try:
        pawn.DialogComponent.StopTalking()
    except Exception:
        pass
    try:
        pawn.StopSounds()
    except Exception:
        pass

    # Re-enter ragdoll for death.  TermRagdoll restored animation state so
    # OnPlayDeathPizazz could complete; now InitRagdoll puts the pawn back
    # into ragdoll for the death physics.  This replicates the ragdoll death
    # path at WillowPawn.uc:2472-2531, skipping PlayTechDeath entirely.
    if mesh is not None:
        mesh.bPauseAnims = True

    if mesh is None or not pawn.InitRagdoll():
        # InitRagdoll failed - destroy pawn as vanilla does for bDestroyOnDeath
        try:
            pawn.Destroy()
        except Exception:
            pass
        log(f"[IS] PlayDying PRE: {name} destroyed (InitRagdoll failed)")
        return Block

    pawn.bUnderFullRagdollControl = True
    mesh.SetRBChannel(16)
    mesh.SetRBCollidesWithChannel(16, True)
    mesh.SetRBCollidesWithChannel(0, True)
    pawn.CollisionComponent = mesh
    pawn.CollisionComponent.SetBlockRigidBody(True)

    try:
        pawn.GotoState("Dying")
    except Exception as e:
        log(f"[IS] GotoState error: {e}")

    # Disable actor blocking so the player can walk through the corpse.
    # RB channels still handle world collision (floor).
    try:
        pawn.SetCollision(True, False, True)
    except Exception:
        pass

    # Set body cleanup timer - native PlayDying sets LifeSpan for body removal.
    try:
        pawn.LifeSpan = 2.0
    except Exception:
        pass

    # Apply stored death impulse (normally done in POST hook)
    pid = id(pawn)
    stored = DEATH_IMPULSE.pop(pid, None)
    if stored is not None:
        apply_impulse(pawn, *stored)

    log(f"[IS] PlayDying PRE: {name} done")
    return Block


@hook("WillowGame.WillowPawn:PlayDying", Type.POST)
def on_play_dying(
    obj: unreal.UObject,
    args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    pawn = obj
    pid = id(pawn)
    uid = _pawn_uid(pawn)
    try:
        dn = str(pawn.AIClass.Name) if pawn.AIClass is not None else "?"
    except Exception:
        dn = "?"
    log(f"[IS] PlayDying: {dn} pid={pid}")

    # Client: drain queued death impulse from host broadcast
    if multiplayer.is_client():
        multiplayer.drain_client_death(pawn)
        return

    impact_damage.clear_pawn(uid)

    stored = DEATH_IMPULSE.pop(pid, None)
    if stored is None:
        return

    mesh = pawn.Mesh
    if mesh is None:
        return

    # Force ragdoll if game chose a non-ragdoll death (tech death, animation)
    if not pawn.bUnderFullRagdollControl:
        if pawn.CurrentDeathAnimation is not None:
            pawn.CurrentDeathAnimation = None
        mesh.bPauseAnims = True
        if not pawn.InitRagdoll():
            return
        pawn.bUnderFullRagdollControl = True
        mesh.SetRBChannel(16)
        mesh.SetRBCollidesWithChannel(16, True)
        mesh.SetRBCollidesWithChannel(0, True)
        pawn.CollisionComponent = mesh
        pawn.CollisionComponent.SetBlockRigidBody(True)

    apply_impulse(pawn, *stored)
    # Death impulse was already broadcast from on_take_damage when DEATH_IMPULSE was stored


force_per_source = NestedOption(
    "Force Per Source",
    children=[
        bullet_force_mult,
        grenades.grenade_force_mult,
        rockets.rocket_force_mult,
        melee_force_mult,
        skill_force_mult,
        vehicle_force_mult,
        shield_force_mult,
        other_force_mult,
    ],
    description="Per-weapon-type force multipliers.",
)

singularity_menu = NestedOption(
    "Singularity",
    children=[
        *singularity.get_options(),
    ],
    description="Singularity grenade gravity well behavior — force, pull, radius, rotation.",
)

impact_damage_menu = NestedOption(
    "Impact Damage",
    children=[
        impact_damage.impact_damage_scale,
        impact_damage.min_impact_velocity,
        impact_damage.impact_cooldown,
        impact_damage.launch_grace,
        impact_damage.launch_speed_threshold,
        impact_damage.crit_bones_only,
        impact_damage.non_crit_min_velocity,
        impact_damage.non_crit_damage_scale,
        impact_damage.max_impacts_per_frame,
    ],
    description="Collision damage when ragdolled enemies hit surfaces.",
)

ragdoll_physics = NestedOption(
    "Ragdoll Physics",
    children=[
        ragdoll_friction,
        ragdoll_bounciness,
        terminal_velocity,
    ],
    description="Physical behavior of ragdolled enemies.",
)

object_physics_menu = NestedOption(
    "Object Physics",
    children=object_physics.get_options(),
    description="Explosions and singularity wells affect loot pickups and physics props.",
)

bowling_menu = NestedOption(
    "Ragdoll Bowling",
    children=bowling.get_options(),
    description="Experimental: fast ragdolls knock over standing enemies on proximity.",
)

performance_menu = NestedOption(
    "Performance",
    children=[
        disable_ragdoll_collision,
        max_well_ragdolls,
        well_scan_enabled,
        log_perf,
    ],
    description=(
        "Options to reduce PhysX load and profile performance. "
        "See also: Singularity (pull/radius tuning), Ragdoll Bowling (proximity knockdowns)."
    ),
)

advanced_menu = NestedOption(
    "Advanced",
    children=[
        recovery_time,
        rest_velocity,
        min_ragdoll_velocity,
        damage_severity_threshold,
        low_health_threshold,
        accumulation_window,
        max_bone_force,
        min_explosion_force,
        log_to_console,
    ],
    description="Fine-tuning thresholds. Change this to fine tune (not always intuitive)",
)


def _collect_all_sliders() -> list[SliderOption]:
    """Collect all SliderOption instances from the nested menu structure."""
    result = []
    for opt in (
        force_per_source,
        singularity_menu,
        impact_damage_menu,
        ragdoll_physics,
        object_physics_menu,
        bowling_menu,
        performance_menu,
        advanced_menu,
    ):
        for child in opt.children:
            if isinstance(child, SliderOption):
                result.append(child)
    # Top-level sliders
    for opt in (impulse_scale,):
        if isinstance(opt, SliderOption):
            result.append(opt)
    return result


@ButtonOption("Restore All Defaults")
def restore_defaults(_btn: ButtonOption) -> None:
    """Reset all options to their default values."""
    for opt in _collect_all_sliders():
        opt.value = opt.default_value
    mod.save_settings()


mod = build_mod(
    options=[
        impulse_scale,
        force_per_source,
        singularity_menu,
        impact_damage_menu,
        ragdoll_physics,
        object_physics_menu,
        bowling_menu,
        performance_menu,
        stagger_filters_menu,
        advanced_menu,
        restore_defaults,
    ],
)

# Register network functions for multiplayer support
multiplayer._apply_impulse_fn = apply_impulse
multiplayer._log_fn = log
multiplayer.register_network_functions(mod)


# --- Settings sync on option change for multiplayer ---


def _on_option_change(opt, new_value) -> None:
    """Broadcast settings when host changes any option.

    Note: on_change fires BEFORE the value is set on the option.
    We set it here so broadcast_settings picks up the new value.
    The framework will set it again afterward (harmless, same value).
    """
    opt.value = new_value
    if not multiplayer.is_client() and multiplayer.is_multiplayer():
        multiplayer.broadcast_settings()


def _install_option_change_hooks() -> None:
    """Install on_change callbacks on all syncable options."""

    def _hook_options(options):
        for opt in options:
            if isinstance(opt, NestedOption):
                _hook_options(opt.children)
            elif isinstance(opt, (SliderOption, BoolOption)):
                if opt.identifier not in multiplayer._LOCAL_OPTION_IDS:
                    opt.on_change = _on_option_change

    _hook_options(mod.options)


_install_option_change_hooks()
