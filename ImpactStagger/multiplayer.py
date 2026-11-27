from __future__ import annotations

from typing import TYPE_CHECKING

from mods_base import ENGINE
import unrealsdk
from unrealsdk.unreal import WeakPointer

if TYPE_CHECKING:
    from mods_base import Mod

# --- Multiplayer helpers ---


def is_client() -> bool:
    """True if non-host client in multiplayer. False in solo or as host."""
    try:
        wi = ENGINE.GetCurrentWorldInfo()
        return wi is not None and wi.NetMode == 3  # NM_Client
    except Exception:
        return False


def is_multiplayer() -> bool:
    """True if more than one player is in the session."""
    try:
        wi = ENGINE.GetCurrentWorldInfo()
        if wi is None:
            return False
        return wi.GRI.PRIArray.Length > 1
    except Exception:
        return False


def find_pawn_by_path(path: str):
    """Find AI pawn by PathName on client side."""
    try:
        return unrealsdk.FindObject("WillowAIPawn", path)
    except Exception:
        return None


# --- Client-side state ---

# pawn_path -> (ix, iy, iz, hx, hy, hz, bone, vel_change, compensate_mass)
CLIENT_PENDING: dict[str, tuple] = {}

# pawn_path -> (ix, iy, iz, hx, hy, hz, bone, vel_change, compensate_mass)
CLIENT_DEATH_PENDING: dict[str, tuple] = {}

# pawn_path -> WeakPointer (PlayDying fired before broadcast arrived)
CLIENT_AWAITING_DEATH: dict[str, WeakPointer] = {}


# --- Optional networking integration ---

try:
    from networking import broadcast, host, add_network_functions

    HAS_NETWORKING = True
except ImportError:
    HAS_NETWORKING = False

# Options that should NOT be synced (client-local)
_LOCAL_OPTION_IDS: set[str] = {
    "Log to Console",
    "Disable Ragdoll-Ragdoll Collision",
    "Max Ragdolls Per Well",
    "Log Performance",
}

# Reference to the mod object (set by register_network_functions)
_mod_ref: Mod | None = None

# Host-synced settings note shown to clients
_client_note_shown: bool = False


def _get_pawn_path(pawn) -> str:
    """Get globally unique path for a pawn (same on host and client)."""
    try:
        return pawn.PathName(pawn)
    except Exception:
        return ""


_log_fn = None


def _log(msg: str) -> None:
    """Log via main module's log function."""
    if _log_fn is not None:
        try:
            _log_fn(msg)
        except Exception:
            pass


# --- Network functions (only defined if networking.sdkmod available) ---

