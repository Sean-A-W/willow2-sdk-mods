from __future__ import annotations

import math

from mods_base import get_pc, build_mod, hook, keybind, BoolOption, NestedOption
import unrealsdk
from uemath import Vector
from uemath.constants import URU_TO_RADIANS
from unrealsdk.hooks import Type
from unrealsdk.unreal import BoundFunction, UObject, WrappedStruct


# --- Options ---

auto_kill = BoolOption(
    "Kill Volumes",
    False,
    description="Automatically draw kill volumes on map load.",
)
auto_collision = BoolOption(
    "Collision Volumes",
    False,
    description="Automatically draw collision volumes on map load.",
)
auto_noclip = BoolOption(
    "Collision Noclip",
    False,
    description="Automatically disable invisible collision on map load.",
)
auto_fake = BoolOption(
    "Hide Fake Meshes",
    False,
    description="Automatically hide non-colliding meshes on map load.",
)
auto_solidify = BoolOption(
    "Solidify Meshes",
    False,
    description="Automatically solidify non-colliding meshes on map load.",
)
solidify_strict = BoolOption(
    "Solidify: Skip Bounding Boxes",
    True,
    description=(
        "When enabled, solidify skips meshes with simple bounding-box"
        " collision (boxes, spheres, capsules, or convex hulls with 8"
        " or fewer vertices). Disable to solidify everything that has"
        " any collision geometry."
    ),
)

auto_on_load = NestedOption(
    "Auto-Run on Map Load",
    children=[auto_kill, auto_collision, auto_noclip, auto_fake, auto_solidify],
    description="Select which features activate automatically when loading a new map.",
)


# --- Helpers ---


def _show_message(message, title="Collision Toolkit"):
    pc = get_pc()
    if pc is None:
        return
    movie = pc.myHUD.HUDMovie
    if movie is None:
        return
    movie.AddTrainingText(
        message,
        title,
        3,
        unrealsdk.make_struct("Color"),
        "",
        False,
        0,
        pc.PlayerReplicationInfo,
        True,
        0,
        0,
    )


def _log(message):
    unrealsdk.logging.info(message)


def _iter_len(array):
    n = 0
    if array is not None:
        for _ in array:
            n += 1
    return n


def _tri_area(p1, p2, p3):
    v1 = Vector(p2) - Vector(p1)
    v2 = Vector(p3) - Vector(p1)
    return v1.cross(v2).magnitude / 2


def _transform_point(point, transform):
    x, y, z = point
    xx = transform.XPlane.X
    xy = transform.XPlane.Y
    xz = transform.XPlane.Z
    yx = transform.YPlane.X
    yy = transform.YPlane.Y
    yz = transform.YPlane.Z
    zx = transform.ZPlane.X
    zy = transform.ZPlane.Y
    zz = transform.ZPlane.Z
    wx = transform.WPlane.X
    wy = transform.WPlane.Y
    wz = transform.WPlane.Z
    return (
        xx * x + yx * y + zx * z + wx,
        xy * x + yy * y + zy * z + wy,
        xz * x + yz * y + zz * z + wz,
    )


def _rotate_point(point, pitch, yaw, roll):
    if point[0] == 0 and point[1] == 0 and point[2] == 0:
        return point
    if pitch == 0 and yaw == 0 and roll == 0:
        return point
    pitch = URU_TO_RADIANS * -pitch
    yaw = URU_TO_RADIANS * yaw
    roll = URU_TO_RADIANS * -roll
    cosa = math.cos(yaw)
    sina = math.sin(yaw)
    cosb = math.cos(pitch)
    sinb = math.sin(pitch)
    cosc = math.cos(roll)
    sinc = math.sin(roll)
    Axx = cosa * cosb
    Axy = cosa * sinb * sinc - sina * cosc
    Axz = cosa * sinb * cosc + sina * sinc
    Ayx = sina * cosb
    Ayy = sina * sinb * sinc + cosa * cosc
    Ayz = sina * sinb * cosc - cosa * sinc
    Azx = -sinb
    Azy = cosb * sinc
    Azz = cosb * cosc
    px, py, pz = point
    return (
        Axx * px + Axy * py + Axz * pz,
        Ayx * px + Ayy * py + Ayz * pz,
        Azx * px + Azy * py + Azz * pz,
    )


