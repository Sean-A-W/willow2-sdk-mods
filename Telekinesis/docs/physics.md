# Spring Physics

## Force Model

The held enemy is pulled toward a target point in front of the player's eyes
using a spring-damper system:

```
desired_pos = eye_position + forward_direction * capture_distance

displacement = desired_pos - current_pos
velocity     = root_body.GetUnrealWorldVelocity()

force = displacement * spring_strength - velocity * damping_factor
```

Applied via `target.Mesh.AddForce(force_vector)` each frame.

### Spring Strength (k)

Controls how aggressively the enemy tracks the crosshair.
- Low (10–30): floaty, slow tracking, enemies swing around
- Medium (50): responsive but natural
- High (100–500): snappy, almost instant tracking

### Damping Factor (c)

Counteracts velocity to prevent oscillation.
- 0: no damping — enemy swings back and forth endlessly
- 30: moderate damping — smooth deceleration
- 100: heavy damping — very sluggish movement

### Tuning Tips

- High spring + low damping = bouncy, oscillating hold
- High spring + high damping = snappy but stable
- Low spring + low damping = floaty and lazy (Darth Vader style)
- The ratio `c / k` determines the damping character:
  - `c/k < 1` = underdamped (some oscillation)
  - `c/k ≈ 1` = critically damped (fastest settle, no overshoot)
  - `c/k > 1` = overdamped (slow settle, no overshoot)

## Gravity Control

While held, the enemy's gravity is reduced via two mechanisms:

1. **`pawn.CustomGravityScaling`** — actor-level gravity multiplier
2. **Per-body `CustomGravityFactor`** — set on every `PhysicsAssetInstance.Bodies[i]`

Both are set to `hold_gravity / 100.0` on activation and restored to `1.0`
on release. Default hold gravity is 0% (weightless).

Without gravity reduction, the spring force must constantly fight gravity,
causing the enemy to sag below the crosshair and oscillate vertically.

## Capture Distance

The distance from the player's eye to the held enemy, locked at the moment
of capture.

- Computed as `distance(player_eye, target_position)` at activation
- Clamped to `min_hold_distance` (prevents clipping into player model)
- Can be overridden with `hold_distance_override` (0 = use actual distance)

The enemy is held at this fixed distance along the player's look direction.
Looking up/down/left/right moves the target point, and the spring pulls the
enemy to follow.

## Camera & Crosshair Alignment

The desired position is computed from `pc.CalcViewLocation` and `pc.CalcViewRotation`
(the actual camera values the crosshair traces from), not from `player.Location + EyeHeight`
which can differ due to camera bob/offset.

A vertical offset is added to the desired position so the pawn's visual center
(torso) aligns with the crosshair rather than their feet:

```
des_z += target.CylinderComponent.CollisionHeight  # fallback: 80 UU
```

`target.Location` is at the pawn's root (feet), not their center of mass. Without
this offset, held enemies consistently appear below the crosshair. The offset is
world-space (always vertical) rather than a pitch adjustment, which would cause
the target to drift forward/backward when looking up/down.

## Ragdoll Maintenance

The enemy must stay ragdolled for the entire hold duration. BL2's stagger
system has a recovery timer (`StaggerEndRagdollTime`) that normally brings
the enemy back to standing after a short period.

- On activation: `StaggerEndRagdollTime = now + 300.0` (5 minutes)
- Each frame: `StaggerEndRagdollTime = now + 2.0` (rolling extension)
- On release: `StaggerEndRagdollTime = now + 0.3` (quick recovery)

## Charge System

- Starts at 1.0 (or partial if early-activated during cooldown)
- **Base drain**: `charge_drain_rate / 100.0` per second, scaled by duration multiplier
- **Work drain**: `mass * |a·v| * force_drain_rate * 1e-10` per tick — force × distance (work)
- When charge hits 0: automatic release

### Work-Based Drain (Force × Distance)

Charge drain is proportional to the **work done** moving the enemy:
```
power = acceleration · velocity  (dot product)
work_drain = mass * |power| * factor * duration_mult * dt
```

- **Velocity source**: `target.Velocity` (single property read) rather than averaging
  `GetUnrealWorldVelocity()` across all physics bodies. The per-body average is still
  used for the **damping term** (where precision prevents feedback loops on light enemies),
  but drain only needs an approximate velocity for charge cost feel.
- **Dot product** means perpendicular force (smooth orbiting) costs nearly zero
- **Parallel force** (throwing, accelerating toward/away) costs maximum
- **Mass scaling** makes heavier enemies more expensive to move
- **No sqrt needed** — cheaper than the old acceleration-magnitude formula

### Phaselock Duration Skill Scaling

At grab time, the mod reads `Att_Phaselock_Duration` from the pawn's attribute
system (`GD_Siren_Skills.Misc.Att_Phaselock_Duration`). This includes all
vanilla skill bonuses and modded skill effects.

```
duration_mult = base_duration / effective_duration
```

- Base Phaselock duration: 6.0s
- With Quicken grade 5 (+25s): effective = 31s → mult = 0.194 (~5× longer holds)
- Both base drain and work drain are multiplied by this factor
- Read once at grab time (skill tree doesn't change mid-hold)

### Cooldown via Vanilla Pool

The charge level is written to the vanilla `SkillCooldownPool` every tick:
`pool = max * (1.0 - charge)`. This drives the HUD cooldown icon. On release,
we simply stop updating the pool — vanilla's configured drain rate handles the
cooldown automatically. Short hold = short cooldown, long hold = long cooldown.

### Early Activation

If `min_activation_pct` < 100, the player can activate before cooldown
finishes. The charge starts at the cooldown progress fraction:
- 60% cooldown complete → 0.6 starting charge → ~20s hold (at drain rate 3)

Checked directly via pool value in the `ServerStartActionSkill` hook.
