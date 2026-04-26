"""
CPCE v8 - Evidence Linker
Builds a cross-page evidence graph BEFORE per-page scoring so that
every page knows how many other pages cite it and whether it resolves an exhibit.

Wraps and extends CrossPageMemoryEngine with a pre-scan pass.
"""
import re
from typing import List, Dict, Set, Optional
from dataclasses import dataclass, field

from .cross_page_memory import CrossPageMemoryEngine
from .models import SemanticFeatures


# ─────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────

@dataclass
class PageLinkInfo:
    """Evidence linking info for a single page."""
    page_id: int
    incoming_ref_count: int = 0          # pages that explicitly reference this page
    is_exhibit_resolution: bool = False  # this page contains the exhibit body
    resolved_exhibits: List[str] = field(default_factory=list)
    outgoing_refs: List[str] = field(default_factory=list)  # refs this page makes
    referenced_by: List[int] = field(default_factory=list)  # page indices that cite this
    # Reference intent classification:
    # exhibit_reference=1.0, citation_reference=0.7, casual_mention=0.3
    reference_strength: float = 0.0     # weighted avg intent strength of incoming refs
    # Universal directive detection: how many forward-looking legal instructions
    # this page contains (e.g. "as shown below", "see the following image").
    # A non-zero value means the NEXT page is being set up as evidence.
    directive_count: int = 0
    # Set when a directive on the PREVIOUS page named a visual feature that is
    # actually confirmed present on THIS page (e.g. page N says "see red text
    # below" and this page has red pixels).  Triggers hard COLOR forcing.
    directive_visual_confirmed: bool = False
    directive_visual_features: List[str] = field(default_factory=list)
    # Set when THIS page contains a directive AND the visual feature it names
    # is confirmed on THIS same page (e.g. page 2 says "see red text below"
    # AND page 2 itself has red pixels).  The source page is also evidence.
    directive_self_confirmed: bool = False
    directive_self_features: List[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────
# Evidence Link Graph
# ─────────────────────────────────────────────────────────────

class EvidenceLinkGraph:
    """
    Pre-scans all page texts to build a cross-page evidence graph.

    Two-pass algorithm:
      Pass 1 — find every exhibit label on each page (where exhibits live)
      Pass 2 — find every reference (page X, Exhibit A, Figure N) and
                resolve it to the target page

    Result: each page knows:
      • how many other pages reference it        (incoming_ref_count)
      • whether it contains an exhibit body      (is_exhibit_resolution)
      • which exhibits it resolves               (resolved_exhibits)
    """

    # Patterns that cite a specific page number (v17: expanded)
    PAGE_REF_PATTERNS = [
        r'see\s+page\s+(\d+)',
        r'refer\s+to\s+page\s+(\d+)',
        r'\(page\s+(\d+)\)',
        r'as\s+shown\s+on\s+page\s+(\d+)',
        r'as\s+discussed\s+on\s+page\s+(\d+)',
        r'found\s+on\s+page\s+(\d+)',
        # v17: additional patterns
        r'on\s+page\s+(\d+)',
        r'page\s+(\d+)\s+(?:above|below|herein|hereof|thereof)',
        r'pp?\.\s*(\d+)',                         # "p. 5" or "pp. 5"
        r'at\s+page\s+(\d+)',
        r'pages?\s+(\d+)',                        # "page 5" or "pages 5"
    ]

    # Patterns that reference an exhibit by label (v17: expanded)
    EXHIBIT_REF_PATTERNS = [
        r'see\s+exhibit\s+([A-Z0-9-]+)',
        r'refer\s+to\s+exhibit\s+([A-Z0-9-]+)',
        r'see\s+exh\.?\s*([A-Z0-9-]+)',
        r'as\s+shown\s+in\s+exhibit\s+([A-Z0-9-]+)',
        r'\(ex\.?\s*([A-Z0-9-]+)\)',
        r'exhibit\s+([A-Z0-9-]+)\s+(?:shows|depicts|contains|attached)',
        r'attached\s+as\s+exhibit\s+([A-Z0-9-]+)',
        r'exhibit\s+([A-Z0-9-]+)\s+hereto',
        # v17: additional patterns
        r'per\s+exhibit\s+([A-Z0-9-]+)',
        r'pursuant\s+to\s+exhibit\s+([A-Z0-9-]+)',
        r'see\s+attached\s+(?:exhibit\s+)?([A-Z0-9-]+)',
        r'exhibit\s+([A-Z0-9-]+)\s+(?:is\s+)?attached',
        r'identified\s+as\s+exhibit\s+([A-Z0-9-]+)',
        r'marked\s+as\s+exhibit\s+([A-Z0-9-]+)',
        r'labeled\s+(?:as\s+)?exhibit\s+([A-Z0-9-]+)',
        r'(?:figure|fig\.?)\s+([0-9]+)',          # "Figure 3", "Fig. 3"
        r'table\s+([0-9]+)',                       # "Table 2"
        r'chart\s+([0-9]+)',                       # "Chart 1"
    ]

    # Patterns that declare an exhibit on a page (v17: expanded)
    EXHIBIT_DECLARATION_PATTERNS = [
        r'^exhibit\s+([A-Z0-9-]+)',               # start of text
        r'\bexhibit\s+([A-Z0-9-]+)\b',            # anywhere
        r'\battachment\s+([A-Z0-9-]+)\b',
        r'\bappendix\s+([A-Z0-9-]+)\b',
        # v17: additional declaration patterns
        r'\bexh\.\s*([A-Z0-9-]+)\b',             # "Exh. A"
        r'\bschedule\s+([A-Z0-9-]+)\b',           # "Schedule A"
        r'\bfigure\s+([0-9]+)\b',                 # "Figure 1"
        r'\btable\s+([0-9]+)\b',                  # "Table 1" (as page title)
    ]

    # ── Universal instruction detector vocabulary ─────────────────────────
    # Inspired by the intent-structure approach: a directive is detected when
    # an instruction verb co-occurs with a reference direction OR a visual
    # attribute — regardless of the exact phrasing.
    #
    # "see this below in red"  →  instruction="see" + reference="below" + visual="red"  ✓
    # "refer to the attached diagram" → instruction="refer" + visual="diagram"           ✓
    # "note the highlighted section" → instruction="note" + visual="highlighted"         ✓
    _INSTRUCTION_VERBS = frozenset([
        "see", "refer", "check", "review", "examine", "inspect",
        "consider", "note", "observe", "consult", "view",
    ])
    _REFERENCE_WORDS = frozenset([
        "below", "above", "following", "attached", "herein", "hereto",
        "next", "subsequent", "previous", "appended", "enclosed",
    ])
    _VISUAL_ATTR_KEYWORDS = frozenset([
        "red", "blue", "green", "yellow", "highlighted", "highlight",
        "color", "colour", "circled", "underlined", "bold", "marked",
        "image", "photo", "photograph", "picture", "figure", "diagram",
        "chart", "graph", "exhibit", "document", "illustration",
    ])

    # Intent weights: how strongly does each reference type assert importance?
    _EXHIBIT_REF_INTENT:  float = 1.00   # "See Exhibit A" — strongest
    _PAGE_REF_INTENT:     float = 0.70   # "see page 5"    — moderate
    _FORWARD_REF_INTENT:  float = 0.35   # universal instruction hit — weakest (target uncertain)
    _CASUAL_INTENT:       float = 0.30   # fallback

    # Map: visual keyword → (VisualFeatures attr, threshold)
    # Used to check whether the NEXT page actually contains what was referenced.
    _DIRECTIVE_FEATURE_MAP: Dict[str, tuple] = {
        # Use color_density (general) for color keywords so attorney red/orange text,
        # stamps, and any visible color all qualify.  Threshold 0.005 = 0.5% of pixels.
        "red":          ("color_density",              0.005),
        "blue":         ("color_density",              0.005),
        "circled":      ("color_density",              0.005),
        "marked":       ("color_density",              0.005),
        "yellow":       ("highlight_density",          0.001),
        "green":        ("highlight_density",          0.001),
        "highlighted":  ("highlight_density",          0.001),
        "highlight":    ("highlight_density",          0.001),
        "underlined":   ("highlighted_text_density",   0.001),
        "bold":         ("highlighted_text_density",   0.001),
        "photo":        ("photo_regions",              1),
        "photograph":   ("photo_regions",              1),
        "image":        ("photo_regions",              1),
        "picture":      ("photo_regions",              1),
        "chart":        ("chart_regions",              1),
        "graph":        ("chart_regions",              1),
        "diagram":      ("chart_regions",              1),
        "figure":       ("chart_regions",              1),
        "color":        ("color_density",              0.03),
        "colour":       ("color_density",              0.03),
    }

    @classmethod
    def detect_forward_directive(cls, text_lower: str):
        """
        Universal intent-structure detector for forward-looking legal directives.

        A sentence is a forward directive when it contains:
          • an instruction verb  (see / refer / check / note …)   AND
          • a reference word     (below / following / attached …)
            OR a visual attribute  (red / highlighted / image …)

        Returns (count: int, visual_keywords: Set[str]).
          count          — number of independent directive sentences (capped at 3)
          visual_keywords — union of _VISUAL_ATTR_KEYWORDS found across all directive sentences;
                           used to check whether the target page has the referenced feature.
        """
        sentences = re.split(r'[.\n;]', text_lower)
        count = 0
        visual_keywords: Set[str] = set()
        for sent in sentences:
            words = set(re.findall(r'\b\w+\b', sent))
            has_instruction = bool(words & cls._INSTRUCTION_VERBS)
            if not has_instruction:
                continue
            has_reference = bool(words & cls._REFERENCE_WORDS)
            has_visual    = bool(words & cls._VISUAL_ATTR_KEYWORDS)
            if has_reference or has_visual:
                count += 1
                visual_keywords |= (words & cls._VISUAL_ATTR_KEYWORDS)
                if count >= 3:
                    break
        return count, visual_keywords

    @classmethod
    def _directive_feature_confirmed(cls, visual_keywords: Set[str], vf) -> List[str]:
        """
        Check whether the visual features of a page confirm any of the
        keywords extracted from a directive sentence.

        Returns the list of confirmed keyword matches (empty = no match).
        """
        confirmed: List[str] = []
        for kw in visual_keywords:
            spec = cls._DIRECTIVE_FEATURE_MAP.get(kw)
            if spec is None:
                continue
            attr, threshold = spec
            val = getattr(vf, attr, 0)
            # photo_regions / chart_regions are int counts; others are floats
            if isinstance(threshold, int):
                if int(val) >= threshold:
                    confirmed.append(kw)
            else:
                if float(val) >= threshold:
                    confirmed.append(kw)
        return confirmed

    def __init__(self):
        self._links: Dict[int, PageLinkInfo] = {}
        self._exhibit_location: Dict[str, int] = {}  # exhibit_label -> page_idx
        self._memory = CrossPageMemoryEngine()
        self._ref_strength_sums: Dict[int, float] = {}   # accumulator during build()

    # Color mention keywords mapped to visual feature confirmation check
    # Used in the color reference propagation pass.
    _COLOR_VISUAL_SIGNALS: Dict[str, str] = {
        # keyword → which visual attribute to check
        "red":         "stamp_or_highlight",
        "blue":        "stamp_or_highlight",
        "green":       "highlight",
        "yellow":      "highlight",
        "highlighted": "highlight",
        "highlight":   "highlight",
        "circled":     "stamp_or_highlight",
        "marked":      "stamp_or_highlight",
        "color":       "any_color",
        "colour":      "any_color",
    }

    @classmethod
    def _extract_color_mentions(cls, text_lower: str) -> Set[str]:
        """Return the set of color/visual-attribute keywords found in the text."""
        words = set(re.findall(r'\b\w+\b', text_lower))
        return words & set(cls._COLOR_VISUAL_SIGNALS.keys())

    @staticmethod
    def _visual_has_color_signal(signal_type: str, vf) -> bool:
        """Check whether a page's visual features confirm a given color signal type."""
        if signal_type == "highlight":
            return getattr(vf, 'highlight_density', 0) > 0.001
        if signal_type == "stamp_or_highlight":
            # Include general color_density so red attorney text also qualifies.
            return (
                getattr(vf, 'stamp_density', 0) > 0.0005
                or getattr(vf, 'highlight_density', 0) > 0.001
                or getattr(vf, 'color_density', 0) > 0.005
            )
        if signal_type == "any_color":
            return getattr(vf, 'color_density', 0) > 0.015
        return False

    def build(
        self,
        texts: List[str],
        semantic_features: List[SemanticFeatures],
        visual_features: Optional[List] = None,   # List[VisualFeatures] for color propagation
    ) -> None:
        """
        Build the full evidence link graph from all page texts.
        Must be called once before calling get_link_info().

        visual_features (optional): when provided, a color reference propagation
        pass is run after the standard 3-pass build.  Any page that mentions a
        color/visual attribute ("see red text", "highlighted section") creates
        a weak reference edge to every other page whose visual data confirms
        that attribute exists.
        """
        num_pages = len(texts)
        self._links = {i: PageLinkInfo(page_id=i) for i in range(num_pages)}
        self._ref_strength_sums = {}
        # Populated during Pass 2; consumed in Pass 5 (visual feature confirmation)
        _pending_directive_visuals: Dict[int, Set[str]] = {}

        # ── Pass 1: locate exhibits ────────────────────────────────
        for i, text in enumerate(texts):
            if not text:
                continue
            text_lower = text.lower()
            for pattern in self.EXHIBIT_DECLARATION_PATTERNS:
                for match in re.finditer(pattern, text_lower):
                    label = match.group(1).upper()
                    # Only record the first occurrence (where the exhibit body lives)
                    if label not in self._exhibit_location:
                        self._exhibit_location[label] = i

            # Also use semantic features exhibit mentions
            sf = semantic_features[i] if i < len(semantic_features) else None
            if sf and sf.exhibit_mentions > 0:
                # Trust semantic feature extraction for additional labels
                pass

        # ── Pass 2: scan references and resolve targets ────────────
        for i, text in enumerate(texts):
            if not text:
                continue
            text_lower = text.lower()

            # Page number references (citation_reference intent = 0.70)
            for pattern in self.PAGE_REF_PATTERNS:
                for match in re.finditer(pattern, text_lower):
                    try:
                        target_1indexed = int(match.group(1))
                        target = target_1indexed - 1   # convert to 0-indexed
                        if 0 <= target < num_pages and target != i:
                            self._links[i].outgoing_refs.append(f"page_{target_1indexed}")
                            self._links[target].incoming_ref_count += 1
                            self._links[target].referenced_by.append(i)
                            self._ref_strength_sums[target] = (
                                self._ref_strength_sums.get(target, 0.0) + self._PAGE_REF_INTENT
                            )
                    except (ValueError, IndexError):
                        pass

            # Exhibit references (exhibit_reference intent = 1.00)
            for pattern in self.EXHIBIT_REF_PATTERNS:
                for match in re.finditer(pattern, text_lower):
                    label = match.group(1).upper()
                    self._links[i].outgoing_refs.append(f"exhibit_{label}")
                    target = self._exhibit_location.get(label)
                    if target is not None and target != i:
                        self._links[target].incoming_ref_count += 1
                        if target not in self._links[target].referenced_by:
                            self._links[target].referenced_by.append(i)
                        self._ref_strength_sums[target] = (
                            self._ref_strength_sums.get(target, 0.0) + self._EXHIBIT_REF_INTENT
                        )

            # ── Universal forward directive detection ──────────────
            # Uses the intent-structure approach: instruction_verb +
            # (reference_word OR visual_attribute).  This catches loose
            # phrasing like "see this below in red" that rigid regex misses.
            directive_hits, directive_vis_kw = self.detect_forward_directive(text_lower)

            if directive_hits > 0:
                self._links[i].directive_count = directive_hits
                self._links[i].outgoing_refs.append(f"forward_directive_x{directive_hits}")
                # Store visual keywords for feature-confirmation in Pass 5 below.
                # Key = source page, value = keywords from its directive sentences.
                _pending_directive_visuals[i] = directive_vis_kw
                next_page = i + 1
                if next_page < num_pages:
                    self._links[next_page].incoming_ref_count += 1
                    if i not in self._links[next_page].referenced_by:
                        self._links[next_page].referenced_by.append(i)
                    self._ref_strength_sums[next_page] = (
                        self._ref_strength_sums.get(next_page, 0.0)
                        + self._FORWARD_REF_INTENT * directive_hits
                    )

        # ── Pass 3: mark exhibit resolutions ─────────────────────
        for exhibit_label, page_idx in self._exhibit_location.items():
            info = self._links[page_idx]
            info.is_exhibit_resolution = True
            if exhibit_label not in info.resolved_exhibits:
                info.resolved_exhibits.append(exhibit_label)

        # ── Compute weighted reference_strength per target page ───
        for page_idx, info in self._links.items():
            if info.incoming_ref_count > 0:
                info.reference_strength = round(
                    self._ref_strength_sums.get(page_idx, 0.0) / info.incoming_ref_count, 4
                )

        # ── Pass 4: Color reference propagation ───────────────────
        # When page i mentions a color or visual attribute ("see red text",
        # "highlighted section") AND page j's visual data confirms that
        # attribute is actually present, create a weak reference edge
        # i → j.  Intent weight = _CASUAL_INTENT (0.30).
        #
        # This closes the loop between textual intent and visual evidence:
        # text says "red" → visual confirms red pixels → pages are linked.
        if visual_features and len(visual_features) == num_pages:
            for i, text in enumerate(texts):
                if not text:
                    continue
                text_lower = text.lower()
                color_mentions = self._extract_color_mentions(text_lower)
                if not color_mentions:
                    continue

                for j in range(i + 1, num_pages):
                    # Only link FORWARD (j > i).  Color mentions like "see red below"
                    # refer to upcoming pages.  Backward links are handled by explicit
                    # page-number references in Pass 2 and should not be created here.
                    vf = visual_features[j]
                    confirmed = False
                    for keyword in color_mentions:
                        signal_type = self._COLOR_VISUAL_SIGNALS.get(keyword, "any_color")
                        if self._visual_has_color_signal(signal_type, vf):
                            confirmed = True
                            break

                    if confirmed:
                        self._links[j].incoming_ref_count += 1
                        if i not in self._links[j].referenced_by:
                            self._links[j].referenced_by.append(i)
                        self._ref_strength_sums[j] = (
                            self._ref_strength_sums.get(j, 0.0) + self._CASUAL_INTENT
                        )
                        self._links[i].outgoing_refs.append(f"color_ref_to_page_{j + 1}")

            # Recompute reference_strength after color propagation
            for page_idx, info in self._links.items():
                if info.incoming_ref_count > 0:
                    info.reference_strength = round(
                        self._ref_strength_sums.get(page_idx, 0.0) / info.incoming_ref_count, 4
                    )

        # ── Pass 5: Directive visual-feature confirmation ─────────
        # For every page i that had a forward directive, check whether page i+1
        # actually contains the visual feature that the directive named.
        #   "see red text below"   → page i+1 must have stamp/highlight pixels
        #   "refer to photo below" → page i+1 must have photo_regions ≥ 1
        # If confirmed → mark page i+1 as directive_visual_confirmed so the
        # engine can force COLOR regardless of pertinence score.
        # If visual_features are not provided → confirmation stays False and
        # the existing weak reference is kept as-is (no regression).
        if visual_features and len(visual_features) == num_pages:
            for src_page, vis_kw in _pending_directive_visuals.items():
                if not vis_kw:
                    continue  # no visual keywords in directive → nothing to confirm
                next_page = src_page + 1
                if next_page >= num_pages:
                    continue
                vf = visual_features[next_page]
                confirmed = self._directive_feature_confirmed(vis_kw, vf)
                if confirmed:
                    self._links[next_page].directive_visual_confirmed = True
                    self._links[next_page].directive_visual_features  = confirmed
                    # Upgrade the reference intent to PAGE_REF_INTENT (feature-verified)
                    self._ref_strength_sums[next_page] = (
                        self._ref_strength_sums.get(next_page, 0.0) + self._PAGE_REF_INTENT
                    )
                    # Recompute reference_strength for this page only
                    info = self._links[next_page]
                    info.reference_strength = round(
                        self._ref_strength_sums.get(next_page, 0.0) / info.incoming_ref_count, 4
                    )

                # Self-confirmation: check whether the SOURCE page itself also
                # has the visual feature it's pointing to.
                # "see red text below" on page 2 + page 2 has red → page 2 is also COLOR.
                src_vf = visual_features[src_page]
                self_confirmed = self._directive_feature_confirmed(vis_kw, src_vf)
                if self_confirmed:
                    self._links[src_page].directive_self_confirmed = True
                    self._links[src_page].directive_self_features  = self_confirmed

        # ── Populate CrossPageMemoryEngine (side-channel) ─────────
        for i, text in enumerate(texts):
            sf = semantic_features[i] if i < len(semantic_features) else None
            exhibit_mentions = []
            if sf:
                exhibit_mentions = [f"EX{sf.exhibit_mentions}"] if sf.exhibit_mentions else []
            self._memory.process_page(i, text, exhibit_mentions)

    def get_link_info(self, page_idx: int) -> PageLinkInfo:
        """Return evidence link info for a page (safe — returns empty if not built)."""
        return self._links.get(page_idx, PageLinkInfo(page_id=page_idx))

    def get_memory_context(self, page_idx: int) -> Dict:
        """Return cross-page memory context for a page."""
        return self._memory.get_page_context(page_idx)

    def get_rolling_context_score(self, page_idx: int) -> float:
        """
        Delegate to CrossPageMemoryEngine for rolling narrative context score.
        Returns [0, 1]: how strongly prior pages set up the current page via
        concept thread continuity and explicit backward citations.
        """
        return self._memory.get_rolling_context_score(page_idx)

    def get_rolling_context(self, page_idx: int, window: int = 3) -> str:
        """Return human-readable rolling context summary from prior pages."""
        return self._memory.get_rolling_context(page_idx, window)

    def summary(self) -> Dict:
        """Debug summary of the link graph."""
        total_incoming = sum(v.incoming_ref_count for v in self._links.values())
        resolved_exhibits = sum(1 for v in self._links.values() if v.is_exhibit_resolution)
        total_directives = sum(v.directive_count for v in self._links.values())
        return {
            "total_pages": len(self._links),
            "total_incoming_refs": total_incoming,
            "exhibit_locations": dict(self._exhibit_location),
            "resolved_exhibit_pages": resolved_exhibits,
            "total_forward_directives": total_directives,
        }
