from __future__ import annotations

from typing import Any

from mods_base import build_mod, hook, get_pc, BoolOption, SliderOption
import unrealsdk
from unrealsdk.hooks import Type
from unrealsdk.unreal import UObject, WrappedStruct, BoundFunction


# --- Options ---

assume_vanilla_bpds = BoolOption(
    "Assume Vanilla BPDs",
    False,
    description=(
        "When enabled, known vanilla weapon behaviours (e.g. Vladof launcher"
        " free shots) are corrected in the DPS calculation and the (!)"
        " warning is removed."
    ),
)

display_duration = SliderOption(
    "Display Duration",
    3,
    min_value=1,
    max_value=10,
    description="How long the DPS notification stays on screen (seconds).",
)


# --- Weapon Stats ---

# Attribute paths for weapon stats. Each maps a friendly name to an
# AttributeDefinition object path that can be resolved at runtime via
# unrealsdk.find_object("AttributeDefinition", path).
WEAPON_ATTRIBUTES: dict[str, str] = {
    # Core stats used by the default DPS formula
    "damage": "D_Attributes.Weapon.WeaponDamage",
    "fire_interval": "D_Attributes.Weapon.WeaponFireInterval",
    "mag_size": "D_Attributes.Weapon.WeaponClipSize",
    "reload_time": "D_Attributes.Weapon.WeaponReloadSpeed",
    "projectiles": "D_Attributes.Weapon.WeaponProjectilesPerShot",
    "shot_cost": "D_Attributes.Weapon.WeaponShotCost",
    # Additional stats available for custom formulas
    "spread": "D_Attributes.Weapon.WeaponSpread",
    "melee_damage": "D_Attributes.Weapon.WeaponMeleeDamage",
    "status_effect_damage": "D_Attributes.Weapon.WeaponStatusEffectDamage",
    "status_effect_chance": "D_Attributes.Weapon.WeaponBaseStatusEffectChanceModifier",
    "burst_count": "D_Attributes.Weapon.WeaponAutomaticBurstCount",
    "burst_interval": "D_Attributes.Weapon.WeaponBurstInterval",
    "extra_shot_chance": "D_Attributes.Weapon.WeaponExtraShotChance",
    "projectile_speed": "D_Attributes.Weapon.WeaponProjectileSpeedMultiplier",
    "ricochets": "D_Attributes.Weapon.WeaponAdditionalRicochets",
    "equip_time": "D_Attributes.Weapon.WeaponEquipTime",
    "zoom_fov": "D_Attributes.Weapon.WeaponZoomEndFOV",
    "knockback": "D_Attributes.Weapon.WeaponKnockback",
}

_attr_cache: dict[str, UObject | None] = {}


def _find_attr(key: str) -> UObject | None:
    if key not in _attr_cache:
        try:
            _attr_cache[key] = unrealsdk.find_object("AttributeDefinition", WEAPON_ATTRIBUTES[key])
        except Exception:
            _attr_cache[key] = None
    return _attr_cache[key]


def read_weapon_stat(weapon: UObject, key: str, default: float = 0.0) -> float:
    """Read a resolved attribute value from a weapon.

    Args:
        weapon:  The WillowWeapon UObject.
        key:     A key from WEAPON_ATTRIBUTES (e.g. "damage", "fire_interval").
        default: Returned when the attribute cannot be found or read.
    """
    attr = _find_attr(key)
    if attr is None:
        return default
    try:
        val = attr.GetValue(weapon)
        return float(val[0]) if isinstance(val, (tuple, list)) else float(val)
    except Exception:
        return default


_CRIT_ATTR_PATH = "D_Attributes.GameplayAttributes.PlayerCriticalHitBonus"

_PART_SLOTS = (
    "WeaponTypeDefinition",
    "BalanceDefinition",
    "ManufacturerDefinition",
    "BodyPartDefinition",
    "GripPartDefinition",
    "BarrelPartDefinition",
    "SightPartDefinition",
    "StockPartDefinition",
    "ElementalPartDefinition",
    "Accessory1PartDefinition",
    "Accessory2PartDefinition",
    "MaterialPartDefinition",
)


