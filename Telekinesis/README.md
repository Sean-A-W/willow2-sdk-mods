# Telekinesis

Replaces the Siren's Phaselock with telekinesis. Lift enemies and loot toward your crosshair, held at capture distance. Scroll to push/pull, M1 to throw, press the skill button again to release.

## Installation

Requires [willow2-mod-manager](https://github.com/bl-sdk/willow2-mod-manager) and [uemath](https://github.com/juso40/bl2sdk_Mods). Download `Telekinesis.sdkmod` from [Releases](../../releases) and drop it into your `sdk_mods/` folder.

## Features

### Telekinetic Grab
Pressing Phaselock grabs the targeted enemy and ragdolls them. They float toward your crosshair and are held at the distance they were captured. The vanilla Phaselock code is completely bypassed — no orb, no cooldown animation, no Phaselock effect on the enemy.

### Spring Physics
The held enemy tracks your crosshair via spring-damper physics. Smooth orbiting is nearly free; violent acceleration drains charge faster. Whole-mesh mode applies acceleration to the entire ragdoll, while bone-grab mode uses a PID controller on a single bone for more natural-looking holds.

### Throw
Press M1 (left click) while holding to throw the enemy. A short windup plays, then a burst of force launches them in your look direction. Throw strength scales with remaining charge — more charge means a harder throw.

### Scroll Push/Pull
Scroll the mouse wheel while holding to push the enemy further away or pull them closer. Configurable step size and invertible direction.

### Loot Grab
When no enemy is targeted, the grab picks up nearby loot pickups instead. Pickups float toward your crosshair and can be thrown. Mission items and ECHOs are skipped by default.

### Charge System
Grabbing consumes a flat activation cost. Holding drains charge over time, with extra drain from work done (force x distance x mass). Heavier enemies and violent movement cost more. You can re-activate before the cooldown finishes — early activation gets proportionally less charge.

### Hold Dead Targets
When a held enemy dies, you keep holding their ragdoll until you release or charge runs out.

### Fallback to Phaselock
Targets that can't be ragdolled (Goliaths, Constructors, etc.) get vanilla Phaselock instead if this option is enabled.

### Visual Effects
Hand orb particle, Siren tattoo glow, target point light, and Phaselock sound effects. The orb dynamically scales based on drain intensity with a configurable pulse. All FX fade out smoothly on release.

## Configuration

All settings are in the mod menu.

### Core Settings

| Setting | Default | Description |
|---|---|---|
| Telekinesis Enabled | true | Master toggle — when off, Phaselock works normally |
| Spring Strength | 50 | Pull strength toward crosshair. Higher = snappier |
| Hold Gravity % | 100 | Gravity while held. 0 = weightless, 100 = full gravity |
| Min Hold Distance | 200 | Minimum distance — prevents clipping into player |
| Max Hold Distance | 4000 | Maximum capture range |
| Hold Distance Override | 0 | Fixed hold distance. 0 = use actual distance at capture |
| Scroll Distance Step | 100 | Push/pull per scroll notch (UU) |
| Invert Scroll | true | Swap scroll direction |

### Bone Grab PID

Controls the per-bone physics controller. Only active when a specific bone is grabbed.

| Setting | Default | Description |
|---|---|---|
| Bone Kp | 20 | Proportional gain — pull strength toward target |
| Bone Ki | 0 | Integral gain — corrects steady-state error for heavy pawns |
| Bone Kd | 50 | Derivative gain — damping. >10 = overdamped, <10 = bouncy |

### Charge & Drain

| Setting | Default | Description |
|---|---|---|
| Charge Drain Rate | 0 | Passive drain (hundredths/s). 0 = no passive drain |
| Force Drain Rate | 10 | Work-based drain. Heavier + faster = more drain |
| Min Activation Charge % | 5 | Minimum cooldown progress to activate early |
| Activation Cost % | 20 | Charge consumed per grab |

### Throw Settings

| Setting | Default | Description |
|---|---|---|
| Throw Force | 15000 | Acceleration during throw burst |
| Throw Duration | 28 | Force application time (hundredths of a second) |
| Throw Windup % | 20 | Time spent winding up before force applies |
| Throw Recovery % | 50 | Time at end where force stops (hand returning) |

### Behavior

| Setting | Default | Description |
|---|---|---|
| Hold Dead Targets | true | Keep holding ragdoll after death |
| Fallback to Phaselock | false | Use vanilla Phaselock on non-ragdollable targets |

### Loot Grab

| Setting | Default | Description |
|---|---|---|
| Enable Loot Grab | true | Grab loot pickups when no enemy targeted |
| Loot Drain % | 100 | Charge drain multiplier for loot (vs enemies) |
| Skip Mission Items | false | Skip mission items and ECHOs |

### FX Settings

| Setting | Default | Description |
|---|---|---|
| Enable Sounds | true | Play Phaselock sound effects |
| Sound Peak Offset | 55 | Windup sound timing (hundredths of a second) |
| Throw Sound Offset | 1000 | Release sound lead time (ms) |
| Grab Anim Freeze Point | 40 | Where hand animation freezes (hundredths of a second) |
| Resume Hand Anim on Release | true | Play rest of animation on release |
| Orb Min Scale % | 50 | Orb size when idle |
| Orb Max Scale % | 150 | Orb size at max drain |
| Orb Responsiveness | 80 | How quickly orb reacts to drain changes |
| Orb Drain Sensitivity | 5 | Drain needed to reach full orb size |
| Pulse Frequency | 15 | Pulse speed (tenths of Hz) |
| Pulse Magnitude % | 8 | Pulse amplitude |
| Tattoo Phase Offset | 0 | Phase offset between orb and tattoo pulse (degrees) |

## Architecture

For modders interested in the codebase:

| Module | Responsibility |
|---|---|
| `__init__.py` | Hooks, state, spring physics, throw, charge, targeting, animations |
| `fx.py` | Hand orb, tattoo glow, target light, sounds, fade management |
| `object_grab.py` | Loot pickup grab — targeting, physics init, hold/throw tick |

### Hook Pipeline

1. **ServerStartActionSkill PRE** — blocks vanilla Phaselock entirely; handles activation (target detection, stagger, state init) and release (toggle off)
2. **PlayerTick PRE** — per-frame spring physics, charge drain, throw force, FX updates, windup/sound scheduling
3. **PrevWeapon/NextWeapon PRE** — scroll wheel push/pull while holding
4. **StartFire PRE** — M1 throw; blocks ADS while holding
5. **EquipWeaponFromSlot PRE** — blocks weapon switch (number keys) while holding
6. **MeleeAttack PRE** — blocks melee while holding
7. **ThrowGrenade PRE** — blocks grenade throws while holding
8. **ShowStatusMenu PRE** — blocks inventory/menus while holding

## Known Issues

- Certain enemies (Goliaths, Constructors) lack StaggerSettings and cannot be ragdolled. Fallback to Phaselock handles these when enabled.
- Loot pickups may fall asleep in PhysX during hold — the mod detects this and re-initializes physics automatically.
- Very heavy ragdolls (e.g. loaders) require the PID integral term to reach the crosshair; with Ki=0 they may hover below target.
- Scavengers spin uncontrollably when grabbed. This is a vanilla physics issue — Scavengers use the same skeleton, physics asset, constraints, and stagger settings as regular Marauders (`Skel_BanditMarauder`, `StaggerDef_MarauderShared`), but their backpack mesh geometry self-intersects with the torso collision bodies during ragdoll. PhysX resolves the penetration with impulses that create angular momentum, causing the spin. The angular momentum persists after release. This cannot be fixed without disabling self-collision on the ragdoll, which would also disable world collision response.