# --- State ---

_kill_data: dict = {}
_collision_data: dict = {}
_next_colors = [0, 0]
_color_slot = 0
_show_kill = False
_show_collision = False
_hide_fake = False
_hidden_components: list = []
_noclip_collision = False
_disabled_colliders: list = []
_solidify_meshes = False
_solidified_components: list = []

_COLORS = [
    (255, 0, 0),  # red
    (0, 255, 0),  # green
    (0, 0, 255),  # blue
    (0, 255, 255),  # cyan
    (255, 0, 255),  # magenta
    (255, 255, 0),  # yellow
]


# --- Volume drawing ---


def _subdivide(i1, i2, i3, verts, vert_map):
    e1 = vert_map.get((i1, i2), None)
    if e1 is None:
        v1 = verts[i1]
        v2 = verts[i2]
        verts.append(((v1[0] + v2[0]) / 2, (v1[1] + v2[1]) / 2, (v1[2] + v2[2]) / 2))
        e1 = len(verts) - 1
        vert_map[(i1, i2)] = e1
    e2 = vert_map.get((i2, i3), None)
    if e2 is None:
        v1 = verts[i2]
        v2 = verts[i3]
        verts.append(((v1[0] + v2[0]) / 2, (v1[1] + v2[1]) / 2, (v1[2] + v2[2]) / 2))
        e2 = len(verts) - 1
        vert_map[(i2, i3)] = e2
    e3 = vert_map.get((i1, i3), None)
    if e3 is None:
        v1 = verts[i1]
        v2 = verts[i3]
        verts.append(((v1[0] + v2[0]) / 2, (v1[1] + v2[1]) / 2, (v1[2] + v2[2]) / 2))
        e3 = len(verts) - 1
        vert_map[(i1, i3)] = e3
    return [e1, e2, e3, i1, e1, e3, e1, i2, e2, e3, e2, i3]


def _add_entity(volume, vertices, tris, collection, subdivisions=2):
    global _next_colors
    vert_map = {}
    for _ in range(subdivisions):
        i = 0
        length = len(tris)
        while i < length:
            v1 = tris[i]
            v2 = tris[i + 1]
            v3 = tris[i + 2]
            area = _tri_area(vertices[v1], vertices[v2], vertices[v3])
            if area >= 1000:
                new_tris = _subdivide(v1, v2, v3, vertices, vert_map)
                tris = tris[0:i] + new_tris + tris[i + 3 :]
                length += len(new_tris) - 3
                i += len(new_tris)
            else:
                i += 3
    color_index = _next_colors[_color_slot]
    color = _COLORS[color_index]
    collection[volume] = {"verts": vertices, "tris": tris, "color": color}
    color_index += 1
    if color_index >= len(_COLORS):
        color_index = 0
    _next_colors[_color_slot] = color_index


def _warn_if_complicated(volume):
    rot = volume.Rotation
    if rot.Pitch != 0 or rot.Yaw != 0 or rot.Roll != 0:
        _log(f"volume has Rotation = ({rot.Pitch}, {rot.Yaw}, {rot.Roll})")
    ds = volume.DrawScale3D
    if ds.X != 1 or ds.Y != 1 or ds.Z != 1:
        _log(f"volume has DrawScale3D = ({ds.X}, {ds.Y}, {ds.Z})")
    agg = volume.CollisionComponent.BrushAggGeom
    length = _iter_len(agg.SphereElems)
    if length > 0:
        _log(f"volume has SphereElems = {length}")
    length = _iter_len(agg.BoxElems)
    if length > 0:
        _log(f"volume has BoxElems = {length}")
    length = _iter_len(agg.SphylElems)
    if length > 0:
        _log(f"volume has SphylElems = {length}")
    length = _iter_len(agg.ConvexElems)
    if length > 1:
        _log(f"volume has ConvexElems = {length}")


def _does_blocker_block(component):
    return (
        component.bBlockPlayers
        and component.CollideActors
        and component.BlockActors
        and component.BlockNonZeroExtent
    )


