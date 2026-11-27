from __future__ import annotations

from math import sqrt
import unrealsdk
from mods_base import SliderOption


def is_singularity(dmg_name: str) -> bool:
    """Check if a damage type name is from a singularity grenade."""
    return "Singularity" in dmg_name


def is_singularity_causer(causer) -> bool:
    """Check if a DamageCauser is a singularity grenade projectile.

    Singularity pull arrives as TakeDamage with DamageType=None and a
    WillowProjectile whose Definition.Name contains 'Singularity'
    (e.g. 'Grenade_SingularityChild').
    """
    try:
        if causer is not None:
            defn = causer.Definition
            if defn is not None and "Singularity" in defn.Name:
                return True
    except Exception:
        pass
    return False


# --- Sliders ---

singularity_momentum_scale = SliderOption(
    "Singularity Initial Force %",
    5,
    0,
    200,
    1,
    True,
    description=(
        "Force % for the initial singularity impulse that ragdolls enemies. "
        "0 = no initial force, 100 = normal. "
        "Default: 5"
    ),
)

min_singularity_force = SliderOption(
    "Min Singularity Force",
    0,
    0,
    1000,
    10,
    True,
    description=("Minimum force floor for singularity pull. 0 = no floor. Default: 0"),
)

singularity_pull_strength = SliderOption(
    "Singularity Pull Strength",
    70,
    0,
    5000,
    10,
    True,
    description=(
        "Continuous pull force applied each frame via AddRadialForce. "
        "Higher = stronger gravity well. 0 = no continuous pull. "
        "Default: 70"
    ),
)

singularity_well_duration = SliderOption(
    "Singularity Time to Detonation",
    22,
    1,
    100,
    1,
    True,
    description=(
        "How long (tenths of second) the gravity well pulls before detonation. "
        "22 = 2.2s (matches vanilla timing). "
        "Default: 22"
    ),
)

singularity_catch_radius = SliderOption(
    "Singularity Catch Radius",
    1000,
    100,
    2600,
    50,
    True,
    description=(
        "Max radius for singularity pull. Enemies beyond this are not pulled. "
        "Also used to adopt latecomers during the well's lifetime. "
        "Default: 1000"
    ),
)

singularity_escape_multiplier = SliderOption(
    "Singularity Escape Radius %",
    120,
    100,
    500,
    10,
    True,
    description=(
        "Escape radius as % of catch radius. Enemies beyond this distance break free. "
        "120 = 1.2x catch radius (1000 catch -> 1200 escape). Always >= catch radius. "
        "Default: 120"
    ),
)

singularity_rotation_strength = SliderOption(
    "Singularity Rotation %",
    100,
    0,
    1000,
    10,
    True,
    description=(
        "Tangential (swirl) force as % of pull strength. "
        "Adds a spinning motion around the well center. "
        "0 = no rotation (straight pull), 100 = equal to pull. "
        "Default: 30"
    ),
)


# Reference for physics mass equalization (kept for compatibility)
MASS_REFERENCE = 16.0


# --- Active gravity wells ---

# Each well: [x, y, z, expire_time, set_of_pawn_uids]
# Pawn UIDs are PathName strings (stable across Python wrapper GC).
ACTIVE_WELLS: list[list] = []

# Reusable Vector struct for tangential force — avoids make_struct() per pawn per frame.
_tang_force = None


def add_well(x: float, y: float, z: float, current_time: float) -> None:
    """Register a new gravity well at the given location."""
    duration = singularity_well_duration.value / 10.0
    ACTIVE_WELLS.append([x, y, z, current_time + duration, set()])


def register_pawn(uid: str, max_per_well: int = 0) -> None:
    """Register a pawn UID with the most recent gravity well."""
    if ACTIVE_WELLS:
        well = ACTIVE_WELLS[-1]
        if max_per_well > 0 and len(well[4]) >= max_per_well:
            return
        well[4].add(uid)


def register_pawn_at(uid: str, wx: float, wy: float, wz: float, max_per_well: int = 0) -> None:
    """Register a pawn UID with the well at the given location."""
    for well in ACTIVE_WELLS:
        if abs(well[0] - wx) < 1 and abs(well[1] - wy) < 1 and abs(well[2] - wz) < 1:
            if max_per_well > 0 and len(well[4]) >= max_per_well:
                return
            well[4].add(uid)
            return


