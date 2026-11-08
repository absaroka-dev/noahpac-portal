"""Hybrid retrieval helpers (dependency-free): BM25, query expansion, reciprocal-rank fusion.

Embeddings alone retrieve acronyms, AFSC codes, ICD codes and table numbers poorly, so exact-term search
runs next to the vector search and the two rankings are fused.
"""
import json
import math
import re
from collections import Counter
from pathlib import Path

_TOKEN = re.compile(r"[A-Za-z0-9]+(?:[.\-][A-Za-z0-9]+)*")
_STOP = frozenset("a an and are as at be by for from has have in is it its of on or that the this to was were will with".split())


def tokenize(text):
    out = []
    for t in _TOKEN.findall(text.lower()):
        if t in _STOP:
            continue
        out.append(t)
        if "." in t or "-" in t:  # "6.12.3" also matches its parts, "5-2" -> "5", "2"
            out.extend(p for p in re.split(r"[.\-]", t) if p and p not in _STOP)
    return out


class BM25:
    def __init__(self, docs, k1=1.5, b=0.75):
        self.k1, self.b = k1, b
        self.tf = [Counter(tokenize(d)) for d in docs]
        self.len = [sum(c.values()) for c in self.tf]
        self.avg = (sum(self.len) / len(self.len)) if self.len else 0.0
        df = Counter()
        for c in self.tf:
            df.update(c.keys())
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def scores(self, query):
        q = tokenize(query)
        res = []
        for i, c in enumerate(self.tf):
            s = 0.0
            for t in q:
                f = c.get(t)
                if not f:
                    continue
                denom = f + self.k1 * (1 - self.b + self.b * self.len[i] / (self.avg or 1))
                s += self.idf.get(t, 0.0) * f * (self.k1 + 1) / denom
            res.append(s)
        return res

    def top(self, query, k):
        sc = self.scores(query)
        order = sorted(range(len(sc)), key=lambda i: -sc[i])[:k]
        return [(i, sc[i]) for i in order if sc[i] > 0]


def load_glossary(path):
    try:
        data = json.loads(Path(path).read_text())
        return {str(k).lower(): str(v) for k, v in data.items() if not str(k).startswith("_")}
    except (OSError, ValueError):
        return {}


def expand_query(query, glossary):
    """Append the long form of any known abbreviation found as a whole word in the query."""
    extra = []
    words = set(w.lower() for w in re.findall(r"[A-Za-z0-9]+", query))
    for abbr, long in glossary.items():
        if abbr in words:
            extra.append(long)
    return query + (" " + " ".join(extra) if extra else "")


def rrf(rankings, k=60, top_n=8):
    """Reciprocal-rank fusion of several ranked id lists."""
    score = {}
    for ranking in rankings:
        for rank, ident in enumerate(ranking):
            score[ident] = score.get(ident, 0.0) + 1.0 / (k + rank + 1)
    return [i for i, _ in sorted(score.items(), key=lambda kv: -kv[1])][:top_n]
