"""Neural codebase memory: the embedding-backed search path, its incremental
vector cache, persistence, and graceful fallback — all with a deterministic
fake embedder (no real model needed)."""

import math

import glmcode.codebase_memory as cm
from glmcode.codebase_memory import CodebaseIndex, Embedder, tokenize


class FakeEmbedder(Embedder):
    """Deterministic normalized bag-of-words vectors over a fixed hashed space,
    so cosine reflects token overlap (good enough to test retrieval + caching).
    Counts how many texts it embeds, to prove the cache is incremental."""

    DIM = 64

    def __init__(self):
        self.calls = 0

    def embed(self, texts):
        self.calls += len(texts)
        out = []
        for t in texts:
            v = [0.0] * self.DIM
            for tok in tokenize(t):
                v[hash(tok) % self.DIM] += 1.0
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / norm for x in v])
        return out


def _write(root, name, text):
    p = root / name
    p.write_text(text, encoding="utf-8")
    return p


def _index(tmp_path, monkeypatch, embedder):
    monkeypatch.setattr(cm, "MEMORY_DIR", tmp_path.with_name(tmp_path.name + "_mem"))
    return CodebaseIndex(tmp_path, embedder=embedder)


def test_neural_search_surfaces_relevant_file(tmp_path, monkeypatch):
    _write(tmp_path, "auth.py", "def login(user, password): return check_password(user, password)\n")
    _write(tmp_path, "geometry.py", "def area(radius): return 3.14 * radius * radius\n")
    idx = _index(tmp_path, monkeypatch, FakeEmbedder())
    idx.refresh()
    hits = idx.search("password login", k=2)
    assert hits and hits[0]["path"] == "auth.py"


def test_vector_cache_is_incremental(tmp_path, monkeypatch):
    _write(tmp_path, "a.py", "alpha alpha\n")
    _write(tmp_path, "b.py", "beta beta\n")
    emb = FakeEmbedder()
    idx = _index(tmp_path, monkeypatch, emb)
    idx.refresh()
    after_first = emb.calls
    assert after_first >= 2                       # embedded both chunks
    idx.refresh()                                 # nothing changed
    assert emb.calls == after_first               # no re-embedding
    _write(tmp_path, "b.py", "beta gamma delta epsilon\n")
    idx.refresh()
    assert emb.calls == after_first + 1           # only the changed chunk


def test_vectors_persist_across_instances(tmp_path, monkeypatch):
    _write(tmp_path, "a.py", "persisted content here\n")
    emb = FakeEmbedder()
    _index(tmp_path, monkeypatch, emb).refresh()
    calls_after_build = emb.calls
    emb2 = FakeEmbedder()
    idx2 = _index(tmp_path, monkeypatch, emb2)
    idx2.refresh()
    assert emb2.calls == 0                         # loaded cached vectors, no re-embed
    assert idx2.search("persisted content")


def test_falls_back_to_lexical_when_embed_fails(tmp_path, monkeypatch):
    class Broken(Embedder):
        def embed(self, texts):
            raise RuntimeError("model exploded")
    _write(tmp_path, "auth.py", "def login(password): ...\n")
    idx = _index(tmp_path, monkeypatch, Broken())
    idx.refresh()                                  # embedding fails silently
    # search still works via the lexical fallback
    hits = idx.search("login password")
    assert hits and hits[0]["path"] == "auth.py"


def test_lexical_still_default_without_embedder(tmp_path, monkeypatch):
    monkeypatch.setattr(cm, "MEMORY_DIR", tmp_path.with_name(tmp_path.name + "_mem"))
    _write(tmp_path, "x.py", "widget render theme color\n")
    idx = CodebaseIndex(tmp_path)                  # no embedder
    idx.refresh()
    assert idx._embedder is None and idx.search("render theme")


def test_neural_toggle_selects_embedder(monkeypatch):
    monkeypatch.setattr(cm.NeuralEmbedder, "packages_installed", staticmethod(lambda: True))
    cm.set_neural_enabled(True)
    assert isinstance(cm.neural_embedder(), cm.NeuralEmbedder)
    cm.set_neural_enabled(False)
    assert cm.neural_embedder() is None
    # enabled but package missing -> None (silent lexical fallback)
    cm.set_neural_enabled(True)
    monkeypatch.setattr(cm.NeuralEmbedder, "packages_installed", staticmethod(lambda: False))
    assert cm.neural_embedder() is None
    cm.set_neural_enabled(False)
