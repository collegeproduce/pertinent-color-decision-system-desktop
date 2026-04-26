"""
CPCE - User Hint Engine

Human-in-the-loop hint interface for paralegal/attorney annotations.
Applied after arbitration, before _make_decision_v10().

Hint types
----------
"pertinent"  → floor effective_pertinence to PERTINENT_FLOOR (0.76).
               Guard: requires visual_evidence_score >= PERTINENT_MIN_VE (0.10).
               Prevents flagging confirmed-blank pages as COLOR.

"decorative" → cap effective_pertinence at DECORATIVE_CAP (0.50).
               Guard: suppressed if authority_weight >= DECORATIVE_AUTHORITY_BYPASS (0.80).
               Never hide confirmed photographic/stamp evidence.

"review"     → force decision_zone to "review_required" regardless of score.
               Score is NOT modified; the zone override persists through BERT pass.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional


# ─────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────

PERTINENT_FLOOR             = 0.76   # minimum pertinence when hint="pertinent"
DECORATIVE_CAP              = 0.50   # maximum pertinence when hint="decorative"
PERTINENT_MIN_VE            = 0.10   # blank-page guard: require some visual evidence
DECORATIVE_AUTHORITY_BYPASS = 0.80   # suppress decorative cap for high-authority evidence


# ─────────────────────────────────────────────────────────────
# Data Structure
# ─────────────────────────────────────────────────────────────

@dataclass
class HintResult:
    """Output of UserHintEngine.apply() for one page."""
    effective_pertinence: float   # possibly modified pertinence
    force_review:         bool    # True → override decision_zone to REVIEW_REQUIRED
    hint_applied:         bool    # True → a pertinent/decorative floor/cap was applied
    override_reason:      str     # reasoning trace entry (empty if no hint)
    warning:              str     # safety-guard warning (empty if no guard triggered)


# ─────────────────────────────────────────────────────────────
# Engine
# ─────────────────────────────────────────────────────────────

class UserHintEngine:
    """
    Applies per-page human hints to the pertinence score.

    Usage
    -----
    1. Call set_hints(page_hints) at the start of process_document().
    2. Call apply(page_idx, ...) after arbitration in _process_single_page().
    3. Use force_review from the result to override the decision zone after
       _make_decision_v10() — and again in the BERT refinement pass.
    """

    def __init__(self) -> None:
        self._hints: Dict[int, str] = {}

    # ── Public API ────────────────────────────────────────────

    def set_hints(self, page_hints: Optional[Dict[int, str]]) -> None:
        """
        Register per-page hints for this document run.

        page_hints: {page_index: hint_type}  where hint_type is one of
            "pertinent", "decorative", "review".
        Pass None (or omit) to disable all hints.
        """
        self._hints = page_hints if page_hints is not None else {}

    def apply(
        self,
        page_idx:              int,
        effective_pertinence:  float,
        visual_evidence_score: float,
        authority_weight:      float,
    ) -> HintResult:
        """
        Apply the hint for page_idx (if any) and return a HintResult.

        Parameters
        ----------
        page_idx              : 0-based page index
        effective_pertinence  : score after arbitration
        visual_evidence_score : from VisualFeatures.visual_evidence_score
        authority_weight      : from ArbitrationResult.authority_weight
        """
        hint = self._hints.get(page_idx)
        if hint is None:
            return HintResult(
                effective_pertinence=effective_pertinence,
                force_review=False,
                hint_applied=False,
                override_reason="",
                warning="",
            )

        return self._dispatch(hint, page_idx, effective_pertinence,
                              visual_evidence_score, authority_weight)

    # ── Internal dispatch ─────────────────────────────────────

    def _dispatch(
        self,
        hint:                  str,
        page_idx:              int,
        effective_pertinence:  float,
        visual_evidence_score: float,
        authority_weight:      float,
    ) -> HintResult:
        if hint == "pertinent":
            return self._apply_pertinent(
                page_idx, effective_pertinence, visual_evidence_score
            )
        elif hint == "decorative":
            return self._apply_decorative(
                page_idx, effective_pertinence, authority_weight
            )
        elif hint == "review":
            return self._apply_review(page_idx, effective_pertinence)
        else:
            # Unknown hint type — ignore with a warning
            return HintResult(
                effective_pertinence=effective_pertinence,
                force_review=False,
                hint_applied=False,
                override_reason="",
                warning=(
                    f"Page {page_idx}: unknown hint type '{hint}' ignored. "
                    "Valid types: 'pertinent', 'decorative', 'review'."
                ),
            )

    def _apply_pertinent(
        self,
        page_idx:              int,
        effective_pertinence:  float,
        visual_evidence_score: float,
    ) -> HintResult:
        """Floor pertinence to COLOR zone (0.76)."""
        # Guard: require minimum visual evidence to prevent blank-page COLOR decisions
        if visual_evidence_score < PERTINENT_MIN_VE:
            return HintResult(
                effective_pertinence=effective_pertinence,
                force_review=False,
                hint_applied=False,
                override_reason="",
                warning=(
                    f"Page {page_idx}: 'pertinent' hint NOT applied — "
                    f"visual_evidence_score {visual_evidence_score:.3f} < "
                    f"minimum {PERTINENT_MIN_VE:.2f} (possible blank/empty page)."
                ),
            )

        new_score = round(max(effective_pertinence, PERTINENT_FLOOR), 4)
        changed = new_score != effective_pertinence
        reason = (
            f"User hint 'pertinent' (page {page_idx}): "
            f"effective_pertinence {effective_pertinence:.4f} → {new_score:.4f} "
            f"[floor={PERTINENT_FLOOR}]"
            if changed else
            f"User hint 'pertinent' (page {page_idx}): "
            f"score {effective_pertinence:.4f} already >= floor {PERTINENT_FLOOR} — no change"
        )
        return HintResult(
            effective_pertinence=new_score,
            force_review=False,
            hint_applied=True,
            override_reason=reason,
            warning="",
        )

    def _apply_decorative(
        self,
        page_idx:          int,
        effective_pertinence: float,
        authority_weight:  float,
    ) -> HintResult:
        """Cap pertinence at DECORATIVE_CAP (0.50)."""
        # Guard: never hide self-evidently probative evidence (photos, stamps)
        if authority_weight >= DECORATIVE_AUTHORITY_BYPASS:
            return HintResult(
                effective_pertinence=effective_pertinence,
                force_review=False,
                hint_applied=False,
                override_reason="",
                warning=(
                    f"Page {page_idx}: 'decorative' hint NOT applied — "
                    f"authority_weight {authority_weight:.3f} >= "
                    f"bypass threshold {DECORATIVE_AUTHORITY_BYPASS:.2f} "
                    "(confirmed high-authority evidence — cannot suppress)."
                ),
            )

        new_score = round(min(effective_pertinence, DECORATIVE_CAP), 4)
        changed = new_score != effective_pertinence
        reason = (
            f"User hint 'decorative' (page {page_idx}): "
            f"effective_pertinence {effective_pertinence:.4f} → {new_score:.4f} "
            f"[cap={DECORATIVE_CAP}]"
            if changed else
            f"User hint 'decorative' (page {page_idx}): "
            f"score {effective_pertinence:.4f} already <= cap {DECORATIVE_CAP} — no change"
        )
        return HintResult(
            effective_pertinence=new_score,
            force_review=False,
            hint_applied=True,
            override_reason=reason,
            warning="",
        )

    def _apply_review(
        self,
        page_idx:             int,
        effective_pertinence: float,
    ) -> HintResult:
        """Force decision zone to REVIEW_REQUIRED (score unchanged)."""
        reason = (
            f"User hint 'review' (page {page_idx}): "
            f"decision forced to REVIEW_REQUIRED regardless of score {effective_pertinence:.4f}."
        )
        return HintResult(
            effective_pertinence=effective_pertinence,  # score not modified
            force_review=True,
            hint_applied=True,
            override_reason=reason,
            warning="",
        )