def _add_new_volume(volume, collection):
    _warn_if_complicated(volume)
    for elem in volume.CollisionComponent.BrushAggGeom.ConvexElems:
        vertices = []
        tris = []
        for item in elem.VertexData:
            vertices.append(
                (
                    item.X + volume.Location.X,
                    item.Y + volume.Location.Y,
                    item.Z + volume.Location.Z,
                )
            )
        for item in elem.FaceTriData:
            tris.append(item)
        _add_entity(volume, vertices, tris, collection)
        break
    return True


def _add_blocking_mesh_component(component, collection, key):
    if component is None or not _does_blocker_block(component):
        return False
    if component.bIsDisabled:
        _log("note: found bIsDisabled BlockingMeshComponent")
    mesh = component.StaticMesh
    geom = mesh.BodySetup.AggGeom
    mesh_name = str(mesh)
    vertices = []
    tris = []
    if _iter_len(geom.SphereElems) > 0:
        _log("Could not add BlockingMeshActor with SphereElems")
        return False
    if _iter_len(geom.SphylElems) > 0:
        _log("Could not add BlockingMeshActor with SphylElems")
        return False
    if _iter_len(geom.BoxElems) + _iter_len(geom.ConvexElems) > 1:
        _log("Could not add BlockingMeshActor with multiple BoxElems/ConvexElems")
        return False
    if _iter_len(geom.ConvexElems) == 1:
        if "Common_Meshes.Blocking.Blocking_Cylinder" not in mesh_name:
            _log(f'note: non-Cylinder BlockingMeshActor w/ ConvexElem: "{mesh_name}"')
        for vertex in geom.ConvexElems[0].VertexData:
            vertices.append(
                _transform_point(
                    (vertex.X, vertex.Y, vertex.Z),
                    component._LocalToWorld,
                )
            )
        for tri in geom.ConvexElems[0].FaceTriData:
            tris.append(tri)
    elif _iter_len(geom.BoxElems) == 1:
        if "Common_Meshes.Blocking.Blocking_Cube" not in mesh_name:
            _log(f'non-Cube BlockingMeshActor w/ BoxElem: "{mesh_name}"')
            return False
        size = 256
        vertices.append(_transform_point((0, 0, 0), component._LocalToWorld))
        vertices.append(_transform_point((0, 0, size), component._LocalToWorld))
        vertices.append(_transform_point((0, size, size), component._LocalToWorld))
        vertices.append(_transform_point((0, size, 0), component._LocalToWorld))
        vertices.append(_transform_point((size, 0, 0), component._LocalToWorld))
        vertices.append(_transform_point((size, 0, size), component._LocalToWorld))
        vertices.append(_transform_point((size, size, size), component._LocalToWorld))
        vertices.append(_transform_point((size, size, 0), component._LocalToWorld))
        tris = [
            0,
            1,
            2,
            2,
            3,
            0,
            4,
            5,
            1,
            1,
            0,
            4,
            7,
            6,
            5,
            5,
            4,
            7,
            3,
            2,
            6,
            6,
            7,
            3,
            1,
            5,
            6,
            6,
            2,
            1,
            4,
            0,
            3,
            3,
            7,
            4,
        ]
    else:
        if "Common_Meshes.Blocking.Blocking_Plane" not in mesh_name:
            _log(f'non-Plane BlockingMeshActor w/ no AggGeom: "{mesh_name}"')
            return False
        size = 256
        vertices.append(_transform_point((0, 0, 0), component._LocalToWorld))
        vertices.append(_transform_point((0, 0, size), component._LocalToWorld))
        vertices.append(_transform_point((0, size, size), component._LocalToWorld))
        vertices.append(_transform_point((0, size, 0), component._LocalToWorld))
        tris = [0, 1, 2, 2, 3, 0]
    _add_entity(key, vertices, tris, collection)
    return True


def _add_blocking_mesh_collection(volume, collection):
    added_any = False
    for component in volume.Components:
        if component not in collection:
            if _add_blocking_mesh_component(component, collection, component):
                added_any = True
    return added_any


def _add_behavior_volume(volume, collection):
    if volume.Definition is not None and "BehaviorVolume_KillPawn" in str(volume.Definition):
        return _add_new_volume(volume, collection)
    return False


def _add_touch_volume(seq_event, collection):
    volume = seq_event.Originator
    if volume is not None and "TriggerVolume" in str(volume) and volume not in collection:
        for output in seq_event.OutputLinks:
            for link in output.Links:
                if "SeqAct_CausePlayerDeath" in str(link.LinkedOp):
                    return _add_new_volume(volume, collection)
    return False


