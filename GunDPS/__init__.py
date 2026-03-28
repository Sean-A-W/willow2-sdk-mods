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
        " free shots, Torgue AR grenade barrels) are corrected in the DPS"
        " calculation and the (!) warning is removed."
    ),
)

display_duration = SliderOption(
    "Display Duration",
    3,
    min_value=1,
    max_value=10,
    description="How long the DPS notification stays on screen (seconds).",
)


# --- Attribute Reading ---

# Maps friendly stat names to AttributeDefinition object paths.
# Resolved at runtime via unrealsdk.find_object("AttributeDefinition", path).
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
            _attr_cache[key] = unrealsdk.find_object(
                "AttributeDefinition", WEAPON_ATTRIBUTES[key]
            )
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


# --- Crit Calculation ---

_CRIT_ATTR_PATH = "D_Attributes.GameplayAttributes.PlayerCriticalHitBonus"

# All weapon part slot names on DefinitionData.
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
    """Collect crit modifiers from all weapon parts' ExternalAttributeEffects.

    Uses the bl2.parts attribute formula which separates bonuses by modifier
    type and by sign for Scale modifiers:
        Final = (Base + PreAdd) * (1 + PosScale) / (1 - NegScale) + PostAdd

    Returns (crit_preadd, crit_pos_scale, crit_neg_scale, crit_postadd).
    crit_neg_scale will be <= 0 (already negative).
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


# --- BPD Scanning ---
#
# BehaviorProviderDefinitions (BPDs) contain behavior trees that can change
# how a weapon fires, consumes ammo, or deals damage. We scan them for two
# purposes:
#   1. Detect unknown DPS-affecting behaviors -> show (!) warning
#   2. Read splash damage scale from Behavior_Explode instances

# Behaviors in this set are cosmetic / visual and don't affect DPS.
_HARMLESS_BEHAVIORS: set[str] = {
    "Behavior_RunBehaviorCollection",
    "Behavior_CompareFloat",
    "Behavior_SetFloatParam",
    "Behavior_SetVectorParam",
    "Behavior_SetObjectParam",
    "Behavior_AddInstanceData",
    "Behavior_ChangeSpin",
    "Behavior_Explode",  # splash damage - read separately by _get_bpd_splash_scale
    "ProjectileBehavior_Detonate",
}


def _get_bpd_behavior_names(bpd: UObject) -> list[str]:
    """Return class names of all behaviors inside a BPD.

    Used by both the DPS-behavior detector and the splash scale reader.
    """
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


def _get_bpd_splash_scale(bpd: UObject) -> float:
    """Read splash damage scale from Behavior_Explode instances in a BPD.

    Each Behavior_Explode has a DamageFormula with BaseValueScaleConstant
    representing the fraction of card damage dealt as additional splash
    (e.g. 0.8 = 80%, 1.0 = 100%).

    Returns the highest scale found, or 0.0 if no Behavior_Explode exists.
    """
    highest_scale = 0.0
    try:
        sequences = bpd.BehaviorSequences
        if sequences is None:
            return highest_scale
        for seq in sequences:
            try:
                actions = seq.BehaviorData2
                if actions is None:
                    continue
                for action in actions:
                    try:
                        behavior = action.Behavior
                        if behavior is None:
                            continue
                        if str(behavior.Class.Name) != "Behavior_Explode":
                            continue
                        scale = float(behavior.DamageFormula.BaseValueScaleConstant)
                        if scale > highest_scale:
                            highest_scale = scale
                    except Exception:
                        continue
            except Exception:
                continue
    except Exception:
        pass
    return highest_scale


def _collect_all_bpds(weapon: UObject) -> list[UObject]:
    """Gather every BPD reachable from a weapon (type, projectile, all parts).

    Used by both the splash scanner and the unknown-behavior detector to
    avoid duplicating the traversal logic.
    """
    bpds: list[UObject] = []
    try:
        definition_data = weapon.DefinitionData
    except Exception:
        return bpds

    # Weapon type and its projectile
    try:
        weapon_type = definition_data.WeaponTypeDefinition
        if weapon_type is not None:
            bpd = weapon_type.BehaviorProviderDefinition
            if bpd is not None:
                bpds.append(bpd)
            firing_mode = weapon_type.DefaultFiringModeDefinition
            if firing_mode is not None:
                proj_def = firing_mode.ProjectileDefinition
                if proj_def is not None:
                    proj_bpd = proj_def.BehaviorProviderDefinition
                    if proj_bpd is not None:
                        bpds.append(proj_bpd)
    except Exception:
        pass

    # All parts and their custom firing mode projectiles
    for slot in _PART_SLOTS:
        try:
            part = getattr(definition_data, slot)
            if part is None:
                continue
            bpd = part.BehaviorProviderDefinition
            if bpd is not None:
                bpds.append(bpd)
            try:
                cfm = part.CustomFiringModeDefinition
                if cfm is not None:
                    cproj = cfm.ProjectileDefinition
                    if cproj is not None:
                        cpbpd = cproj.BehaviorProviderDefinition
                        if cpbpd is not None:
                            bpds.append(cpbpd)
            except Exception:
                pass
        except Exception:
            continue

    return bpds


def _get_weapon_splash_scale(weapon: UObject) -> float:
    """Return the highest splash damage scale found across all weapon BPDs."""
    highest_scale = 0.0
    for bpd in _collect_all_bpds(weapon):
        scale = _get_bpd_splash_scale(bpd)
        if scale > highest_scale:
            highest_scale = scale
    return highest_scale


def _has_nonstandard_bpd(weapon: UObject) -> bool:
    """Return True if any weapon BPD contains unknown DPS-affecting behaviors.

    Also returns True if any part has a CustomFiringModeDefinition, since
    normal weapons never have one and it indicates special projectile
    behaviour we can't calculate without building a tool to evaluate
    without building a proper interpreter for BPDs.
    """
    for bpd in _collect_all_bpds(weapon):
        for name in _get_bpd_behavior_names(bpd):
            if name not in _HARMLESS_BEHAVIORS:
                return True

    # Custom firing modes on parts indicate special projectile behaviour
    try:
        definition_data = weapon.DefinitionData
        for slot in _PART_SLOTS:
            try:
                part = getattr(definition_data, slot)
                if part is not None and part.CustomFiringModeDefinition is not None:
                    return True
            except Exception:
                continue
    except Exception:
        pass

    return False


# --- Pure Splash Detection ---


def _is_pure_splash(weapon: UObject) -> bool:
    """Return True if the weapon delivers ALL its card damage as splash.

    Rocket-type weapons (FireType == EWWFT_Rocket) deal all damage through
    HurtRadius which cannot crit. Bullet/hitscan weapons deliver card
    damage as a direct hit (crittable) even if they also have splash on top.
    """
    try:
        weapon_type = weapon.DefinitionData.WeaponTypeDefinition
        if weapon_type is None:
            return False
        # EWWFT_Rocket == 2
        firing_mode = weapon_type.DefaultFiringModeDefinition
        if firing_mode is not None:
            fire_type = str(firing_mode.FireType)
            if "Rocket" in fire_type or fire_type == "2":
                return True
        # Fallback: weapon type name
        type_name = str(weapon_type.Name) if weapon_type.Name is not None else ""
        if "Launcher" in type_name:
            return True
    except Exception:
        pass
    return False


# --- Vanilla BPD Assumptions ---
#
# These corrections assume unmodded weapon archetypes. Only applied when
# the "Assume Vanilla BPDs" option is enabled, since mods may change what
# these weapons do.

_VANILLA_MAG_SIZE_OVERRIDES: dict[tuple[str, str], float] = {
    ("Vladof", "Launcher"): 1.5,  # every 3rd shot is free
}

_VANILLA_SPLASH_BARRELS: tuple[str, ...] = (
    "AR_Barrel_Torgue",  # converts bullets to grenades (pure splash)
)


def _get_manufacturer_name(weapon: UObject) -> str:
    try:
        mfr = weapon.DefinitionData.ManufacturerDefinition
        if mfr is not None:
            return str(mfr.Name)
    except Exception:
        pass
    return ""


def _get_weapon_type_name(weapon: UObject) -> str:
    try:
        wtype = weapon.DefinitionData.WeaponTypeDefinition
        if wtype is not None:
            return str(wtype.Name)
    except Exception:
        pass
    return ""


def _is_vanilla_splash_barrel(weapon: UObject) -> bool:
    """Return True if the barrel is known to convert bullets to pure splash."""
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
    """Return the mag size multiplier for a known vanilla BPD combo, or None."""
    manufacturer = _get_manufacturer_name(weapon)
    type_name = _get_weapon_type_name(weapon)
    for (mfr_pattern, type_pattern), multiplier in _VANILLA_MAG_SIZE_OVERRIDES.items():
        if mfr_pattern in manufacturer and type_pattern in type_name:
            return multiplier
    return None


# --- Stat Collection ---


def get_weapon_stats(weapon: UObject) -> dict[str, float | bool]:
    """Read all weapon stats into a dict.

    Every key from WEAPON_ATTRIBUTES is included with its resolved float
    value, plus the following derived entries:

    Derived floats:
        fire_rate      - Shots per second (1 / fire_interval).
        crit_mult      - Crit multiplier from weapon parts using the
                         bl2.parts attribute formula.
        splash_scale   - Fraction of card damage dealt as additional splash
                         (e.g. 0.8 = 80%).  Read from Behavior_Explode
                         instances in the weapon's BPDs.  0.0 if none.

    Flags (bool):
        has_bpd        - True if any BPD on the weapon contains behaviors
                         not in the known-harmless set, or if any part has
                         a CustomFiringModeDefinition.  The display appends
                         "(!)" to warn the user.
        pure_splash    - True if ALL card damage is delivered as splash
                         (e.g. rocket launchers).  Splash cannot crit.
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
    crit_preadd, crit_pos_scale, crit_neg_scale, crit_postadd = (
        _get_weapon_crit_parts(weapon)
    )
    neg_scale_divisor = 1.0 - crit_neg_scale
    if neg_scale_divisor <= 0:
        neg_scale_divisor = 0.001
    stats["crit_mult"] = (
        (2.0 + crit_preadd) * (1.0 + crit_pos_scale) / neg_scale_divisor
        + crit_postadd
    )

    stats["splash_scale"] = _get_weapon_splash_scale(weapon)

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
# takes a stats dict (from get_weapon_stats) and returns a tuple or None.


