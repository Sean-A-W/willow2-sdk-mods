# Telekinesis Architecture

## Overview

Telekinesis replaces the Siren's Phaselock with a telekinesis mod for the Siren.
Lift enemies and loot toward your crosshair, held at capture distance.
Press the skill button again to drop them.

## File Structure

```
Telekinesis/
├── __init__.py        # All hooks, state, physics, options
├── fx.py              # Hand orb, tattoo glow, target light, sounds, fade
├── object_grab.py     # Loot pickup grab — targeting, physics, hold/throw
├── pyproject.toml     # SDK mod metadata
└── docs/
    ├── architecture.md  # This file
    ├── hooks.md         # Hook chain details and activation flow
    ├── physics.md       # Spring physics, gravity, charge system
    ├── animation.md     # Grab animation, orb FX, throw, sounds
    ├── pitfalls.md      # Known issues and gotchas
    └── ue3-reference.md # UE3 API reference

scripts/                   # pyexec diagnostic/test scripts (separate from mod packages)
├── diag_skel.py           # SkelControl enumeration
├── diag_duration*.py      # Phaselock duration skill diagnostics
├── diag_emitter*.py       # Particle emitter inspection
├── diag_sounds.py         # AkEvent reference dump
├── test_grabanim*.py      # Grab animation tests
├── test_handorb*.py       # Hand orb FX tests
└── ...
```

## Design Philosophy

### v1 Problem: Fighting Vanilla

The original Telekinesis had 9+ hooks trying to coexist with vanilla Phaselock:
- Blocked `LiftTarget`, `UpdateLiftedPawn`, `SpawnBubbleFX`
- Fought `IsLiftedPawnIncapacitated` stagger detection
- Required `UnStagger` hook to prevent recovery
- `ServerDeactivateSkill` hung because vanilla state was corrupted
  (LiftedPawn set + target staggered = broken cleanup)
- Subsequence augment searched for new targets on skill end

### v2 Solution: Block ServerStartActionSkill

Block `ServerStartActionSkill` entirely for Siren. Vanilla Phaselock never
runs at all — no skill activation, no target selection, no bubble FX.
We handle everything ourselves: cooldown (via pool manipulation), target
selection, stagger, spring physics, animation, and FX.

The only exception is the fizzle path: when `Fallback to Phaselock` is off
and the target is unstaggerable, we let vanilla activate but block
`PhaseLockTarget` and call `FizzleOut()` — vanilla plays the miss animation
and handles cooldown reset naturally.

## State

```
_active              bool        Currently holding an enemy
_target_wp           WeakPointer Held WillowAIPawn (weak ref — safe on menu exit)
_target_uid          str         PathName of held pawn
_capture_dist        float       Lock distance from player eye to target
_charge              float       0.0–1.0, drains while holding
_activation_charge   float       Charge fraction for next activation (1.0 or partial)
_grab_blend_node                 AnimNodeSpecialMoveBlend for the grab animation
_grab_pending        bool        True during windup — anim plays but no forces yet
_grab_bone           str         Bone name to apply force to ('' = whole mesh)
_grab_mass           float       Total PhysX ragdoll mass
_grab_body_count     int         Number of physics bodies in ragdoll
_duration_mult       float       Drain rate multiplier from Phaselock duration skills
_throwing            bool        True during throw force phase
_throw_pending       float       TimeSeconds when throw force starts (0 = not pending)
_throw_windup_start  float       TimeSeconds when throw windup began
_throw_dir           tuple       Unit direction for throw
_throw_elapsed       float       Seconds since throw force started
_throw_sound_played  bool        True once throw release sound has fired
_throw_sound_time    float       TimeSeconds when throw sound should fire (0 = not scheduled)
_force_fizzle        bool        One-shot: let vanilla activate but block PhaseLockTarget → fizzle
_orb_intensity       float       Smoothed 0–1 drain intensity for orb scale
```

All UObject references use `WeakPointer` to prevent native crashes when
objects are destroyed (e.g. transitioning to main menu while holding).

## Activation Flow

```
Player presses skill button
  → StartActionSkill (exec, client)
  → ServerStartActionSkill (reliable server)  ← WE BLOCK HERE
      Our _on_server_start():
        - Validate Siren, not FFYL, not already holding
        - Check cooldown pool for early activation
        - Validate target (alive, WillowAIPawn, staggerable, in range)
        - _activate():
            - Compute capture distance
            - Store WeakPointers, start charge
            - Start 1P grab animation (PlayCustomAnim)
            - Start tattoo glow, voice line
            - Schedule grab moment (stagger + FX after windup delay)
        - Return Block (vanilla never runs)
```

## Release Flow

```
Player presses skill button again (or charge depletes / target dies)
  → StartActionSkillActiveAbility  ← WE INTERCEPT (re-press)
  → _release():
      - Restore gravity to 1.0
      - Set short ragdoll recovery timer (0.3s)
      - Stop grab animation
      - Call skill.EndSkill()
          → vanilla OnActionSkillEnded
          → ServerDeactivateSkill (no hang — LiftedPawn is None)
          → cooldown starts naturally
```

## Option Defaults

| Option | Default | Range | Description |
|--------|---------|-------|-------------|
| Telekinesis Enabled | True | — | Master toggle |
| Spring Strength | 50 | 10–500 | Pull force toward crosshair |
| Hold Gravity % | 0 | 0–100 | Gravity while held (0 = weightless) |
| Min Hold Distance | 200 | 100–1000 | Prevents clipping into player |
| Max Hold Distance | 2000 | 500–5000 | Maximum capture range |
| Hold Distance Override | 0 | 0–3000 | Override capture dist (0 = auto) |
| Throw Force | 5000 | 500–50000 | Acceleration for M1 throw |
| Throw Duration | 28 | 1–200 | Hundredths of sec for throw force |
| Throw Windup % | 20 | 0–80 | % of total throw time as windup |
| Throw Recovery % | 50 | 0–80 | % of total throw time as recovery (no force) |
| Charge Drain Rate | 3 | 0–20 | Base drain (hundredths/sec) |
| Force Drain Rate | 20 | 0–200 | Work-based drain factor |
| Min Activation Charge % | 5 | 1–100 | Cooldown % needed for early activation |
| Activation Cost % | 20 | 0–100 | Charge deducted on each activation |
| Hold Dead Targets | True | — | Keep holding dead ragdolls |
| Fallback to Phaselock | True | — | Vanilla Phaselock for unstaggerable targets |
| **FX Settings** | | | |
| Enable Sounds | True | — | Phaselock sound effects |
| Sound Peak Offset | 55 | 0–100 | Windup sound alignment (hundredths) |
| Throw Sound Offset | 1000 | 100–2000 | Throw release sound lead time (ms) |
| Grab Anim Freeze Point | 40 | 0–87 | Hand pose hold point (hundredths) |
| Resume Hand Anim on Release | True | — | Play reverse anim on release |
| Orb Min Scale % | 50 | 0–100 | Orb size when idle |
| Orb Max Scale % | 150 | 100–300 | Orb size at peak drain |
| Orb Responsiveness | 80 | 10–200 | How fast orb reacts (tenths) |
| Orb Drain Sensitivity | 80 | 10–500 | Drain needed for full orb (ten-thousandths) |
| Log to Console | False | — | Debug logging via `[TK]` prefix |
