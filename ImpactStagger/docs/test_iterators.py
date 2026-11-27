"""
Test UE3 native iterator functions from Python SDK.
Run with: pyexec test_iterators.py
Must be in-game with enemies spawned nearby for meaningful results.
"""

import unrealsdk
from time import perf_counter

log = print

log("=" * 60)
log("UE3 NATIVE ITERATOR FUNCTION TESTS")
log("=" * 60)

# Get WorldInfo — find the one that actually has pawns
wis = unrealsdk.find_all("WorldInfo")
wi = None
for w in wis:
    if w.Name.startswith("Default__"):
        continue
    print(f"  WorldInfo: {w} — PawnList: {w.PawnList}")
    if w.PawnList is not None and wi is None:
        wi = w

if wi is None:
    # fallback to first non-CDO
    for w in wis:
        if not w.Name.startswith("Default__"):
            wi = w
            break

if wi is None:
    print("ERROR: No WorldInfo found")
else:
    print(f"Using WorldInfo: {wi}")

# Get player pawn
player = None
for pc in unrealsdk.find_all("WillowPlayerController"):
    if pc.Name.startswith("Default__"):
        continue
    if pc.Pawn is not None:
        player = pc.Pawn
        break

if player is None:
    log("ERROR: No player pawn found — are you in-game?")
    raise SystemExit

log(f"Player: {player}")
pawn_class = unrealsdk.find_class("WillowAIPawn")
actor_class = unrealsdk.find_class("Actor")

# ─── Step 1: Check if iterator functions are visible ───
log("")
log("─── STEP 1: Attribute access ───")

tests = [
    ("WorldInfo.AllPawns", wi, "AllPawns"),
    ("WorldInfo.AllControllers", wi, "AllControllers"),
    ("Actor.VisibleActors", player, "VisibleActors"),
    ("Actor.VisibleCollidingActors", player, "VisibleCollidingActors"),
    ("Actor.CollidingActors", player, "CollidingActors"),
]

for label, obj, attr in tests:
    try:
        val = getattr(obj, attr)
        log(f"  {label} = {type(val).__name__}: {val}")
    except Exception as e:
        log(f"  {label} -> {type(e).__name__}: {e}")

# ─── Step 2: Try calling AllPawns ───
log("")
log("─── STEP 2: Call AllPawns ───")

# Try various calling conventions
call_tests = [
    ("AllPawns(class)", lambda: wi.AllPawns(pawn_class)),
    ("AllPawns(class, 0, 0, 0)", lambda: wi.AllPawns(pawn_class, 0.0, 0.0, 0.0)),
    ("AllPawns(class, loc, radius)", lambda: wi.AllPawns(pawn_class, player.Location, 10000.0)),
]

for label, fn in call_tests:
    try:
        result = fn()
        log(f"  {label} -> {type(result).__name__}: {result}")
        # If iterable, try to consume
        if result is not None:
            try:
                items = list(result)
                log(f"    list() -> {len(items)} items: {items[:3]}")
            except Exception as e2:
                log(f"    list() failed: {type(e2).__name__}: {e2}")
    except Exception as e:
        log(f"  {label} -> {type(e).__name__}: {e}")

# ─── Step 3: Try VisibleCollidingActors / CollidingActors ───
log("")
log("─── STEP 3: Radius-based iterator calls ───")

ploc = player.Location
log(f"  Player location: ({ploc.X:.0f}, {ploc.Y:.0f}, {ploc.Z:.0f})")

radius_tests = [
    (
        "CollidingActors(class, loc, radius)",
        lambda: player.CollidingActors(pawn_class, player.Location, 5000.0),
    ),
    (
        "VisibleCollidingActors(class, loc, radius)",
        lambda: player.VisibleCollidingActors(pawn_class, player.Location, 5000.0),
    ),
    (
        "VisibleActors(class, loc, radius)",
        lambda: player.VisibleActors(pawn_class, player.Location, 5000.0),
    ),
]

for label, fn in radius_tests:
    try:
        result = fn()
        log(f"  {label} -> {type(result).__name__}: {result}")
        if result is not None:
            try:
                items = list(result)
                log(f"    list() -> {len(items)} items")
                for item in items[:5]:
                    log(f"      {item}")
            except Exception as e2:
                log(f"    list() failed: {type(e2).__name__}: {e2}")
    except Exception as e:
        log(f"  {label} -> {type(e).__name__}: {e}")

# ─── Step 3b: Try calling with Actor class instead ───
log("")
log("─── STEP 3b: Try with Actor class ───")
try:
    result = player.CollidingActors(actor_class, player.Location, 5000.0)
    log(f"  CollidingActors(Actor, loc, 5000) -> {type(result).__name__}: {result}")
    if result is not None:
        try:
            items = list(result)
            log(f"    list() -> {len(items)} items")
            for item in items[:5]:
                log(f"      {item}")
        except Exception as e2:
            log(f"    list() failed: {type(e2).__name__}: {e2}")
except Exception as e:
    log(f"  CollidingActors(Actor) -> {type(e).__name__}: {e}")

# ─── Step 4: Baseline benchmark (PawnList walk + distance check) ───
log("")
log("─── STEP 4: Baseline benchmark ───")


def _iter_ai_pawns():
    pawn = wi.PawnList
    while pawn is not None:
        if pawn.Class.Name == "WillowAIPawn":
            yield pawn
        pawn = pawn.NextPawn


def manual_radius_check(center_x, center_y, center_z, radius):
    r_sq = radius * radius
    results = []
    for pawn in _iter_ai_pawns():
        loc = pawn.Location
        dx = loc.X - center_x
        dy = loc.Y - center_y
        dz = loc.Z - center_z
        if dx * dx + dy * dy + dz * dz <= r_sq:
            results.append(pawn)
    return results


# Warm up
ploc = player.Location
nearby = manual_radius_check(ploc.X, ploc.Y, ploc.Z, 5000.0)
log(f"  Manual check found {len(nearby)} pawns within 5000 units")
for p in nearby[:5]:
    log(f"    {p.AIClass.Name if p.AIClass else p.Name}")

# Benchmark manual approach
N = 100
ploc = player.Location
t0 = perf_counter()
for _ in range(N):
    manual_radius_check(ploc.X, ploc.Y, ploc.Z, 5000.0)
t1 = perf_counter()
manual_avg = (t1 - t0) / N * 1000  # ms
log(f"  Manual PawnList walk + distance: {manual_avg:.3f} ms avg ({N} runs)")

# If any native iterator worked, we'd benchmark it here too
# (will add dynamically if step 2/3 succeed)

log("")
log("=" * 60)
log("TESTS COMPLETE — check results above")
log("=" * 60)
