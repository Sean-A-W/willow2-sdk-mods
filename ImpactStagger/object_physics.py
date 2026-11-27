from __future__ import annotations

import unrealsdk
from mods_base import BoolOption, SliderOption
from unrealsdk.unreal import WeakPointer

from . import singularity


# --- Sliders ---

object_physics_enabled = BoolOption(
    "Object Physics Enabled",
    False,
    description=(
        "[EXPERIMENTAL] Explosions and singularity wells affect loot "
        "pickups and physics props. May cause crashes "
        "-- enable with logging to help debug."
    ),
)

loot_scatter_force = SliderOption(
    "Loot Scatter Force %",
    5,
    0,
    100,
    1,
    True,
    description="Force multiplier for scattering loot pickups. 0 = disabled. Default: 5",
)

prop_scatter_force = SliderOption(
    "Prop Scatter Force %",
    5,
    0,
    100,
    1,
    True,
    description="Force multiplier for pushing physics props (KActors). 0 = disabled. Default: 5",
)

scatter_radius = SliderOption(
    "Scatter Radius",
    500,
    100,
    2000,
    50,
    True,
    description=("Radius for explosion scatter effects on loot and props. Default: 500"),
)


# --- Deduplication for one-shot scatter ---

_RECENT_SCATTERS: list[tuple[float, float, float, float]] = []
_SCATTER_DEDUP_DIST_SQ = 50.0**2
_SCATTER_DEDUP_TIME = 0.15

# Pending scatters queued from hooks, processed in PlayerTick.
_PENDING_SCATTERS: list[tuple[float, float, float, float, float]] = []


def queue_scatter(
    ox: float,
    oy: float,
    oz: float,
    strength: float,
    current_time: float,
) -> None:
    """Queue a scatter for deferred processing in PlayerTick.

    Called from hooks — does NOT touch any engine objects.
    """
    if not object_physics_enabled.value:
        return

    for sx, sy, sz, st in _RECENT_SCATTERS:
        if current_time - st > _SCATTER_DEDUP_TIME:
            continue
        dx, dy, dz = ox - sx, oy - sy, oz - sz
        if dx * dx + dy * dy + dz * dz < _SCATTER_DEDUP_DIST_SQ:
            return

    for px, py, pz, _ps, pt in _PENDING_SCATTERS:
        if current_time - pt > _SCATTER_DEDUP_TIME:
            continue
        dx, dy, dz = ox - px, oy - py, oz - pz
        if dx * dx + dy * dy + dz * dz < _SCATTER_DEDUP_DIST_SQ:
            return

    _PENDING_SCATTERS.append((ox, oy, oz, strength, current_time))