def _add_boundary_turret(turret, collection):
    height = 10000
    kd = turret.KillDistance
    vw = turret.ViewWidth
    yaw = turret.Rotation.Yaw
    v0 = _rotate_point((kd, -vw, -height), 0, yaw, 0)
    v1 = _rotate_point((kd, -vw, height), 0, yaw, 0)
    v2 = _rotate_point((kd, vw, height), 0, yaw, 0)
    v3 = _rotate_point((kd, vw, -height), 0, yaw, 0)
    lx = turret.Location.X
    ly = turret.Location.Y
    lz = turret.Location.Z
    v0 = (v0[0] + lx, v0[1] + ly, v0[2] + lz)
    v1 = (v1[0] + lx, v1[1] + ly, v1[2] + lz)
    v2 = (v2[0] + lx, v2[1] + ly, v2[2] + lz)
    v3 = (v3[0] + lx, v3[1] + ly, v3[2] + lz)
    vertices = [v0, v1, v2, v3]
    tris = [0, 1, 2, 2, 3, 0]
    _add_entity(turret, vertices, tris, collection, 3)


def _update_volumes(class_names, master):
    if not isinstance(class_names, list):
        class_names = [class_names]
    for class_name in class_names:
        if class_name in master:
            collection = master[class_name]
        else:
            collection = {}
            master[class_name] = collection
        all_volumes = unrealsdk.find_all(class_name)
        all_volumes = [v for v in all_volumes if "PersistentLevel" in str(v)]
        for volume in all_volumes:
            always_update = class_name in [
                "BlockingMeshCollectionActor",
                "SeqEvent_Touch",
            ]
            if always_update or volume not in collection:
                if class_name == "BlockingMeshActor":
                    result = _add_blocking_mesh_component(
                        volume.CollisionComponent,
                        collection,
                        volume,
                    )
                elif class_name == "BlockingMeshCollectionActor":
                    result = _add_blocking_mesh_collection(volume, collection)
                elif class_name == "WillowBoundaryTurret":
                    result = _add_boundary_turret(volume, collection)
                elif class_name == "SeqEvent_Touch":
                    result = _add_touch_volume(volume, collection)
                elif class_name == "BehaviorVolume":
                    result = _add_behavior_volume(volume, collection)
                else:
                    result = _add_new_volume(volume, collection)
                if result:
                    _log(f"added: {volume}")


def _start_drawing_volumes(class_names, names, master):
    if not isinstance(class_names, list):
        class_names = [class_names]
    if not isinstance(names, list):
        names = [names]
    msg = ""
    pc = get_pc()
    if pc is None:
        _show_message("No player")
        return
    for class_name, name in zip(class_names, names, strict=False):
        if class_name in master:
            collection = master[class_name]
            count = 0
            for key in collection:
                count += 1
                info = collection[key]
                verts = info["verts"]
                tris = info["tris"]
                color = info["color"]
                i = 0
                while i < len(tris):
                    r, g, b = color
                    pc.DrawDebugLine(
                        verts[tris[i + 0]],
                        verts[tris[i + 1]],
                        r,
                        g,
                        b,
                        True,
                        86400,
                    )
                    pc.DrawDebugLine(
                        verts[tris[i + 1]],
                        verts[tris[i + 2]],
                        r,
                        g,
                        b,
                        True,
                        86400,
                    )
                    pc.DrawDebugLine(
                        verts[tris[i + 2]],
                        verts[tris[i + 0]],
                        r,
                        g,
                        b,
                        True,
                        86400,
                    )
                    i += 3
            if msg != "":
                msg += "\n"
            msg += f"{name}: {count}"
    if msg != "":
        _show_message(msg)


def _stop_drawing_volumes():
    pc = get_pc()
    if pc is not None:
        pc.FlushPersistentDebugLines()
    _show_message("Stopped drawing")


def _clear_all_volumes():
    global _kill_data, _collision_data, _hide_fake, _hidden_components
    _kill_data = {}
    _collision_data = {}
    pc = get_pc()
    if pc is not None:
        pc.FlushPersistentDebugLines()
    _hide_fake = False
    _hidden_components = []


