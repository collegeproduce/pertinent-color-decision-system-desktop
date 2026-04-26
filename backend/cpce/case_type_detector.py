"""
CPCE v16 - Case-Aware Visual Intelligence — Case Type Detector

Detects case type from the document corpus and defines what visual elements
are legally material for each case type. The engine must know WHAT KIND OF
CASE this is before it can determine whether color on any given page matters.

Case type shapes:
  - Visual element importance (which visuals are legally critical)
  - Phrase matching priorities (text signals that matter most)
  - Page role weights in the pertinence engine
  - BERT reference document (domain-matched semantic comparison)
  - Adaptive weight presets (wv, ws priorities shift per case)

Supported case types:
  personal_injury   → injury photos, x-rays, medical scans CRITICAL
  medical           → diagnostic imaging, clinical photos CRITICAL
  contract_dispute  → signatures, stamps, highlighted terms CRITICAL
  ip                → technical diagrams, product photos CRITICAL
  real_estate       → property photos, site plans, survey charts CRITICAL
  evidence_hearing  → exhibit photos, forensic evidence CRITICAL
  criminal          → crime scene, forensic photos CRITICAL
  insurance         → damage photos, medical evidence CRITICAL
  general_litigation → balanced weighting
  unknown           → balanced weighting (fallback)
"""
from __future__ import annotations

from typing import List, Dict, Tuple
import re


class CaseType:
    """Case types that drive color intelligence decisions."""
    PERSONAL_INJURY       = "personal_injury"
    MEDICAL               = "medical"
    CONTRACT_DISPUTE      = "contract_dispute"
    INTELLECTUAL_PROPERTY = "ip"
    REAL_ESTATE           = "real_estate"
    EVIDENCE_HEARING      = "evidence_hearing"
    CRIMINAL              = "criminal"
    INSURANCE             = "insurance"
    GENERAL_LITIGATION    = "general_litigation"
    UNKNOWN               = "unknown"


# ─── Case-appropriate BERT reference texts ──────────────────────────────────
# These replace the generic legal reference when BERT evaluates pages.
# Each text represents what a "high color importance" page looks like for that case.
CASE_BERT_REFERENCES: Dict[str, str] = {
    CaseType.PERSONAL_INJURY: (
        "The plaintiff sustained injuries documented in attached medical photographs. "
        "X-ray images and MRI scans confirm physical trauma extent and severity. "
        "The accident scene photographs show the mechanism of injury clearly. "
        "Medical records document treatment for injuries shown in these exhibits. "
        "Damages illustrated in photographic evidence and medical imaging attached hereto."
    ),
    CaseType.MEDICAL: (
        "Medical imaging including x-rays, MRI, and CT scans attached as clinical evidence. "
        "Diagnostic photographs document the patient's condition and treatment progress. "
        "Pathology reports with clinical photographs are submitted as exhibits hereto. "
        "Radiology images confirm the diagnosis shown in the attached medical records. "
        "Surgical photographs and post-operative imaging are attached for court review."
    ),
    CaseType.CONTRACT_DISPUTE: (
        "The signature on this document confirms agreement to the stated contract terms. "
        "Official stamps and notary seals authenticate this agreement as legally binding. "
        "Highlighted clauses indicate the disputed contractual provisions at issue. "
        "The notarized signature page is attached as Exhibit A to this agreement. "
        "Amendment signatures and counter-signatures are shown on the attached pages."
    ),
    CaseType.INTELLECTUAL_PROPERTY: (
        "Technical diagrams and engineering drawings illustrate the patented invention. "
        "Product photographs show the infringing design compared to the protected trademark. "
        "Charts and graphs demonstrate market share and financial damages from infringement. "
        "The technical drawings attached as exhibits show each claimed invention element. "
        "Comparative photographs document the trademark confusion and consumer harm caused."
    ),
    CaseType.REAL_ESTATE: (
        "Property photographs document the condition of the real estate in dispute. "
        "Survey maps and site plans attached as exhibits show the boundary lines at issue. "
        "Photographs of structural damage are submitted as evidence of defects claimed. "
        "Aerial photography and satellite imagery show the disputed property boundaries. "
        "Financial charts illustrate property valuation and comparable market analysis."
    ),
    CaseType.EVIDENCE_HEARING: (
        "This exhibit contains photographic evidence relevant to the matter before the court. "
        "Attached photographs document the chain of custody for physical evidence presented. "
        "Forensic photographs and laboratory images are submitted for court consideration. "
        "Demonstrative exhibits with color-coded charts are attached hereto as evidence. "
        "Witness photographs and scene reconstruction images are included as marked exhibits."
    ),
    CaseType.CRIMINAL: (
        "Crime scene photographs document the physical evidence recovered at the scene. "
        "Forensic evidence photographs show physical evidence collected by investigators. "
        "Surveillance photographs are attached as exhibits to support the prosecution. "
        "Medical examiner photographs and autopsy images are submitted as required evidence. "
        "Chain of custody documentation with photographic evidence is attached and marked."
    ),
    CaseType.INSURANCE: (
        "Damage photographs document the extent of loss claimed in this insurance matter. "
        "Vehicle photographs show the collision damage and required repair in detail. "
        "Property damage photographs are submitted in support of the insurance claim. "
        "Medical photographs and treatment records document the injuries claimed herein. "
        "Independent adjuster photographs document the assessed property damage and loss."
    ),
    CaseType.GENERAL_LITIGATION: (
        "This exhibit contains photographic evidence of the plaintiff's injuries sustained. "
        "The medical records and x-ray images are attached as supporting evidence hereto. "
        "Financial damages are documented in the attached charts and tables provided. "
        "The signature on this document confirms authenticity of the agreement executed. "
        "Pursuant to court order the following evidence is submitted for consideration."
    ),
}
# Fallback reference used before case type is detected
_DEFAULT_BERT_REFERENCE = CASE_BERT_REFERENCES[CaseType.GENERAL_LITIGATION]


