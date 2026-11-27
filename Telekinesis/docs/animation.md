# Visual Effects & Animation

## Overview of FX Applied During Telekinesis

| Effect | Target | Implementation |
|--------|--------|---------------|
| Grab animation (1P) | Player arms | `Phase_Lock_Lift` frozen on Arms WeaponAction node, reverse on release |
| Blue orb (1P) | Player hand | ParticleSystemComponent on `LilithMeleeFX` socket, dynamic scale from drain, fade on release |
| Tattoo glow (1P) | Player arms | `p_EnablePowerEmissive` 0→1 on grab, fades 1→0 on release |
| Point light | Held pawn | PointLightComponent attached to target, brightness fade on release |
| Sound — windup | Player pawn | `Ak_Play_FX_Player_Phaselock_Activate` timed to peak at grab |
| Sound — hold loop | Player pawn | `Ak_Play_FX_Player_Phaselock_Loop` starts at grab moment |
| Sound — release | Player pawn | `Ak_Play_FX_Player_Phaselock_Collapse` on cleanup |
| Sound — voice | Player pawn | `Ak_Play_VOBD_Siren_PL_Start_Action_Skill` Maya cast line on F press |
| **TODO: Bubble** | Held pawn | 3-emitter system (FadeIn -> Loop -> FadeOut) |
| **TODO: 3P animation** | Player body | `AA_PhaseLock` via SMComponent on 3P mesh |

---

## 1P Grab Animation

### Key Facts (Discovered via Testing)
- **1P Arms mesh**: `pawn.Arms` (`GearboxSkeletalMeshComponent`)
- **Animation name**: `Phase_Lock_Lift` (0.87s) from AnimSet `Anim_Siren.Siren_1st`
- **Other anims**: `Phase_Lock_Fail` (0.53s), `Kiss_of_Death`, `Rolling_Thunder` (Scorn), `Melee`
- **Node**: `WeaponAction` (`AnimNodeSpecialMoveBlend`) on `Arms.Animations` (AnimTree)
- **3P equivalent**: `AA_PhaseLock` on `pawn.Mesh` via `SMComponent.Play(PhaselockSMD_Hit)` -- invisible in 1P

### Freeze Method
`Rate=0` doesn't work (engine skips the animation entirely). Use:
```python
node.PlayCustomAnim(
    "Phase_Lock_Lift",
    Rate=0.001,  # near-zero but nonzero
    BlendInTime=0.15,
    BlendOutTime=99.0,  # huge -- never blends out
    bLooping=True,
    bOverride=True,
    StartTime=0.40,  # hand-extended pose
)
# Then seek to exact frame:
seq = node.GetCustomAnimNodeSeq()
seq.SetPosition(0.40, False)
```

Every tick, re-seek to prevent drift: `seq.SetPosition(0.40, False)`

### Finding the Node
```python
arms_path = pawn.Arms.PathName(pawn.Arms)
for n in unrealsdk.find_all("AnimNodeSpecialMoveBlend"):
    np = n.PathName(n)
    if arms_path in np and "Default__" not in np and str(n.NodeName) == "WeaponAction":
        return n
```

---

## Blue Orb (Hand Particle)

### Source: `LiftActionSkill.uc` lines 1057-1073
Vanilla creates a `ParticleSystemComponent`, attaches to `LilithMeleeFX` socket on Arms.

### Properties (from LiftActionSkill defaults)
- `FirstPersonParticleSystem` -- the particle template
- `FirstPersonAttachmentName = 'LilithMeleeFX'`
- `FirstPersonTranslation = (3.5, -4.0, -0.5)`
- `FirstPersonScale = 0.35`
- Foreground rendering: `SetDepthPriorityGroup(1)`, `SetInWorldForeground(True)`
- Owner only: `SetOnlyOwnerSee(True)`

### Template: `FX_CHAR_Siren.Particles.Part_SirenASHandOrb`
11 emitters, all one-shot by default. Diagnostic script: `scripts/tk_diag_emitter_test.py`

