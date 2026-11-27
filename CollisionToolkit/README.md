# Collision Toolkit

A [willow2-mod-manager](https://github.com/bl-sdk/oak-mod-manager) SDK mod for Borderlands 2 / TPS / AoDK that lets you visualize, disable, and manipulate invisible collision geometry for map exploration and debugging.

## Keybinds

| Key | Function | Description |
|-----|----------|-------------|
| F2 | Toggle Kill Volumes | Draws kill volumes, trigger-death volumes, behavior kill volumes, and boundary turret kill planes as colored wireframes. |
| F3 | Toggle Collision Volumes | Draws invisible blocking geometry (`BlockingMeshActor`, `BlockingMeshCollectionActor`, `BlockingVolume`) as colored wireframes. |
| F4 | Toggle Collision Noclip | Disables all invisible blocking geometry — walk and drive through invisible walls. Disables both player collision (`CollideActors`) and vehicle collision (`BlockRigidBody`). Also covers `VehicleBlockingVolume`. |
| F5 | Toggle Fake Meshes | Hides visible meshes and terrain that have no collision (decorative geometry), making it easier to see what's actually solid. |
| F7 | Toggle Solidify Meshes | Enables collision on visible meshes that currently have none (e.g. roofs, decorative architecture). Only solidifies meshes with proper convex collision geometry — skips bounding-box approximations that wouldn't match the visual shape. |

All keybinds are rebindable from the mod menu. All state resets automatically on map transitions.

## Installation

Drop the `CollisionToolkit` folder into your `sdk_mods` directory (or a secondary mods directory configured in your SDK settings).

## Technical Notes

- Wireframes are drawn using `DrawDebugLine` with automatic triangle subdivision for visibility on large surfaces.
- Only actors in `PersistentLevel` are processed (streaming/sub-level actors are excluded).
- **Noclip (F4)** disables two independent collision systems: Unreal's `CollideActors`/`BlockActors` (affects players on foot) and PhysX's `BlockRigidBody` (affects vehicles). Both are restored on toggle-off.
- **Solidify (F7)** filters out meshes whose collision geometry is just a bounding-box approximation (single `BoxElem`, `SphereElem`, `SphylElem`, or a `ConvexElem` with 8 or fewer vertices). Only meshes with detailed convex hulls (>8 vertices) or multi-element convex decompositions are solidified.
- Original collision property values are saved per-component and fully restored when toggling off. On map change, state is simply cleared (old map UObjects are destroyed by the engine).

## Credits

Based on [CollisionVisualizer](https://github.com/NoneGiven/bl-sdk-mods/tree/main/CollisionVisualizer) by NoneGiven.
