"""
rag/retrieve.py — Hybrid TF-IDF + Word Vector Retrieval
========================================================
Scores each chunk as a weighted combination of:
  - TF-IDF cosine similarity  (lexical match — exact terms, numbers, names)
  - Word vector cosine similarity (semantic match — related concepts)

Two word vector backends supported:
  - "glove"  : GloVe wiki-gigaword-100 via gensim (~400MB, best quality)
  - "spacy"  : en_core_web_md via spaCy (~50MB, easiest install)

Backend is selected via WORD_VEC_BACKEND constant below,
or passed per-call via retrieve(..., backend="glove").

Install:
    pip install gensim                              # for glove
    pip install spacy && python -m spacy download en_core_web_md  # for spacy

Usage:
    from retrieve import retrieve

    chunks = retrieve("attendance percentage this year", k=5)
    # Returns: [{"text": str, "source": str, "score": float, "tfidf": float, "wv": float}]

"""

import logging
import threading
import hashlib
import numpy as np
from sklearn.metrics.pairwise import cosine_similarity

# Import get_index from ingest — single source of truth for the index.
# collection_size() is also imported from ingest so both pipeline and retrieve
# share the same, correctly-implemented version.
from rag.ingest import get_index, collection_size # noqa: F401  (re-exported)

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
WORD_VEC_BACKEND = "spacy"   # "glove" or "spacy" — change to switch globally
TFIDF_WEIGHT     = 0.8       # α — weight for TF-IDF score
WV_WEIGHT        = 0.2       # (1-α) — weight for word vector score
DEFAULT_K        = 5
MIN_SCORE        = 0.05      # hybrid scores are naturally lower than pure TF-IDF


# ── [FIX 2] Thread-safe singleton loaders ─────────────────────────────────────
_glove_model  = None
_spacy_model  = None
_glove_lock   = threading.Lock()
_spacy_lock   = threading.Lock()


def _get_glove():
    """Load GloVe once, thread-safe. Returns model or None on failure."""
    global _glove_model
    if _glove_model is not None:
        return _glove_model
    with _glove_lock:
        if _glove_model is not None:     # double-check inside lock
            return _glove_model
        try:
            import gensim.downloader as api
            logger.info("Loading GloVe model (glove-wiki-gigaword-100)...")
            _glove_model = api.load("glove-wiki-gigaword-100")
            logger.info("GloVe loaded: %d vocab", len(_glove_model))
        except Exception as e:
            logger.error("Failed to load GloVe model: %s — falling back to TF-IDF only.", e)
            _glove_model     = None
    return _glove_model


def _get_spacy():
    """Load spaCy once, thread-safe. Returns nlp or None on failure."""
    global _spacy_model
    if _spacy_model is not None:
        return _spacy_model
    with _spacy_lock:
        if _spacy_model is not None:     # double-check inside lock
            return _spacy_model
        try:
            import spacy
            logger.info("Loading spaCy model (en_core_web_md)...")
            _spacy_model = spacy.load("en_core_web_md", disable=["parser", "ner", "tagger", "lemmatizer"])
            logger.info("spaCy loaded.")
        except Exception as e:
            logger.error("Failed to load spaCy model: %s — falling back to TF-IDF only.", e)
            _spacy_model = None
    return _spacy_model


# ── Word vector helpers ───────────────────────────────────────────────────────

def _text_to_vec_glove(text: str, model) -> "np.ndarray | None":
    """
    Average GloVe word vectors for all known tokens in text.
    Returns None if no tokens are in vocabulary.
    """
    tokens = text.lower().split()
    vecs   = [model[t] for t in tokens if t in model]
    if not vecs:
        return None
    return np.mean(vecs, axis=0)