| Idx | Name | Duration | Particle Life | Key Modules | Visual | Sustain Strategy |
|-----|------|----------|---------------|-------------|--------|-----------------|
| 0 | Particle Emitter | 1.0s | 0.25s | ColorOverLife, MeshRotation | Wide firefly trails swirling in palm | **Loop** (EmitterLoops=0) |
| 1 | Particle Emitter | 1.0s | 0.25s | ColorOverLife, MeshRotation | Medium firefly trails | **Loop** |
| 2 | Particle Emitter | 1.0s | 0.25s | ColorOverLife, MeshRotation | Tight concentrated firefly trails | **Loop** |
| 3 | Particle Emitter | 1.0s | 0.2s | SizeMultiplyLife 15→1 | Suck/implode effect | **One-shot** (startup) |
| 4 | Suction | 1.0s | 1.0s | SizeMultiplyLife 10→0 | Light-warping black hole effect | **One-shot** (startup) |
| 5 | ModulateBlack | 1.0s | 1.0s | SizeMultiplyLife 2.8→0 | Massive black sphere — **DISABLED** | Never use |
| 6 | Center | 1.0s | 1.25s | SizeMultiplyLife 0→2.2→0 | Blue orb — starts big, shrinks | **Sustain**: flatten SizeMultiplyLife to 1.0 + extend lifetime |
| 7 | After smoke | 0.5s | 1.5-1.8s | SizeMultiplyLife, Velocity, SubUV | Faint blue smoke wisps from hand | **Loop** (loops nicely as-is) |
| 8 | InitialFlash | 0.25s | 0.05-0.09s | SizeMultiplyLife 30→0 | Bright flash across screen | **One-shot** (startup) |
| 9 | EndBrightness | 1.0s | 0.25-0.45s | SizeMultiplyLife 15→0 | Blue/white gleam behind hand | **One-shot** (startup) |
| 10 | InitialFlash | 1.0s | 0.1-0.25s | SizeMultiplyLife 30→0 | Bright flash concentrated on hand | **One-shot** (startup) |

### Sustain Approach

**Problem**: All emitters are one-shot. Simply reactivating on tick causes flicker.
Simply extending particle lifetime freezes the SizeMultiplyLife curve at t=0, which
for emitter 6 is size X=0 (invisible).

**Solution per emitter type**:
- **Loop emitters (0-2, 7)**: Set `EmitterLoops=0`. Short-lived particles cycle
  naturally without visible pop. Emitter 7 (smoke) loops nicely as-is.
- **Sustain emitter (6)**: Flatten `SizeMultiplyLife.LookupTable` to all 1.0
  (constant size = base StartSize), extend particle lifetime to 999s (freezes
  ColorScaleOverLife at full opacity), extend EmitterDuration to 999s, set
  `bKillOnCompleted=False`. Single batch of particles lives forever at constant
  size. **Template stays permanently flattened** — never restore on release
  (see Shared Template Corruption below). On release, the PSC is simply
  deactivated and faded out via scale interpolation.
- **Startup-only (3, 4, 8, 9, 10)**: Play once at activation, never loop.
- **Disabled (5)**: `bEnabled = False` — black sphere obscures everything.

### Key Detail: SizeMultiplyLife LookupTable
The `LifeMultiplier` distribution uses a `LookupTable` array sampled over relative
particle time (0.0 at spawn, 1.0 at death). `LookupTableChunkSize=3` means each
sample is an (X, Y, Z) scale vector. Setting all entries to 1.0 gives constant 1x
scale. The curve is NOT absolute size — it multiplies the base `StartSize`.

### Dynamic Orb Scale (Drain Intensity)

The orb grows/shrinks based on how much charge is being drained, providing visual
feedback on energy expenditure:

```
target_intensity = clamp(work_drain / drain_ref, 0, 1)
_orb_intensity += (target - current) * min(smooth_speed * dt, 1)  # exponential lerp
scale = BASE_SCALE * (min_frac + (1 - min_frac) * intensity * max_mult)
```

- **Idle hold** (a ⊥ v, near-zero drain): orb at `min_frac` of base (default 50%)
- **Violent movement** (high drain): orb grows up to `max_mult` of base (default 150%)
- **Smoothing**: exponential lerp prevents jitter from per-tick drain fluctuations
- All parameters configurable via FX Settings submenu sliders

On release, the fade-out system captures the orb's **current dynamic scale** and fades
from there to 0 — no snap to full size.

### Getting the Template
Since we block ServerStartActionSkill (no skill instance created), use
`find_all("LiftActionSkill")` and read from the CDO (Default__ object).

### Shared Template Corruption (Critical Pitfall)
The particle template (`Part_SirenASHandOrb`) is a **shared asset** — all PSC instances
reference the same emitter modules. Modifying the template (flatten curves) and later
restoring it while a previous PSC still has live particles causes race conditions:
- Rapid grab→release→grab cycles corrupt emitter 6 (orb disappears on every other grab)
- `_flatten_emitter_curves` re-saving current (already flattened) values as "originals"
- Old PSC particles dying mid-restore, new PSC activating with half-restored state