# ─── Visual priority weights per case type ───────────────────────────────────
# Used by _calculate_visual_score_v7() — keys match the priority dict signature.
CASE_VISUAL_PRIORITIES: Dict[str, Dict[str, float]] = {
    CaseType.PERSONAL_INJURY: {
        "photos": 0.78, "charts": 0.08, "stamps": 0.06,
        "highlights": 0.05, "general_color": 0.03,
    },
    CaseType.MEDICAL: {
        "photos": 0.84, "charts": 0.08, "stamps": 0.03,
        "highlights": 0.03, "general_color": 0.02,
    },
    CaseType.CONTRACT_DISPUTE: {
        "photos": 0.08, "charts": 0.20, "stamps": 0.50,
        "highlights": 0.16, "general_color": 0.06,
    },
    CaseType.INTELLECTUAL_PROPERTY: {
        "photos": 0.18, "charts": 0.62, "stamps": 0.06,
        "highlights": 0.08, "general_color": 0.06,
    },
    CaseType.REAL_ESTATE: {
        "photos": 0.50, "charts": 0.32, "stamps": 0.08,
        "highlights": 0.06, "general_color": 0.04,
    },
    CaseType.EVIDENCE_HEARING: {
        "photos": 0.60, "charts": 0.22, "stamps": 0.08,
        "highlights": 0.06, "general_color": 0.04,
    },
    CaseType.CRIMINAL: {
        "photos": 0.78, "charts": 0.08, "stamps": 0.06,
        "highlights": 0.05, "general_color": 0.03,
    },
    CaseType.INSURANCE: {
        "photos": 0.65, "charts": 0.18, "stamps": 0.08,
        "highlights": 0.06, "general_color": 0.03,
    },
    CaseType.GENERAL_LITIGATION: {
        "photos": 0.40, "charts": 0.25, "stamps": 0.15,
        "highlights": 0.10, "general_color": 0.10,
    },
    CaseType.UNKNOWN: {
        "photos": 0.40, "charts": 0.25, "stamps": 0.15,
        "highlights": 0.10, "general_color": 0.10,
    },
}

# Which element_type names (from VisualElementClassifier) matter most per case
CASE_IMPORTANT_TYPES: Dict[str, List[str]] = {
    CaseType.PERSONAL_INJURY:       ["evidence_photo", "medical_scan"],
    CaseType.MEDICAL:               ["evidence_photo", "medical_scan"],
    CaseType.CONTRACT_DISPUTE:      ["signature", "stamp", "highlight", "exhibit_label"],
    CaseType.INTELLECTUAL_PROPERTY: ["chart", "graph"],
    CaseType.REAL_ESTATE:           ["evidence_photo", "chart", "graph"],
    CaseType.EVIDENCE_HEARING:      ["evidence_photo", "medical_scan", "exhibit_label"],
    CaseType.CRIMINAL:              ["evidence_photo", "exhibit_label"],
    CaseType.INSURANCE:             ["evidence_photo", "chart"],
    CaseType.GENERAL_LITIGATION:    ["evidence_photo", "chart", "stamp", "highlight"],
    CaseType.UNKNOWN:               ["evidence_photo", "chart", "stamp", "highlight"],
}

