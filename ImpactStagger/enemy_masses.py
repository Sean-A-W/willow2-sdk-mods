from __future__ import annotations


# Real-world-like masses for BL2 enemies, in kg at DrawScale=1.0.
# Higher = heavier = harder to launch with the same impulse.
# Effective mass = ENEMY_MASS[name] * DrawScale³ (double size = 8x mass).
# Keyed by AIClass.Name.
ENEMY_MASS: dict[str, float] = {
    # Skags
    "CharClass_SkagPup": 35,  # medium dog
    "CharClass_SkagAdult": 60,  # large dog
    "CharClass_SkagSpitter": 60,
    "CharClass_SkagBarfer": 60,
    "CharClass_SkagFreeWilly": 60,
    "CharClass_SkagPair": 60,  # paired skag variant
    "CharClass_SkagAlpha": 75,  # very large dog
    "CharClass_SkagChubby": 90,  # ds=2.0 in bl2 recoded
    "CharClass_SkagBadassSlag": 80,  # bear sized
    "CharClass_SkagBadassShock": 80,
    "CharClass_SkagBadassFire": 80,
    "CharClass_SkagBadassCorrosive": 80,
    # Rakk - flying, very light
    "CharClass_RakkDrone": 8,  # large bird
    "CharClass_RakkBadass": 12,  # larger bird
    "CharClass_RakkSlag": 8,
    "CharClass_RakkChubby": 8,  # ds=2.0 -> effective 64
    "CharClass_NamedRakk1": 10,  # named variant
    # Spiderants - insectoid quadrupeds
    "CharClass_SpiderantSpiderling": 25,  # small
    "CharClass_SpiderantWorker": 35,
    "CharClass_SpiderantWarrior": 45,
    "CharClass_SpiderantSoldier": 50,
    "CharClass_SpiderantSlagged": 50,
    "CharClass_SpiderantRoyalGuard": 60,
    "CharClass_SpiderantQueen": 70,  # large matriarch
    "CharClass_SpiderantKnight": 65,  # heavy armored
    "CharClass_SpiderantChubby": 50,  # ds=2.0 -> effective 400
    # Midgets - small humans
    "CharClass_PsychoMidget": 40,
    "CharClass_MarauderMidget": 40,
    "CharClass_SuicideMidget": 40,
    "CharClass_MidgetBone1": 40,  # named midget
    "CharClass_BadassMidget": 50,  # Goliath rig, heavier
    "CharClass_NomadMidget": 45,  # Nomad rig midget
    "CharClass_MidgetShield": 45,  # shield adds weight
    "CharClass_MidgetShield2": 45,
    "CharClass_MidgetShield3": 45,
    "CharClass_GoliathMidget": 50,  # Goliath rig, staggerable unlike full Goliaths
    "CharClass_LootMidget_Marauder": 40,
    "CharClass_LootMidget_CombatEngineer": 40,
    "CharClass_LootMidget_Goliath": 50,
    "CharClass_LootMidget_Nomad": 45,
    "CharClass_LootMidget_Psycho": 40,
    "CharClass_LootMidget_Rat": 40,
    # Psychos - average human, unarmored
    "CharClass_Psycho": 75,
    "CharClass_MarauderPsycho": 75,
    "CharClass_SuicidePsycho": 75,
    "CharClass_PsychoSnow": 75,
    "CharClass_PsychoBurning": 75,  # burning psycho
    "CharClass_PsychoSlag": 75,
    "CharClass_ProspectorPsycho": 75,
    "CharClass_ArmoredPsycho": 85,  # armored
    "CharClass_PsychoBadass": 95,  # large, muscular
    # Marauders - human with gear
    "CharClass_MarauderGrunt": 80,
    "CharClass_MarauderRegular": 85,
    "CharClass_MarauderElite": 90,  # heavier gear
    "CharClass_MarauderIntro": 80,  # tutorial marauder
    "CharClass_MarauderRider": 80,
    "CharClass_Scavenger": 80,
    "CharClass_MarauderBadass": 100,  # heavy gear
    "CharClass_BlackOps": 85,  # Hyperion soldier
    "CharClass_EngineerRifle": 85,
    "CharClass_FleshStick": 80,
    "CharClass_JacksBodyDouble": 80,
    "CharClass_TundraPatrol": 85,
    "CharClass_ResistanceFighter": 80,
    "CharClass_Matchstick": 80,  # named marauder
    "CharClass_SarcasticSlab": 85,  # named marauder
    "CharClass_Undertaker": 85,  # named marauder
    # Hyperion soldiers
    "CharClass_HyperionHawk": 85,
    "CharClass_HyperionInfiltrator": 85,
    "CharClass_HyperionSoldier": 85,
    "CharClass_Engineer_DJHyperion": 85,
    "CharClass_Ep6_Roland": 85,  # Roland as combatant
    # Gutter bandits
    "CharClass_BarloGutter": 80,
    "CharClass_EnnisGutter": 80,
    "CharClass_HarkGutter": 80,
    "CharClass_ShinGutter": 80,
    # Nomads / Bruisers - large, heavily armored
    "CharClass_Nomad": 150,
    "CharClass_NomadBadass": 170,
    "CharClass_NomadNamed_1": 150,  # Bad Maw, ds=1.5 -> effective 506
    "CharClass_NomadShield": 150,
    "CharClass_NomadShieldOnly": 200,  # massive shield
    "CharClass_NomadShock": 150,
    "CharClass_Prospector": 150,
    "CharClass_FireMad": 150,
    "CharClass_MrMercy": 150,
    "CharClass_Bruiser": 140,  # Nomad rig, slightly lighter
    "CharClass_Muscles": 150,  # named Nomad
    "CharClass_PapaBandit": 160,  # named Nomad, heavy
    "CharClass_Flynt": 150,  # boss, Nomad rig
    # Rats
    "CharClass_Boll": 80,
    "CharClass_ProspectorRat": 75,
    "CharClass_RatGrunt": 65,  # light, agile
    "CharClass_RatLab": 70,
    "CharClass_RatMidget": 40,  # small rat
    "CharClass_RatPlague": 70,
    "CharClass_RatRider": 70,
    "CharClass_RatThief": 65,  # light, fast
    "CharClass_RatEasterEgg": 70,
    "CharClass_Rat_Dan": 70,  # named rat (TMNT)
    "CharClass_Rat_Lee": 70,
    "CharClass_Rat_Mick": 70,
    "CharClass_Rat_Ralph": 70,
    "CharClass_Mortar": 75,  # named rat boss
    # Named / unique bandits
    "CharClass_SavageLee": 85,
    "CharClass_Daisy": 80,
    "CharClass_Boom": 80,  # Boom (small, marauder rig)
    "CharClass_BoomBoom": 85,  # Bewm's turret partner
    "CharClass_BFF_Jim": 80,  # friendship gulag
    "CharClass_BFF_Lindy": 80,
    "CharClass_BFF_OCantler": 80,
    "CharClass_BFF_Sam": 80,
    # Primal Beasts / Bullymongs
    "CharClass_PrimalBeast": 25,
    "CharClass_PrimalBeastBaby": 15,  # baby bullymong
    "CharClass_PrimalBeastAdult": 35,
    "CharClass_PrimalBeastBadass": 45,
    "CharClass_PrimalBeast_Ranged": 30,
    "CharClass_PrimalBeast_Bedrock": 35,  # bedrock variant
    "CharClass_PrimalBeast_BedrockBadass": 45,
    "CharClass_PrimalBeast_KnuckleDragger": 40,  # intro boss
    "CharClass_Demong": 35,  # bullymong variant
    "CharClass_BullyRot": 25,
    # BugMorphs - insectoids
    "CharClass_BugMorph": 20,
    "CharClass_BugMoprhRaid": 20,
    "CharClass_BugMoprhUltimate": 20,
    "CharClass_Bugmorph_Badass": 20,
    "CharClass_Bugmorph_SuperBadass": 20,
    "CharClass_BugMorphBadass_Mutated": 35,
    "CharClass_BugMorph_Adult": 50,
    "CharClass_BugMorphChubby": 50,
    "CharClass_Bugmorph_SirReginald": 50,
    # Stalkers
    "CharClass_Stalker": 45,
    "CharClass_StalkerDay": 45,
    "CharClass_StalkerFreeWilly": 45,
    "CharClass_StalkerNight": 55,
    "CharClass_StalkerSlag": 55,
    "CharClass_StalkerBadassMale": 70,
    "CharClass_StalkerChubby": 45,  # ds=2.0 in bl2 recoded -> effective 360
    "CharClass_Stalker_Name1": 90,
    "CharClass_StalkerPair": 55,  # paired stalker variant
    "CharClass_StalkerSpiral": 50,  # spiral stalker
    # Loaders - robots
    "CharClass_LoaderGUN": 120,
    "CharClass_LoaderBUL": 120,
    "CharClass_LoaderEXP": 120,
    "CharClass_LoaderHOT": 120,
    "CharClass_LoaderPWR": 120,
    "CharClass_LoaderION": 120,
    "CharClass_LoaderJET": 120,
    "CharClass_LoaderRPG": 120,
    "CharClass_LoaderSGT": 120,
    "CharClass_LoaderGuard": 120,
    "CharClass_LoaderJunk": 120,  # junk loader
    "CharClass_JimmyJenkins": 120,
    "CharClass_LoaderWAR": 250,  # heavy robot
    "CharClass_LoaderWAR_1340": 250,  # named WAR loader
    "CharClass_LoaderBadass": 250,
    "CharClass_BoneHead2": 120,  # BoneHead 2.0, loader rig
    "CharClass_PinocchioBot": 120,  # loader rig
    "CharClass_Willhelm": 160,  # boss, armored
    "CharClass_LootMidget_LoaderGUN": 120,
    "CharClass_LootMidget_LoaderJET": 120,
    "CharClass_LootMidget_LoaderWAR": 120,
    # LWT Loaders - small
    "CharClass_LoaderLWT": 40,
    # Engineers
    "CharClass_EngineerArms": 100,  # with equipment
    "CharClass_LootMidget_Engineer": 45,
    "CharClass_TravelingSalesman": 85,
    # Probes - small flying robots
    "CharClass_Probes": 30,
    "CharClass_ProbeBadass": 30,
    "CharClass_ProbeShield": 30,
    "CharClass_ProbeShieldBadass": 30,
    # Gyrocopters
    "CharClass_Gyrocopter": 60,
    "CharClass_GyrocopterBadass": 60,
    "CharClass_Gyrocopter-BunkerBoss": 60,  # BNK-3R minion
}


def get_base_mass(ai_name: str | None) -> float | None:
    """Look up base mass (kg at ds=1.0) from dict. Returns None if unknown."""
    if ai_name is not None:
        return ENEMY_MASS.get(ai_name)
    return None