def _get_weapon_crit_parts(weapon: UObject) -> tuple[float, float, float, float]:
    """Collect the weapon's crit modifiers from all parts' ExternalAttributeEffects.

    Uses the attribute formula which separates bonuses by modifier
    type and by sign for Scale modifiers (taken from bl2.parts):
        Final = (Base + PreAdd) * (1 + PosScale) / (1 - NegScale) + PostAdd

    Returns (crit_preadd, crit_pos_scale, crit_neg_scale, crit_postadd) sums.
    """
    crit_preadd = 0.0
    crit_pos_scale = 0.0
    crit_neg_scale = 0.0
    crit_postadd = 0.0
    try:
        definition_data = weapon.DefinitionData
    except Exception:
        return crit_preadd, crit_pos_scale, crit_neg_scale, crit_postadd

    for slot in _PART_SLOTS:
        try:
            part = getattr(definition_data, slot)
        except Exception:
            continue
        if part is None:
            continue
        try:
            effects = part.ExternalAttributeEffects
        except Exception:
            continue
        if effects is None:
            continue
        for effect in effects:
            try:
                attr = effect.AttributeToModify
                if attr is None:
                    continue
                if attr.PathName(attr) != _CRIT_ATTR_PATH:
                    continue
                modifier = effect.BaseModifierValue
                bonus_value = modifier.BaseValueConstant * modifier.BaseValueScaleConstant
                modifier_type = str(effect.ModifierType)
                if "PreAdd" in modifier_type:
                    crit_preadd += bonus_value
                elif "PostAdd" in modifier_type:
                    crit_postadd += bonus_value
                else:
                    # Scale (default) - positive and negative values go in
                    # different buckets per the bl2.parts formula.
                    if bonus_value >= 0:
                        crit_pos_scale += bonus_value
                    else:
                        crit_neg_scale += bonus_value  # already negative
            except Exception:
                continue
    return crit_preadd, crit_pos_scale, crit_neg_scale, crit_postadd


# Behavior class names that are known to NOT affect DPS calculations.
# These are visual, cosmetic, parameter-setting, or projectile detonation
# behaviors (splash detonation is already handled by _is_pure_splash).
_HARMLESS_BEHAVIORS: set[str] = {
    "Behavior_RunBehaviorCollection",
    "Behavior_CompareFloat",
    "Behavior_SetFloatParam",
    "Behavior_SetVectorParam",
    "Behavior_SetObjectParam",
    "Behavior_AddInstanceData",
    "Behavior_ChangeSpin",
    "Behavior_Explode",
    "ProjectileBehavior_Detonate",
}


def _get_bpd_behavior_names(bpd: UObject) -> list[str]:
    """Return class names of all behaviors inside a BPD."""
    names: list[str] = []
    try:
        sequences = bpd.BehaviorSequences
        if sequences is None:
            return names
        for seq in sequences:
            try:
                actions = seq.BehaviorData2
                if actions is None:
                    continue
                for action in actions:
                    try:
                        behavior = action.Behavior
                        if behavior is not None:
                            names.append(str(behavior.Class.Name))
                    except Exception:
                        continue
            except Exception:
                continue
    except Exception:
        pass
    return names


def _bpd_has_dps_behaviors(bpd: UObject) -> bool:
    """Return True if a BPD contains any behaviors not in the harmless set."""
    for name in _get_bpd_behavior_names(bpd):
        if name not in _HARMLESS_BEHAVIORS:
            return True
    return False


def _has_nonstandard_bpd(weapon: UObject) -> bool:
    """Return True if the weapon has a BPD with DPS-affecting behaviors.

    Scans all BPDs on the weapon (type, projectile, parts) and checks
    if any contain behaviors outside the known-harmless set.
    """
    try:
        definition_data = weapon.DefinitionData
    except Exception:
        return False

    # Check the weapon type definition
    try:
        weapon_type = definition_data.WeaponTypeDefinition
        if weapon_type is not None:
            bpd = weapon_type.BehaviorProviderDefinition
            if bpd is not None and _bpd_has_dps_behaviors(bpd):
                return True
            # Check the projectile definition
            firing_mode = weapon_type.DefaultFiringModeDefinition
            if firing_mode is not None:
                projectile_def = firing_mode.ProjectileDefinition
                if projectile_def is not None:
                    proj_bpd = projectile_def.BehaviorProviderDefinition
                    if proj_bpd is not None and _bpd_has_dps_behaviors(proj_bpd):
                        return True
    except Exception:
        pass

    # Check all part BPDs and custom firing modes
    for slot in _PART_SLOTS:
        try:
            part = getattr(definition_data, slot)
            if part is None:
                continue
            bpd = part.BehaviorProviderDefinition
            if bpd is not None and _bpd_has_dps_behaviors(bpd):
                return True
            # Normal weapons don't have custom firing modes on parts.
            # If one exists, the weapon likely has special projectile
            # behaviour (splitting, grenades, etc.) we can't calculate.
            try:
                if part.CustomFiringModeDefinition is not None:
                    return True
            except Exception:
                pass
        except Exception:
            continue

    return False


