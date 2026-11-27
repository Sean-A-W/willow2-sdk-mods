# ImpactStagger Architecture

## Module Structure

```
ImpactStagger/
├── __init__.py        # Hooks, shared state, core stagger/impulse logic
├── pyproject.toml     # SDK mod metadata
├── enemy_masses.py    # ENEMY_MASS dict, mass helpers, physics mass caching
├── bowling.py         # Ragdoll-on-enemy collision (Touch-based)
├── impact_damage.py   # Collision damage, crit detection, sliders
├── singularity.py     # Gravity wells, detection, force computation, sliders
├── rockets.py         # Rocket detection, sliders, getters
├── grenades.py        # Grenade detection, sliders, getters
├── multiplayer.py     # MP helpers, networking.sdkmod integration, settings sync
├── object_physics.py  # Loot/prop scatter (DORMANT — code exists but disabled due to crash)
├── enemy_data.json    # Dumped enemy data reference
├── damage_sources.json # Catalogued damage source names
└── unknown_enemies.json # Enemies not yet in ENEMY_MASS dict
```

## Hook Chain

| Hook | Type | Purpose |
|------|------|---------|
| `WillowAIPawn:TakeDamage` | PRE | Main entry — pellet accumulation, stagger trigger, follow-up hits, singularity pulls |
| `WillowAIPawn:HandleMomentum` | PRE | Blocks native momentum when TakeDamage handled it |
| `WillowAIPawn:SetupRagdollPhysics` | POST | Applies deferred impulse, sets physics properties (friction, bounciness, recovery time) |
| `WillowAIPawn:PlayDying` | POST | Forces ragdoll death for qualifying kills |
| `WillowAIPawn:PlayHardFlinch` | PRE | Replaces flinch with ragdoll |
| `WillowAIPawn:Touch` | PRE | Ragdoll bowling — detects ragdolled pawn overlapping standing pawn |
| `WillowAIPawn:RigidBodyCollision` | PRE | Impact damage on collision (applied directly, not deferred) |
| `WillowPlayerController:PlayerTick` | PRE | Singularity well ticks, bowling cleanup |

## Data Flow

1. **TakeDamage PRE** receives damage event, identifies source type (bullet, rocket, grenade, singularity)
2. Estimates resulting velocity from force and mass; checks against ragdoll threshold
3. If threshold met: calls `pawn.Stagger(None)` to trigger ragdoll
4. Queues impulse in `PENDING_IMPULSE` dict (keyed by `id(pawn)`, alive on stack)
5. **SetupRagdollPhysics POST** fires when ragdoll is ready — dequeues and applies impulse via `AddImpulse`
6. For already-ragdolled enemies: impulse applied immediately in TakeDamage (no Stagger call needed)

## Key Design Decisions

### id(pawn) vs PathName(pawn)
- **Short-lived dicts** (PENDING_IMPULSE, DEATH_IMPULSE, IMPULSE_HANDLED): use `id(pawn)` — the Python wrapper is alive on the call stack, so `id()` is stable
- **Persistent dicts** (RAGDOLLED_PAWNS, singularity well pawn sets): use `pawn.PathName(pawn)` via `_pawn_uid()` — `id()` returns the Python wrapper address which can be reused after GC

### Deferred Processing
- **Never call `find_all()` or physics manipulation inside TakeDamage** — causes fatal native crash
- **Impact damage from RigidBodyCollision** applied directly using plain `DamageType` (not deferred). Previously queued to PlayerTick, but the actual hang was caused by using `WillowDmgSource_Bullet` — plain `DamageType` works fine inside PhysX callbacks.
- **Singularity well ticks** run in PlayerTick at 0.25s intervals

### HandleMomentum Blocking
- HandleMomentum fires **multiple times** per TakeDamage (UE3 inheritance chain)
- Receives **mass-divided** momentum (TakeDamage receives raw)
- Must be blocked for ALL TakeDamage code paths, otherwise causes limp ragdolls or orbit launches
