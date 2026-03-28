# Gun DPS

Displays a DPS estimate for weapons, shown as a HUD notification whenever you view a weapon's item card. The formula can be customised if you know how to write Python.

## How It Works

**DPS** is calculated from card damage, fire rate, mag size, shot cost, and reload time. Splash damage is detected from `Behavior_Explode` instances in the weapon's BPDs and added on top of impact damage.

**Crit DPS** is shown when the weapon's parts give it a crit bonus. Only the impact portion of damage can crit - splash damage cannot. Weapons that deal pure splash (e.g. rocket launchers) hide the crit line entirely.

**`(!)` warning** appears when the mod detects unknown DPS-affecting behaviors in the weapon's BPDs (free shots, ammo return, bullet splitting, etc.). Cosmetic behaviors (visuals, zoom, elemental effects) are filtered out. Weapons with a `CustomFiringModeDefinition` on any part are also flagged since these indicate non-standard projectile behaviour.

**Infinite magazine** weapons (shot cost of 0) are handled correctly - reload time is ignored and DPS simplifies to `damage * projectiles * fire_rate`.

## Crit Formula

Crit multiplier uses the attribute formula from bl2.parts:

```
crit_mult = (2 + PreAdd) * (1 + PosScale) / (1 - NegScale) + PostAdd
```

Positive and negative Scale bonuses from weapon parts are bucketed separately: positive values multiply up in the numerator, negative values divide in the denominator.

## Splash Damage

Splash scale is read at runtime from `Behavior_Explode.DamageFormula.BaseValueScaleConstant` in the weapon's BPDs. For example, a Maliwan pistol has a scale of 0.8 (80% of card damage as additional splash), while a Torgue pistol has 1.0 (100%).

Total DPS and Crit DPS account for this:

```
total_dps = impact_dps + splash_dps
crit_dps  = impact_dps * crit_mult + splash_dps
```

## Options

- **Display Duration** - How long the notification stays on screen (1-10 seconds).
- **Assume Vanilla BPDs** (off by default) - Applies known corrections for unmodded weapon archetypes. Currently handles:
  - Vladof launchers: effective mag size x1.5 (every 3rd shot is free)
  - Torgue AR barrels: treated as pure splash (grenades, cannot crit)

  Disable this if you have mods that rework weapon archetypes.

## Custom Formulas

The default formula is:

```
impact_dps = (damage * projectiles * fire_rate * shots_per_mag)
           / (fire_rate * reload_time + shots_per_mag)
splash_dps = impact_dps * splash_scale
```

To use your own formula, edit the `calc_dps()` function in `__init__.py`. It receives a stats dict from `get_weapon_stats()` and should return a `(impact_dps, splash_dps)` tuple or `None`.

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
| `shot_cost` | float | Ammo consumed per shot (0 means infinite mag) |
| `crit_mult` | float | Crit multiplier from the bl2.parts formula |
| `splash_scale` | float | Fraction of card damage as additional splash (0.0 = none) |
| `has_bpd` | bool | True if unknown DPS-affecting behaviors were found |
| `pure_splash` | bool | True if ALL card damage is splash (e.g. rockets) |
| `spread` | float | Weapon spread (accuracy cone in degrees) |
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
def calc_dps(stats: dict[str, float | bool]) -> tuple[float, float] | None:
    """Example: penalise DPS based on weapon spread (accuracy cone in degrees).

    A tighter spread (lower value) means more of your damage actually lands.
    This applies a simple accuracy factor: 1.0 at spread 0, falling off as
    spread increases.
    """
    fire_rate = stats["fire_rate"]
    mag_size = stats["mag_size"]
    shot_cost = stats["shot_cost"]
    reload_time = stats["reload_time"]
    damage = stats["damage"]
    projectile_count = stats["projectiles"]
    spread = stats["spread"]
    splash_scale = float(stats.get("splash_scale", 0.0))

    if fire_rate <= 0 or mag_size <= 0:
        return None

    shots_per_mag = mag_size / shot_cost
    single_shot_damage = damage * max(projectile_count, 1.0)

    time_per_mag_cycle = fire_rate * reload_time + shots_per_mag
    if time_per_mag_cycle <= 0:
        return None

    raw_dps = (single_shot_damage * fire_rate * shots_per_mag) / time_per_mag_cycle

    # Scale DPS down by spread - a sniper (~0.5) keeps ~67% of its DPS,
    # a wide shotgun (~10) keeps only ~9%.
    accuracy_factor = 1.0 / (1.0 + spread)
    impact_dps = raw_dps * accuracy_factor
    splash_dps = impact_dps * splash_scale

    return impact_dps, splash_dps
```

To add new attributes, add their `AttributeDefinition` path to the `WEAPON_ATTRIBUTES` dict. You can find all available attributes by running `scripts/dps_find_attrs.py` with `pyexec`.

## Debugging

Run `scripts/dps_debug_weapon.py` with `pyexec` to hook into weapon card views and print detailed info about BPDs, behaviors, fire types, and custom firing modes for every weapon you inspect.

## Credits

- **Lootlemon** (https://www.lootlemon.com/tools/dps-calculator) - DPS formula used as the basis for calculation.
- **bl2.parts** (https://bl2.parts/calculations/) - Attribute resolution formula showing how positive and negative Scale bonuses are applied differently. Credit to the many modders who reversed the game to document these equations.
- **Vault Hunter Hub** (https://vaulthunter.info/borderlands-tps/guides/damage-formula) - Damage and crit formula breakdowns, confirming how splash damage and crit interact.

## Planned Features

- Option to switch to a DPS display in the item card stat bars (top stats)
- Option to switch to a DPS display in the item card fun stats text area
