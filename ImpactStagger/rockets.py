from __future__ import annotations

from mods_base import SliderOption


def is_rocket(dmg_name: str) -> bool:
    """Check if a damage type name is from a rocket launcher."""
    return dmg_name == "WillowDmgSource_Rocket"


rocket_force_mult = SliderOption(
    "Rocket Force %",
    500,
    0,
    1000,
    5,
    True,
    description=(
        "Force % for rocket explosions. "
        "Rockets have low base momentum (~10k) from the engine so this defaults higher. "
        "0 = no force, 100 = engine default, 500 = 5x. "
        "Default: 500"
    ),
)

ragdoll_hit_scale_rocket = SliderOption(
    "Ragdoll Hit Scale % (Rocket)",
    100,
    0,
    400,
    5,
    True,
    description=(
        "Force on already-ragdolled enemies from rocket follow-up hits,"
        " as % of Impulse Scale. Default: 100"
    ),
)


def get_source_scale() -> float:
    """Return the rocket force multiplier from slider."""
    return rocket_force_mult.value / 100.0


def get_ragdoll_hit_scale() -> float:
    """Return the rocket ragdoll hit scale (0-1 range)."""
    return ragdoll_hit_scale_rocket.value / 100.0


def get_options() -> list:
    """Return rocket slider options for build_mod."""
    return [rocket_force_mult, ragdoll_hit_scale_rocket]
