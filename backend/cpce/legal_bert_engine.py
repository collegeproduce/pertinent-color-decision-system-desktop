"""
CPCE v12 - Legal-BERT Semantic Engine
INT8-quantized, CPU-only, lazy-loaded, embedding-cached.

Fallback chain (tries in order):
  1. sentence-transformers  (small, fast, easy install)
  2. HuggingFace transformers + dynamic INT8 quantization
  3. Unavailable (returns 0.0 — TF-IDF carries the semantic signal)

Conditional activation (never runs on every page):
  - Pages in REVIEW_REQUIRED zone
  - Pages where TF-IDF score < AMBIGUOUS_TFIDF_THRESHOLD
  - Pages with detected signal conflicts
  - Pages with ambiguous mid-range semantic score

Usage:
    engine = LegalBertEngine()
    scores = engine.get_legal_scores_batch(["See Exhibit A...", "Pursuant to..."])
"""
from __future__ import annotations

import os
import hashlib
import numpy as np
from typing import List, Dict, Optional

# ─────────────────────────────────────────────────────────────
# Legal reference text — the "gold standard" legal evidence page
# ─────────────────────────────────────────────────────────────
_LEGAL_REFERENCE = (
    "This exhibit contains photographic evidence of the plaintiff's injuries. "
    "The medical records and x-ray images are attached as supporting evidence. "
    "Financial damages are documented in the attached charts and tables. "
    "The signature on this document confirms authenticity of the agreement. "
    "Exhibit A shows the accident scene and visible damage to the vehicle. "
    "Pursuant to court order the following evidence is submitted for consideration."
)

# Model candidates — tried in order; first successful load wins.
# v14: nlpaueb/legal-bert-small-uncased is preferred (legal-domain aligned, lightweight).
# Falls back to MiniLM (already installed) if legal model is unavailable.
_MODEL_CANDIDATES = [
    "nlpaueb/legal-bert-small-uncased",           # legal-domain BERT, 512-dim, installed
    "sentence-transformers/all-MiniLM-L6-v2",    # 22 MB generic fallback
    "distilbert-base-uncased",                    # 66 MB INT8 fallback
    "nlpaueb/legal-bert-base-uncased",            # 400 MB full legal BERT
]


