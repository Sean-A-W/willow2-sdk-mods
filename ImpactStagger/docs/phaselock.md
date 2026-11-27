# Phaselock Gravity Well (Planned Feature)

## Context
The Siren's Phaselock action skill lifts one enemy into a bubble. We want to add a gravity well effect: while the enemy is locked in the bubble, nearby ragdollable enemies get force-ragdolled and pulled toward the bubble. The Phaselocked target stays frozen (vanilla behavior preserved). Own set of sliders, separate from singularity grenades.

## Requirements (confirmed)
- Keep vanilla Phaselock behavior for the locked enemy (protected from ragdoll)
- Gravity well at locked enemy's position while in EPLS_Locked state (state 3)
- Force ragdoll + pull: standing enemies within radius get `Stagger(None)` and pulled in
- No damage on Phaselock end — enemies just fall and recover naturally
- Own sliders (not shared with singularity)
- Well origin tracks the bobbing locked enemy each frame

## UE3 Research

### LiftActionSkill (Phaselock implementation)
- **Source**: `WillowGame\Classes\LiftActionSkill.uc` (extends ActionSkill)
- **State machine** (EPhaselockState):
  - 0 = EPLS_None (not active)
  - 1 = EPLS_Intro (lifting animation, ~1.3s)
  - 2 = EPLS_Outro (fade out before release)
  - 3 = EPLS_Locked (target suspended in bubble) — **our trigger state**
  - 4 = EPLS_Ruin (held for Ruin skill)
  - 5 = EPLS_Subsequence (projectile on target death)

### Key Properties (all replicated)
- `CurrentState` (EPhaselockState) — current phase
- `LiftedPawn` (WillowPawn) — the Phaselocked target
- `LiftEndLocation` (Vector) — static suspend point
- `SkillDuration` (float) — total duration (~8.3s default)
- `bReplicatedCharmed` (bool) — if target is charmed (Thoughtlock)

### Key Functions
- `OnActionSkillTick(float DeltaTime)` — fires every frame while active
- `OnActionSkillStarted()` → `SelectTarget()` → `PhaseLockTarget()`
- `LockTarget()` — transitions to EPLS_Locked (state 3)
- `OnActionSkillEnded()` — fires when skill deactivates
- `UpdateLiftedPawn()` — sets `LiftedPawn.Location` each frame (includes bobbing)
- `IsLiftedPawnIncapacitated()` — checks `LiftedPawn.IsStaggered()` — **staggering breaks Phaselock!**

### Duration
- Lift phase: 1.3s (LiftDuration)
- Lock phase: ~7s base (LockDurationFormula) + skill tree modifiers
- Total: ~8.3s default

### Bobbing
- `LiftBobFrequency = 0.5`, `LiftBobAmplitude = 30 UU`
- `GetBobLocation()`: `LiftEndLocation.Z + sin(elapsed * freq * π) * amplitude`

## Critical: Phaselock Protection
`IsLiftedPawnIncapacitated()` checks `LiftedPawn.IsStaggered()` — if our mod staggers the locked target, it **breaks Phaselock** via `InterruptPhaseLock()`. Must skip the locked target in:
1. `on_take_damage` — early return if pawn UID matches locked target
2. `_scan_standing_enemies_near_wells` — skip in singularity well scan
3. `phaselock.scan_and_capture` — skip in phaselock well scan
4. `bowling.tick_bowling` — skip in bowling scan

## Implementation Plan

### New File: `phaselock.py`

#### State
```python
# Only one Phaselock at a time (one Siren, one skill)
ACTIVE_PHASELOCK: list | None = None  # [locked_pawn_uid, set_of_pulled_uids, WeakPointer(skill)]
PHASELOCKED_TARGET_UID: str | None = None
```

#### Sliders
| Slider | Default | Range | Description |
|--------|---------|-------|-------------|
| Phaselock Gravity Well (Bool) | True | — | Master toggle |
| Phaselock Pull Strength | 70 | 0–5000 | Continuous pull force per frame |
| Phaselock Catch Radius | 1000 | 100–3000 | Radius to capture standing enemies |
| Phaselock Escape Radius % | 120 | 100–500 | % of catch radius for eviction |
| Phaselock Initial Pull Force | 2000 | 0–10000 | One-shot impulse on first capture |