def calc_dps(stats: dict[str, float | bool]) -> tuple[float, float] | None:
    """Calculate weapon DPS, split into impact and splash components.

    Impact DPS is crittable. Splash DPS is not.

    Formula:
        single_shot_damage = damage * projectiles
        shots_per_mag      = mag_size / shot_cost
        impact_dps = (single_shot_damage * fire_rate * shots_per_mag)
                   / (fire_rate * reload_time + shots_per_mag)
        splash_dps = impact_dps * splash_scale

    Returns:
        (impact_dps, splash_dps) tuple, or None if stats are invalid.
    """
    fire_rate = stats["fire_rate"]
    mag_size = stats["mag_size"]
    shot_cost = stats["shot_cost"]
    reload_time = stats["reload_time"]
    damage = stats["damage"]
    projectile_count = stats["projectiles"]
    splash_scale = float(stats.get("splash_scale", 0.0))

    if fire_rate <= 0 or mag_size <= 0:
        return None

    shots_per_mag = mag_size / shot_cost
    single_shot_damage = damage * max(projectile_count, 1.0)

    time_per_mag_cycle = fire_rate * reload_time + shots_per_mag
    if time_per_mag_cycle <= 0:
        return None

    impact_dps = (
        (single_shot_damage * fire_rate * shots_per_mag) / time_per_mag_cycle
    )
    splash_dps = impact_dps * splash_scale

    return impact_dps, splash_dps


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
    result = calc_dps(stats)
    if result is not None:
        impact_dps, splash_dps = result
        total_dps = impact_dps + splash_dps
        crit_mult = stats["crit_mult"]
        is_pure_splash = bool(stats["pure_splash"])
        has_bpd = bool(stats["has_bpd"])

        if is_pure_splash:
            crit_dps = None
        elif crit_mult != 1.0:
            # Only impact damage can crit, splash cannot
            crit_dps = impact_dps * crit_mult + splash_dps
        else:
            crit_dps = None

        _show_dps(total_dps, crit_dps, has_nonstandard_bpd=has_bpd)


# --- Build Mod ---

mod = build_mod(
    options=[display_duration, assume_vanilla_bpds],
)
