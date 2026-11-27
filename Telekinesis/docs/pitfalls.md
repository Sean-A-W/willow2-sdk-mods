# Pitfalls and Known Issues

## Critical: Hook Function Types

Not all UE3 function types can be reliably blocked via pyunrealsdk PRE hooks:

| Function Type | PRE Block Works? | Example |
|---------------|-----------------|---------|
| `simulated function` | Yes | `LiftActionSkill:PhaseLockTarget` |
| `exec function` | No | `WillowPlayerController:StartActionSkill` |
| `reliable server function` | No | `WillowPlayerController:ServerStartActionSkill` |

This is why Telekinesis hooks at the `LiftActionSkill` level rather than
intercepting the activation at `StartActionSkill` or `ServerStartActionSkill`.

## Critical: WeakPointer for UObject References

**Never store direct UObject references across frames.**

When the player exits to the main menu while holding an enemy, all game
UObjects are destroyed. If `_target` is a direct reference, accessing any
property on it causes a native crash (segfault on destroyed UObject).

Solution: `WeakPointer(obj)` — calling the WeakPointer returns `None` if
the object has been destroyed. All UObject references (`_target_wp`,
`_skill_wp`) use WeakPointer with accessor functions (`_get_target()`,
`_get_skill()`).

## Critical: EndSkill vs ServerDeactivateSkill

v1 called `ServerDeactivateSkill` directly and experienced hangs. The cause
was corrupted vanilla state: `LiftedPawn` was set to the target, but the
target was staggered. On deactivation, vanilla tried to clean up the lifted
pawn and hit edge cases.

v2 avoids this entirely: `LiftedPawn` is never set (we block `PhaseLockTarget`),
so `EndSkill()` → `ServerDeactivateSkill()` has nothing to clean up. The skill
deactivates cleanly and cooldown starts normally.

If `EndSkill()` does hang for any reason, a fallback would be to set a flag
and let `IsLiftedPawnIncapacitated` return `True` on the next tick, making
vanilla end the skill naturally via `InterruptPhaseLock()`.

## Unstaggerable Enemies

Some enemies cannot be ragdolled and should fall through to vanilla Phaselock:

- **`bVehicleBlockingActor`**: Goliaths, Foreman, Loader SuperBadass, Constructors
- **`IgnoreMomentumTransfer`**: similar heavy enemies
- **Scripted pawns**: `bUsedByMatinee` (cutscene actors)

The `PhaseLockTarget` hook checks these and returns `None` (let vanilla handle)
if the target can't be ragdolled.

## OnActionSkillTick Block Side Effects

Blocking `OnActionSkillTick` also blocks `super.OnActionSkillTick()` in
`ActionSkill`, which handles:
- Duration tracking via timers
- Behavior system ticks

Since we blocked `PhaseLockTarget`, no timers were set (the timer setup is
inside `PhaseLockTarget`). The skill stays active indefinitely until we
explicitly call `EndSkill()`. This is intentional — charge drain controls
the hold duration, not vanilla skill duration.

## Charmed Targets (Thoughtlock)

The Thoughtlock augment causes the Phaselocked target to become mind-controlled
instead of lifted into a bubble. `PhaseLockTarget` still fires, but the target
walks around as an ally.

Currently Telekinesis does not special-case this — it will ragdoll and hold
a Thoughtlock target the same as any other. This may or may not be desirable.
To skip Thoughtlock targets, check `skill.bCharmed` or `CanCharmTarget()`.

## FFYL (Fight For Your Life)

If the player goes down while holding:
- `PlayerTick` detects `pc.Pawn.IsInjured()` and calls `_release()`
- `IsActionSkillOnCooldown` hook skips early activation during FFYL

## Multiplayer

Not yet tested. Considerations:
- `PhaseLockTarget` is `function` (server-side) — hook only fires on host
- `PlayerTick` hooks on the local controller
- Forces applied via `Mesh.AddForce` are local physics
- May need replication for clients to see the held enemy move

## Pawn UID

Uses `pawn.PathName(pawn)` for the held target UID, NOT `id(pawn)`.
`id()` returns the Python wrapper address which changes between hook
invocations. PathName is stable across the pawn's lifetime.