#### Functions
- `is_phaselocked(uid) -> bool` — protection check
- `start_well(skill, locked_uid)` — initialize on state 3
- `end_well()` — cleanup on state change or skill end
- `tick_well(current_time, ragdolled_pawns, log_fn)` — per-frame pull (mirrors `singularity.tick_wells`)
  - Read `skill.LiftedPawn.Location` for well center (tracks bobbing)
  - `AddRadialForce` with negative strength, mass-equalized
  - Extend `StaggerEndRagdollTime` to prevent recovery
  - Evict beyond escape radius
- `scan_and_capture(current_time, ragdolled_pawns, log_fn, can_ragdoll_fn, pawn_uid_fn, pending_impulse)` — throttled `find_all` scan (0.25s), stagger standing enemies within catch radius
- `get_options() -> list`

### Changes to `__init__.py`

#### New Hooks
1. **`LiftActionSkill:OnActionSkillTick` (PRE)** — detect state 3, call `start_well()` on first locked tick, `end_well()` on state change
   - Skip if `phaselock.phaselock_well_enabled.value` is False
   - Skip charmed targets (`skill.bReplicatedCharmed`)
   - Skip if `multiplayer.is_client()`

2. **`LiftActionSkill:OnActionSkillEnded` (PRE)** — call `end_well()` for cleanup

#### PlayerTick Additions
After singularity wells block:
```python
if phaselock.ACTIVE_PHASELOCK is not None and phaselock.phaselock_well_enabled.value:
    phaselock.tick_well(current_time, RAGDOLLED_PAWNS, log)
    if phaselock.ACTIVE_PHASELOCK is not None:
        if current_time - _phaselock_scan_time >= 0.25:
            _phaselock_scan_time = current_time
            phaselock.scan_and_capture(current_time, RAGDOLLED_PAWNS, log, can_ragdoll, _pawn_uid, PENDING_IMPULSE)
```

#### `need_time` Gate
```python
need_time = singularity.ACTIVE_WELLS or bowling.bowling_enabled.value or phaselock.ACTIVE_PHASELOCK is not None
```

#### Protection Checks
TakeDamage:
```python
if phaselock.is_phaselocked(uid):
    IMPULSE_HANDLED.add(pid)
    return
```

Well scan + bowling: `if phaselock.is_phaselocked(uid): continue`

#### Menu
```python
phaselock_menu = NestedOption("Phaselock Gravity Well", children=phaselock.get_options(), ...)
```

### bowling.py Changes
Add phaselock protection check (skip locked target UID).

## Edge Cases
- **Charmed (Thoughtlock)**: target is mind-controlled, not in bubble — skip well creation
- **Subsequence**: state leaves 3 → well ends → new lock → new well
- **Ruin**: state becomes 4 → well ends (correct)
- **Target killed**: `InterruptPhaseLock()` → `OnActionSkillEnded()` → cleanup fires
- **Not playing Siren**: hooks never fire
- **Multiplayer**: host only (`is_client()` guard)

## State Flow
1. Siren Phaselocks enemy → state 1 (EPLS_Intro), 1.3s lift animation
2. `LockTarget()` → state 3 (EPLS_Locked)
3. `OnActionSkillTick` PRE: detect state 3, `start_well(skill, uid)`
4. PlayerTick each frame: `tick_well()` reads `skill.LiftedPawn.Location`, applies `AddRadialForce`
5. PlayerTick every 0.25s: `scan_and_capture()` finds standing enemies, `Stagger(None)` + registers
6. Phaselock ends → `OnActionSkillEnded` PRE: `end_well()`. Pulled enemies recover naturally.

## Testing
1. Play as Siren, Phaselock an enemy — verify vanilla behavior preserved
2. Nearby enemies should get ragdolled and pulled toward bubble
3. Kill Phaselocked enemy — well ends, pulled enemies recover
4. Let Phaselock expire — same cleanup
5. Toggle off — no pulling behavior
6. No crash on Phaselock start/end
7. Thoughtlock augment — should NOT create well
8. Enable logging, check `[IS-PL]` messages