def _is_pure_splash(weapon: UObject) -> bool:
    """Return True if the weapon delivers all its card damage as splash.

    Rocket-type weapons deal all damage through HurtRadius (splash) which
    cannot crit.  Bullet/hitscan weapons deliver card damage as a direct hit
    (crittable) even if they also have splash on top. This is important
    information to calculate valid crit damage.
    """
    try:
        definition_data = weapon.DefinitionData
        weapon_type = definition_data.WeaponTypeDefinition
        if weapon_type is None:
            return False
        # Check FireType on the default firing mode - EWWFT_Rocket == 2
        firing_mode = weapon_type.DefaultFiringModeDefinition
        if firing_mode is not None:
            fire_type = str(firing_mode.FireType)
            if "Rocket" in fire_type or fire_type == "2":
                return True
        # Fallback: check weapon type name for launcher types
        type_name = str(weapon_type.Name) if weapon_type.Name is not None else ""
        if "Launcher" in type_name:
            return True
    except Exception:
        pass
    return False


# --- Vanilla BPD assumptions (only applied when the option is enabled) ---

# Known vanilla manufacturer+type combos and their effective mag size
# multipliers.
_VANILLA_MAG_SIZE_OVERRIDES: dict[tuple[str, str], float] = {
    ("Vladof", "Launcher"): 1.5,  # every 3rd shot is free
}

# Known vanilla barrel paths that convert bullets to pure splash
# (grenades). These cannot crit.
_VANILLA_SPLASH_BARRELS: tuple[str, ...] = ("AR_Barrel_Torgue",)


def _get_manufacturer_name(weapon: UObject) -> str:
    """Return the manufacturer name (e.g. 'Vladof') or empty string."""
    try:
        mfr = weapon.DefinitionData.ManufacturerDefinition
        if mfr is not None:
            return str(mfr.Name)
    except Exception:
        pass
    return ""


def _get_weapon_type_name(weapon: UObject) -> str:
    """Return the weapon type name (e.g. 'WT_Vladof_Launcher') or empty string."""
    try:
        wtype = weapon.DefinitionData.WeaponTypeDefinition
        if wtype is not None:
            return str(wtype.Name)
    except Exception:
        pass
    return ""


def _is_vanilla_splash_barrel(weapon: UObject) -> bool:
    """Return True if the weapon has a barrel known to fire pure splash."""
    try:
        barrel = weapon.DefinitionData.BarrelPartDefinition
        if barrel is not None:
            barrel_path = str(barrel.PathName(barrel))
            for pattern in _VANILLA_SPLASH_BARRELS:
                if pattern in barrel_path:
                    return True
    except Exception:
        pass
    return False


def _get_vanilla_mag_size_mult(weapon: UObject) -> float | None:
    """Return the mag size multiplier if this is a known vanilla BPD combo."""
    manufacturer = _get_manufacturer_name(weapon)
    type_name = _get_weapon_type_name(weapon)
    for (mfr_pattern, type_pattern), multiplier in _VANILLA_MAG_SIZE_OVERRIDES.items():
        if mfr_pattern in manufacturer and type_pattern in type_name:
            return multiplier
    return None


def get_weapon_stats(weapon: UObject) -> dict[str, float | bool]:
    """Read all weapon stats into a dict.

    Every key from WEAPON_ATTRIBUTES is included with its resolved float
    value, plus the following derived entries:

    Derived floats (from core attributes):
        fire_rate     - Shots per second (1 / fire_interval).
        crit_mult     - Crit multiplier from weapon parts, computed with the
                        bl2.parts attribute formula (see below).

    Flags (bool):
        has_bpd       - True when the weapon type has a
                        BehaviorProviderDefinition.  Weapons with a BPD may
                        have special firing behaviours (free shots, ammo
                        return, etc.) that the standard DPS formula cannot
                        account for.  The default display appends "(!)" to
                        warn the user.
        pure_splash   - True when ALL of the weapon's card damage is
                        delivered as splash (e.g. rocket launchers).  Splash
                        damage cannot crit, so crit_mult should be ignored
                        for these weapons.

    Crit formula (credit to Zetters who showed me this:
    https://bl2.parts/calculations/ (I'm assuming him, Apple and other
    modders made made the site after reversing the game):
        crit_mult = (Base + PreAdd) * (1 + PosScale) / (1 - NegScale) + PostAdd
        where Base = 2.0 (BL2 default crit multiplier).
        Positive and negative Scale bonuses from weapon parts are bucketed
        separately: positive values multiply up in the numerator, negative
        values divide in the denominator.
    """
    stats: dict[str, float | bool] = {
        key: read_weapon_stat(weapon, key) for key in WEAPON_ATTRIBUTES
    }

    # --- Derived floats ---

    # Weapons with shot_cost 0 never consume ammo and never reload.
    # Set reload_time to 0 and shot_cost to 1 so the DPS formula
    # naturally simplifies to: damage * projectiles * fire_rate.
    if stats["shot_cost"] <= 0:
        stats["shot_cost"] = 1.0
        stats["reload_time"] = 0.0

    fire_interval = stats["fire_interval"]
    stats["fire_rate"] = (1.0 / fire_interval) if fire_interval > 0 else 0.0

    # crit_neg_scale is already negative, so (1 - negative) == (1 + abs).
    crit_preadd, crit_pos_scale, crit_neg_scale, crit_postadd = _get_weapon_crit_parts(weapon)
    neg_scale_divisor = 1.0 - crit_neg_scale
    if neg_scale_divisor <= 0:
        neg_scale_divisor = 0.001
    stats["crit_mult"] = (2.0 + crit_preadd) * (
        1.0 + crit_pos_scale
    ) / neg_scale_divisor + crit_postadd

    # --- Vanilla BPD corrections ---

    vanilla_corrected = False
    if assume_vanilla_bpds.value:
        vanilla_mag_mult = _get_vanilla_mag_size_mult(weapon)
        if vanilla_mag_mult is not None:
            stats["mag_size"] = float(stats["mag_size"]) * vanilla_mag_mult
            vanilla_corrected = True

    # --- Flags ---

    has_bpd = _has_nonstandard_bpd(weapon)
    if vanilla_corrected:
        has_bpd = False
    stats["has_bpd"] = has_bpd

    pure_splash = _is_pure_splash(weapon)
    if assume_vanilla_bpds.value and _is_vanilla_splash_barrel(weapon):
        pure_splash = True
    stats["pure_splash"] = pure_splash

    return stats


