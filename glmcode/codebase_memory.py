"""Codebase memory: a local, offline retrieval index over the project's files,
so the agent can find the *relevant* code for a question instead of guessing or
burning exploratory tool calls (the biggest quality lever for a small model).

The default index is dependency-free and works immediately: files are chunked,
tokenised (with identifier splitting so getUserName -> get user name), and
ranked by TF-IDF cosine similarity — essentially a smarter, ranked grep that
understands code identifiers. It's incremental (only changed files are re-read)
and persisted per project under CONFIG_DIR/memory.

The Embedder is pluggable: a neural sentence-embedding backend (lazy-installed,
like the TTS/STT models) can drop in later for true synonym-level semantic
search; the index/search/persistence around it stay the same.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from pathlib import Path

from .config import CONFIG_DIR
from .tools import DEFAULT_IGNORES, _is_binary

MEMORY_DIR = CONFIG_DIR / "memory"

# Only index reasonably-sized text files; skip generated/minified noise.
_MAX_FILE_BYTES = 400_000
_SKIP_SUFFIXES = {".min.js", ".map", ".lock", ".snap"}
_CHUNK_LINES = 40          # lines per chunk
_CHUNK_OVERLAP = 8         # overlapping lines so a match near a boundary survives
_MAX_FILES = 4000

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")
_CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z][a-z]+|[a-z0-9]+|[A-Z]+|[0-9]+")


def tokenize(text: str) -> list[str]:
    """Lowercased word tokens, with code identifiers split into their parts
    (snake_case and camelCase both), so a query for 'user name' matches
    getUserName / user_name in the code."""
    out: list[str] = []
    for raw in _TOKEN_RE.findall(text):
        low = raw.lower()
        out.append(low)
        parts = [p.lower() for p in _CAMEL_RE.findall(raw) if p]
        if len(parts) > 1:
            out.extend(parts)
    return out


def _content_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]


class Embedder:
    """Turns text into a normalized dense vector. Implementations: a real neural
    sentence-embedding model (NeuralEmbedder) for synonym-level search, or a
    deterministic fake for tests."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError


class NeuralEmbedder(Embedder):
    """Local sentence-embedding model (all-MiniLM-L6-v2 by default), lazily
    installed/downloaded like the TTS/STT models. Runs fully offline once set
    up. Absent/unavailable -> the caller falls back to the lexical index."""

    _model = None

    def __init__(self, model_name: str = "sentence-transformers/all-MiniLM-L6-v2"):
        self.model_name = model_name

    @staticmethod
    def packages_installed() -> bool:
        import importlib.util
        return importlib.util.find_spec("sentence_transformers") is not None

    def _load(self):
        if NeuralEmbedder._model is None:
            from sentence_transformers import SentenceTransformer
            NeuralEmbedder._model = SentenceTransformer(self.model_name)
        return NeuralEmbedder._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        vecs = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return [list(map(float, v)) for v in vecs]


def _cosine(a: list[float], b: list[float]) -> float:
    # vectors are stored normalized, so cosine is just the dot product
    return sum(x * y for x, y in zip(a, b))


def _file_sig(p: Path) -> list:
    try:
        st = p.stat()
        return [int(st.st_mtime), st.st_size]
    except OSError:
        return [0, 0]


def _iter_source_files(root: Path):
    import os
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in DEFAULT_IGNORES and not d.startswith(".git")]
        for name in filenames:
            if name.startswith("."):
                continue
            p = Path(dirpath) / name
            if p.suffix.lower() in _SKIP_SUFFIXES:
                continue
            try:
                if p.stat().st_size > _MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            yield p
            count += 1
            if count >= _MAX_FILES:
                return


def _chunk_text(text: str) -> list[dict]:
    lines = text.splitlines()
    if not lines:
        return []
    chunks = []
    step = max(1, _CHUNK_LINES - _CHUNK_OVERLAP)
    for start in range(0, len(lines), step):
        piece = lines[start:start + _CHUNK_LINES]
        body = "\n".join(piece).strip()
        if body:
            chunks.append({"start": start + 1, "end": start + len(piece), "text": body})
        if start + _CHUNK_LINES >= len(lines):
            break
    return chunks


