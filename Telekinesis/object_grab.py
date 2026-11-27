"""Object grab — handles loot pickup grabbing via Telekinesis.

Uses the game's PickupList (same as LootCollector) to find lootable items
near the crosshair. Only WillowPickup objects are supported — barrels,
physics props, and other non-loot objects are excluded.

Called from Telekinesis __init__.py at dispatch points where object-specific
logic diverges from pawn-specific logic.
"""

from __future__ import annotations

import math

from mods_base import BoolOption, SliderOption
from uemath import Rotator
from unrealsdk.unreal import WeakPointer


# ─── Options ────────────────────────────────────────────────────────────────

enable_object_grab = BoolOption(
    "Enable Loot Grab",
    True,
    description="Grab loot pickups when no enemy is targeted.",
)

object_drain_mult = SliderOption(
    "Loot Drain %",
    100,
    0,
    200,
    10,
    True,
    description=(
        "Charge drain multiplier when holding loot (percentage). "
        "100 = same as enemies, 50 = half drain. Default: 100"
    ),
)

skip_mission_items = BoolOption(
    "Skip Mission Items",
    False,
    description=(
        "Skip mission items and non-pickupable objects. "
        "Disable to grab everything including mission items (may break quests)."
    ),
)


# ─── State (module globals, read by __init__.py for branching) ──────────────

target_is_object: bool = False
object_mesh = None  # CollisionComponent reference
object_type: str = ""  # always 'pickup' now


# ─── Constants ──────────────────────────────────────────────────────────────

_AIM_RADIUS = 200.0


# ─── Internal Helpers ───────────────────────────────────────────────────────


def _apply_impulse(target, ix: float, iy: float, iz: float) -> None:
    """Apply an instantaneous impulse to the pickup's collision component.

    Uses target.Velocity as a temp vector struct.
    """
    try:
        target.Velocity.X = ix
        target.Velocity.Y = iy
        target.Velocity.Z = iz
        if object_mesh is not None:
            object_mesh.AddImpulse(target.Velocity)
    except Exception:
        pass


def _is_lootable(pickup) -> bool:
    """Filter a WillowPickup — skips mission/non-pickupable items when enabled."""
    if not skip_mission_items.value:
        return True
    try:
        if pickup.bIsMissionItem:
            return False
        if not pickup.bPickupable:
            return False
        # Skip ECHOs (ZippyFrame == "None" means ECHO recorder)
        inv = pickup.Inventory
        if inv is not None and str(inv.GetZippyFrame()) == "None":
            return False
    except Exception:
        return False
    return True


# ─── Public API ─────────────────────────────────────────────────────────────


def trace_for_object(pc, player, max_dist, log):
    """Find the lootable pickup closest to the crosshair aim ray.

    Uses pc.GetWillowGlobals().PickupList — the game's global pickup
    registry — instead of scanning all object classes with find_all.
    """
    try:
        view_loc = pc.CalcViewLocation
        view_rot = pc.CalcViewRotation
        eye_x, eye_y, eye_z = view_loc.X, view_loc.Y, view_loc.Z
        fwd = Rotator(view_rot).to_vector()
        fwd_x, fwd_y, fwd_z = fwd.x, fwd.y, fwd.z
    except Exception:
        return None

    try:
        pickup_list = pc.GetWillowGlobals().PickupList
        if pickup_list is None:
            return None
    except Exception:
        return None

    best = None
    best_perp = _AIM_RADIUS + 1.0

    for i in range(len(pickup_list)):
        try:
            pickup = pickup_list[i]
            if pickup is None:
                continue

            if not _is_lootable(pickup):
                continue

            loc = pickup.Location
            dx = loc.X - eye_x
            dy = loc.Y - eye_y
            dz = loc.Z - eye_z

            # Project onto aim ray
            along = dx * fwd_x + dy * fwd_y + dz * fwd_z
            if along < 0 or along > max_dist:
                continue

            # Perpendicular distance from aim ray (how far off-crosshair)
            px = dx - fwd_x * along
            py = dy - fwd_y * along
            pz = dz - fwd_z * along
            perp = math.sqrt(px * px + py * py + pz * pz)
            if perp > _AIM_RADIUS:
                continue

            # Pick the one closest to crosshair center
            if perp < best_perp:
                best_perp = perp
                best = pickup
        except Exception:
            continue

    if best is not None:
        try:
            log(f"Pickup found: {best.PathName(best)} perp={best_perp:.0f}")
        except Exception:
            pass
    return best


def activate(pc, player, obj, options, log):
    """Set up loot grab state. Returns dict of shared state values, or None.

    The returned dict contains: target_wp, target_uid, capture_dist, charge,
    grab_bone, grab_mass, saved_stagger_settings.
    """
    global target_is_object, object_mesh, object_type

    try:
        mesh = obj.CollisionComponent
        if mesh is None:
            log("Loot grab: no CollisionComponent")
            return None
    except Exception:
        log("Loot grab: CollisionComponent access failed")
        return None

    target_is_object = True
    object_mesh = mesh
    object_type = "pickup"

    # Compute capture distance
    try:
        view_loc = pc.CalcViewLocation
        eye_x, eye_y, eye_z = view_loc.X, view_loc.Y, view_loc.Z
    except Exception:
        eye_x = player.Location.X
        eye_y = player.Location.Y
        eye_z = player.Location.Z + player.EyeHeight

    try:
        tl = obj.Location
        dx, dy, dz = tl.X - eye_x, tl.Y - eye_y, tl.Z - eye_z
        dist = math.sqrt(dx * dx + dy * dy + dz * dz)
    except Exception:
        dist = 500.0

    override = options["hold_distance_override"]
    if override > 0:
        dist = float(override)
    capture_dist = max(float(options["min_hold_distance"]), dist)

    try:
        target_uid = obj.PathName(obj)
    except Exception:
        target_uid = ""

    charge = options["activation_charge"] - (options["activation_cost"] / 100.0)
    if charge <= 0.0:
        charge = 0.01

    log(f"Loot activate: dist={capture_dist:.0f}")

    return {
        "target_wp": WeakPointer(obj),
        "target_uid": target_uid,
        "capture_dist": capture_dist,
        "charge": charge,
        "grab_bone": "",
        "grab_mass": 50.0,
        "saved_stagger_settings": None,
    }