# --- DPS Calculation ---
#
# To use a custom formula, replace calc_dps() with your own function that
# takes a stats dict (from get_weapon_stats) and returns a float or None.


def calc_dps(stats: dict[str, float | bool]) -> float | None:
    """Calculate weapon DPS using the lootlemon formula.

    Formula:
        single_shot_damage = damage * projectiles
        shots_per_mag      = mag_size / shot_cost
        dps = (single_shot_damage * fire_rate * shots_per_mag)
            / (fire_rate * reload_time + shots_per_mag)

    Args:
        stats: Weapon stats dict from get_weapon_stats().

    Returns:
        DPS value, or None if the weapon stats are invalid.
    """
    fire_rate = stats["fire_rate"]
    mag_size = stats["mag_size"]
    shot_cost = stats["shot_cost"]
    reload_time = stats["reload_time"]
    damage = stats["damage"]
    projectile_count = stats["projectiles"]

    if fire_rate <= 0 or mag_size <= 0:
        return None

    shots_per_mag = mag_size / shot_cost
    single_shot_damage = damage * max(projectile_count, 1.0)

    time_per_mag_cycle = fire_rate * reload_time + shots_per_mag
    if time_per_mag_cycle <= 0:
        return None

    return (single_shot_damage * fire_rate * shots_per_mag) / time_per_mag_cycle


# --- Display ---


def _show_dps(
    dps: float,
    crit_dps: float | None,
    has_nonstandard_bpd: bool = False,
) -> None:
    pc = get_pc()
    if pc is None:
        return
    bpd_warning = " (!)" if has_nonstandard_bpd else ""
    text = f"DPS: {dps:,.0f}{bpd_warning}"
    if crit_dps is not None:
        text += f"\nCrit DPS: {crit_dps:,.0f}{bpd_warning}"
    try:
        movie = pc.myHUD.HUDMovie
        if movie is not None:
            movie.AddTrainingText(
                text,
                "Weapon Stats",
                display_duration.value,
                unrealsdk.make_struct("Color"),
                "",
                False,
                0,
                pc.PlayerReplicationInfo,
                True,
                0,
                0,
            )
    except Exception:
        pass


# --- Hook ---


@hook("WillowGame.ItemCardGFxObject:SetItemCardEx", Type.POST)
def _on_set_item_card(
    _obj: UObject,
    args: WrappedStruct,
    _ret: Any,
    _func: BoundFunction,
) -> None:
    try:
        item = args.InventoryItem
    except Exception:
        return

    if item is None:
        return

    try:
        if item.Class.Name != "WillowWeapon":
            return
    except Exception:
        return

    stats = get_weapon_stats(item)
    dps = calc_dps(stats)
    if dps is not None:
        crit_mult = stats["crit_mult"]
        is_splash = bool(stats["pure_splash"])
        has_bpd = bool(stats["has_bpd"])

        if is_splash:
            crit_dps = None  # splash damage cannot crit
        elif crit_mult != 1.0:
            crit_dps = dps * crit_mult
        else:
            crit_dps = None

        _show_dps(dps, crit_dps, has_nonstandard_bpd=has_bpd)


# --- Build Mod ---

mod = build_mod(
    options=[display_duration, assume_vanilla_bpds],
)