def _reset_all():
    global _show_kill, _show_collision, _noclip_collision, _disabled_colliders
    global _solidify_meshes, _solidified_components, _next_colors
    _show_kill = False
    _show_collision = False
    _noclip_collision = False
    _disabled_colliders = []
    _solidify_meshes = False
    _solidified_components = []
    _clear_all_volumes()
    _next_colors = [0, 0]


# --- Toggle functions ---


def _toggle_kill():
    global _show_kill, _show_collision, _color_slot
    _show_kill = not _show_kill
    if _show_kill:
        _color_slot = 0
        kill_classes = [
            "PlayerKillVolume",
            "SeqEvent_Touch",
            "BehaviorVolume",
            "WillowBoundaryTurret",
        ]
        _update_volumes(kill_classes, _kill_data)
        kill_names = [
            "Kill volumes",
            "Trigger volumes",
            "Behavior volumes",
            "Turrets",
        ]
        _start_drawing_volumes(
            kill_classes,
            kill_names,
            _kill_data,
        )
    else:
        _stop_drawing_volumes()
        _show_collision = False


def _toggle_collision():
    global _show_collision, _show_kill, _color_slot
    _show_collision = not _show_collision
    if _show_collision:
        _color_slot = 1
        coll_classes = [
            "BlockingMeshCollectionActor",
            "BlockingMeshActor",
            "BlockingVolume",
        ]
        _update_volumes(coll_classes, _collision_data)
        coll_names = [
            "Collection blocking meshes",
            "Blocking mesh actors",
            "Blocking volumes",
        ]
        _start_drawing_volumes(
            coll_classes,
            coll_names,
            _collision_data,
        )
    else:
        _stop_drawing_volumes()
        _show_kill = False


def _toggle_noclip():
    global _noclip_collision, _disabled_colliders
    _noclip_collision = not _noclip_collision
    if _noclip_collision:
        count = 0
        # Disable both Unreal collision (CollideActors) for players on foot
        # and PhysX collision (BlockRigidBody) for vehicles
        for class_name in ["BlockingMeshActor", "BlockingVolume", "VehicleBlockingVolume"]:
            for volume in unrealsdk.find_all(class_name):
                if "PersistentLevel" not in str(volume):
                    continue
                comp = volume.CollisionComponent
                if comp is None or (not comp.CollideActors and not comp.BlockRigidBody):
                    continue
                _disabled_colliders.append(
                    (
                        comp,
                        comp.CollideActors,
                        comp.BlockActors,
                        comp.BlockNonZeroExtent,
                        comp.BlockRigidBody,
                    )
                )
                comp.CollideActors = False
                comp.SetBlockRigidBody(False)
                count += 1
        for volume in unrealsdk.find_all("BlockingMeshCollectionActor"):
            if "PersistentLevel" not in str(volume):
                continue
            for comp in volume.Components:
                if comp is None or (not comp.CollideActors and not comp.BlockRigidBody):
                    continue
                _disabled_colliders.append(
                    (
                        comp,
                        comp.CollideActors,
                        comp.BlockActors,
                        comp.BlockNonZeroExtent,
                        comp.BlockRigidBody,
                    )
                )
                comp.CollideActors = False
                comp.SetBlockRigidBody(False)
                count += 1
        _show_message(f"Collision noclip ON ({count} colliders disabled)")
    else:
        for comp, collide, block, block_nz, block_rb in _disabled_colliders:
            comp.CollideActors = collide
            comp.BlockActors = block
            comp.BlockNonZeroExtent = block_nz
            comp.SetBlockRigidBody(block_rb)
        _disabled_colliders = []
        _show_message("Collision noclip OFF")


def _has_good_collision(component):
    mesh = component.StaticMesh
    if mesh is None or mesh.BodySetup is None:
        return False
    geom = mesh.BodySetup.AggGeom
    if geom is None:
        return False
    n_box = _iter_len(geom.BoxElems)
    n_sphere = _iter_len(geom.SphereElems)
    n_sphyl = _iter_len(geom.SphylElems)
    n_convex = _iter_len(geom.ConvexElems)
    if n_box + n_sphere + n_sphyl + n_convex == 0:
        return False
    if not solidify_strict.value:
        return True
    if n_convex == 0:
        return False
    if n_convex == 1 and n_box == 0 and n_sphere == 0 and n_sphyl == 0:
        verts = 0
        for _v in geom.ConvexElems[0].VertexData:
            verts += 1
        if verts <= 8:
            return False
    return True


