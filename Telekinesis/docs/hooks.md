# Hook Chain

## Summary

| # | Hook | Type | Purpose |
|---|------|------|---------|
| 1 | `WillowPlayerController:ServerStartActionSkill` | PRE | Block vanilla, run `_activate()` or fizzle |
| 2 | `LiftActionSkill:PhaseLockTarget` | PRE | Block vanilla lift during forced fizzle |
| 3 | `WillowPlayerController:PlayerTick` | PRE | Spring physics, charge drain, FX upkeep |
| 4 | `WillowInventoryManager:PrevWeapon` | PRE | Block scroll while holding |
| 5 | `WillowInventoryManager:NextWeapon` | PRE | Block scroll while holding |
| 6 | `WillowPlayerController:StartFire` | PRE | M1 triggers throw |
| 7 | `WillowPlayerController:EquipWeaponFromSlot` | PRE | Block weapon swap while holding |
| 8 | `WillowPlayerController:MeleeAttack` | PRE | Block melee while holding |
| 9 | `WillowPlayerController:ThrowGrenade` | PRE | Block grenade while holding |
| 10 | `WillowPlayerController:ShowStatusMenu` | PRE | Block menu while holding |

## Hook Details

### 1. ServerStartActionSkill PRE — Entry Point

Fires on every skill activation attempt. We intercept for Siren (LiftActionSkill) only.

**Flow:**
1. Check enabled, Siren class, not FFYL
2. If already holding → `_release()` on re-press, return `Block`
3. Check cooldown pool for early activation (partial charge)
4. Validate target from `args.SkillTarget`:
   - `None` → return `None` (vanilla handles miss/fizzle)
   - Not `WillowAIPawn` → return `None` (vanilla Phaselock for Res, etc.)
   - Dead → return `Block`
   - Unstaggerable (`bVehicleBlockingActor` / `IgnoreMomentumTransfer`):
     - Fallback on → return `None` (vanilla Phaselock)
     - Fallback off → set `_force_fizzle = True`, return `None` (forced fizzle)
   - Scripted/matinee → return `Block`
   - Out of range → return `Block`
5. Valid target → `_activate()`, return `Block`

Returning `Block` prevents vanilla from running at all. Returning `None` lets
vanilla handle the activation (for fizzle, fallback, or miss cases).

### 2. PhaseLockTarget PRE — Forced Fizzle

Only active when `_force_fizzle` is set (unstaggerable target, fallback disabled).
Blocks vanilla from lifting the enemy and calls `FizzleOut()` on the LiftActionSkill
instance. Vanilla sees `bFizzled = true` → plays miss animation, miss impact FX/sound,
and schedules cooldown reset via `ResetSkillCooldown()`.

This hook is a no-op (returns `None`) in all other cases.

### 3. PlayerTick PRE — Physics Loop

Runs every frame. Handles (in order):

1. **Fading FX** — process `_fading_fx` list (runs even when not active)
2. **Target/player validation** — WeakPointer expiry, death, FFYL
3. **Charge drain** — base drain + work-based drain, release when depleted
4. **Ragdoll maintenance** — extend `StaggerEndRagdollTime`
5. **Throw sound scheduling** — timestamp-based, fires during windup or force phase
6. **Throw windup** — pullback with sine ease-out, spring still holds
7. **Throw force** — directional acceleration, burst FX at apex, release on completion
8. **Spring physics** — desired position from camera, spring-damper force, mass normalization
9. **Orb scale** — dynamic scale from drain intensity via exponential lerp
10. **Cooldown pool** — write charge to vanilla pool for HUD display

### 4–5. PrevWeapon / NextWeapon PRE — Scroll Lock

While holding, blocks weapon scrolling. Scroll input is used for push/pull
(adjusting `_capture_dist`) instead.

### 6. StartFire PRE — Throw Trigger

M1 (left click) while holding triggers `_throw_target()`. Blocks the normal
fire input so the player doesn't shoot while holding.

### 7–10. Action Blocks

Block weapon swap, melee, grenade, and status menu while holding to prevent
breaking the hold state.

## Why ServerStartActionSkill?

### Previous Architecture (v1)

The original approach hooked `LiftActionSkill:PhaseLockTarget` (simulated function)
as the entry point, with 6+ additional hooks on LiftActionSkill to suppress vanilla
tick, stagger detection, fizzle, etc.

### Current Architecture (v2)

Blocking `ServerStartActionSkill` is simpler: vanilla never runs, so there's nothing
to suppress. One hook replaces six. The only additional hook needed is `PhaseLockTarget`
for the forced fizzle case (which must let vanilla activate to get the fizzle animation).

### Why Not Block StartActionSkill Instead?

`StartActionSkill` is an `exec function` (client-side) — pyunrealsdk PRE hooks on exec
functions cannot reliably block execution. `ServerStartActionSkill` is a `reliable server
function` which CAN be blocked via PRE hooks in the SDK.