**Solution**: Flatten the template once on first activation and **never restore it**.
On release, just `DeactivateSystem()` the PSC and fade it out via `_fading_fx`.
The original `SizeMultiplyLife` curves for emitter 6 are saved in `_original_emitter_state`
but only as a safeguard — they are never written back to the template.

---

## Tattoo Glow

### Discovery
- `ArmsMaterialInstance` on `WillowPlayerPawn` has zero params by default -- all inherited
- Parent: `Char_Siren.Mati_Siren_Hands` -> has `p_EnablePowerEmissive=0.0` and
  `p_PowerEmissiveColor=(0.0, 14.554, 20.0, 0.0)` (HDR blue)
- The actual rendered material is `Arms.GetMaterial(0)` (a different MIC from ArmsMaterialInstance)
- Must set `p_EnablePowerEmissive=1.0` on BOTH `Arms.GetMaterial(0)` AND `ArmsMaterialInstance`

### Important: MaterialInstanceConstant accepts ANY parameter name
`SetScalarParameterValue` will happily store any string -- only params referenced by
the shader actually affect rendering. Don't assume a parameter "works" just because
`Set` didn't error.

---

## Target Point Light

### Source: `LiftActionSkill.uc` lines 96, 1357-1370
Vanilla attaches a `PointLightComponent` directly to `LiftedPawn`.

### Properties
```
Radius=500, FalloffExponent=0.5, Brightness=4.0
LightColor=(R=96, G=128, B=255)  -- purple/blue
CastShadows=False, CastStaticShadows=False, CastDynamicShadows=False
```

### Vanilla Behavior
- Light brightness is modulated by phase-in/out alpha (0->1 during intro, 1->0 during outro)
- Attached via `LiftedPawn.AttachComponent(PhaselockLight)`
- Detached via `PhaselockLight.DetachFromAny()`

### Our Implementation
Construct PointLightComponent, set properties, attach to target on activate, detach on cleanup.

---

## Vanilla Bubble FX (TODO)

Three-phase emitter system spawned as `WillowEmitter` children of the phaselocked pawn:

### Phase 1: FadeIn (0.2s)
- `BubbleFXParticleSystem_FadeIn`
- Spawned at `LiftEndLocation`, scaled by `Mesh.Bounds.SphereRadius / 66.7`

### Phase 2: Loop (full lock duration)
- `BubbleFXParticleSystem` (main looping bubble)
- Float param `PhaselockLifeTime` set to duration
- Float param `SphereCollapse` animated from 0 to 0.75 over `CollapseDuration=2.0s`
  (creates the visual collapse as phaselock expires)
- `LifeSpan` set to `StateDuration + 0.2`

### Phase 3: FadeOut (0.2s overlap)
- `BubbleFXParticleSystem_FadeOut`
- Replaces loop emitter

### Key Detail
All bubble emitters are `WillowEmitter` (extends `Emitter`), NOT `ParticleSystemComponent`.
They're spawned as Actors (`Spawn()`), not constructed as components.
Scaled dynamically per-enemy using mesh bounds.

---

## UE3 Animation API Reference

### AnimNodeSlot (base for AnimNodeSpecialMoveBlend)
- `PlayCustomAnim(AnimName, Rate, BlendIn, BlendOut, bLooping, bOverride, StartTime, EndTime)`
- `StopCustomAnim(BlendOutTime)` -- blend out and return to base animation
- `GetCustomAnimNodeSeq()` -- returns the underlying `AnimNodeSequence`

### AnimNodeSequence
- `SetPosition(NewTime, bFireNotifies)` -- seek to a specific time
- `CurrentTime` -- current playback position

### SpecialMoveComponent (used for 3P, NOT for 1P)
- `Play(smd)` -- plays on 3P mesh (pawn.Mesh), invisible in first person
- `GetSMNode()` -- returns the AnimNodeSpecialMoveBlend

---

## Sound Effects

### Discovery Process

Vanilla Phaselock sounds are **not** baked into animations or behaviors:
- All phaselock AnimSequences (`Phase_Lock_Lift`, `PhaseLock_Loop`, etc.) have **zero AkEvent notifies**
- All `SpecialMove_PhaseLock` instances have **empty** StartBehaviors/StopBehaviors
- `LiftActionSkill` has no AkEvent-typed properties (only a `StopSound` function)
- Sounds are triggered by **native C++ code** inside `LiftActionSkill` (not visible in UC extraction)