def process_pending_scatters(log_fn=None) -> None:
    """Process all queued scatters in a single batched pass.

    Called from PlayerTick. Breadcrumb logging before each native call
    so the last log line before a crash identifies the culprit.
    """
    if not _PENDING_SCATTERS:
        return
    if not object_physics_enabled.value:
        _PENDING_SCATTERS.clear()
        return

    to_process = list(_PENDING_SCATTERS)
    _PENDING_SCATTERS.clear()

    # Dedup the batch
    unique: list[tuple[float, float, float, float, float]] = []
    for ox, oy, oz, strength, t in to_process:
        is_dup = False
        for sx, sy, sz, st in _RECENT_SCATTERS:
            if t - st > _SCATTER_DEDUP_TIME:
                continue
            dx, dy, dz = ox - sx, oy - sy, oz - sz
            if dx * dx + dy * dy + dz * dz < _SCATTER_DEDUP_DIST_SQ:
                is_dup = True
                break
        if not is_dup:
            for ux, uy, uz, _us, _ut in unique:
                dx, dy, dz = ox - ux, oy - uy, oz - uz
                if dx * dx + dy * dy + dz * dz < _SCATTER_DEDUP_DIST_SQ:
                    is_dup = True
                    break
        if not is_dup:
            unique.append((ox, oy, oz, strength, t))
            _RECENT_SCATTERS.append((ox, oy, oz, t))

    if not unique:
        return

    # Prune old entries
    now = unique[-1][4]
    i = 0
    while i < len(_RECENT_SCATTERS):
        if now - _RECENT_SCATTERS[i][3] > _SCATTER_DEDUP_TIME:
            _RECENT_SCATTERS.pop(i)
        else:
            i += 1

    radius = float(scatter_radius.value)
    radius_sq = radius * radius
    origin = unrealsdk.make_struct("Vector")

    if log_fn:
        log_fn(f"[IS-OBJ] processing {len(unique)} scatter origin(s)")

    # --- Single find_all pass for loot pickups ---
    loot_scale = loot_scatter_force.value / 100.0
    if loot_scale > 0:
        if log_fn:
            log_fn("[IS-OBJ] BREADCRUMB: about to find_all WillowPickup")
        pickups = list(unrealsdk.find_all("WillowPickup"))
        if log_fn:
            log_fn(f"[IS-OBJ] BREADCRUMB: found {len(pickups)} pickups")
        count = 0
        for idx, pickup in enumerate(pickups):
            if log_fn:
                log_fn(f"[IS-OBJ] BREADCRUMB: pickup {idx} reading Location")
            try:
                loc = pickup.Location
                px, py, pz = loc.X, loc.Y, loc.Z
            except Exception:
                continue
            best_str = 0.0
            best_ox, best_oy, best_oz = 0.0, 0.0, 0.0
            for ox, oy, oz, strength, _ in unique:
                dx, dy, dz = px - ox, py - oy, pz - oz
                dist_sq = dx * dx + dy * dy + dz * dz
                if dist_sq <= radius_sq and strength > best_str:
                    best_str = strength
                    best_ox, best_oy, best_oz = ox, oy, oz
            if best_str <= 0:
                continue
            if log_fn:
                log_fn(f"[IS-OBJ] BREADCRUMB: pickup {idx} ConvertFixedToRigidBody")
            try:
                pickup.ConvertFixedToRigidBody()
            except Exception as e:
                if log_fn:
                    log_fn(f"[IS-OBJ] ConvertFixedToRigidBody failed: {e}")
                continue
            if log_fn:
                log_fn(f"[IS-OBJ] BREADCRUMB: pickup {idx} getting CollisionComponent")
            mesh = None
            try:
                mesh = pickup.CollisionComponent
            except Exception:
                pass
            if mesh is None:
                if log_fn:
                    log_fn(f"[IS-OBJ] BREADCRUMB: pickup {idx} getting Mesh")
                try:
                    mesh = pickup.Mesh
                except Exception:
                    pass
            if mesh is None:
                continue
            if log_fn:
                log_fn(
                    f"[IS-OBJ] BREADCRUMB: pickup {idx} "
                    f"AddRadialImpulse str={best_str * loot_scale:.0f}"
                )
            origin.X, origin.Y, origin.Z = best_ox, best_oy, best_oz
            try:
                mesh.AddRadialImpulse(origin, radius, best_str * loot_scale, 1)
                count += 1
            except Exception as e:
                if log_fn:
                    log_fn(f"[IS-OBJ] AddRadialImpulse failed: {e}")
        if log_fn:
            log_fn(f"[IS-OBJ] scattered {count}/{len(pickups)} pickups")

    # --- Single find_all pass for KActors ---
    prop_scale = prop_scatter_force.value / 100.0
    if prop_scale > 0:
        if log_fn:
            log_fn("[IS-OBJ] BREADCRUMB: about to find_all KActor")
        actors = list(unrealsdk.find_all("KActor"))
        if log_fn:
            log_fn(f"[IS-OBJ] BREADCRUMB: found {len(actors)} KActors")
        count = 0
        for idx, actor in enumerate(actors):
            if log_fn:
                log_fn(f"[IS-OBJ] BREADCRUMB: KActor {idx} reading Location")
            try:
                loc = actor.Location
                ax, ay, az = loc.X, loc.Y, loc.Z
            except Exception:
                continue
            best_str = 0.0
            best_ox, best_oy, best_oz = 0.0, 0.0, 0.0
            for ox, oy, oz, strength, _ in unique:
                dx, dy, dz = ax - ox, ay - oy, az - oz
                dist_sq = dx * dx + dy * dy + dz * dz
                if dist_sq <= radius_sq and strength > best_str:
                    best_str = strength
                    best_ox, best_oy, best_oz = ox, oy, oz
            if best_str <= 0:
                continue
            origin.X, origin.Y, origin.Z = best_ox, best_oy, best_oz
            if log_fn:
                log_fn(f"[IS-OBJ] BREADCRUMB: KActor {idx} WakeRigidBody + AddRadialImpulse")
            try:
                smc = actor.StaticMeshComponent
                if smc is None:
                    continue
                smc.WakeRigidBody()
                smc.AddRadialImpulse(origin, radius, best_str * prop_scale, 1)
                count += 1
            except Exception as e:
                if log_fn:
                    log_fn(f"[IS-OBJ] KActor impulse failed: {e}")
        if log_fn:
            log_fn(f"[IS-OBJ] pushed {count}/{len(actors)} KActors")

    if log_fn:
        log_fn("[IS-OBJ] BREADCRUMB: scatter pass complete")


