from __future__ import annotations

from math import sqrt
from time import perf_counter

from mods_base import BoolOption, SliderOption
from uemath import Vector


bowling_enabled = BoolOption(
    "Enable Bowling",
    False,
    description=("Ragdolled enemies bowl over standing enemies on contact."),
)

bowling_min_velocity = SliderOption(
    "Min Bowling Velocity",
    30,
    0,
    100,
    1,
    True,
    description=(
        "Minimum ragdoll speed to count as a bowler. 0 = any contact triggers bowling. Default: 30"
    ),
)

bowling_force_mult = SliderOption(
    "Bowling Force %",
    100,
    0,
    500,
    10,
    True,
    description=(
        "Momentum multiplier applied to bowled enemy from the ragdoll's velocity. "
        "100 = transfer ragdoll's momentum 1:1. "
        "Default: 100"
    ),
)


# Avoid repeat hits: (bowler_uid, target_uid) -> timestamp of hit
_hit_pairs: dict[tuple[str, str], float] = {}
_HIT_COOLDOWN: float = 0.5  # seconds before same pair can trigger again


def handle_touch(
    bowler,
    bowler_uid: str,
    target,
    ragdolled_pawns: dict,
    log_fn,
    can_ragdoll_fn,
    pawn_uid_fn,
    pending_impulse: dict,
    get_target_mass_fn=None,
    apply_damage_fn=None,
    ragdoll_collision_disabled: bool = False,
) -> None:
    """Handle a Touch event where bowler (ragdolled) overlaps target (standing).

    Uses inelastic collision: v_final = (m1 * v1) / (m1 + m2).
    Both pawns end up at the same velocity. Bowler is slowed, target is launched.
    Called from the WillowAIPawn:Touch hook in __init__.py.

    apply_damage_fn: optional callback(pawn, speed) to deal bowling impact damage
    directly. Used when ragdoll-ragdoll collisions are disabled (so RigidBodyCollision
    won't fire between bowler and target). When None, no damage is dealt here —
    the target takes damage when they hit a wall/floor instead.

    ragdoll_collision_disabled: when True, applies velocity directly via
    SetRBLinearVelocity instead of queuing through PENDING_IMPULSE. When False,
    queues the impulse for SetupRagdollPhysics and lets the engine handle collision.
    """
    global _hit_pairs

    # Target UID + dedup
    t_uid = pawn_uid_fn(target)
    if not t_uid:
        return
    if t_uid in ragdolled_pawns:
        return
    pair = (bowler_uid, t_uid)
    now = perf_counter()
    last_hit = _hit_pairs.get(pair)
    if last_hit is not None and now - last_hit < _HIT_COOLDOWN:
        return

    # Check bowler velocity
    min_vel_sq = float(bowling_min_velocity.value) ** 2
    try:
        root_body = bowler.Mesh.GetRootBodyInstance()
        if root_body is None:
            return
        rb_vel = root_body.GetUnrealWorldVelocity()
        vx, vy, vz = rb_vel.X, rb_vel.Y, rb_vel.Z
        speed_sq = Vector(rb_vel).magnitude_squared
        if speed_sq < min_vel_sq:
            return
    except Exception:
        return

    # Validate target is eligible
    try:
        if target.bTearOff or target.bHardAttach:
            return
        if target.HealthPool is None or target.HealthPool.Data is None:
            return
        if target.GetHealth() <= 0:
            return
        if not can_ragdoll_fn(target):
            return
    except Exception:
        return

    # Inelastic collision: v_final = (m1 * v1) / (m1 + m2)
    # Bowler (m1) hits stationary target (m2, v2=0).
    bowler_mass = ragdolled_pawns[bowler_uid][1]
    if bowler_mass <= 0:
        bowler_mass = 100.0
    target_mass = 100.0
    if get_target_mass_fn:
        try:
            target_mass = get_target_mass_fn(target)
        except Exception:
            pass
    if target_mass <= 0:
        target_mass = 100.0

    mass_ratio = bowler_mass / (bowler_mass + target_mass)
    force_scale = bowling_force_mult.value / 100.0

    # Final velocity for both pawns after collision
    fx = vx * mass_ratio * force_scale
    fy = vy * mass_ratio * force_scale
    fz = vz * mass_ratio * force_scale

    _hit_pairs[pair] = now

    try:
        ename = target.AIClass.Name if target.AIClass is not None else "?"
    except Exception:
        ename = "?"
    if log_fn:
        speed = sqrt(speed_sq)
        v_final = speed * mass_ratio
        impulse_mag = sqrt(fx * fx + fy * fy + fz * fz)
        log_fn(
            f"[IS-BOWL] {ename} uid={t_uid} by {bowler_uid} "
            f"v={speed:.0f} m1={bowler_mass:.0f} m2={target_mass:.0f} "
            f"ratio={mass_ratio:.3f} scale={force_scale:.2f} "
            f"v_final={v_final:.0f} impulse={impulse_mag:.0f}"
        )

    # Apply shared post-collision velocity to target as impulse.
    # Queue by id() for SetupRagdollPhysics, then fallback-apply if not consumed
    # (id() can be unstable across hook calls if the SDK creates a new wrapper).
    if ragdoll_collision_disabled:
        # Apply velocity directly — can't rely on physics collision between ragdolls.
        # SetRBLinearVelocity sets all bones to uniform velocity (whole-body launch).
        target.Stagger(None)
        try:
            mesh = target.Mesh
            mesh.WakeRigidBody()
            mesh.SetRBLinearVelocity(_make_vector(fx, fy, fz), False)
        except Exception:
            pass
    else:
        # Queue impulse for SetupRagdollPhysics — engine ragdoll collision handles the rest.
        pid = id(target)
        pending_impulse[pid] = (fx, fy, fz, target.Location, "", True, False)
        target.Stagger(None)
        leftover = pending_impulse.pop(pid, None)
        if leftover is not None:
            try:
                target.Mesh.AddImpulse(
                    _make_vector(fx, fy, fz),
                    target.Location,
                    "",
                    True,
                )
            except Exception:
                pass

    # Deal bowling impact damage when ragdoll-ragdoll collisions are disabled.
    # Uses relative speed between bowler and target for physically correct damage.
    if apply_damage_fn is not None:
        try:
            tv = target.Velocity
            rel_sq = (vx - tv.X) ** 2 + (vy - tv.Y) ** 2 + (vz - tv.Z) ** 2
        except Exception:
            rel_sq = speed_sq
        apply_damage_fn(target, sqrt(rel_sq))

    # Slow the bowler to post-collision velocity using velocity change
    # delta_v = v_final - v_current = v * (ratio - 1)
    try:
        dv_scale = mass_ratio - 1.0  # negative: decelerates
        bowler.Mesh.AddImpulse(
            _make_vector(vx * dv_scale, vy * dv_scale, vz * dv_scale),
            bowler.Location,
            "",
            True,  # bVelChange — applies as velocity, not force (bypasses mass division)
        )
    except Exception:
        pass


_bowling_vec = None


def _make_vector(x: float, y: float, z: float):
    """Reuse a single Vector struct to avoid allocation per call."""
    global _bowling_vec
    if _bowling_vec is None:
        import unrealsdk

        _bowling_vec = unrealsdk.make_struct("Vector")
    _bowling_vec.X = x
    _bowling_vec.Y = y
    _bowling_vec.Z = z
    return _bowling_vec


def cleanup_hit_pairs(ragdolled_pawns: dict) -> None:
    """Remove stale and expired hit pairs."""
    global _hit_pairs
    if _hit_pairs:
        now = perf_counter()
        active_uids = set(ragdolled_pawns.keys())
        _hit_pairs = {
            pair: t
            for pair, t in _hit_pairs.items()
            if pair[0] in active_uids and now - t < _HIT_COOLDOWN
        }


def get_options() -> list:
    """Return bowling options for the NestedOption group."""
    return [
        bowling_enabled,
        bowling_min_velocity,
        bowling_force_mult,
    ]
