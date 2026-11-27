# Default Settings (Play-Tested)

| Category | Setting | Default | Notes |
|---|---|---|---|
| **Force %** | Bullet | 300 | |
| | Grenade | 100 | Engine momentum ~70k |
| | Rocket | 500 | Engine momentum ~10k (hardcoded across all launchers) |
| | Melee/Skill/Vehicle/Shield/Other | 100 | |
| **Singularity** | Initial Force % | 5 | |
| | Pull Strength | 70 | |
| | Catch Radius | 1000 | UU; gates initial pull |
| | Escape Radius % | 120 | % of catch radius; evicts pawns that break free |
| | Time to Detonation | 22 | 2.2 seconds |
| **Thresholds** | Min Ragdoll Velocity | 1232 | Estimated velocity gate for fresh staggers |
| | Damage Severity % | 0 | Disabled; only checked for fresh staggers |
| **Physics** | Recovery Time | 3 | 0.3 seconds |
| | Rest Velocity | 200 | Speed threshold before recovery can start |
| | Friction | 12 | |
| | Bounciness | 5 | |
| **Impact Damage** | Impact Damage % | 75 | `max_health * (speed / 10000) * (scale / 100)` |
| | Min Impact Velocity | 1200 | Minimum speed for any impact damage |
| | Non-Crit Min Velocity | 1800 | Minimum speed for non-crit hit damage |
| | Non-Crit Damage % | 50 | |
| **Debug** | Log to Console | false | |
| | Log to File | false | |

## Damage Source Types

- Bullets: `WillowDmgSource_Bullet`, `_Shotgun`, `_Pistol`, `_Sniper`, `_SubMachineGun`, `_MachineGun`
- Grenades: `WillowDmgSource_Grenade`
- Rockets: `WillowDmgSource_Rocket`
- Singularity: `DamageCauser.Definition.Name` contains `'Singularity'` (DamageType=None)

## Stagger Eligibility

### Unstaggerable (bVehicleBlockingActor)
Goliaths, Foreman, LoaderSuperBadass, Constructors, Turrets

### Unstaggerable (StaggerSettings=None)
NPCs: Mordecai, TinyTina, RolandSoldier

### Scripted Pawns (skip ragdoll)
`bUsedByMatinee` or `Controller.ScriptedMoveTarget` — ragdolling these causes game hangs

### Death Override Blacklist
- `CharClass_LoaderEXP` — special PlayTechDeath sequence hangs when ragdolled
- `CharClass_BoomBoom` — vehicle Kismet script conflicts with ragdoll