# --- Singularity pull for loot pickups ---

_WELL_PICKUPS: dict[tuple[float, float, float], dict[int, WeakPointer]] = {}
_pull_log_counter = 0


def pull_nearby_objects(wells: list, current_time: float, log_fn=None) -> None:
    """Pull loot pickups toward active singularity wells each frame."""
    global _pull_log_counter

    if not object_physics_enabled.value:
        return
    loot_scale = loot_scatter_force.value / 100.0
    if loot_scale <= 0:
        return
    if not wells:
        _WELL_PICKUPS.clear()
        return

    active_keys = set()
    for well in wells:
        active_keys.add((well[0], well[1], well[2]))
    expired_keys = [k for k in _WELL_PICKUPS if k not in active_keys]
    for k in expired_keys:
        del _WELL_PICKUPS[k]

    catch_r = float(singularity.singularity_catch_radius.value)
    catch_r_sq = catch_r * catch_r
    pull_str = float(singularity.singularity_pull_strength.value) * loot_scale
    if pull_str <= 0:
        return

    escape_r = catch_r * (singularity.singularity_escape_multiplier.value / 100.0)
    escape_r_sq = escape_r * escape_r

    origin = unrealsdk.make_struct("Vector")

    _pull_log_counter += 1
    should_log = log_fn and (_pull_log_counter % 60 == 1)

    for well in wells:
        wx, wy, wz = well[0], well[1], well[2]
        well_key = (wx, wy, wz)

        tracked = _WELL_PICKUPS.get(well_key)
        if tracked is None:
            tracked = {}
            for pickup in unrealsdk.find_all("WillowPickup"):
                try:
                    loc = pickup.Location
                    dx = loc.X - wx
                    dy = loc.Y - wy
                    dz = loc.Z - wz
                    if dx * dx + dy * dy + dz * dz <= catch_r_sq:
                        try:
                            pickup.ConvertFixedToRigidBody()
                        except Exception:
                            continue
                        tracked[id(pickup)] = WeakPointer(pickup)
                except Exception:
                    pass
            _WELL_PICKUPS[well_key] = tracked
            if tracked and log_fn:
                log_fn(
                    f"[IS-OBJ] well ({wx:.0f},{wy:.0f},{wz:.0f}): tracking {len(tracked)} pickups"
                )

        if not tracked:
            continue

        origin.X, origin.Y, origin.Z = wx, wy, wz

        to_remove = []
        for pid, wp in tracked.items():
            pickup = wp()
            if pickup is None:
                to_remove.append(pid)
                continue
            try:
                loc = pickup.Location
                dx = loc.X - wx
                dy = loc.Y - wy
                dz = loc.Z - wz
                if dx * dx + dy * dy + dz * dz > escape_r_sq:
                    to_remove.append(pid)
                    continue
            except Exception:
                to_remove.append(pid)
                continue
            mesh = None
            try:
                mesh = pickup.CollisionComponent
            except Exception:
                pass
            if mesh is None:
                try:
                    mesh = pickup.Mesh
                except Exception:
                    pass
            if mesh is None:
                to_remove.append(pid)
                continue
            try:
                mesh.AddRadialForce(origin, escape_r, -pull_str, 0)
            except Exception:
                to_remove.append(pid)

        for pid in to_remove:
            del tracked[pid]

        if should_log and tracked:
            log_fn(f"[IS-OBJ] pulling {len(tracked)} pickups toward well")


def get_options() -> list:
    """Return object physics options for build_mod."""
    return [
        object_physics_enabled,
        loot_scatter_force,
        prop_scatter_force,
        scatter_radius,
    ]
