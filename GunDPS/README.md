# Gun DPS

Displays a DPS estimate for weapons, shown as a HUD notification whenever you view a weapon's item card. The formula can be customised if you know how to write Python.

Crit DPS is shown when the weapon's parts give it a crit bonus. Weapons that deal pure splash damage (e.g. rocket launchers) hide the crit line since splash cannot crit.

If the weapon has a BehaviorProviderDefinition (BPD) anywhere - on the weapon type, projectile, or any part - a `(!)` marker is appended to the DPS numbers. This warns that the weapon may have special mechanics (free shots, ammo return, bullet splitting, etc.) that the formula cannot account for.

Weapons with a shot cost of 0 (infinite magazine) are handled correctly - reload time is ignored and DPS simplifies to `damage * projectiles * fire_rate`.

## Crit Formula

Crit multiplier uses the attribute formula from bl2.parts:

```
crit_mult = (2 + PreAdd) * (1 + PosScale) / (1 - NegScale) + PostAdd
```

Positive and negative Scale bonuses from weapon parts are bucketed separately: positive values multiply up in the numerator, negative values divide in the denominator. This matches how the game engine resolves all attributes internally.

## Options

- **Display Duration** - How long the notification stays on screen (1-10 seconds).

## Custom Formulas

The default formula is:

```
single_shot_damage = damage * projectiles
shots_per_mag      = mag_size / shot_cost
dps = (single_shot_damage * fire_rate * shots_per_mag)
    / (fire_rate * reload_time + shots_per_mag)
```

To use your own formula, edit the `calc_dps()` function in `__init__.py`. It receives a stats dict from `get_weapon_stats()` and should return a `float` or `None`.

### Available Stats

The stats dict contains resolved attribute values for the weapon being viewed:

| Key | Type | Description |
|-----|------|-------------|
| `damage` | float | Card damage per projectile |
| `fire_interval` | float | Seconds between shots |
| `fire_rate` | float | Shots per second (derived: `1 / fire_interval`) |
| `mag_size` | float | Magazine capacity |
| `reload_time` | float | Reload time in seconds (set to 0 when `shot_cost` is 0) |
| `projectiles` | float | Projectiles per shot (pellet count) |
| `shot_cost` | float | Ammo consumed per shot (0 means infinite mag - see above) |
| `crit_mult` | float | Crit multiplier from the bl2.parts formula (see above) |
| `has_bpd` | bool | True if the weapon has any BPD (weapon type, projectile, or parts) |
| `pure_splash` | bool | True if ALL card damage is delivered as splash (e.g. rockets) |
| `spread` | float | Weapon spread (accuracy) |
| `melee_damage` | float | Melee override damage |
| `status_effect_damage` | float | Elemental DoT damage |
| `status_effect_chance` | float | Elemental proc chance modifier |
| `burst_count` | float | Shots per burst (burst-fire weapons) |
| `burst_interval` | float | Delay between bursts |
| `extra_shot_chance` | float | Chance to fire an extra shot |
| `projectile_speed` | float | Projectile speed multiplier |
| `ricochets` | float | Additional ricochet count |
| `equip_time` | float | Weapon swap-in time |
| `zoom_fov` | float | ADS zoom FOV |
| `knockback` | float | Knockback force |

### Example: Custom Formula

```python
def calc_dps(stats: dict[str, float | bool]) -> float | None:
    fire_rate = stats["fire_rate"]
    mag_size = stats["mag_size"]
    shot_cost = stats["shot_cost"]
    reload_time = stats["reload_time"]
    damage = stats["damage"]
    projectile_count = stats["projectiles"]
    extra_shot_chance = stats["extra_shot_chance"]

    if fire_rate <= 0 or mag_size <= 0:
        return None

    shots_per_mag = mag_size / shot_cost
    effective_projectiles = max(projectile_count, 1.0) * (1.0 + extra_shot_chance)
    single_shot_damage = damage * effective_projectiles

    time_per_mag_cycle = fire_rate * reload_time + shots_per_mag
    if time_per_mag_cycle <= 0:
        return None

    return (single_shot_damage * fire_rate * shots_per_mag) / time_per_mag_cycle
```

To add new attributes, add their `AttributeDefinition` path to the `WEAPON_ATTRIBUTES` dict. You can find all available attributes by running `scripts/dps_find_attrs.py` with `pyexec`.

## Credits

- **Lootlemon** (https://www.lootlemon.com/tools/dps-calculator) - DPS formula used as the basis for calculation.
- **bl2.parts** (https://bl2.parts/calculations/) - Attribute resolution formula showing how positive and negative Scale bonuses are applied differently. Credit to the many modders who reversed the game to document these equations.
- **Vault Hunter Hub** (https://vaulthunter.info/borderlands-tps/guides/damage-formula) - Damage and crit formula breakdowns, confirming how splash damage and crit interact.

## Planned Features

- Otion to switch to a DPS display in the item card stat bars (top stats)
- Otion to switch to a DPS display in the item card fun stats text area
