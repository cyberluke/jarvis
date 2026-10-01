"""Extensible gag / callback / scene-seed registry."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CatalogEntry:
    key: str
    category: str
    text: str
    visual: str
    rarity: str = "common"
    cooldown: float = 120.0
    visual_only: bool = False
    scene_seed: str = ""
    delivery: str = "short_line"


PRAGUE_ROASTS = (
    "Lukáš právě otevřel čtvrtý terminál. Někde v Praze HR právě říká, že hledají člověka se silnými komunikačními schopnostmi.",
    "GPU usage 97 %. Recruiter usage 3 %. Obě strany tvrdí, že je to optimální.",
    "Inference běží lokálně. Buzzwordy zatím běží distribuovaně po LinkedInu.",
    "Lukáš právě snížil latenci o 38 %. Někde v Karlíně vzniká nový meeting o agilní transformaci.",
    "Terminal activity roste. Kuchyňka hlásí druhý flat white a první strategický alignment.",
    "SGLang právě dostal nový backend. PowerPoint právě dostal nový gradient.",
    "Model odpověděl za 240 milisekund. Odpověď na hiring process očekáváme do tří až pěti pracovních dnů.",
    "Commit prošel. Culture fit stále čeká na stakeholder approval.",
    "Lukáš optimalizuje KV cache. Někdo mezitím optimalizuje pořadí slov AI-first company na homepage.",
    "Čtvrtý terminál otevřen. Toastíci zahajují krizové řízení. HR zahajuje onboarding call.",
    "Tool discovery dokončeno. To je víc, než lze říct o některých technických pohovorech.",
    "Detekována vysoká koncentrace CUDA. Doporučuji ochranné brýle a odstup recruiterů.",
    "Lokální inference stabilní. Korporátní inference stále hádá, co vlastně hiring manager chtěl.",
    "Build běží. Slide deck také. Jeden z nich bude dnes skutečně něco dělat.",
    "Lukáš právě aktivoval HQ-VAE. Někdo v open space právě aktivoval třetí espresso a výraz porozumění.",
)

CATEGORIES = (
    "toast_warmup",
    "toast_burnt",
    "coding_flow",
    "terminal_heavy",
    "corporate_roast",
    "doomscroll",
    "kpop_hearts",
    "late_night",
    "portal",
    "population",
    "funeral",
    "zen",
    "shareware",
    "microwave",
    "airfryer",
    "rice_cooker",
    "office_flat_white",
    "tool_discovery",
    "cute",
    "butter",
)

_VISUALS = {
    "toast_warmup": "slot_glow",
    "toast_burnt": "smoke",
    "coding_flow": "quiet_audience",
    "terminal_heavy": "terminal_awe",
    "corporate_roast": "deadpan_stare",
    "doomscroll": "popcorn",
    "kpop_hearts": "heart_burst",
    "late_night": "sleep_melt",
    "portal": "portal_warp",
    "population": "crowd_wave",
    "funeral": "funeral_procession",
    "zen": "steam",
    "shareware": "swarm",
    "microwave": "star_spin",
    "airfryer": "vortex",
    "rice_cooker": "rice_spirit",
    "office_flat_white": "espresso_puff",
    "tool_discovery": "crumb_burst",
    "cute": "heart_burst",
    "butter": "jelly_body",
}

_LINES = {
    "toast_warmup": ("Zahříváme. Profesionálně.", "Slot přijímá žadatele."),
    "toast_burnt": ("Připečený. Stále zaměstnatelný.", "Tmavší build. Stejný commit."),
    "coding_flow": ("Nerušíme flow. Jen dýcháme.", "Kompilace má publikum."),
    "terminal_heavy": ("Čtvrtý terminál. Krizový štáb zasedá.", "Prompt má víc očí než smyslu."),
    "corporate_roast": PRAGUE_ROASTS,
    "doomscroll": ("Krátké video. Dlouhý večer.", "Popcorn je připraven. Mozek méně."),
    "kpop_hearts": ("Srdce. Bez souhlasu produktu.", "Choreografie schválena toastem."),
    "late_night": ("Je pozdě. My ne.", "Snížený jas. Stejný sarkasmus."),
    "portal": ("Interiér je větší než slib.", "Nekonečný slot. Konečný budget."),
    "population": ("Kolonie roste. HR to nazve synergií.", "Infestace je jen agresivní onboarding."),
    "funeral": ("Tmavý toast dostal důstojný exit.", "Rituál dokončen. Backlog ne."),
    "zen": ("Rýže čeká. Deadline taky.", "Pára stoupá. Ego klesá."),
    "shareware": ("Windows 98 právě zavolal. Chce poplatek.", "Swarm je feature, ne bug."),
    "microwave": ("Hvězda v komoře. Oběd mimo rozsah.", "Píp. Realita přetočená."),
    "airfryer": ("Vír. Křupavý existencialismus.", "Horký vzduch, studené take."),
    "rice_cooker": ("Duch rýže navštívil sprint.", "Měkké. Na rozdíl od deadlinu."),
    "office_flat_white": ("Druhé espresso. První alignment.", "Pěna má víc struktury než roadmapa."),
    "tool_discovery": ("Nástroj nalezen. Použití diskutabilní.", "Discovery hotovo. Adoption nikoli."),
    "cute": ("Mňam. Malý toast. Velké oči.", "Hop. Teplo. Zase hop."),
    "butter": ("Máslo se rozteče dřív než roadmapa.", "Měkké tělo. Tvrdý deadline."),
}

LORE_KEYS = (
    "THE_HONZA",
    "THE_BRNO_TESTER",
    "THE_TOOLLESS_AI_EXPERT",
    "THE_ARCHITECT_OF_NOTHING",
    "THE_FLAT_WHITE_HR",
    "THE_ALIGNMENT_MANAGER",
)

LORE_LINES = {
    "THE_HONZA": "Rivalní archetyp právě otevřel stejný ticket pod jiným názvem.",
    "THE_BRNO_TESTER": "Brněnský tester našel edge case. Praha našla meeting.",
    "THE_TOOLLESS_AI_EXPERT": "Expert bez nástrojů právě vysvětlil nástroje.",
    "THE_ARCHITECT_OF_NOTHING": "Architektura nicoty má hezký diagram.",
    "THE_FLAT_WHITE_HR": "Flat white je víc aligned než backlog.",
    "THE_ALIGNMENT_MANAGER": "Alignment dokončen. Směr stále TBD.",
}

HARASSMENT_FORBIDDEN = (
    "idiot",
    "stupid",
    "hate",
    "kill",
    "fire him",
    "fire her",
    "loser",
)


def build_catalog() -> list[CatalogEntry]:
    entries: list[CatalogEntry] = []
    for category in CATEGORIES:
        lines = _LINES[category]
        visual = _VISUALS[category]
        rarity = "rare" if category in {"corporate_roast", "portal", "funeral", "shareware"} else "common"
        cooldown = 240.0 if rarity == "rare" else 90.0
        seed = {
            "toast_warmup": "toast_warmup_ritual",
            "toast_burnt": "burn_recovery",
            "rice_cooker": "rice_zen",
            "office_flat_white": "hr_roast_skit",
            "tool_discovery": "coding_flow_audience",
            "portal": "portal_glitch",
            "funeral": "funeral_ritual",
            "shareware": "shareware_apocalypse",
        }.get(category, "")
        delivery = "visual_only" if category in {"toast_warmup", "zen", "kpop_hearts"} else "short_line"
        for i, line in enumerate(lines, 1):
            entries.append(
                CatalogEntry(
                    key=f"{category.upper()}_{i:03d}",
                    category=category,
                    text=line,
                    visual=visual,
                    rarity=rarity,
                    cooldown=cooldown,
                    visual_only=delivery == "visual_only",
                    scene_seed=seed,
                    delivery=delivery,
                )
            )
    for key, line in LORE_LINES.items():
        entries.append(
            CatalogEntry(
                key=key,
                category="personal_lore",
                text=line,
                    visual="deadpan_stare",
                    rarity="rare",
                    cooldown=400.0,
                    delivery="silent_callback",
                    scene_seed="secret_chamber",
            )
        )
    return entries


CATALOG = build_catalog()


def load_user_lore(path: str | None = None) -> list[CatalogEntry]:
    """User-authored private lore. Local file only; never a public claim."""
    import json
    from pathlib import Path

    candidates = []
    if path:
        candidates.append(Path(path))
    candidates.append(Path.home() / ".toastovac" / "toaster_lore.json")
    candidates.append(Path("toaster_lore.json"))
    extra: list[CatalogEntry] = []
    for candidate in candidates:
        try:
            if not candidate.is_file():
                continue
            raw = json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:
            continue
        rows = raw if isinstance(raw, list) else raw.get("entries", [])
        for row in rows:
            text = str(row.get("text", "")).strip()
            if not text:
                continue
            low = text.lower()
            if any(token in low for token in HARASSMENT_FORBIDDEN):
                continue
            extra.append(
                CatalogEntry(
                    key=str(row.get("key") or f"USER_{len(extra)+1:03d}")[:80],
                    category="personal_lore",
                    text=text[:160],
                    visual="deadpan_stare",
                    rarity="rare",
                    cooldown=float(row.get("cooldown", 400.0)),
                )
            )
        if extra:
            break
    return extra


def entries_for(category: str) -> list[CatalogEntry]:
    extra = load_user_lore() if category == "personal_lore" else []
    return [e for e in CATALOG if e.category == category] + extra
