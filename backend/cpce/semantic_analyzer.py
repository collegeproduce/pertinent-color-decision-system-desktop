"""
CPCE v7 - Semantic Analyzer
TF-IDF primary engine + RapidFuzz fallback per specification section 6.
"""
import numpy as np
from typing import List, Tuple, Dict, Any, Optional
from rapidfuzz import fuzz, process
import warnings

from .models import SemanticFeatures, CPCEConfig


class TFIDFEngine:
    """
    TF-IDF based semantic engine for document-level analysis.
    CPU-efficient, deterministic, no LLMs or embeddings.
    """

    # v17: Expanded legal domain reference corpus — covers all 8 case types.
    # Previously PI-biased; now balanced across case types to fix TF-IDF = 0
    # for contract, IP, criminal, insurance documents.
    LEGAL_CORPUS = [
        # Personal injury / medical
        "plaintiff defendant court evidence testimony exhibit medical injury damages",
        "exhibit photograph image chart graph medical x-ray diagnosis mri scan",
        "injury wound trauma medical treatment hospital surgery x-ray blood",
        "medical records diagnostic imaging clinical photograph injury documentation",
        # Contract
        "contract agreement damages injunction financial loss liability breach",
        "signature notary witness affidavit deposition sworn statement",
        "contract breach obligation warranty termination indemnity executed signed",
        "notarized signature stamp seal executed parties agree binding agreement",
        # IP / technical
        "patent trademark infringement claim invention prior art technical drawing",
        "technical diagram engineering drawing schematic blueprint claim chart embodiment",
        "financial chart revenue profit loss earnings graph data table visualization",
        # Evidence / criminal
        "evidence photograph exhibit attachment legal document court filing",
        "crime scene forensic fingerprint dna surveillance chain of custody evidence",
        "exhibit marked admitted testimony deposition witness hearing demonstrative",
        # Insurance / real estate
        "property damage insurance claim adjuster photograph loss appraisal repair",
        "property real estate deed survey boundary appraisal site plan photograph",
    ]

    # High-value legal keywords for importance scoring
    COLOR_KEYWORDS = [
        'exhibit', 'photograph', 'image', 'x-ray', 'mri', 'scan', 'chart',
        'graph', 'diagram', 'medical', 'injury', 'evidence', 'blood', 'wound',
        'figure', 'table', 'financial', 'signature', 'stamp', 'seal',
    ]

    def __init__(self):
        # _vectorizer: fitted ONLY on LEGAL_CORPUS — never overwritten.
        # Used by get_legal_similarity() to measure "how legal is this page?".
        self._vectorizer = None
        self._fitted = False
        # _doc_vectorizer: fitted on document pages for clustering.
        # Separate so document-specific IDF doesn't corrupt the legal reference.
        self._doc_vectorizer = None
        self._page_vectors = None
        self._cluster_labels: List[int] = []
        # Pre-compute the corpus reference vector once after fitting
        self._corpus_ref_vec = None
        self._initialize()

    # v17: Expanded legal boost terms — covers all 8 case types.
    # Each matched term adds +0.04 to TF-IDF score (capped at +0.40).
    LEGAL_BOOST_TERMS = [
        # Generic legal
        "exhibit", "evidence", "affidavit", "invoice", "damages",
        "plaintiff", "defendant", "photograph", "deposition", "testimony", "witness",
        # Medical / PI
        "medical", "injury", "x-ray", "mri", "ct scan", "radiograph", "scan",
        "hospital", "surgery", "treatment", "diagnosis", "fracture", "trauma",
        # Contract
        "contract", "agreement", "signature", "signed", "notarized", "notary",
        "seal", "stamp", "breach", "obligation", "warranty", "executed",
        "indemnity", "termination", "liability", "payment",
        # IP / technical
        "patent", "trademark", "infringement", "claim", "invention",
        "technical", "diagram", "drawing", "chart", "graph",
        # Criminal / evidence
        "forensic", "fingerprint", "dna", "surveillance", "crime scene",
        # Insurance / property
        "property", "damage", "adjuster", "appraisal", "claim", "loss",
    ]

    def _initialize(self):
        """Fit the legal-reference vectorizer on the corpus. Never re-fit this."""
        try:
            from sklearn.feature_extraction.text import TfidfVectorizer
            import numpy as np
            self._vectorizer = TfidfVectorizer(
                max_features=500,
                stop_words='english',
                ngram_range=(1, 2),   # Reduced from (1,3): trigrams rarely match page text
                                      # and inflate vocabulary at the cost of unigram coverage
                min_df=1,
                sublinear_tf=True,
            )
            self._vectorizer.fit(self.LEGAL_CORPUS)

            # Build reference vector as centroid of individual corpus doc vectors.
            # The old approach joined all 16 sentences into one string and transformed
            # it — producing a dense 500-dim vector with huge L2 norm, so cosine
            # similarity against any sparse page vector was always near zero.
            # The centroid (mean) keeps the same semantic "center" but the L2 norm
            # stays comparable to a typical page vector, giving meaningful similarities.
            corpus_matrix = self._vectorizer.transform(self.LEGAL_CORPUS)  # (16, n_features)
            self._corpus_ref_vec = np.asarray(corpus_matrix.mean(axis=0))  # (1, n_features)
            self._fitted = True
            print(f"  TF-IDF vectorizer fitted: {len(self._vectorizer.vocabulary_)} terms "
                  f"(cosine reference built from {len(self.LEGAL_CORPUS)} corpus docs)")
        except ImportError:
            print("  TF-IDF: sklearn not available — keyword-only mode")
            self._fitted = False
        except Exception as e:
            # Surface the actual error so it can be diagnosed rather than silently dropped
            print(f"  TF-IDF init FAILED: {type(e).__name__}: {e}")
            self._fitted = False

    def fit_document(self, texts: List[str]) -> None:
        """
        Fit a SEPARATE document vectorizer on the actual pages for clustering.

        Critical: self._vectorizer (legal reference) is NEVER re-fitted here.
        Re-fitting on document pages collapses the IDF of legal terms that
        appear in every corpus sentence (exhibit, medical, evidence…) toward
        zero, making every cosine similarity return 0.
        """
        if not self._fitted:
            return
        try:
            from sklearn.feature_extraction.text import TfidfVectorizer
            non_empty = [t for t in texts if t.strip()]
            if not non_empty:
                return
            self._doc_vectorizer = TfidfVectorizer(
                max_features=500,
                stop_words='english',
                ngram_range=(1, 2),
                min_df=1,
                sublinear_tf=True,
            )
            self._doc_vectorizer.fit(non_empty)
            # Store page vectors for clustering (uses doc-fitted vocab)
            self._page_vectors = self._doc_vectorizer.transform(
                [t if t.strip() else " " for t in texts]
            )
        except Exception:
            pass

    def get_legal_similarity(self, text: str) -> float:
        """
        Cosine similarity between page text and the legal reference corpus centroid,
        plus keyword boosting to restore signal when cosine is near zero.

        Keyword boost: each matched LEGAL_BOOST_TERM adds +0.04 (capped at +0.40).

        Evaluation order:
          1. Keyword boost is computed first, before any TF-IDF guard — so even
             when sklearn is absent (_fitted=False) or the vectorizer throws, pages
             with clear legal vocabulary still score > 0 instead of hard 0.0.
          2. Cosine similarity is added on top when sklearn is available.
        """
        if not text.strip():
            return 0.0

        # Always compute keyword boost — independent of sklearn availability.
        text_lower = text.lower()
        hits = sum(1 for term in self.LEGAL_BOOST_TERMS if term in text_lower)
        keyword_boost = min(0.40, hits * 0.04)

        if not self._fitted:
            # sklearn absent or failed to initialise — return keyword-only signal.
            return keyword_boost

        sim = 0.0
        try:
            from sklearn.metrics.pairwise import cosine_similarity as sk_cosine
            page_vec = self._vectorizer.transform([text])
            sim = float(sk_cosine(page_vec, self._corpus_ref_vec)[0][0])
            sim = max(0.0, min(1.0, sim))
        except Exception as _e:
            # Log once per unique error type so repeated calls don't flood output
            _etype = type(_e).__name__
            if not hasattr(self, '_cosine_err_logged'):
                self._cosine_err_logged = set()
            if _etype not in self._cosine_err_logged:
                print(f"  TF-IDF cosine FAILED ({_etype}: {_e}) — keyword-only fallback")
                self._cosine_err_logged.add(_etype)

        return min(1.0, sim + keyword_boost)

    def get_top_terms(self, text: str, n: int = 5) -> List[str]:
        """Return top N TF-IDF terms from text using the legal-reference vectorizer."""
        if not self._fitted or not text.strip():
            return []
        try:
            vec = self._vectorizer.transform([text])
            feature_names = self._vectorizer.get_feature_names_out()
            scores = vec.toarray()[0]
            top_idx = scores.argsort()[-n:][::-1]
            return [feature_names[i] for i in top_idx if scores[i] > 0]
        except Exception:
            return []

    def get_keyword_importance(self, text: str) -> float:
        """Score based on presence of color-relevant legal keywords."""
        if not text:
            return 0.0
        text_lower = text.lower()
        top_terms = set(self.get_top_terms(text, 20))
        hits = sum(1 for kw in self.COLOR_KEYWORDS if kw in top_terms or kw in text_lower)
        return min(1.0, hits / max(len(self.COLOR_KEYWORDS), 1) * 3)

    def cluster_by_tfidf(self, texts: List[str], n_clusters: int = None) -> List[int]:
        """
        Cluster page texts using KMeans on TF-IDF vectors.
        Uses _doc_vectorizer (document-fitted) for better intra-document discrimination.
        Falls back to single-cluster if sklearn unavailable or too few pages.
        """
        num_pages = len(texts)
        if num_pages < 3 or not self._fitted:
            return [0] * num_pages
        try:
            from sklearn.cluster import KMeans
            # Prefer the doc-fitted vectorizer; fall back to corpus vectorizer
            vectorizer = self._doc_vectorizer if self._doc_vectorizer is not None else self._vectorizer
            vecs = vectorizer.transform([t if t.strip() else " " for t in texts])
            if n_clusters is None:
                n_clusters = max(2, min(5, num_pages // 3))
            n_clusters = min(n_clusters, num_pages)
            km = KMeans(n_clusters=n_clusters, random_state=42, n_init=10)
            labels = km.fit_predict(vecs).tolist()
            self._cluster_labels = labels
            return labels
        except Exception:
            return [0] * num_pages


class SemanticAnalyzer:
    """
    Semantic analysis using Legal-BERT embeddings and fuzzy matching.
    Per specification section 6.
    """
    
    # Legal domain keywords for semantic analysis
    LEGAL_KEYWORDS = [
        'plaintiff', 'defendant', 'court', 'jurisdiction',
        'statute', 'regulation', 'precedent', 'holding',
        'evidence', 'testimony', 'affidavit', 'deposition',
        'exhibit', 'attachment', 'contract', 'agreement',
        'damages', 'injunction', 'motion', 'brief'
    ]
    
    HIGH_RISK_KEYWORDS = [
        'injury', 'wound', 'medical', 'treatment',
        'damages', 'loss', 'financial', 'evidence',
        'photograph', 'exhibit', 'x-ray', 'trauma'
    ]
    
    def __init__(self, config: CPCEConfig = None):
        self.config = config or CPCEConfig()
        self._model = None
        self._tokenizer = None
        self._embedding_cache: Dict[str, np.ndarray] = {}
    
    def _load_model(self):
        """Skip Legal-BERT model loading to prevent hanging."""
        # Skip model loading for now - using fuzzy matching only
        self._model = None
        self._tokenizer = None
        print("Using fuzzy matching only for semantic analysis (Legal-BERT disabled)")
    
    def analyze(self, text: str) -> SemanticFeatures:
        """
        Perform semantic analysis on extracted text.
        """
        features = SemanticFeatures()
        
        if not text or not text.strip():
            return features
        
        # Extract key phrases
        features.key_phrases = self._extract_key_phrases(text)
        
        # Count exhibit mentions
        features.exhibit_mentions = len(self._extract_exhibit_mentions(text))
        
        # Count cross-references
        features.cross_references = len(self._extract_cross_references(text))
        
        # Calculate fuzzy match score
        features.fuzzy_match_score = self._calculate_fuzzy_score(text)
        
        # Calculate embedding similarity (if model available)
        features.embedding = self._get_embedding(text)
        if features.embedding is not None:
            # Compare with legal domain reference
            features.embedding_similarity_score = self._calculate_embedding_similarity(
                features.embedding, text
            )
        
        # Calculate final semantic score per specification section 3
        features.semantic_score = (
            self.config.alpha * features.embedding_similarity_score +
            self.config.beta * features.fuzzy_match_score
        )
        
        return features
    
    def _extract_key_phrases(self, text: str) -> List[str]:
        """Extract key legal phrases from text."""
        phrases = []
        text_lower = text.lower()
        
        for keyword in self.LEGAL_KEYWORDS:
            if keyword in text_lower:
                phrases.append(keyword)
        
        return phrases
    
    def _extract_exhibit_mentions(self, text: str) -> List[str]:
        """Extract exhibit mentions with comprehensive pattern matching."""
        import re
        patterns = [
            r'[Ee]xhibit\s+([A-Z0-9-]+)',           # Exhibit A, Exhibit 1, Exhibit 12-A
            r'[Ee]xh\.?\s*([A-Z0-9-]+)',            # Exh. A, Exh 1
            r'[Aa]ttachment\s+([A-Z0-9-]+)',         # Attachment 1, Attachment A
            r'[Aa]ppendix\s+([A-Z0-9-]+)',          # Appendix A
            r'[Ff]igure\s+([0-9]+)',                 # Figure 1
            r'[Tt]able\s+([0-9]+)',                  # Table 1
            r'See\s+[Ee]xhibit\s+([A-Z0-9-]+)',     # See Exhibit A
            r'[Ee]xhibit\s+([A-Z])\s+attached',     # Exhibit A attached
            r'\(Ex\.?\s*([A-Z0-9-]+)\)',            # (Ex. A), (Exh. 1)
        ]
        
        all_mentions = []
        for pattern in patterns:
            matches = re.findall(pattern, text)
            all_mentions.extend(matches)
        
        return list(set(all_mentions))  # Remove duplicates
    
    def _extract_cross_references(self, text: str) -> List[str]:
        """Extract cross-references to other pages/sections."""
        import re
        patterns = [
            r'see\s+page\s+(\d+)',
            r'see\s+([Aa]ttachment|[Ee]xhibit)\s+([A-Z0-9-]+)',
            r'refer\s+to\s+page\s+(\d+)',
        ]
        
        refs = []
        for pattern in patterns:
            matches = re.findall(pattern, text)
            refs.extend(matches)
        
        return refs
    
    def _calculate_fuzzy_score(self, text: str) -> float:
        """
        Calculate fuzzy matching score against legal keywords.
        Uses RapidFuzz per specification section 6.
        """
        if not text:
            return 0.0
        
        text_lower = text.lower()
        scores = []
        
        # Check against high-risk keywords first
        for keyword in self.HIGH_RISK_KEYWORDS:
            score = fuzz.partial_ratio(keyword, text_lower) / 100.0
            if score > 0.7:
                scores.append(score * 1.2)  # Boost high-risk matches
        
        # Check against legal keywords
        for keyword in self.LEGAL_KEYWORDS:
            score = fuzz.partial_ratio(keyword, text_lower) / 100.0
            if score > 0.6:
                scores.append(score)
        
        if not scores:
            return 0.0
        
        # Return top scores average
        scores = sorted(scores, reverse=True)[:5]
        return float(np.mean(scores))
    
    def _get_embedding(self, text: str) -> Optional[np.ndarray]:
        """Get embedding vector for text using Legal-BERT."""
        self._load_model()
        
        if self._model is None or self._tokenizer is None:
            return None
        
        # Check cache
        text_hash = hash(text[:200])  # Use first 200 chars for cache key
        if text_hash in self._embedding_cache:
            return self._embedding_cache[text_hash]
        
        try:
            import torch
            
            # Truncate long texts
            text = text[:512]
            
            # Tokenize
            inputs = self._tokenizer(
                text,
                return_tensors="pt",
                truncation=True,
                padding=True,
                max_length=512
            )
            
            # Get embeddings with timeout
            import signal
            
            def timeout_handler(signum, frame):
                raise TimeoutError("Embedding calculation timed out")
            
            # Set timeout for 10 seconds
            signal.signal(signal.SIGALRM, timeout_handler)
            signal.alarm(10)
            
            try:
                with torch.no_grad():
                    outputs = self._model(**inputs)
                    # Use mean of last hidden states
                    embedding = outputs.last_hidden_state.mean(dim=1).squeeze()
                    embedding_np = embedding.numpy().astype(np.float64)
            finally:
                signal.alarm(0)  # Cancel timeout
            
            # Cache and return
            self._embedding_cache[text_hash] = embedding_np
            return embedding_np
            
        except TimeoutError:
            print("Embedding calculation timed out, skipping")
            return None
        except Exception as e:
            warnings.warn(f"Embedding calculation failed: {e}")
            return None
    
    def _calculate_embedding_similarity(self, embedding: np.ndarray, text: str) -> float:
        """
        Calculate similarity between text embedding and legal reference.
        """
        # Create a reference embedding from legal keywords
        ref_text = " ".join(self.LEGAL_KEYWORDS + self.HIGH_RISK_KEYWORDS)
        ref_embedding = self._get_embedding(ref_text)
        
        if ref_embedding is None:
            return 0.0
        
        # Cosine similarity
        similarity = self._cosine_similarity(embedding, ref_embedding)
        
        return float(max(0, min(1, similarity)))
    
    def _cosine_similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        """Calculate cosine similarity between two vectors."""
        epsilon = self.config.epsilon
        dot_product = np.dot(a, b)
        norm_a = np.linalg.norm(a)
        norm_b = np.linalg.norm(b)
        
        if norm_a < epsilon or norm_b < epsilon:
            return 0.0
        
        return dot_product / (norm_a * norm_b + epsilon)
    
    def calculate_semantic_score(self, features: SemanticFeatures) -> float:
        """
        Calculate final semantic signal score per specification section 3.
        """
        return float(features.semantic_score)