def _is_component_relevant(component):
    parent_name = str(component.Outer)
    return (
        "PersistentLevel" in parent_name
        and (
            "StaticMeshActor" in parent_name
            or "StaticMeshCollectionActor" in parent_name
            or "InterpActor" in parent_name
        )
        and component.StaticMesh is not None
        and "Skybox.Meshes.Sky_Dome" not in str(component.StaticMesh)
    )


def _is_component_fake(component):
    return (
        not component.CollideActors
        or not component.BlockActors
        or not component.BlockNonZeroExtent
        or not component.Outer.bBlockActors
        or not component.Outer.bCollideActors
    )


def _toggle_solidify():
    global _solidify_meshes, _solidified_components
    _solidify_meshes = not _solidify_meshes
    if _solidify_meshes:
        count = 0
        for component in unrealsdk.find_all("StaticMeshComponent"):
            if not _is_component_relevant(component):
                continue
            if not _is_component_fake(component):
                continue
            if not _has_good_collision(component):
                continue
            actor = component.Outer
            _solidified_components.append(
                (
                    component,
                    actor,
                    component.CollideActors,
                    component.BlockActors,
                    component.BlockNonZeroExtent,
                    actor.bCollideActors,
                    actor.bBlockActors,
                )
            )
            component.CollideActors = True
            component.BlockActors = True
            component.BlockNonZeroExtent = True
            actor.bCollideActors = True
            actor.bBlockActors = True
            count += 1
        _show_message(f"Solidify ON ({count} meshes)")
    else:
        for (
            comp,
            actor,
            comp_collide,
            comp_block,
            comp_block_nz,
            actor_collide,
            actor_block,
        ) in _solidified_components:
            comp.CollideActors = comp_collide
            comp.BlockActors = comp_block
            comp.BlockNonZeroExtent = comp_block_nz
            actor.bCollideActors = actor_collide
            actor.bBlockActors = actor_block
        _solidified_components = []
        _show_message("Solidify OFF")


def _toggle_fake():
    global _hide_fake, _hidden_components
    _hide_fake = not _hide_fake
    if _hide_fake:
        for component in unrealsdk.find_all("StaticMeshComponent"):
            if (
                _is_component_relevant(component)
                and _is_component_fake(component)
                and not component.HiddenGame
            ):
                _hidden_components.append(component)
                component.SetHidden(True)
        for terrain in unrealsdk.find_all("Terrain"):
            if not terrain.bBlockUnreal:
                for tcomp in terrain.TerrainComponents:
                    if not tcomp.HiddenGame:
                        _hidden_components.append(tcomp)
                        tcomp.SetHidden(True)
    else:
        for component in _hidden_components:
            component.SetHidden(False)
        _hidden_components = []
    _show_message("Hiding fake meshes" if _hide_fake else "Showing fake meshes")


# --- Keybinds ---


@keybind("Toggle Kill Volumes", "F2")
def kb_kill() -> None:
    _toggle_kill()


@keybind("Toggle Collision Volumes", "F3")
def kb_collision() -> None:
    _toggle_collision()


@keybind("Toggle Collision Noclip", "F4")
def kb_noclip() -> None:
    _toggle_noclip()


@keybind("Toggle Fake Meshes", "F5")
def kb_fake() -> None:
    _toggle_fake()


@keybind("Toggle Solidify Meshes", "F7")
def kb_solidify() -> None:
    _toggle_solidify()


# --- Map load reset hook ---


@hook(
    "WillowGame.WillowPlayerController:WillowClientDisableLoadingMovie",
    Type.PRE,
)
def on_map_load(
    obj: UObject,
    args: WrappedStruct,
    ret: any,
    func: BoundFunction,
) -> None:
    _reset_all()
    if auto_kill.value:
        _toggle_kill()
    if auto_collision.value:
        _toggle_collision()
    if auto_noclip.value:
        _toggle_noclip()
    if auto_fake.value:
        _toggle_fake()
    if auto_solidify.value:
        _toggle_solidify()


# --- Build mod ---

mod = build_mod(
    options=[auto_on_load, solidify_strict],
)