class CodebaseIndex:
    """An incremental, persisted retrieval index for one project directory."""

    def __init__(self, root: Path, embedder: Embedder | None = None):
        self.root = Path(root).resolve()
        self._files: dict[str, dict] = {}   # rel_path -> {"sig":[..], "chunks":[..]}
        self._embedder = embedder
        # content-hash -> normalized vector, so re-embedding is incremental
        # (only new/changed chunks are sent to the model). Persisted alongside.
        self._vectors: dict[str, list[float]] = {}
        self._path = MEMORY_DIR / (self._key() + ".json")
        self._load()

    def _key(self) -> str:
        return hashlib.sha1(str(self.root).encode("utf-8")).hexdigest()[:16]

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("root") == str(self.root):
                self._files = data.get("files", {})
                self._vectors = data.get("vectors", {}) or {}
        except (OSError, ValueError):
            self._files = {}

    def _save(self) -> None:
        try:
            MEMORY_DIR.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            payload = {"root": str(self.root), "files": self._files}
            if self._embedder is not None:
                payload["vectors"] = self._vectors
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(self._path)
        except OSError:
            pass

    def refresh(self) -> int:
        """Bring the index up to date with the files on disk, reading only what
        changed. Returns the number of files (re)indexed."""
        seen: set[str] = set()
        reindexed = 0
        for p in _iter_source_files(self.root):
            try:
                rel = p.relative_to(self.root).as_posix()
            except ValueError:
                continue
            seen.add(rel)
            sig = _file_sig(p)
            cached = self._files.get(rel)
            if cached and cached.get("sig") == sig:
                continue
            if _is_binary(p):
                self._files.pop(rel, None)
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            self._files[rel] = {"sig": sig, "chunks": _chunk_text(text)}
            reindexed += 1
        # Drop files that no longer exist.
        for gone in set(self._files) - seen:
            del self._files[gone]
        if self._embedder is not None:
            self._refresh_vectors()
        self._save()
        return reindexed

    def _refresh_vectors(self) -> None:
        """Embed any chunk whose content we haven't embedded yet, and drop
        vectors for content that's no longer present. Only new/changed chunks
        hit the model, so re-indexing stays cheap."""
        wanted: dict[str, str] = {}     # hash -> text
        for _, ch in self._corpus():
            wanted[_content_hash(ch["text"])] = ch["text"]
        missing = [h for h in wanted if h not in self._vectors]
        if missing:
            texts = [wanted[h] for h in missing]
            try:
                vecs = self._embedder.embed(texts)
            except Exception:
                return   # model unavailable -> leave cache; search falls back
            for h, v in zip(missing, vecs):
                self._vectors[h] = v
        # prune vectors for content that's gone
        for stale in set(self._vectors) - set(wanted):
            del self._vectors[stale]

    def _neural_search(self, query: str, k: int) -> list[dict]:
        try:
            qv = self._embedder.embed([query])[0]
        except Exception:
            return self._lexical_search(query, k)   # model failed -> fall back
        results = []
        for rel, ch in self._corpus():
            v = self._vectors.get(_content_hash(ch["text"]))
            if not v:
                continue
            score = _cosine(qv, v)
            if score > 0:
                results.append({"path": rel, "start": ch["start"], "end": ch["end"],
                                "score": round(score, 4), "text": ch["text"]})
        results.sort(key=lambda r: r["score"], reverse=True)
        return results[:k]

    def _corpus(self) -> list[tuple[str, dict]]:
        return [(rel, ch) for rel, f in self._files.items() for ch in f["chunks"]]

    def search(self, query: str, k: int = 6) -> list[dict]:
        """Top-k chunks most relevant to `query`, each {path, start, end, score,
        text}. Neural cosine when an embedder is configured, else TF-IDF."""
        if not (query or "").strip():
            return []
        if self._embedder is not None:
            return self._neural_search(query, k)
        return self._lexical_search(query, k)

    def _lexical_search(self, query: str, k: int = 6) -> list[dict]:
        """TF-IDF cosine over identifier-aware tokens (dependency-free default)."""
        q_tokens = tokenize(query)
        if not q_tokens:
            return []
        corpus = self._corpus()
        if not corpus:
            return []
        n = len(corpus)
        # Document frequency across chunks -> idf.
        doc_tokens = [set(tokenize(ch["text"])) for _, ch in corpus]
        df: dict[str, int] = {}
        for toks in doc_tokens:
            for t in toks:
                df[t] = df.get(t, 0) + 1
        idf = {t: math.log(1 + n / (1 + c)) for t, c in df.items()}

        def vec(tokens: list[str]) -> dict[str, float]:
            tf: dict[str, float] = {}
            for t in tokens:
                tf[t] = tf.get(t, 0.0) + 1.0
            v = {t: (1 + math.log(c)) * idf.get(t, 0.0) for t, c in tf.items()}
            norm = math.sqrt(sum(w * w for w in v.values())) or 1.0
            return {t: w / norm for t, w in v.items()}

        qv = vec(q_tokens)
        results = []
        for rel, ch in corpus:
            dv = vec(tokenize(ch["text"]))
            score = sum(qv.get(t, 0.0) * dv.get(t, 0.0) for t in qv)
            if score > 0:
                results.append({"path": rel, "start": ch["start"], "end": ch["end"],
                                "score": round(score, 4), "text": ch["text"]})
        results.sort(key=lambda r: r["score"], reverse=True)
        return results[:k]


# Opt-in neural retrieval. The GUI flips this from config; when on AND the model
# package is installed, search uses embeddings, else it falls back to lexical.
NEURAL_ENABLED = False


def set_neural_enabled(enabled: bool) -> None:
    global NEURAL_ENABLED
    NEURAL_ENABLED = bool(enabled)


def neural_embedder() -> Embedder | None:
    if NEURAL_ENABLED and NeuralEmbedder.packages_installed():
        return NeuralEmbedder()
    return None


# --- process-wide cache of indexes, refreshed at most every few seconds ------ #
_indexes: dict[str, tuple[float, CodebaseIndex]] = {}
_REFRESH_TTL = 5.0


def get_index(root: Path, embedder: Embedder | None = None) -> CodebaseIndex:
    # Key by mode too, so flipping neural on/off doesn't reuse the wrong index.
    key = f"{Path(root).resolve()}|{'n' if embedder is not None else 'l'}"
    now = time.time()
    hit = _indexes.get(key)
    if hit and now - hit[0] < _REFRESH_TTL:
        return hit[1]
    idx = hit[1] if hit else CodebaseIndex(root, embedder=embedder)
    idx.refresh()
    _indexes[key] = (now, idx)
    return idx


def search_codebase(root: Path, query: str, k: int = 6,
                    embedder: Embedder | None = "auto") -> list[dict]:
    if embedder == "auto":
        embedder = neural_embedder()
    return get_index(root, embedder).search(query, k=k)