CASE_IGNORE_TYPES: Dict[str, List[str]] = {
    ct: ["logo", "decorative"]
    for ct in [
        CaseType.PERSONAL_INJURY, CaseType.MEDICAL, CaseType.CONTRACT_DISPUTE,
        CaseType.INTELLECTUAL_PROPERTY, CaseType.REAL_ESTATE, CaseType.EVIDENCE_HEARING,
        CaseType.CRIMINAL, CaseType.INSURANCE, CaseType.GENERAL_LITIGATION, CaseType.UNKNOWN,
    ]
}


class CaseTypeDetector:
    """
    Detects case type by scanning entire document content and visual features.
    Defines which visual elements are legally material for each detected case.

    Detection method:
      - Keyword frequency analysis per case type (expanded dictionaries)
      - Visual feature bonuses (photos → PI/criminal; stamps → contract; charts → IP)
      - Confidence proportional to dominance of winning type

    After detection, call:
      get_color_priority_for_case(case_type)     → visual scoring weights
      get_important_visual_types(case_type)      → element types that matter
      get_ignore_visual_types(case_type)         → element types to discount
      get_case_legal_reference(case_type)        → BERT reference text
    """

    # ─── Keyword dictionaries (v16 expansion) ────────────────────────────────

    PI_KEYWORDS = [
        "injury", "injured", "plaintiff", "defendant", "accident", "medical",
        "treatment", "diagnosis", "x-ray", "mri", "ct scan", "surgery",
        "hospital", "doctor", "physician", "pain", "suffering", "damages",
        "collision", "crash", "vehicle", "automobile", "motorcycle", "pedestrian",
        "slip and fall", "premises liability", "negligence", "trauma", "wound",
        "fracture", "laceration", "contusion", "bruising", "scarring", "disability",
        "rehabilitation", "physical therapy", "chiropractor", "orthopedic",
        "emergency room", "ambulance", "paramedic", "lost wages", "wage loss",
        "pain and suffering", "medical expenses", "future medical", "personal injury",
        "bodily injury", "soft tissue", "herniated", "disc", "spinal",
    ]

    MEDICAL_KEYWORDS = [
        "patient", "diagnosis", "treatment", "prognosis", "clinical", "physician",
        "radiology", "radiograph", "pathology", "histology", "biopsy", "specimen",
        "laboratory", "lab results", "blood test", "urine test", "culture",
        "x-ray", "mri", "ct scan", "ultrasound", "echocardiogram", "ecg", "ekg",
        "surgery", "surgical", "procedure", "anesthesia", "post-operative",
        "discharge", "prescription", "medication", "dosage", "therapy",
        "oncology", "cardiology", "neurology", "orthopedic", "ophthalmology",
        "medical record", "health record", "icd", "cpt code", "hipaa",
        "clinical photograph", "medical image", "diagnostic image", "scan shows",
        "imaging reveals", "wound photograph", "injury photograph",
    ]

    CONTRACT_KEYWORDS = [
        "contract", "agreement", "breach", "signature", "signed", "party",
        "obligation", "term", "condition", "consideration", "performance",
        "termination", "default", "remedy", "warranty",
        "indemnity", "liability", "confidentiality", "non-disclosure", "nda",
        "clause", "provision", "exhibit", "schedule", "addendum", "amendment",
        "effective date", "governing law", "jurisdiction", "arbitration",
        "force majeure", "liquidated damages", "assignment", "subcontract",
        "vendor", "supplier", "purchase order", "invoice", "payment terms",
        "delivery", "acceptance", "rejection", "notarized", "notary", "seal",
        "executed by", "parties agree", "hereby agree", "witness whereof",
    ]

    IP_KEYWORDS = [
        "patent", "trademark", "copyright", "intellectual property", "ip",
        "infringement", "inventor", "invention", "prior art", "claim",
        "design", "utility", "technology", "innovation", "trade secret",
        "trade dress", "servicemarks", "licensing", "royalty", "patent pending",
        "registered trademark", "copyrighted", "publication", "obviousness",
        "novelty", "specification", "embodiment", "prosecution", "uspto",
        "ipr", "inter partes review", "reexamination", "claim construction",
        "technical drawings", "engineering drawings", "schematics", "diagrams",
        "product design", "patent claim", "claim chart", "infringement analysis",
    ]

    REAL_ESTATE_KEYWORDS = [
        "property", "real estate", "deed", "title", "mortgage", "lease",
        "tenant", "landlord", "boundary", "survey", "zoning", "land use",
        "easement", "encroachment", "condemnation", "eminent domain",
        "parcel", "lot", "acre", "square feet", "square footage", "building",
        "commercial property", "residential", "appraisal", "assessment",
        "foreclosure", "lien", "encumbrance", "title search", "closing",
        "purchase agreement", "listing agreement", "broker", "realtor",
        "site plan", "floor plan", "property photograph", "inspection report",
    ]

    EVIDENCE_KEYWORDS = [
        "exhibit", "evidence", "hearing", "trial", "deposition", "witness",
        "testimony", "forensic", "expert", "demonstrative", "chain of custody",
        "admissible", "foundation", "authentication", "hearsay", "relevance",
        "motion in limine", "exhibit list", "marked as exhibit", "proffered",
        "stipulated", "contested", "objection", "sustained", "overruled",
        "exhibit a", "exhibit b", "plaintiff's exhibit", "defendant's exhibit",
    ]

    CRIMINAL_KEYWORDS = [
        "defendant", "prosecution", "criminal", "crime", "felony", "misdemeanor",
        "arrest", "indictment", "grand jury", "criminal complaint", "arraignment",
        "bail", "bond", "probation", "parole", "sentence", "conviction",
        "acquittal", "plea", "guilty", "not guilty", "nolo contendere",
        "homicide", "assault", "battery", "robbery", "burglary", "theft",
        "fraud", "embezzlement", "drug", "controlled substance", "dui", "dwi",
        "forensic evidence", "crime scene", "ballistics", "dna", "fingerprint",
        "surveillance", "witness statement", "police report", "detective",
        "search warrant", "probable cause", "beyond reasonable doubt",
    ]

    INSURANCE_KEYWORDS = [
        "insurance", "insured", "insurer", "claimant", "claim", "policy",
        "premium", "deductible", "coverage", "liability coverage", "collision",
        "comprehensive", "uninsured motorist", "underinsured", "subrogation",
        "bad faith", "denial", "coverage dispute", "exclusion", "endorsement",
        "adjuster", "appraiser", "appraisal clause", "umpire",
        "property damage", "bodily injury", "medical payments", "loss of use",
        "total loss", "actual cash value", "replacement cost", "diminished value",
        "insurance company", "coverage limit", "policy limit", "bodily injury claim",
    ]

    def __init__(self):
        self.case_type_scores: Dict[str, int] = {}
        self._last_detected: str = CaseType.UNKNOWN
        self._last_confidence: float = 0.5

    def detect_case_type(
        self,
        all_pages_text: List[str],
        all_visual_features: List[Dict] = None,
    ) -> Tuple[str, float]:
        """
        Scan entire document to detect dominant case type.

        Args:
            all_pages_text: Text from all pages in the document.
            all_visual_features: Optional list of dicts/VisualFeatures with
                                 'photo_regions', 'stamp_density', 'chart_regions'.

        Returns:
            (case_type, confidence) — CaseType constant, confidence in [0.30, 0.95].
        """
        full_text = " ".join(t for t in all_pages_text if t)
        full_text_lower = full_text.lower()

        scores: Dict[str, int] = {
            CaseType.PERSONAL_INJURY:       self._count_keywords(full_text_lower, self.PI_KEYWORDS),
            CaseType.MEDICAL:               self._count_keywords(full_text_lower, self.MEDICAL_KEYWORDS),
            CaseType.CONTRACT_DISPUTE:      self._count_keywords(full_text_lower, self.CONTRACT_KEYWORDS),
            CaseType.INTELLECTUAL_PROPERTY: self._count_keywords(full_text_lower, self.IP_KEYWORDS),
            CaseType.REAL_ESTATE:           self._count_keywords(full_text_lower, self.REAL_ESTATE_KEYWORDS),
            CaseType.EVIDENCE_HEARING:      self._count_keywords(full_text_lower, self.EVIDENCE_KEYWORDS),
            CaseType.CRIMINAL:              self._count_keywords(full_text_lower, self.CRIMINAL_KEYWORDS),
            CaseType.INSURANCE:             self._count_keywords(full_text_lower, self.INSURANCE_KEYWORDS),
        }

        # Visual feature bonuses (case-type-specific signals from imagery)
        if all_visual_features:
            for vf in all_visual_features:
                # Support both dict and VisualFeatures object
                photo_r = getattr(vf, "photo_regions", None) or vf.get("photo_regions", 0) if isinstance(vf, dict) else getattr(vf, "photo_regions", 0)
                stamp_d = getattr(vf, "stamp_density", None) or vf.get("stamp_density", 0.0) if isinstance(vf, dict) else getattr(vf, "stamp_density", 0.0)
                chart_r = getattr(vf, "chart_regions", None) or vf.get("chart_regions", 0) if isinstance(vf, dict) else getattr(vf, "chart_regions", 0)

                if photo_r > 0:
                    scores[CaseType.PERSONAL_INJURY] += 3
                    scores[CaseType.EVIDENCE_HEARING] += 2
                    scores[CaseType.CRIMINAL] += 2
                    scores[CaseType.INSURANCE] += 2
                    scores[CaseType.REAL_ESTATE] += 1
                    scores[CaseType.MEDICAL] += 1

                if stamp_d > 0.001:
                    scores[CaseType.CONTRACT_DISPUTE] += 3

                if chart_r > 0:
                    scores[CaseType.INTELLECTUAL_PROPERTY] += 2
                    scores[CaseType.REAL_ESTATE] += 1
                    scores[CaseType.INSURANCE] += 1

        self.case_type_scores = scores

        if not scores or max(scores.values()) == 0:
            self._last_detected = CaseType.GENERAL_LITIGATION
            self._last_confidence = 0.5
            return CaseType.GENERAL_LITIGATION, 0.5

        best_case = max(scores, key=scores.get)
        best_score = scores[best_case]
        total_score = sum(scores.values())

        # Confidence: how dominant is the top case type?
        # raw_ratio ∈ [0,1]; map to [0.30, 0.95] confidence
        raw_ratio = best_score / max(total_score, 1)
        confidence = round(min(0.95, 0.30 + 0.65 * raw_ratio), 3)

        self._last_detected = best_case
        self._last_confidence = confidence
        return best_case, confidence

    # ─── Case intelligence getters ───────────────────────────────────────────

    def get_color_priority_for_case(self, case_type: str) -> Dict[str, float]:
        """
        Return visual scoring priority weights for _calculate_visual_score_v7().
        Keys: 'photos', 'charts', 'stamps', 'highlights', 'general_color'.
        """
        return dict(CASE_VISUAL_PRIORITIES.get(
            case_type, CASE_VISUAL_PRIORITIES[CaseType.GENERAL_LITIGATION]
        ))

    def get_important_visual_types(self, case_type: str) -> List[str]:
        """Element types (from VisualElementClassifier) that are legally material."""
        return list(CASE_IMPORTANT_TYPES.get(case_type, CASE_IMPORTANT_TYPES[CaseType.UNKNOWN]))

    def get_ignore_visual_types(self, case_type: str) -> List[str]:
        """Element types that are legally irrelevant for this case type."""
        return list(CASE_IGNORE_TYPES.get(case_type, CASE_IGNORE_TYPES[CaseType.UNKNOWN]))

    def get_case_legal_reference(self, case_type: str) -> str:
        """
        Domain-appropriate BERT reference text for this case type.
        Passed to LegalBertEngine.set_case_reference() after case detection.
        """
        return CASE_BERT_REFERENCES.get(
            case_type, CASE_BERT_REFERENCES[CaseType.GENERAL_LITIGATION]
        )

    # ─── Private helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _count_keywords(text: str, keywords: List[str]) -> int:
        count = 0
        for keyword in keywords:
            count += len(re.findall(
                r"\b" + re.escape(keyword) + r"\b", text, re.IGNORECASE
            ))
        return count