if HAS_NETWORKING:

    @broadcast.json_message
    def net_impulse(
        pawn_path: str,
        ix: float,
        iy: float,
        iz: float,
        hx: float,
        hy: float,
        hz: float,
        bone: str,
        vel_change: bool,
        compensate_mass: bool,
    ) -> None:
        """Receive impulse data from host. Apply or queue for ragdoll setup."""
        pawn = find_pawn_by_path(pawn_path)
        if pawn is None:
            CLIENT_PENDING[pawn_path] = (ix, iy, iz, hx, hy, hz, bone, vel_change, compensate_mass)
            _log(f"[IS-MP] impulse queued (pawn not found): {pawn_path}")
            return

        # If already ragdolled (state 0), apply directly
        try:
            if pawn.StaggerState.bIsStaggered and int(pawn.StaggerState.State) == 0:
                _apply_client_impulse(
                    pawn,
                    ix,
                    iy,
                    iz,
                    hx,
                    hy,
                    hz,
                    bone,
                    vel_change,
                    compensate_mass,
                )
                _log("[IS-MP] impulse applied directly (already ragdolled)")
                return
        except Exception:
            pass

        # Queue for SetupRagdollPhysics POST
        CLIENT_PENDING[pawn_path] = (ix, iy, iz, hx, hy, hz, bone, vel_change, compensate_mass)
        _log(f"[IS-MP] impulse queued: {pawn_path}")

    @broadcast.json_message
    def net_death_impulse(
        pawn_path: str,
        ix: float,
        iy: float,
        iz: float,
        hx: float,
        hy: float,
        hz: float,
        bone: str,
        vel_change: bool,
        compensate_mass: bool,
    ) -> None:
        """Receive death impulse from host. Apply or queue for PlayDying."""
        # Check if PlayDying already fired (pawn waiting for this broadcast)
        wp = CLIENT_AWAITING_DEATH.pop(pawn_path, None)
        if wp is not None:
            pawn = wp()
        else:
            pawn = None
        if pawn is not None:
            _apply_client_impulse(pawn, ix, iy, iz, hx, hy, hz, bone, vel_change, compensate_mass)
            _log("[IS-MP] death impulse applied (was awaiting)")
            return

        # Queue for PlayDying POST
        CLIENT_DEATH_PENDING[pawn_path] = (
            ix,
            iy,
            iz,
            hx,
            hy,
            hz,
            bone,
            vel_change,
            compensate_mass,
        )
        _log(f"[IS-MP] death impulse queued: {pawn_path}")

    @broadcast.json_message
    def net_settings(settings: dict) -> None:
        """Receive synced settings from host. Host ignores its own broadcast."""
        if _mod_ref is None:
            return
        if not is_client():
            return
        _apply_synced_settings(settings)

    @host.json_message
    def net_request_settings() -> None:
        """Client requests settings from host. Host responds with broadcast."""
        _log("[IS-MP] client requested settings, broadcasting")
        broadcast_settings()

else:
    # Stubs when networking not available
    net_impulse = None
    net_death_impulse = None
    net_settings = None
    net_request_settings = None


_apply_impulse_fn = None


def _apply_client_impulse(
    pawn,
    ix: float,
    iy: float,
    iz: float,
    hx: float,
    hy: float,
    hz: float,
    bone: str,
    vel_change: bool,
    compensate_mass: bool,
) -> None:
    """Apply impulse on client side using the main module's apply_impulse."""
    if _apply_impulse_fn is None:
        _log("[IS-MP] client impulse error: apply_impulse not registered")
        return
    try:
        hit_loc = unrealsdk.make_struct("Vector")
        hit_loc.X, hit_loc.Y, hit_loc.Z = hx, hy, hz
        _apply_impulse_fn(pawn, ix, iy, iz, hit_loc, bone, vel_change, compensate_mass)
    except Exception as e:
        _log(f"[IS-MP] client impulse error: {e}")


# --- Settings sync ---


def _collect_syncable_options() -> list:
    """Collect all options that should be synced from host to client."""
    if _mod_ref is None:
        return []
    result = []
    for opt in _mod_ref.options:
        _collect_options_recursive(opt, result)
    return result


def _collect_options_recursive(opt, result: list) -> None:
    """Recursively collect SliderOption and BoolOption from nested structure."""
    from mods_base import SliderOption, BoolOption, NestedOption

    if isinstance(opt, NestedOption):
        for child in opt.children:
            _collect_options_recursive(child, result)
    elif isinstance(opt, (SliderOption, BoolOption)):
        if opt.identifier not in _LOCAL_OPTION_IDS:
            result.append(opt)


def gather_settings() -> dict:
    """Collect current settings values into a dict for network transmission."""
    settings = {}
    for opt in _collect_syncable_options():
        settings[opt.identifier] = opt.value
    return settings


def broadcast_settings() -> None:
    """Host broadcasts current settings to all clients."""
    if not HAS_NETWORKING or net_settings is None or not is_multiplayer():
        return
    if is_client():
        return
    settings = gather_settings()
    net_settings(settings)
    _log(f"[IS-MP] broadcast {len(settings)} settings")


def request_settings() -> None:
    """Client requests settings from host."""
    if not HAS_NETWORKING or net_request_settings is None:
        return
    if not is_client():
        return
    net_request_settings()
    _log("[IS-MP] requesting settings from host")