def _text_to_vec_spacy(text: str, nlp) -> "np.ndarray | None":
    """
    spaCy document vector (mean of token vectors, ignoring OOV).
    Returns None if all tokens are OOV or the doc has no vector.
    """
    doc = nlp(text[:1000])   # cap length for speed
    if not doc.has_vector or doc.vector_norm == 0:
        return None
    return doc.vector.copy()  # return a copy so the spaCy doc can be GC'd


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity between two 1-D vectors."""
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


# ── [FIX 3] Chunk vector cache — invalidates on content change ────────────────
_chunk_vecs_glove: list  = []
_chunk_vecs_spacy: list  = []
_chunk_hash_glove: str   = ""
_chunk_hash_spacy: str   = ""


def _chunks_fingerprint(chunks: list[dict]) -> str:
    """
    A cheap fingerprint of the chunk list so we detect re-ingestion even when
    the chunk count stays the same.  Uses the first+last chunk texts only —
    fast enough to call on every retrieve().
    """
    if not chunks:
        return ""
    sample = (chunks[0]["text"] + chunks[-1]["text"] + str(len(chunks))).encode()
    return hashlib.md5(sample).hexdigest()


def _get_chunk_vecs(backend: str, chunks: list[dict]) -> list:
    """
    Compute and cache word vectors for all chunks.
    Re-computes when the chunk fingerprint changes (e.g. after re-ingestion).
    Returns a list of np.ndarray | None, one entry per chunk.
    """
    global _chunk_vecs_glove, _chunk_vecs_spacy
    global _chunk_hash_glove, _chunk_hash_spacy

    fp = _chunks_fingerprint(chunks)

    if backend == "glove":
        if _chunk_hash_glove == fp and _chunk_vecs_glove:
            return _chunk_vecs_glove
        model = _get_glove()
        if model is None:
            _chunk_vecs_glove = [None] * len(chunks)
        else:
            logger.info("Computing GloVe vectors for %d chunks...", len(chunks))
            _chunk_vecs_glove = [_text_to_vec_glove(c["text"], model) for c in chunks]
        _chunk_hash_glove = fp
        return _chunk_vecs_glove

    else:  # spacy
        if _chunk_hash_spacy == fp and _chunk_vecs_spacy:
            return _chunk_vecs_spacy
        nlp = _get_spacy()
        if nlp is None:
            _chunk_vecs_spacy = [None] * len(chunks)
        else:
            logger.info("Computing spaCy vectors for %d chunks...", len(chunks))
            _chunk_vecs_spacy = [_text_to_vec_spacy(c["text"], nlp) for c in chunks]
        _chunk_hash_spacy = fp
        return _chunk_vecs_spacy


# ── Main retrieve function ────────────────────────────────────────────────────

def retrieve(
    query:   str,
    k:       int = DEFAULT_K,
    backend: str = WORD_VEC_BACKEND,
    allowed_types=None,
    requested_name=None,
    requested_id=None,
    requested_year=None,
) -> list[dict]:
    """
    Hybrid TF-IDF + word vector retrieval.

    Args:
        query   : natural language question or keyword string
        k       : number of chunks to return
        backend : "glove" or "spacy"

    Returns:
        List of dicts sorted by hybrid score (descending):
        [{"text", "source", "score", "tfidf_score", "wv_score"}, ...]
        Returns [] if the index is empty or an error occurs.

    [FIX 4] Falls back to TF-IDF-only scoring when word vector backend is
            unavailable, instead of crashing.
    """
    if not query or not query.strip():
        return []

    try:
        vectorizer, matrix, chunks = get_index()
        if vectorizer is None or not chunks:
            logger.warning("TF-IDF index not loaded — run init_rag() first.")
            return []

        n = len(chunks)

        # ── TF-IDF scores ─────────────────────────────────────────────────────
        q_tfidf      = vectorizer.transform([query])
        tfidf_scores = cosine_similarity(q_tfidf, matrix).flatten()

        # ── Word vector scores ─────────────────────────────────────────────────
        # [FIX 4/5] If the backend fails to load, q_vec stays None and we skip
        # the WV component entirely — result is pure TF-IDF, not a crash.
        q_vec = None
        try:
            if backend == "glove":
                model = _get_glove()
                if model is not None:
                    q_vec = _text_to_vec_glove(query, model)
            else:
                nlp = _get_spacy()
                if nlp is not None:
                    q_vec = _text_to_vec_spacy(query, nlp)
        except Exception as e:
            logger.warning("Word vector query encoding failed (%s) — using TF-IDF only: %s", backend, e)

        chunk_vecs = _get_chunk_vecs(backend, chunks)

        wv_scores = np.zeros(n)
        if q_vec is not None:
            for i, cv in enumerate(chunk_vecs):
                if cv is not None:
                    wv_scores[i] = _cosine(q_vec, cv)

        # Adjust weights: if WV is unavailable fall back to TF-IDF only
        effective_tfidf_w = TFIDF_WEIGHT
        effective_wv_w    = WV_WEIGHT
        if q_vec is None:
            effective_tfidf_w = 1.0
            effective_wv_w    = 0.0

        # ── Hybrid score ──────────────────────────────────────────────────────
        hybrid = effective_tfidf_w * tfidf_scores + effective_wv_w * wv_scores
        
        # ── Top-k ─────────────────────────────────────────────────────────────
        top_indices = hybrid.argsort()[::-1][:k]

        results = []
        for i in top_indices:
            chunk = chunks[i]
            hybrid_score = float(hybrid[i])
            chunk_text = chunk["text"].lower()
            chunk_type = chunk.get("type")

            if allowed_types and chunk_type not in allowed_types:
                hybrid_score -= 0.50

            if requested_name and requested_name.lower() in chunk_text:
                hybrid_score += 0.30

            if requested_id and str(requested_id) in chunk_text:
                hybrid_score += 0.40

            if requested_year and str(requested_year) in chunk_text:
                hybrid_score += 0.10

            score = round(hybrid_score, 4)
            if score < MIN_SCORE:
                continue
            results.append({
                "text":        chunks[i]["text"],
                "source":      chunks[i]["source"],
                "score":       score,
                "tfidf_score": round(float(tfidf_scores[i]), 4),
                "wv_score":    round(float(wv_scores[i]), 4),
                "staff_id": chunks[i].get("staff_id"),
                "staff_name": chunks[i].get("staff_name"),
                "year": chunks[i].get("year"),
                "type": chunks[i].get("type"),
            })
        logger.info("Retrieve query: %s", query)

        for r in results:
            logger.info(
                "score=%s tfidf=%s wv=%s source=%s text=%s",
                r["score"],
                r["tfidf_score"],
                r["wv_score"],
                r["source"],
                r["text"][:200]
            )
            print(f"[RETRIEVE] Results count: {len(results)}")
            for r in results:
                print(
                f"[RETRIEVE] score={r['score']} source={r['source']} text={r['text'][:120]}"
                )
        return results
        

    except Exception as e:
        logger.error("Hybrid retrieve error: %s", e, exc_info=True)
        print(f"[RETRIEVE] Query: {query}")
        return []
