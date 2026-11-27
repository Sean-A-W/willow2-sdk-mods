# Pitfalls & Lessons Learned

Critical bugs and gotchas encountered during development. Many of these caused hours of debugging.

## SDK / Engine Crashes

### Never call find_all() or physics manipulation inside TakeDamage
`find_all()`, `AddRadialImpulse`, `ConvertFixedToRigidBody`, `WakeRigidBody` inside TakeDamage hooks cause fatal native crash (segfault in pyunrealsdk). UE3 damage processing has internal state that conflicts with iterating/modifying other objects mid-callback. **Always defer to PlayerTick** using a pending queue.

### POST hooks on functions that destroy obj crash
e.g. `WillowProjectile:Explode` POST segfaults because `Explode()` destroys the projectile before POST runs. Reading any property on a destroyed UObject = native crash. **Use PRE** when the function may destroy the object.

### Never use WillowDmgSource_Bullet for programmatic TakeDamage on ragdolled pawns
Using `WillowDmgSource_Bullet` (or any bullet-specific damage type) as the `DamageType` argument when calling `TakeDamage` on ragdolled pawns causes an intermittent engine deadlock/hang. Bullet damage types trigger bullet-specific engine processing (pellet tracking, weapon proc logic, critical hit resolution) that conflicts with active PhysX ragdoll simulation. **Use plain `DamageType` (via `unrealsdk.find_class("DamageType")`) for any programmatic damage** like impact/collision damage. This was confirmed by testing identical TakeDamage calls — `WillowDmgSource_Bullet` hung intermittently while `DamageType` never hung.

### Do NOT hook explosion/projectile functions
Hooking `ExplosionDefinition:PlayExplosion` or `WillowProjectile:Explode` crashes — both PRE and POST caused repeated fatal segfaults. Even just reading args (Location, Momentum, DamageRadius) on these hooks crashes. These functions run in a sensitive engine context (inside damage radius iteration or projectile cleanup) where SDK hook overhead + property access is unsafe.

### find_all("WillowAIPawn") returns dangerous objects
Returns archetypes/templates with `HealthPool.Data=None` — calling `GetHealth()` on these **crashes** (native segfault, null pool dereference). Always check `pawn.HealthPool.Data is not None` before `GetHealth()` outside hooks. Also returns `Default__WillowAIPawn` (CDO) — skip via `pawn.Name.startswith("Default__")`.

## Python / Identity Bugs

### Never use id(pawn) for cross-frame tracking — use _pawn_uid() (PathName)
`id()` returns the Python wrapper address, NOT the UObject address. `WeakPointer` doesn't prevent wrapper GC, so `id()` can be reused by a completely different pawn. Each hook invocation may create a **new Python wrapper** for the same UObject, giving a different `id()`. This broke `RECENT_DAMAGE` accumulation (every bullet got its own window) and impact damage tracking (`LAST_LAUNCH`/`LAST_IMPACT` grace periods never matched).

**Rule**: Any dict/set that persists across separate hook calls or frames MUST use `_pawn_uid(pawn)` (PathName) as key — this includes `RECENT_DAMAGE`, `RAGDOLLED_PAWNS`, `LAST_LAUNCH`, `LAST_IMPACT`, `IMPULSE_HANDLED`, and singularity well pawn sets.

**IMPULSE_HANDLED was NOT safe with `id()`**: TakeDamage PRE returns before native TakeDamage calls HandleMomentum. The wrapper from TakeDamage PRE is GC'd, so HandleMomentum gets a new wrapper with a different `id()`. This caused HandleMomentum to leak through, applying a second (mass-divided) impulse on top of the intended one.

Only truly synchronous call-chain dicts (`PENDING_IMPULSE`, `DEATH_IMPULSE`) are safe with `id()` — these are set during a PRE hook and consumed in a POST hook that fires during the SAME PRE hook's execution (e.g. TakeDamage PRE calls Stagger() which triggers SetupRagdollPhysics POST synchronously, while TakeDamage PRE is still on the stack).

### Global set .add() vs assignment
`IMPULSE_HANDLED = True` creates a local variable (UnboundLocalError), breaking the entire hook function. Must use `IMPULSE_HANDLED.add(pid)`.

### IMPULSE_HANDLED must be set on ALL TakeDamage early returns
Otherwise HandleMomentum leaks through on unhandled paths, causing limp ragdolls or orbit launches.

## UE3 Behavior Gotchas

### HandleMomentum fires multiple times per TakeDamage
BL2's UE3 inheritance chain causes HandleMomentum to fire more than once per damage event. It also receives **mass-divided** momentum while TakeDamage receives **raw** momentum.

### args.HitLocation for explosions is the enemy's position
NOT the explosion origin. Use momentum direction as a proxy for blast direction.

### Struct properties are live references
`pawn.Velocity`, `pawn.Location`, etc. return **live references** to the engine struct. Writing to them modifies the actual engine object — do not use them as temporary vectors.

### EStaggerState values
- 0 = Staggered (ragdolled)
- 1 = Unstaggering
- 2 = BlendAnimStart
- 3 = BlendAnimEnd

SetupRagdollPhysics only fires for states 2 and 3 (not 0 or 1).

### Momentum accumulation applies all force to the triggering pellet's bone
When multi-pellet accumulation (e.g. shotgun) triggers a stagger, the full accumulated force is applied to whatever bone the **last pellet** (the one that tipped the threshold) hit. Per-bone hit locations from earlier pellets are not tracked. This is fine when the first few pellets trigger it (most force comes from follow-up hits which DO go to their own bones). But at high thresholds where most pellets accumulate before staggering, the majority of the force concentrates on one bone instead of being distributed. A proper fix would require tracking per-bone momentum in `RECENT_DAMAGE`.

### Tediore throws have zero momentum
Tediore reload throws fire TakeDamage with `WillowDmgSource_Grenade` and large damage but `momentum = (0, 0, 0)`. The mod skips all zero-momentum hits (to ignore DoT ticks/status effects), so Tediore throws do nothing. A potential fix would be to synthesize an impulse direction from player-to-enemy and derive force from damage, but this would affect all zero-momentum grenades.

### Heavy enemies (bVehicleBlockingActor) cannot be staggered while alive
Goliaths, Spiderant Emperors, etc. lack `StaggerSettings`, so `Stagger(None)` silently fails. They DO ragdoll on death (engine forces it). No workaround found — the vanilla knockback is a pre-baked animation, not physics.

## pyexec Context

- `pyexec` reads file as string and calls `exec()` — functions defined inside are NOT accessible from `py` commands (separate namespace)
- `@hook` decorators don't persist from pyexec context
- For testing, use `py` for one-liners or write persistent mod code instead
