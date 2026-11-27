# Physics & Impulse System

## Impulse Application

All sources use `bVelChange=False` (mass-dependent impulse). The mod compensates for UE3's inconsistent physics masses through the mass normalization system.

### Single-Bone vs Per-Bone

- **Single-bone** (bullets, melee): impulse applied to the hit bone. If the hit bone walks to a parent via hierarchy, use the parent as the target (per-bone on single-bone hits causes force absorption by joint constraints).
- **Per-bone** (rockets, grenades, singularity): impulse distributed across all bones with `1/dist²` weighting from the impact point. Central bones receive more force.

### Bone Walk

When a hit bone has no physics body, the system walks up the skeleton hierarchy to find the nearest parent with a physics body. This ensures hits on non-physical bones (fingers, face bones) still apply force.

## Mass Normalization (enemy_masses.py)

### The Problem
UE3 physics masses are inconsistent and don't scale with `DrawScale`. A Nomad at ds=1.0 has mass 190, but at ds=1.5 (Bad Maw) has mass 1833 (9.65x, not the expected 3.375x for ds³).

### The Solution
`ENEMY_MASS` dict maps enemy names to real-world-like masses in kg. The correction factor is:
```
correction = true_physics_mass / (ENEMY_MASS[name] * ds³)
```

- `get_desired_mass(pawn)` returns `ENEMY_MASS[name] * ds³` or falls back to `true_mass / BodyClass.Mass`
- Physics mass cache keyed by `(PhysicsAsset.Name, DrawScale)` — NOT `id()`
- Large enemies (DrawScale > 1.0) intentionally feel heavier — no force boost compensation

## Velocity Estimation

Before triggering a stagger, the mod estimates resulting velocity:
```
effective_mass = get_desired_mass(pawn) / N
estimated_vel = est_force / effective_mass
```
Where N=16 for single-bone, N=4 for per-bone (per-bone 1/dist² concentrates force on central bones).

## Terminal Velocity

Ragdolled pawns have a velocity cap to prevent absurd launches:
```
headroom = max(0, 1 - current_speed / cap)
```
Retrieved via `mesh.GetRootBodyInstance().GetUnrealWorldVelocity()`. Per-bone also has a separate `max_bone_force` slider.

## Recovery System

- `StaggerEndRagdollTime = currentTime + recovery_time/10` set in SetupRagdollPhysics POST
- `StaggeredRestVelocity` controls speed threshold before recovery starts (default 200)
- HP-scaled recovery animation speed: `rate = 1.0 + 2.0 * hp_pct` (full HP = 3x, near-death = 1x)
- Vanilla failsafes disabled: `StaggeredRestGiveUpTime=0`, `UnstaggerGiveUpTime=0`

## Collision Channels

| Channel | ID | Usage |
|---------|-----|-------|
| RBCC_Default | 0 | World geometry |
| RBCC_Nothing | 1 | Standing pawns (kinematic, not in PhysX dynamic scene) |
| RBCC_Pawn | 2 | Ragdolled pawns (dynamic, collides with 0, 2, 3) |
| RBCC_Vehicle | 3 | Vehicles |
| RBCC_GameplayPhysics | 5 | Gameplay objects |
| RBCC_DeadPawn | 16 | Dead pawns |

Standing pawns use `PhysicsWeight=0`, `SetBlockRigidBody(false)` — they're kinematic and invisible to PhysX dynamic simulation. This is why ragdolls pass through standing enemies (solved by Touch-based bowling instead).

## PhysX in BL2

BL2 uses PhysX SDK 2.x — **single-threaded x87 CPU code** for all gameplay physics. The GPU path (NVIDIA only) handles cloth, fluids, and debris particles — cosmetic only. The PhysX Low/Medium/High setting has zero effect on ragdoll performance. There is no SDK-level mechanism to offload ragdoll physics to GPU.
