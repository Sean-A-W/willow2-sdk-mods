# Ragdoll Bowling

## Overview

When a ragdolled enemy collides with a standing enemy, the standing enemy is staggered and receives momentum transfer. This creates a "bowling" effect where enemies can chain-stagger each other.

## Implementation: Touch-Based (Event-Driven)

Uses `WillowAIPawn:Touch` PRE hook — fires when CylinderComponents overlap (actual pawn contact), including ragdolled pawns.

### How It Works

1. Touch fires on BOTH pawns per overlap — only process when `obj` is the ragdolled bowler (natural dedup)
2. Hook in `__init__.py` checks if `obj` is in `RAGDOLLED_PAWNS`, delegates to `bowling.handle_touch()`
3. `handle_touch` checks:
   - Velocity threshold (ragdolled pawn must be moving fast enough)
   - Target eligibility (standing, alive, not scripted, not unstaggerable)
   - Dedup via `_hit_pairs` (prevent re-staggering the same pair within a cooldown)
4. If eligible: calls `pawn.Stagger(None)` on the target + queues impulse based on bowler's velocity

### Why Touch Instead of Polling

| Approach | Cost | Result |
|----------|------|--------|
| Polling (PawnList scan) | O(ragdolls * standing_enemies) every 0.1s | Worked but unnecessary overhead |
| Touch (event-driven) | O(1) per actual collision | Zero polling, instant detection |

Touch fires when CylinderComponents overlap — exactly when physical contact happens. No scanning needed.

### Failed Approaches

- **CylinderComponent SetBlockRigidBody**: doesn't participate in PhysX — has no effect
- **SkeletalMesh physics bodies on standing enemies**: kinematic bodies don't generate PhysX collision response — ragdolls pass through
- **Polling-based proximity scan**: worked (O(ragdolls * standing_enemies) every 0.1s) but replaced by Touch for better performance
