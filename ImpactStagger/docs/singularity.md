# Singularity System

## Detection

Singularity damage arrives via **TakeDamage** (not HandleMomentum) with:
- `DamageType = None`
- `DamageCauser.Definition.Name` contains `'Singularity'` (e.g. `'Grenade_SingularityChild'`)
- Raw momentum in the 30k-110k range

## Gravity Wells

When a singularity is detected, a gravity well is created at the impact location:
```python
well = [x, y, z, expire_time, set_of_pawn_uids]
```

### Well Behavior
- **Dedup**: wells at the same location (within 1 unit) are merged — prevents duplicate wells from the same grenade
- **Catch radius**: gates initial pull. Enemies beyond it get `return Block` from TakeDamage (no damage, no pull)
- **Latecomer adoption**: each tick, nearby ragdolled pawns within catch radius are adopted into the well
- **Escape radius**: enemies that somehow break free beyond `catch_radius * escape_pct` are evicted
- **Duration**: constant ~2.2s regardless of grenade fuse time

### Pull Force
- `AddRadialForce` with `RIF_Constant` (falloff type 0) — uniform pull regardless of distance
  - `RIF_Linear` (1) caused slingshot: edge enemies barely pulled, center enemies overshot
- Mass-equalized via `compensate_mass`: multiplies force by `true_physics_mass / MASS_REFERENCE` so all enemy types are pulled at similar speed

### Recovery Prevention
During an active well, ragdolled pawns in the well have their `StaggerEndRagdollTime` pushed forward each tick, preventing them from standing up while being pulled.

## Radius Research

- `_causer.DamageRadius` is the **explosion** radius, NOT the pull radius
  - Vanilla singularity grenade: DamageRadius=560 but enemies pulled from 1556-2516 UU away
  - Gun-spawned singularities: DamageRadius=0 but still pull from 2000+ UU
- Actual pull radius is set in native code and not readable from projectile properties
- `FuseTime` on grenade card = time until pull STARTS, not pull duration

## Engine Behavior

The engine fires `HurtRadius` only once when the singularity child spawns. This means:
- Only enemies within range at spawn time receive TakeDamage
- Enemies that walk into range later are NOT hit by TakeDamage
- The latecomer adoption system (PawnList scan at 0.25s intervals) catches these stragglers