_tick_log_counter = 0  # throttle per-frame logs to once per second


def tick_wells(
    current_time: float,
    ragdolled_pawns: dict,
    log_fn=None,
    max_per_well: int = 0,
) -> None:
    """Apply continuous radial pull to pawns originally caught by each well.

    Called every frame from PlayerTick. Uses AddRadialForce with negative strength
    to pull ragdoll bones toward the well center. Only affects pawns that were
    originally pulled by the singularity (tracked per-well).
    """
    global _tick_log_counter

    if not ACTIVE_WELLS:
        return

    # Remove expired wells
    expired = 0
    i = 0
    while i < len(ACTIVE_WELLS):
        if current_time >= ACTIVE_WELLS[i][3]:
            ACTIVE_WELLS.pop(i)
            expired += 1
        else:
            i += 1

    if expired and log_fn:
        log_fn(f"[IS-SING] {expired} well(s) expired, {len(ACTIVE_WELLS)} remaining")

    if not ACTIVE_WELLS or not ragdolled_pawns:
        return

    strength = float(singularity_pull_strength.value)
    if strength <= 0:
        return

    origin = unrealsdk.make_struct("Vector")

    # Throttle verbose logging to ~once per second (every 60 ticks at 60fps)
    _tick_log_counter += 1
    should_log = log_fn and (_tick_log_counter % 60 == 1)

    if should_log:
        log_fn(
            f"[IS-SING] tick: {len(ACTIVE_WELLS)} wells, "
            f"{len(ragdolled_pawns)} ragdolled, str={strength}"
        )

    # Adopt nearby ragdolled pawns not already tracked by any well
    catch_r = float(singularity_catch_radius.value)
    catch_r_sq = catch_r * catch_r
    escape_r = catch_r * (singularity_escape_multiplier.value / 100.0)
    escape_r_sq = escape_r * escape_r
    all_tracked: set[str] = set()
    for well in ACTIVE_WELLS:
        all_tracked.update(well[4])
    for uid, (wp, _mass) in list(ragdolled_pawns.items()):
        if uid in all_tracked:
            continue
        pawn = wp()
        if pawn is None:
            del ragdolled_pawns[uid]
            continue
        try:
            px = pawn.Location.X
            py = pawn.Location.Y
            pz = pawn.Location.Z
        except Exception:
            continue
        for well in ACTIVE_WELLS:
            if max_per_well > 0 and len(well[4]) >= max_per_well:
                continue
            dx, dy, dz = px - well[0], py - well[1], pz - well[2]
            if dx * dx + dy * dy + dz * dz <= catch_r_sq:
                well[4].add(uid)
                if log_fn:
                    log_fn(f"[IS-SING] adopted latecomer: {uid}")
                break

    # Slow-path checks (health, stagger, escape, recovery time) run every 15 ticks
    # to avoid expensive engine reads per pawn per frame. Forces must run every frame.
    do_slow_checks = _tick_log_counter % 15 == 0
    rotation_pct = singularity_rotation_strength.value

    dead_uids = []
    applied = 0
    for well in ACTIVE_WELLS:
        wx, wy, wz = well[0], well[1], well[2]
        well_pawns = well[4]
        if not well_pawns:
            continue
        origin.X, origin.Y, origin.Z = wx, wy, wz

        if should_log:
            log_fn(
                f"[IS-SING] well at ({wx:.0f},{wy:.0f},{wz:.0f}) "
                f"has {len(well_pawns)} registered pawns"
            )

        for uid in list(well_pawns):
            entry = ragdolled_pawns.get(uid)
            if entry is None:
                well_pawns.discard(uid)
                if should_log:
                    log_fn(f"[IS-SING] well pawn not in RAGDOLLED_PAWNS: {uid}")
                continue
            wp, cached_mass = entry
            pawn = wp()
            if pawn is None:
                del ragdolled_pawns[uid]
                well_pawns.discard(uid)
                if log_fn:
                    log_fn(f"[IS-SING] pawn GC'd: {uid}")
                continue

            # Slow-path: health, stagger state, escape radius, recovery timer (~every 0.25s)
            if do_slow_checks:
                try:
                    if pawn.bTearOff or pawn.GetHealth() <= 0:
                        dead_uids.append(uid)
                        well_pawns.discard(uid)
                        continue
                    if not pawn.StaggerState.bIsStaggered:
                        dead_uids.append(uid)
                        well_pawns.discard(uid)
                        if should_log:
                            log_fn(f"[IS-SING] pawn no longer staggered: {uid}")
                        continue
                    # Keep enemy ragdolled while in well — push recovery time forward
                    pawn.StaggerState.StaggerEndRagdollTime = current_time + 1.0
                except Exception as e:
                    dead_uids.append(uid)
                    well_pawns.discard(uid)
                    if log_fn:
                        log_fn(f"[IS-SING] pawn check failed: {uid} {e}")
                    continue

            # Fast-path: mesh + force application (every frame)
            try:
                mesh = pawn.Mesh
                if mesh is None:
                    continue

                # Scale force by pawn mass so all enemies accelerate equally (F=ma).
                pawn_mass = cached_mass if cached_mass > 0 else 100.0
                pawn_strength = strength * (pawn_mass / MASS_REFERENCE)

                # Use constant falloff — uniform pull regardless of distance.
                # Membership tracking handles who gets pulled, not distance falloff.
                mesh.AddRadialForce(origin, escape_r, -pawn_strength, 0)
                applied += 1

                # Apply tangential (swirl) force perpendicular to the radial direction
                if rotation_pct > 0:
                    px, py, pz = pawn.Location.X, pawn.Location.Y, pawn.Location.Z
                    dx, dy, dz = px - wx, py - wy, pz - wz

                    # Evict pawns that have escaped
                    # (check every frame since Location is already read)
                    if dx * dx + dy * dy + dz * dz > escape_r_sq:
                        well_pawns.discard(uid)
                        if should_log:
                            log_fn(f"[IS-SING] pawn escaped well: {uid}")
                        continue

                    dist_sq = dx * dx + dy * dy
                    if dist_sq > 1.0:  # avoid division by zero at center
                        inv_dist = 1.0 / sqrt(dist_sq)
                        # Tangential = cross(radial, up) = (-dy, dx, 0) normalized
                        tang_scale = pawn_strength * (rotation_pct / 100.0)
                        global _tang_force
                        if _tang_force is None:
                            _tang_force = unrealsdk.make_struct("Vector")
                        _tang_force.X = -dy * inv_dist * tang_scale
                        _tang_force.Y = dx * inv_dist * tang_scale
                        _tang_force.Z = 0.0
                        mesh.AddForce(_tang_force)
                elif do_slow_checks:
                    # No rotation — only check escape radius on slow ticks
                    px, py, pz = pawn.Location.X, pawn.Location.Y, pawn.Location.Z
                    dx, dy, dz = px - wx, py - wy, pz - wz
                    if dx * dx + dy * dy + dz * dz > escape_r_sq:
                        well_pawns.discard(uid)
                        if should_log:
                            log_fn(f"[IS-SING] pawn escaped well: {uid}")
                        continue
            except Exception as e:
                if log_fn:
                    log_fn(f"[IS-SING] AddRadialForce/tangential FAILED: {e}")

    if should_log and applied > 0:
        log_fn(f"[IS-SING] applied {applied} radial forces this tick")

    for uid in dead_uids:
        ragdolled_pawns.pop(uid, None)
        if log_fn:
            log_fn(f"[IS-SING] removed dead/recovered: {uid}")


def get_source_scale() -> float:
    """Return the singularity initial force multiplier from slider."""
    return singularity_momentum_scale.value / 100.0


def compute_force(force: float) -> float:
    """Apply sqrt dampening with minimum floor to singularity force."""
    dampened = sqrt(force) if force > 0 else 0.0
    floor = float(min_singularity_force.value)
    return max(dampened, floor)


def get_options() -> list:
    """Return singularity slider options for build_mod."""
    return [
        singularity_momentum_scale,
        min_singularity_force,
        singularity_pull_strength,
        singularity_well_duration,
        singularity_catch_radius,
        singularity_escape_multiplier,
        singularity_rotation_strength,
    ]