Since Telekinesis blocks `ServerStartActionSkill` entirely, **no vanilla sounds fire**.
We trigger them manually via `pawn.PlayAkEvent()`.

### Available AkEvents (from `Ake_FX_Player_Siren`)

| AkEvent | Description | Used |
|---------|-------------|------|
| `Ak_Play_FX_Player_Phaselock_Activate` | ~0.55s windup then whoosh peak | Grab windup |
| `Ak_Play_FX_Player_Phaselock_Loop` | Sustained hum | Hold loop |
| `Ak_Play_FX_Player_Phaselock_Collapse` | Release pop (also stops Loop) | Release |
| `Ak_Play_FX_Player_Phaselock_Miss` | Fizzle/miss | Not used (wrong sound) |
| `Ak_Play_FX_Maya_Phaselock_False_Start` | Failed activation | Fizzle feedback |
| `Ak_Play_FX_Maya_Ruin` | Ruin augment explosion | Not used |
| `Ak_Play_FX_Player_Siren_Scorn_Fire` | Scorn orb launch | Not used |
| `Ak_Play_FX_Player_Siren_Scorn_Orb_Bolt` | Scorn bolt impact | Not used |
| `Ak_Play_FX_Player_Siren_Scorn_Orb_lp` | Scorn orb loop | Not used |
| `Ak_Play_FX_Siren_CloudKill` | Cloud Kill proc | Not used |
| `Ak_Play_VOBD_Siren_PL_Start_Action_Skill` | Maya voice line on cast | Cast voice |

Enemy reaction voice lines also exist (`Ak_Play_VOBD_*_React_ASkill_Siren`) but are
triggered by the enemies' own AI, not by our code.

### Sound Timing Implementation

The Activate sound has a ~550ms buildup before its whoosh peak. The peak must align
with the grab moment for satisfying feel. Timing logic:

