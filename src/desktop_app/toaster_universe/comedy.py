"""Comedy director. Timing, contrast, cooldown. Never explain the punchline."""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from .catalog import CATALOG, CatalogEntry, entries_for
from .config import WorldConfig
from .signals import UserSignals
from .types import ActivityState, ComedyStyle, SceneId


@dataclass
class ComedyBeat:
    entry: CatalogEntry
    style: ComedyStyle
    t: float
    visual_only: bool


class ComedyDirector:
    def __init__(self, cfg: WorldConfig, rng: random.Random) -> None:
        self.cfg = cfg
        self.rng = rng
        self.last_by_key: dict[str, float] = {}
        self.last_by_category: dict[str, float] = {}
        self.last_line_t = -1e9
        self.style = ComedyStyle.TECHNICAL_PRAGUE_DEADPAN
        self.penalties = 0

    def style_for(self, activity: ActivityState, scene: SceneId) -> ComedyStyle:
        if scene in {SceneId.SHAREWARE_APOCALYPSE, SceneId.TOAST_INFESTATION}:
            return ComedyStyle.SHAREWARE_CHAOS
        if scene is SceneId.KPOP_HEART_MODE:
            return ComedyStyle.ANIME_GAG
        if scene in {SceneId.FUNERAL_RITUAL, SceneId.BURN_RECOVERY}:
            return ComedyStyle.DARK_TOAST
        if scene is SceneId.RICE_ZEN:
            return ComedyStyle.ZEN_IRONY
        if scene is SceneId.HR_ROAST_SKIT:
            return ComedyStyle.CORPORATE_ROAST
        if activity is ActivityState.DOOMSCROLLING:
            return ComedyStyle.ABSURD_PROFESSIONAL
        if activity in {ActivityState.CODING_FLOW, ActivityState.TERMINAL_HEAVY}:
            return ComedyStyle.TECHNICAL_PRAGUE_DEADPAN
        if activity is ActivityState.LATE_NIGHT:
            return ComedyStyle.EXISTENTIAL_CRUMB
        if activity is ActivityState.IDLE:
            return ComedyStyle.CUTE_KAWAII
        if scene is SceneId.QUIET_COMPANIONSHIP:
            return ComedyStyle.CUTE_KAWAII
        if scene is SceneId.SECRET_CHAMBER:
            return ComedyStyle.ABSURD_PROFESSIONAL
        if activity is ActivityState.FOCUSED:
            return ComedyStyle.OFFICE_SATIRE
        if activity is ActivityState.BROWSING:
            return ComedyStyle.META_AI
        return ComedyStyle.QUIET_VISUAL_ONLY

    def pick(
        self,
        t: float,
        signals: UserSignals,
        scene: SceneId,
        force_category: str | None = None,
        speech_ok: bool = False,
        ignore_entry_cooldown: bool = False,
    ) -> ComedyBeat | None:
        if signals.activity in {ActivityState.CODING_FLOW, ActivityState.FRANTIC, ActivityState.FOCUSED, ActivityState.TERMINAL_HEAVY}:
            if signals.focus_score >= self.cfg.focus_threshold and not force_category:
                return None
        if (signals.selecting or signals.scrolling) and not force_category:
            return None
        if t - self.last_line_t < self.cfg.comedy_cooldown_sec and not force_category:
            return None
        category = force_category or {
            ActivityState.CODING_FLOW: "coding_flow",
            ActivityState.TERMINAL_HEAVY: "terminal_heavy",
            ActivityState.DOOMSCROLLING: "doomscroll",
            ActivityState.LATE_NIGHT: "late_night",
            ActivityState.IDLE: "cute",
            ActivityState.FOCUSED: "office_flat_white",
            ActivityState.BROWSING: "tool_discovery",
        }.get(signals.activity, "toast_warmup")
        if scene is SceneId.QUIET_COMPANIONSHIP and not force_category:
            category = "cute"
        if scene is SceneId.HR_ROAST_SKIT:
            category = "corporate_roast"
        elif scene is SceneId.FUNERAL_RITUAL:
            category = "funeral"
        elif scene is SceneId.BURN_RECOVERY:
            category = "toast_burnt"
        elif scene is SceneId.PORTAL_GLITCH:
            category = "portal"
        elif scene is SceneId.TOAST_INFESTATION:
            category = "population"
        elif scene is SceneId.RICE_ZEN:
            category = "rice_cooker"
        elif scene is SceneId.MICROWAVE_ANOMALY:
            category = "microwave"
        elif scene is SceneId.AIRFRYER_VORTEX:
            category = "airfryer"
        elif scene is SceneId.SHAREWARE_APOCALYPSE:
            category = "shareware"
        elif scene is SceneId.KPOP_HEART_MODE:
            category = "kpop_hearts"
        elif scene is SceneId.TOAST_WARMUP_RITUAL:
            category = "toast_warmup"
        elif signals.activity is ActivityState.FOCUSED:
            category = "office_flat_white"
        elif signals.activity is ActivityState.BROWSING:
            category = "tool_discovery"
        elif self.cfg.personal_lore_enabled and scene in {SceneId.SECRET_CHAMBER, SceneId.CODING_FLOW_AUDIENCE} and self.rng.random() < 0.18:
            category = "personal_lore"
        if getattr(signals, "browning_comedy", 0.0) >= 0.6 and not force_category:
            category = "toast_burnt"
        pool = entries_for(category) or list(CATALOG)
        ready = []
        for entry in pool:
            if not ignore_entry_cooldown:
                if t - self.last_by_key.get(entry.key, -1e9) < entry.cooldown:
                    self.penalties += 1
                    continue
                if t - self.last_by_category.get(entry.category, -1e9) < entry.cooldown * 0.45:
                    self.penalties += 1
                    continue
            ready.append(entry)
        if not ready:
            return None
        visual_first = [e for e in ready if e.visual_only]
        rare = [e for e in ready if e.rarity == "rare"]
        common = [e for e in ready if e.rarity != "rare"]
        if rare and self.rng.random() < 0.22:
            pool_choice = rare
        else:
            pool_choice = visual_first or common or ready
        entry = self.rng.choice(pool_choice)
        if entry.category == "personal_lore":
            from .catalog import HARASSMENT_FORBIDDEN

            low = entry.text.lower()
            if any(token in low for token in HARASSMENT_FORBIDDEN):
                self.penalties += 1
                return None
        self.last_by_key[entry.key] = t
        self.last_by_category[entry.category] = t
        if speech_ok and not entry.visual_only:
            self.last_line_t = t
        self.style = self.style_for(signals.activity, scene)
        # Ridiculous situations stay deadpan: never wink via punctuation spam.
        if "!" in entry.text or entry.text.strip().endswith("lol"):
            self.penalties += 1
            return None
        return ComedyBeat(entry, self.style, t, entry.visual_only or not speech_ok)
