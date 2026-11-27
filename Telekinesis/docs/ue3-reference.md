# UE3 Reference (Telekinesis)

## LiftActionSkill (Phaselock)

Source: `WillowGame\Classes\LiftActionSkill.uc`

### State Machine (EPhaselockState)
| Value | Name | Description |
|-------|------|-------------|
| 0 | EPLS_None | Not active |
| 1 | EPLS_Intro | Lifting animation (~1.3s) |
| 2 | EPLS_Outro | Fade out before release |
| 3 | EPLS_Locked | Target suspended in bubble |
| 4 | EPLS_Ruin | Held for Ruin sub-skill |
| 5 | EPLS_Subsequence | Projectile seeks new target on death |

### Key Properties
- `CurrentState` — EPhaselockState enum
- `LiftedPawn` — the Phaselocked WillowPawn (None when we block PhaseLockTarget)
- `LiftEndLocation` — static suspend point
- `SkillDuration` — total duration (~8.3s default)
- `bReplicatedCharmed` — Thoughtlock augment active
- `PhaselockSMD_Hit` — SpecialMoveDefinition for the grab animation
- `PhaselockSMD_Miss` — SpecialMoveDefinition for the miss animation

### Key Functions
- `OnActionSkillStarted(pawn, controller, target)` — entry point
- `SelectTarget(target)` — validates and routes to PhaseLockTarget/FizzleOut
- `PhaseLockTarget(target)` — sets up lift (WE BLOCK THIS)
- `FizzleOut()` — no valid target found
- `EndSkill()` — clean deactivation → ServerDeactivateSkill → cooldown
- `OnActionSkillTick(dt)` — per-frame vanilla logic
- `IsLiftedPawnIncapacitated()` — checks if stagger should end skill
- `InterruptPhaseLock()` — forces skill end on stagger detection
- `StartActionSkillActiveAbility(...)` — re-press handler (Ruin/Converge)
- `GetPlayerAnimation(target)` — returns PhaselockSMD_Hit or _Miss

### Activation Chain
```
StartActionSkill (exec, WillowPlayerController)
  → ServerStartActionSkill (reliable server)
  → SkillManager.ActivateSkill
  → ActionSkill.OnActionSkillStarted (sets MyWillowPawn/PC/Controller)
  → LiftActionSkill.OnActionSkillStarted
    → Play grab animation
    → SelectTarget(target)
      → CanPhaseLockTarget(target)?
        → Yes: PhaseLockTarget(target)  ← blocked by Telekinesis
        → No:  FizzleOut()
```

## WillowPlayerController

### Cooldown Pool
- `pc.SkillCooldownPool` — `ResourcePoolReference` struct
- `pc.SkillCooldownPool.Data` — `ResourcePool` object
- `.Data.GetCurrentValue()` — current pool value (0 = ready, max = full cooldown)
- `.Data.SetCurrentValue(float)` — manually set pool value
- `.Data.GetMaxValue()` — pool maximum (typically 12.0)

### Target Selection
- `pc.AutoAimStrategy.GetPreferredTarget(pc, False, True, False)` — vanilla aim assist target

### Other
- `pc.Rotation` — current view rotation (Pitch, Yaw, Roll in UE3 rotation units)
- `pc.Pawn` — the player's WillowPlayerPawn
- `pc.Pawn.IsInjured()` — True during FFYL
- `IsActionSkillOnCooldown()` — checks if skill is cooling down

## Physics

### Gravity
- `pawn.CustomGravityScaling` — actor-level gravity multiplier (float)
- `pawn.Mesh.PhysicsAssetInstance.Bodies[i].CustomGravityFactor` — per-body gravity

### Forces
- `mesh.AddForce(Vector)` — apply force to the root body
- `mesh.AddImpulse(Vector)` — apply instantaneous impulse
- `mesh.GetRootBodyInstance()` — returns root RB_BodyInstance
- `root.GetUnrealWorldVelocity()` — current velocity of root body

### Ragdoll
- `pawn.Stagger(None)` — trigger ragdoll (via ImpactStagger or vanilla stagger system)
- `pawn.IsStaggered()` — check if pawn is currently staggered
- `pawn.StaggerState.State` — EStaggerState (0=Staggered, 1=Unstaggering, 2/3=BlendAnim)
- `pawn.StaggerState.StaggerEndRagdollTime` — TimeSeconds when ragdoll ends

## Animation

### SpecialMoveComponent
- `pawn.GetSMComponent()` — returns the pawn's SpecialMoveComponent
- `smc.Play(smd)` / `smc.Stop(smd)` — high-level play/stop
- `smc.IsPlaying(smd)` — check if a SpecialMoveDefinition is active
- `smc.GetSMNode()` — returns the AnimNodeSpecialMoveBlend

### AnimNodeSpecialMoveBlend (extends AnimNodeSlot)
- `PlayCustomAnim(name, rate, blendIn, blendOut, bLooping, bOverride, startTime, endTime)`
- `StopCustomAnim(blendOutTime)`
- `GetCustomAnimNodeSeq()` — returns AnimNodeSequence for scrubbing
- `GetPlayedAnimation()` — currently playing animation name

### AnimNodeSequence
- `SetPosition(time, bFireNotifies)` — seek to a time in the animation
- `CurrentTime` — current playback position (read-only)

### Bone Control (for directional hand movement)
- `mesh.FindSkelControl(name)` — find a named SkelControl on the AnimTree
- `SkelControlSingleBone.BoneRotation` — Rotator offset
- `SkelControlSingleBone.BoneTranslation` — Vector offset
- `SkelControlBase.SetSkelControlActive(bool)` — enable/disable
- `SkelControlBase.SetSkelControlStrength(float, blendTime)` — blend strength