class LegalBertEngine:
    """
    Lazy-loaded INT8-quantized BERT semantic scorer.

    Only activated when the TF-IDF signal is weak or ambiguous.
    Semantic fusion: 0.70 × bert_score + 0.30 × tfidf_score

    Trigger conditions (use LegalBertEngine.should_activate()):
      - decision_zone == "review_required"
      - tfidf_score < AMBIGUOUS_TFIDF_THRESHOLD (0.25)
      - conflict_type != "none"
      - 0.20 < semantic_score < 0.55 (mid-range ambiguity)
    """

    AMBIGUOUS_TFIDF_THRESHOLD = 0.25
    FUSION_BERT_WEIGHT  = 0.70
    FUSION_TFIDF_WEIGHT = 0.30

    def __init__(self, model_name: str = None, cache_dir: str = None):
        self._requested_model = model_name
        self._cache_dir = cache_dir or os.path.join(
            os.path.expanduser("~"), ".cpce", "models"
        )
        os.makedirs(self._cache_dir, exist_ok=True)

        self._backend:  Optional[str]    = None   # "sentence_transformers" | "transformers_int8"
        self._model                      = None
        self._tokenizer                  = None
        # v16: case-aware reference text — swapped per detected case type
        self._current_reference: str             = _LEGAL_REFERENCE
        self._ref_embedding: Optional[np.ndarray] = None
        self._emb_cache: Dict[str, np.ndarray]    = {}

        self._load_attempted = False
        self._available      = False

    # ── Public API ───────────────────────────────────────────────────

    def is_available(self) -> bool:
        """True after a backend was successfully loaded."""
        return self._available

    @staticmethod
    def should_activate(
        decision_zone:  str,
        tfidf_score:    float,
        conflict_type:  str,
        semantic_score: float,
        visual_score:   float = 0.5,   # v13/v14: visual_evidence_score [0,1]
    ) -> bool:
        """
        v14 (revised): BERT activates whenever a decision is uncertain.

        Hard block (only truly blank/empty pages):
          - visual_evidence_score >= 0.5  → strong visual truth → COLOR decided, BERT redundant
          - visual_evidence_score < 0.03  → blank/empty page → B/W is safe, BERT adds nothing

        For all other pages (the vast majority of legal document text):
          BERT runs when ANY ambiguity signal is present:
          - review_required zone         → explicit uncertainty flag
          - conflict between signals      → visual says X, semantic says Y
          - mid-range semantic score      → text is ambiguous about legal relevance
          - low TF-IDF score             → sparse keyword match needs deeper semantics

        Rationale for relaxing the old 0.15 lower gate:
          Text-heavy legal pages (briefs, motions, exhibit lists) have
          visual_evidence_score ≈ 0 because they contain no photos or charts,
          but they carry rich semantic content (exhibit references, signature
          lines, legal citations) that BERT is best positioned to evaluate.
          Blocking BERT at ve < 0.15 silenced it on exactly the pages where
          it would add the most value.
        """
        # Hard block: clear color decision (strong visual) — BERT is redundant
        if visual_score >= 0.5:
            return False

        # Hard block: truly blank or near-empty page — nothing to embed
        if visual_score < 0.03 and tfidf_score < 0.05 and semantic_score < 0.05:
            return False

        # Activate on any meaningful ambiguity signal
        if decision_zone == "review_required":
            return True
        if conflict_type != "none":
            return True
        if tfidf_score < LegalBertEngine.AMBIGUOUS_TFIDF_THRESHOLD:
            return True
        if 0.10 < semantic_score < 0.60:
            return True
        return False

    def get_legal_score(self, text: str) -> float:
        """Legal relevance score for a single page — [0, 1]."""
        if not text.strip():
            return 0.0
        self._try_load()
        if not self._available:
            return 0.0
        try:
            emb = self._embed(text)
            ref = self._get_ref_embedding()
            return self._cosine(emb, ref)
        except Exception:
            return 0.0

    def get_legal_scores_batch(self, texts: List[str]) -> List[float]:
        """
        Batch inference — significantly faster than repeated get_legal_score() calls.
        Returns scores aligned with input texts.
        """
        if not texts:
            return []
        self._try_load()
        if not self._available:
            return [0.0] * len(texts)
        try:
            return self._score_batch(texts)
        except Exception:
            return [self.get_legal_score(t) for t in texts]

    def set_case_reference(self, case_type: str) -> None:
        """
        v16: Switch the BERT reference document to match the detected case type.

        Enables domain-matched semantic comparison:
          PI / Medical   → medical evidence reference text
          Contract       → signature/agreement reference text
          IP             → technical diagram / patent reference text
          Criminal       → forensic evidence reference text
          Insurance      → damage / claim reference text
          Default        → generic legal evidence text

        Invalidates the cached reference embedding so the next score call
        recomputes against the new reference text.
        """
        try:
            from .case_type_detector import CASE_BERT_REFERENCES, CaseType
            reference = CASE_BERT_REFERENCES.get(
                case_type,
                CASE_BERT_REFERENCES.get(CaseType.GENERAL_LITIGATION, _LEGAL_REFERENCE),
            )
        except ImportError:
            reference = _LEGAL_REFERENCE

        if reference != self._current_reference:
            self._current_reference = reference
            self._ref_embedding = None   # invalidate cached embedding
            print(f"  LegalBERT: reference text switched for case_type='{case_type}'")

    def fuse(self, bert_score: float, tfidf_score: float) -> float:
        """Apply spec fusion: 0.70 × bert + 0.30 × tfidf."""
        return round(
            self.FUSION_BERT_WEIGHT  * bert_score +
            self.FUSION_TFIDF_WEIGHT * tfidf_score,
            4,
        )

    # ── Lazy loading (fallback chain) ─────────────────────────────────

    def _try_load(self):
        if self._load_attempted:
            return
        self._load_attempted = True

        if self._try_sentence_transformers():
            print(f"  LegalBERT: loaded via sentence-transformers [{self._model_name_used}]")
            return

        if self._try_hf_quantized():
            print(f"  LegalBERT: loaded via HuggingFace INT8 [{self._model_name_used}]")
            return

        print("  LegalBERT: unavailable — install sentence-transformers or transformers+torch")

    def _try_sentence_transformers(self) -> bool:
        try:
            from sentence_transformers import SentenceTransformer

            candidates = (
                [self._requested_model] if self._requested_model else _MODEL_CANDIDATES
            )
            for name in candidates:
                if name is None:
                    continue
                try:
                    self._model = SentenceTransformer(
                        name, device="cpu", cache_folder=self._cache_dir
                    )
                    self._model_name_used = name
                    self._backend = "sentence_transformers"
                    self._available = True
                    return True
                except Exception:
                    continue
        except ImportError:
            pass
        return False

    def _try_hf_quantized(self) -> bool:
        try:
            import torch
            from transformers import AutoTokenizer, AutoModel

            candidates = (
                [self._requested_model] if self._requested_model else _MODEL_CANDIDATES
            )
            for name in candidates:
                if name is None:
                    continue
                try:
                    self._tokenizer = AutoTokenizer.from_pretrained(
                        name, cache_dir=self._cache_dir
                    )
                    raw_model = AutoModel.from_pretrained(
                        name, cache_dir=self._cache_dir
                    )
                    raw_model.eval()
                    # Dynamic INT8 quantization — CPU only, 2-4× faster than FP32
                    self._model = torch.quantization.quantize_dynamic(
                        raw_model,
                        {torch.nn.Linear},
                        dtype=torch.qint8,
                    )
                    self._model_name_used = name
                    self._backend = "transformers_int8"
                    self._available = True
                    return True
                except Exception:
                    continue
        except ImportError:
            pass
        return False

    # ── Embedding helpers ─────────────────────────────────────────────

    def _embed(self, text: str) -> np.ndarray:
        """Get embedding with content-hash caching."""
        key = hashlib.md5(text[:500].encode("utf-8", errors="replace")).hexdigest()
        if key in self._emb_cache:
            return self._emb_cache[key]
        emb = self._compute_embedding(text)
        self._emb_cache[key] = emb
        return emb

    def _compute_embedding(self, text: str) -> np.ndarray:
        if self._backend == "sentence_transformers":
            return self._model.encode(
                text[:512], convert_to_numpy=True, show_progress_bar=False
            ).astype(np.float32)

        if self._backend == "transformers_int8":
            return self._hf_mean_pool(text)

        return np.zeros(128, dtype=np.float32)

    def _hf_mean_pool(self, text: str) -> np.ndarray:
        """Mean-pooled embedding from the HuggingFace model."""
        import torch
        inputs = self._tokenizer(
            text[:512],
            return_tensors="pt",
            truncation=True,
            padding=True,
            max_length=512,
        )
        with torch.no_grad():
            outputs = self._model(**inputs)
        hidden = outputs.last_hidden_state.squeeze(0)
        mask   = inputs["attention_mask"].squeeze(0).float().unsqueeze(-1)
        pooled = (hidden * mask).sum(0) / (mask.sum() + 1e-8)
        return pooled.numpy().astype(np.float32)

    def _score_batch(self, texts: List[str]) -> List[float]:
        """Batch-encode and score against the legal reference."""
        ref = self._get_ref_embedding()

        if self._backend == "sentence_transformers":
            clean = [t[:512] if t.strip() else " " for t in texts]
            embeddings = self._model.encode(
                clean,
                batch_size=8,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
            return [float(self._cosine(emb, ref)) for emb in embeddings]

        # HuggingFace: sequential (padding not implemented for batch here)
        return [self.get_legal_score(t) for t in texts]

    def _get_ref_embedding(self) -> np.ndarray:
        if self._ref_embedding is not None:
            return self._ref_embedding
        self._ref_embedding = self._compute_embedding(self._current_reference)
        return self._ref_embedding

    @staticmethod
    def _cosine(a: np.ndarray, b: np.ndarray) -> float:
        na = np.linalg.norm(a)
        nb = np.linalg.norm(b)
        if na < 1e-8 or nb < 1e-8:
            return 0.0
        return float(np.clip(np.dot(a, b) / (na * nb), 0.0, 1.0))
