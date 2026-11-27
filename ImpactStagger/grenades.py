from __future__ import annotations

from mods_base import SliderOption


def is_grenade(dmg_name: str) -> bool:
    """Check if a damage type name is from a grenade."""
    return dmg_name == "WillowDmgSource_Grenade"


grenade_force_mult = SliderOption(
    "Grenade Force %",
    100,
    0,
    1000,
    5,
    True,
    description=(
        "Force % for grenade explosions (including barrels). "
        "0 = no force, 100 = normal, 200 = 2x. "
        "Default: 100"
    ),
)

ragdoll_hit_scale_grenade = SliderOption(
    "Ragdoll Hit Scale % (Grenade)",
    100,
    0,
    400,
    5,
    True,
    description=(
        "Force on already-ragdolled enemies from grenade follow-up hits,"
        " as % of Impulse Scale. Default: 100"
    ),
)


def get_source_scale() -> float:
    """Return the grenade force multiplier from slider."""
    return grenade_force_mult.value / 100.0


def get_ragdoll_hit_scale() -> float:
    """Return the grenade ragdoll hit scale (0-1 range)."""
    return ragdoll_hit_scale_grenade.value / 100.0


def get_options() -> list:
    """Return grenade slider options for build_mod."""
    return [grenade_force_mult, ragdoll_hit_scale_grenade]
