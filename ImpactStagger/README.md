# Impact Stagger

Overhauls how ragdolls get triggered during gameplay. Enemies ragdoll when hit with powerful attacks, take impact damage from collisions, and get pulled by singularity gravity wells.

![](https://github.com/Sean-A-W/media/blob/main/impactStaggerExample1.gif)
![](https://github.com/Sean-A-W/media/blob/main/impactStaggerExample2.gif)
![](https://github.com/Sean-A-W/media/blob/main/impactStaggerExample3.gif)
![](https://github.com/Sean-A-W/media/blob/main/impactStaggerExample4.gif)

## Installation

Requires [willow2-mod-manager](https://github.com/bl-sdk/willow2-mod-manager). Download `ImpactStagger.sdkmod` from [Releases](../../releases) and drop it into your `sdk_mods/` folder.

## Features

### Ragdoll on Hit
Enemies ragdoll when hit hard enough, replacing the vanilla flinch/stagger animation. Multi-pellet weapons (shotguns) and rapid-fire launchers (Badaboom) accumulate momentum within a configurable time window before deciding. Per-bone impulse distribution means explosions launch the whole body while bullets push individual limbs.

### Impact Damage
Ragdolled enemies take damage when they slam into surfaces. Damage scales with collision speed and is proportional to max health. Crit-zone bones (head, eye, mouth depending on enemy type) deal full damage; non-crit bones deal reduced damage at a higher speed threshold.

### Singularity Gravity Wells
Singularity grenades create a continuous gravity well that pulls ragdolled enemies for the full duration, rather than applying a one-time impulse. Enemies within the catch radius get adopted by the well even if they weren't part of the original pull. Enemies that escape beyond the escape radius break free.

### Per-Source Force Tuning
Every damage source type has its own force multiplier: bullets, grenades, rockets, melee, skills, vehicles, shields, and singularities. Rockets default to 5x because the engine gives them low base momentum.

### Ragdoll Bowling
Ragdolled enemies that collide with standing enemies knock them over using inelastic collision physics - a heavy nomad barely slows down hitting a psycho, but a light psycho bouncing off a nomad loses most of its speed. Uses the engine's native Touch collision detection for zero-cost event-driven detection.

### Fast Recovery
Enemies get up faster than vanilla with speed-scaled recovery animations. Healthier enemies recover faster. Re-hitting a recovering enemy re-staggers them.

### Corrected Physics Masses
Each enemy type has a hand-tuned mass based on their size and build. Masses are baked into ragdoll bones on stagger so impulse calculations are consistent regardless of the engine's default values.

## Configuration

All settings are in the mod menu. Here are the key groups:

### Core Settings

| Setting | Default | Description |
|---|---|---|
| Impulse Scale % | 100 | Global force multiplier for all ragdoll impulses |

### Force Per Source

Per-weapon-type force multipliers. 0 = no force, 100 = engine default.

| Setting | Default |
|---|---|
| Bullet Force % | 300 |
| Grenade Force % | 100 |
| Rocket Force % | 500 |
| Melee / Skill / Vehicle / Shield / Other Force % | 100 |
| Singularity Initial Force % | 5 |
| Singularity Pull Strength | 70 |
| Singularity Catch Radius | 1000 |
| Singularity Escape Radius % | 120 |
| Singularity Time to Detonation | 2.2s |

### Impact Damage

| Setting | Default | Description |
|---|---|---|
| Impact Damage % | 75 | Max-health % dealt per impact at reference speed (10000) |
| Min Impact Velocity | 1200 | Below this speed, no impact damage |
| Non-Crit Min Velocity | 1800 | Higher threshold for non-crit bone impacts |
| Non-Crit Damage % | 50 | Damage reduction for non-crit bones |
| Impact Cooldown | 0 | Min time between damage ticks per enemy |
| Crit Bones Only | false | Only deal damage on crit-zone bone impacts |

### Ragdoll Physics

| Setting | Default | Description |
|---|---|---|
| Ragdoll Friction | 1.2 | Higher = more tumble, less slide |
| Ragdoll Bounciness | 0.5 | Restitution of ragdolled enemies |
| Terminal Velocity | 0 (off) | Speed cap - hits apply less force as enemy approaches this |

### Ragdoll Bowling

| Setting | Default | Description |
|---|---|---|
| Enable Bowling | true | Toggle bowling on/off |
| Min Bowling Velocity | 30 | Minimum ragdoll speed to trigger bowling. 0 = any contact, 30 filters near-stationary ragdolls |
| Bowling Force % | 100 | Post-collision velocity multiplier. 100 = realistic inelastic collision |

### Stagger Filters

| Setting | Default | Description |
|---|---|---|
| Filter Bosses | true | Prevent boss enemies from being ragdolled |
| Filter Champions | false | Prevent champion/badass enemies from being ragdolled |

### Advanced

| Setting | Default | Description |
|---|---|---|
| Min Ragdoll Velocity | 1232 | Estimated velocity threshold to allow ragdoll |
| Damage Severity % | 0 (off) | Min damage as % of max health to allow ragdoll |
| Low Health % | 30 | Below this HP %, enemies ragdoll regardless of severity |
| Accumulation Window | 10ms | Time window to accumulate momentum from multi-projectile weapons |
| Recovery Time | 0.3s | How long enemies stay ragdolled before getting up |
| Rest Velocity | 200 | How slow the ragdoll must be before recovery starts |
| Max Per-Bone Force | 500000 | Cap per bone during per-bone explosions |
| Min Explosion Force | 0 | Force floor for explosion ragdolls |

## Architecture

For modders interested in the codebase:

| Module | Responsibility |
|---|---|
| `__init__.py` | Hooks, shared state, core impulse/ragdoll logic |
| `enemy_masses.py` | Per-enemy-type mass dict and helpers |
| `impact_damage.py` | Collision damage calculation, crit detection |
| `singularity.py` | Gravity wells, continuous pull, adoption/escape |
| `rockets.py` / `grenades.py` | Damage type detection, per-source sliders |
| `bowling.py` | Touch-based ragdoll bowling, velocity checks, hit dedup |
| `multiplayer.py` | Networking integration, settings sync, impulse broadcast |
| `object_physics.py` | Loot/prop scatter (dormant - hooks removed due to crash, options registered but inactive) |

### Hook Pipeline

1. **TakeDamage PRE** - pellet accumulation, threshold checks, impulse computation, stagger trigger
2. **HandleMomentum PRE** - blocks native knockback when TakeDamage handled it; catches non-bullet staggers
3. **SetupRagdollPhysics POST** - applies deferred impulse, bakes bone masses, sets physics properties
4. **RigidBodyCollision PRE** - applies impact damage directly (uses plain `DamageType` to avoid engine deadlock)
5. **PlayerTick PRE** - ticks singularity gravity wells, cleans up bowling state
6. **Touch PRE** (WillowAIPawn) - event-driven bowling: detects ragdoll-to-standing-pawn contact, triggers stagger + momentum transfer
7. **PlayDying PRE/POST** - forces ragdoll death with impulse for qualifying kills

## Known Issues

- Killing certain scripted pawns via impact damage can hang the game. This occurs when the pawn is bound to a Kismet sequence or scripted death state that expects a specific execution path. Ragdoll-based death bypasses this flow, causing latent actions to never resolve. Known cases: Out of Body Experience (pre-quest), End of the Line burning exploders, Boom Boom (before Big Bertha).
- Heavy enemies (Goliaths, Spiderant Emperors, etc.) cannot be staggered while alive - they lack `StaggerSettings`, so `Stagger()` silently fails. They do ragdoll on death normally.
- Tediore reload throws fire TakeDamage with zero momentum, so they don't trigger ragdolls. The mod skips all zero-momentum hits to ignore DoT ticks and status effects.

## Multiplayer

Multiplayer is supported when [networking.sdkmod](https://github.com/bl-sdk/networking) is installed. The host computes all ragdoll physics and broadcasts impulses to clients. Gameplay settings are synced from host to clients automatically. Solo play has zero networking overhead.

## Parked Features

Investigated and blocked by engine limitations.

- **Player ragdoll**: Possible via `InitRagdoll()`/`TermRagdoll()` but blocked by camera, input, and recovery challenges.
- **Hardware acceleration**: UE3 ragdolls are CPU-only (PhysX SDK 2.x, single-threaded x87). GPU PhysX only handles cloth/fluids/particles.

## Planned Features

- Phase Lock rework with custom gravity well
- Non-pawn actor interactions (loot, barrels, vehicles, projectiles)
- TPS compatibility testing
- BL3/Wonderlands port (UE4)