def grab_moment(target, log) -> tuple[float, int, float]:
    """Pickup grab fires — initializes pickup physics once.

    Enables RB physics and zeros gravity so the pickup floats.
    Returns (mass, body_count, duration_mult).
    Caller handles FX (hand orb, target light, hold sound).
    """
    try:
        target.AdjustPickupPhysicsAndCollisionForBeingDropped()
        target.InitializePickupForRBPhysics()
        log("Loot: pickup physics initialized")
    except Exception as e:
        log(f"Loot: pickup init failed: {e}")

    # Zero gravity so the pickup floats — spring forces handle positioning
    # from here on, no more per-tick physics re-init.
    set_gravity(0.0)

    # Zero any existing velocity so pickup doesn't drift on grab
    try:
        target.Velocity.X = 0.0
        target.Velocity.Y = 0.0
        target.Velocity.Z = 0.0
        if object_mesh is not None:
            object_mesh.SetRBLinearVelocity(target.Velocity)
    except Exception:
        pass

    return (50.0, 1, 1.0)


def validate(target, log) -> bool:
    """Check if pickup still exists. Returns False if destroyed/collected."""
    try:
        if target.bDeleteMe:
            log("Pickup collected (bDeleteMe)")
            return False
    except Exception:
        log("Pickup invalid")
        return False
    return True


def hold_tick(
    target, des_x, des_y, des_z, alpha, dt, force_drain_rate_val, duration_mult, log
) -> float:
    """Move pickup via velocity toward desired position.

    Sets RB velocity on the collision component each tick so the pickup
    tracks the crosshair. Gravity is zeroed during hold, so no unwanted
    velocity accumulates.
    alpha is the lerp factor (spring_strength * 0.1 * dt from caller).

    Returns work_drain amount, or negative on failure (caller should release).
    """
    try:
        cur_x = target.Location.X
        cur_y = target.Location.Y
        cur_z = target.Location.Z
    except Exception:
        return -1.0

    # Displacement toward desired position
    dx = des_x - cur_x
    dy = des_y - cur_y
    dz = des_z - cur_z
    gap = math.sqrt(dx * dx + dy * dy + dz * dz)

    # Convert lerp alpha to velocity: alpha = stiffness * dt,
    # velocity = displacement * stiffness = displacement * alpha / dt
    if dt > 1e-6:
        s = alpha / dt
    else:
        s = 10.0

    try:
        # If PhysX put the body to sleep, re-init physics to wake it
        if object_mesh is not None and not object_mesh.RigidBodyIsAwake():
            target.AdjustPickupPhysicsAndCollisionForBeingDropped()
            target.InitializePickupForRBPhysics()
            set_gravity(0.0)

        target.Velocity.X = dx * s
        target.Velocity.Y = dy * s
        target.Velocity.Z = dz * s
        if object_mesh is not None:
            object_mesh.SetRBLinearVelocity(target.Velocity)
    except Exception as e:
        log(f"Pickup velocity failed: {e}")
        return -1.0

    # Drain based on gap
    work_drain = gap * alpha * (force_drain_rate_val * 1e-6) * duration_mult * dt
    work_drain *= object_drain_mult.value / 100.0
    return work_drain


def throw_tick(target, throw_dir, speed, log) -> bool:
    """Apply impulse along throw direction. Returns False on failure.

    speed is an impulse magnitude. Applied via AddImpulse on the
    collision component — physics handles the arc from here.
    """
    try:
        _apply_impulse(
            target,
            throw_dir[0] * speed,
            throw_dir[1] * speed,
            throw_dir[2] * speed,
        )
    except Exception as e:
        log(f"Pickup throw impulse failed: {e}")
        return False
    return True


def height_offset() -> float:
    """Height offset for crosshair alignment. Zero for pickups — origin is at visual center."""
    return 0.0


def drain_mult() -> float:
    """Returns object_drain_mult.value / 100.0."""
    return object_drain_mult.value / 100.0


def set_gravity(scale: float) -> None:
    """Set gravity on object_mesh physics bodies."""
    if object_mesh is None:
        return
    try:
        root = object_mesh.GetRootBodyInstance()
        if root is not None:
            root.CustomGravityFactor = scale
    except Exception:
        pass


def cleanup(target) -> None:
    """Pickup-specific cleanup — restore gravity, keep throw momentum."""
    set_gravity(1.0)


def reset_state() -> None:
    """Clear all object-specific globals."""
    global target_is_object, object_mesh, object_type
    target_is_object = False
    object_mesh = None
    object_type = ""