- **Windup >= peak offset**: sound starts at `grab_time - peak_offset` (delayed after F press)
- **Windup < peak offset**: sound starts immediately on F press (can't start earlier)
- **Windup = 0**: windup sound skipped entirely, only Loop + Collapse play

Peak offset is configurable via the "Sound Peak Offset" slider (default 55 = 0.55s).

**Wwise limitation**: AkEvents do not support seeking/offset — playback always starts
from the beginning. For short windups, the peak won't perfectly align, but the buildup
is still audible.

### Looping sounds

Wwise looping sounds (like Loop) are stopped by playing a corresponding "stop" event.
The Collapse event naturally stops the Loop. `pawn.StopSounds()` is a nuclear fallback
that stops ALL sounds on the pawn.

### Release Animation & Fade-Out System

On release, the grab animation plays in **reverse** from the freeze point back to frame 0.
`PlayCustomAnim`'s Rate parameter does NOT visually reverse the animation. Instead:
```python
node.PlayCustomAnim("Phase_Lock_Lift", 0.001, 0.0, 0.2, False, True, freeze)
seq = node.GetCustomAnimNodeSeq()
seq.Rate = -1.0  # set negative rate on underlying AnimNodeSequence
seq.SetPosition(freeze, False)  # start from freeze point
```
The hand retracts back to idle over ~0.4s, paired with the Collapse sound.

**All visual effects fade out over 0.5s** via the `_fading_fx` system — a list of
`(kind, obj, end_time)` tuples processed at the top of `PlayerTick` BEFORE the `_active`
check (so fades continue running after cleanup completes):

| Kind | Object | Fade Behavior |
|------|--------|---------------|
| `'psc'` | Hand PSC | Scale interpolates from captured dynamic scale → 0.0 (no snap to full size) |
| `'light'` | Point light | Brightness interpolates from 4.0 → 0.0 |
| `'tattoo'` | Arms material | `p_EnablePowerEmissive` interpolates from 1.0 → 0.0 |

At fade end, each object is fully cleaned up (detached, deactivated, parameter reset).

**Release burst was removed** — replaying flash/suction emitters on release caused shared
template corruption (see Shared Template Corruption above). The fade-out provides smooth
visual feedback without template modification.

**Throw burst FX** — at throw apex (force→recovery transition), a separate PSC is spawned
on the hand socket using the same template. It plays the one-shot suction/flash emitters
and is queued into `_fading_fx` for cleanup after 0.3s.

### Throw (M1 / Left Click)

Left click while holding throws the held pawn in the look direction. Uses the `Melee`
animation with `bNoNotifies=True` to suppress the melee sound effect.

#### Throw Timeline

The throw has three phases, all controlled by sliders:

```
|--- windup ---|--- force ---|--- recovery ---|
     20%            (auto)         50%
```

- **Throw Duration slider** = force application time (default 28 = 0.28s)
- **Total time** = `force_time / (1 - windup% - recovery%)`
- Default: `0.28 / (1 - 0.20 - 0.50) = 0.93s` total

1. **Windup** (`_throw_pending`): Melee anim plays, spring still holds the enemy,
   but the spring target is offset **backward** along the throw direction. Pullback
   uses sine ease-out (`sin(progress * π/2)`) — fast initial pull that decelerates
   and hangs at the peak. Pullback distance = 30% of capture distance.
2. **Force** (`_throwing`): Directional acceleration applied each tick. Gravity set
   to zero so the pawn doesn't sag. Work-based drain depletes charge. Burst FX fires
   at the end (throw apex). Release on charge depletion or duration expiry.
3. **Recovery**: Force stops, animation blends out (hand returns to neutral).
   No further force applied — pawn flies on momentum.

#### Animation Speed Scaling

The melee animation (0.7s at 1.0× speed) is scaled to fill the full throw timeline:
```
rate = MELEE_ANIM_DURATION / total_time  (0.7 / total)
blend_in = windup_time    (seamless hold→throw transition)
blend_out = recovery_time (smooth return to idle)
```
Default: `0.7 / 0.93 = 0.75×` speed.

#### Throw Sound Scheduling

The release sound (`Ak_Play_FX_Player_Phaselock_Collapse`) is scheduled via timestamp
to fire before the throw apex. Configurable via "Sound Peak Offset" slider (default
1000ms). The sound fires at `apex_time - offset`, clamped to the M1 press time. If
the offset exceeds the available time, the sound fires immediately on M1 press (during
windup).

#### Force & Drain

Force scales with remaining charge: `throw_force * max(_charge, 0.3)` — a minimum 30%
floor ensures throws always have meaningful force even at low charge. Work-based drain
(`mass * |power| * factor * dt`) depletes charge proportionally to actual work done.

`_grab_blend_node` is set to `None` after the throw animation starts so that cleanup
doesn't play the reverse grab animation on top of the throw.

### Fizzle (Unstaggerable Target, Fallback Off)

When "Fallback to Phaselock" is disabled and the target can't be ragdolled
(Goliaths, Constructors, etc.), we play fizzle feedback without involving vanilla:

1. **`Phase_Lock_Fail`** animation on 1P arms via `PlayCustomAnim` (0.1s blend out)
2. **`FirstPersonParticleSystem_Fizzled`** template spawned on `LilithMeleeFX` socket
   — vanilla's own fizzle particle FX (read from `LiftActionSkill` defaults)
3. **`Ak_Play_FX_Maya_Phaselock_False_Start`** sound (failed activation)

No cooldown consumed (`ServerStartActionSkill` returns `Block`). The PSC is a one-shot
system that cleans itself up — no fade management needed.

### Activation Cost

To prevent infinite re-grab exploit (grab→release→grab with no charge loss), each
activation deducts a configurable percentage of total charge. Default: 20%.
Configurable via the "Activation Cost %" slider in mod options.

### Diagnostic Scripts

Located in `scripts/` (set `pyexec_root` in `unrealsdk.user.toml` to point there):

- **`tk_diag_sounds.py`** — dumps all AkEvent references related to Phaselock.
  Checks AnimSequence notifies, SpecialMove behaviors, LiftActionSkill properties,
  and all loaded AkEvent objects matching phaselock/siren/lift keywords.
  Output written to `scripts/tk_diag_sounds_output.txt`.

- **`tk_diag_play_sounds.py`** — audition tool for testing AkEvents.
  Run `pyexec tk_diag_play_sounds.py`, then `py S(0)` through `py S(10)` to play each
  sound individually. `py X()` to stop looping sounds. Uses `builtins` to persist
  the `S()` and `X()` functions across `py` commands.

---

## Spring-Damper Physics & Mass Normalization

### The Problem
`mesh.AddForce(vec)` applies the force vector to **every body** in the skeleton.
A ragdoll with N bodies and total mass M gets effective acceleration = `F * N / M`.
Light enemies (Rakks: 12 kg, 24 bodies) accelerated ~20x faster than humanoids
(~190 kg, ~20 bodies) under the same spring constant, creating a feedback loop:
huge velocity → damping overcorrects → bigger force → instant explosion.

### Failed Approaches
- **Per-bone AddForce**: `mesh.AddForce(vec, pos, boneName)` — bone name parameter
  had no effect. Force still applied to all bodies or was silently ignored.
- **Per-bone AddImpulse**: `mesh.AddImpulse(vec, loc, boneName, bVelChange)` — also
  had no effect, despite being the exact API that works in ImpactStagger. The difference
  is ImpactStagger applies impulses once (in SetupRagdollPhysics POST), while we apply
  every tick. Reason unknown — may be a PhysX limitation on continuous per-bone impulses.
- **bVelChange=True**: Same result — no movement at all.
- **Constructed vs live structs**: Tried using `pawn.Velocity` as temp vector (ImpactStagger
  pattern) vs `unrealsdk.make_struct("Vector")`. No difference.

### Working Solution
Compute desired acceleration (mass-independent), then convert to per-body force:
```
desired_accel = k * displacement - 2*sqrt(k) * velocity    (critically damped)
per_body_force = desired_accel * M_total / N_bodies
mesh.AddForce(per_body_force)   → applied to all N bodies
effective_force = per_body_force * N = desired_accel * M_total
actual_accel = effective_force / M_total = desired_accel     ✓
```

### Velocity Averaging (Critical for Non-Humanoids)
The damping term uses the ragdoll's velocity to oppose motion. Using the **root body's**
velocity alone causes a feedback loop on light enemies (Rakks, skags, primal beasts):
individual body velocities diverge wildly due to joint constraints, the root body
oscillates/reverses direction each frame, and the damping term amplifies instead of
dampening (acceleration climbs: 10k → 21k → 50k while displacement shrinks).

**Fix**: Average `GetUnrealWorldVelocity()` across **all bodies** in the
`PhysicsAssetInstance`. The average cancels out per-body jitter, giving a velocity
that represents the ragdoll's actual bulk motion. Humanoids work with root-only
because their bodies are heavy (~9.5 kg each) and move cohesively; light enemies
(~0.5 kg/body) need the average.

### Mass Reading
PhysX body masses are only available when ragdolled (`PhysicsAssetInstance` populated).
Read in `_grab_moment()` after `Stagger()`, using `GetBodyMass()` per body (proven in
ImpactStagger). Cached in `_grab_mass` (total) and `_grab_body_count`.

### Release Recovery
`StaggerEndRagdollTime` set to `now + 2.0` on release (was 0.3s). Short values caused
instant recovery animations while the pawn was still airborne. 2s gives time to fall
and settle. `StaggeredRestVelocity` still gates recovery until actually at rest.

### Work-Based Charge Drain (Force × Distance)
In addition to the base drain rate (configurable, can be 0 — see below), charge drains
proportionally to the **work done** moving the enemy (dot product of acceleration and velocity):
```
power = a · v  (dot product — no sqrt needed)
work_drain = mass * |power| * force_drain_rate * 1e-10 * duration_mult * dt
```
Smooth orbiting (a ⊥ v) costs nearly zero. Violent acceleration costs maximum.
Heavier enemies cost more. All drain rates scaled by Phaselock duration skills.
Configurable via the "Force Drain Rate" slider (default 20, range 0-200, 0=disabled).

### Base Drain Rate Can Be Zero
The "Charge Drain Rate" slider minimum changed from 1 to 0, allowing charge to drain
**only** from force usage (force-proportional drain above). With both drain rates at 0,
charge never depletes (hold indefinitely).

### Hold Dead Targets
Optional toggle (default on). When a held pawn dies, `_target_dead` flag is set and
the spring keeps applying forces to the dead ragdoll. Release on F press, charge
depletion, or pawn despawn (WeakPointer expiry). Charge still drains normally.

---

## Future: Directional Hand Movement (Shelved)

Planned: subtly move the Siren's left hand in the direction of the applied force.

**SkelControl approach**: `LeftHandIK` (WillowSkelControlHandPlacement) exists on the
player mesh at strength 0. `EnableHandIK(0, True)` + `SetSkelControlStrength(1.0, 0.0)`
was tested but had no visible effect. May need different activation or a different control.

**Animation scrubbing fallback**: Vary `SetPosition()` slightly (0.35-0.45) based on
force direction. Limited to forward/back but requires zero setup.