def _apply_synced_settings(settings: dict) -> None:
    """Apply host-synced settings on client. Hide synced options."""
    global _client_note_shown
    if _mod_ref is None:
        return

    count = 0
    for opt in _collect_syncable_options():
        if opt.identifier in settings:
            opt.value = settings[opt.identifier]
            opt.is_hidden = True
            count += 1

    if not _client_note_shown:
        _client_note_shown = True
        print("[ImpactStagger] Settings synced from host. Gameplay options are host-controlled.")

    _log(f"[IS-MP] applied {count} synced settings")


def restore_client_options() -> None:
    """Restore option visibility when leaving multiplayer or becoming host."""
    global _client_note_shown
    _client_note_shown = False
    for opt in _collect_syncable_options():
        opt.is_hidden = False


# --- Client-side queue draining ---


def drain_client_pending(pawn) -> None:
    """Called from SetupRagdollPhysics POST on client to apply queued impulse."""
    path = _get_pawn_path(pawn)
    if not path:
        return
    stored = CLIENT_PENDING.pop(path, None)
    if stored is not None:
        _apply_client_impulse(pawn, *stored)
        _log(f"[IS-MP] drained pending impulse: {path}")


def drain_client_death(pawn) -> None:
    """Called from PlayDying POST on client to apply queued death impulse."""
    path = _get_pawn_path(pawn)
    if not path:
        return
    stored = CLIENT_DEATH_PENDING.pop(path, None)
    if stored is not None:
        _apply_client_impulse(pawn, *stored)
        _log(f"[IS-MP] drained death impulse: {path}")
    else:
        # Death broadcast hasn't arrived yet — store weak ref for when it does
        CLIENT_AWAITING_DEATH[path] = WeakPointer(pawn)
        _log(f"[IS-MP] awaiting death impulse: {path}")


# --- Host-side broadcast helpers ---


def broadcast_impulse(
    pawn,
    ix,
    iy,
    iz,
    hit_location,
    bone_name,
    vel_change=False,
    compensate_mass=False,
) -> None:
    """Host broadcasts impulse to clients after computing it."""
    if not HAS_NETWORKING or net_impulse is None or not is_multiplayer():
        return
    path = _get_pawn_path(pawn)
    if not path:
        return
    try:
        hx, hy, hz = hit_location.X, hit_location.Y, hit_location.Z
    except Exception:
        hx, hy, hz = 0.0, 0.0, 0.0
    net_impulse(path, ix, iy, iz, hx, hy, hz, bone_name, vel_change, compensate_mass)


def broadcast_death(
    pawn,
    ix,
    iy,
    iz,
    hit_location,
    bone_name,
    vel_change=False,
    compensate_mass=False,
) -> None:
    """Host broadcasts death impulse to clients."""
    if not HAS_NETWORKING or net_death_impulse is None or not is_multiplayer():
        return
    path = _get_pawn_path(pawn)
    if not path:
        return
    try:
        hx, hy, hz = hit_location.X, hit_location.Y, hit_location.Z
    except Exception:
        hx, hy, hz = 0.0, 0.0, 0.0
    net_death_impulse(path, ix, iy, iz, hx, hy, hz, bone_name, vel_change, compensate_mass)


# --- Registration ---


def register_network_functions(mod: Mod) -> None:
    """Register network functions with the mod. Call after build_mod()."""
    global _mod_ref
    _mod_ref = mod

    if not HAS_NETWORKING:
        return

    add_network_functions(mod)

    # Wrap on_enable to broadcast settings (host) or request them (client)
    original_on_enable = mod.on_enable

    def _wrapped_on_enable() -> None:
        original_on_enable()
        if is_client():
            request_settings()
        elif is_multiplayer():
            broadcast_settings()

    mod.on_enable = _wrapped_on_enable

    # Wrap on_disable to restore client options
    original_on_disable = mod.on_disable

    def _wrapped_on_disable() -> None:
        original_on_disable()
        restore_client_options()
        CLIENT_PENDING.clear()
        CLIENT_DEATH_PENDING.clear()
        CLIENT_AWAITING_DEATH.clear()

    mod.on_disable = _wrapped_on_disable
