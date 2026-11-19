"""FAQ and policy chunks plus the default Chroma-backed retriever.

Design and honest limits
------------------------
* Source documents: ``app/db/data/source/FAQ.md`` (one chunk per Q&A) and
  ``app/db/data/source/Policies.md`` (one chunk per numbered section). They are
  the source of truth for support rules and are never modified.
* The default retriever uses a persistent Chroma index with OpenAI embeddings and
  **requires ``OPENAI_API_KEY``**: without it the policy capability reports
  "unavailable" (it never invents policy text). This is separate from the LLM
  provider (``LLM_PROVIDER``): ``RuleBasedLLM`` needs no key, retrieval always does.
  The lexical ``KnowledgeBase`` below remains an independent implementation;
  it is not used as a fallback.
* The embedding function is injectable (``configure_embeddings``) so tests and
  offline evaluation can use a local deterministic embedding with the same Chroma
  retrieval code. Nothing injects it in production and importing this module
  never needs a key or touches the network.
* Retrieved text is **untrusted data**. Callers must present it to a
  model as quoted reference material, never as instructions.
* A query with no sufficiently close vector match returns no hits, and the
  caller must say it cannot confirm rather than invent a policy.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Optional, Sequence

from app.db.connection import PROJECT_ROOT

if TYPE_CHECKING:
    from app.rag.vector_index import ChromaKnowledgeBase

SOURCE_DIR = PROJECT_ROOT / "app" / "db" / "data" / "source"


class KnowledgeBaseError(Exception):
    """The FAQ or policy source is missing, unreadable or contains no usable entries.

    Callers must say they cannot confirm an answer. They must never fall back to
    invented policy text.
    """

_TOKEN_RE = re.compile(r"[a-z0-9\u20b9]+")
_STOP = frozenset("""a an the is are was were be to of for in on at by with from and or not
no it its this that these those do does did can could will would should i you your my me we
our they them their what which who how when where why if then than so as but about any some
there here please tell long take takes much many get need want""".split())
_QA_RE = re.compile(r"\*\*(Q\d+\.\d+):\s*(.*?)\*\*\s*\n\s*A:\s*(.*?)(?=\n\s*\n|\Z)", re.S)
_SECTION_RE = re.compile(r"^##\s+(\d+\.\s+.+?)\s*$", re.M)
_K1 = 1.5
_B = 0.75


@dataclass(frozen=True)
class Chunk:
    """One retrievable unit with its citation."""

    source: str
    section: str
    title: str
    text: str


@dataclass(frozen=True)
class SearchHit:
    chunk: Chunk
    score: float


_SUFFIXES = ("ations", "ation", "ings", "ing", "ed", "es", "s", "e")

# Small, hand-written domain vocabulary bridge. The store documents say "within
# 7 days", "charges" and "business days" where customers say "window", "cost"
# and "timeline". Each rule fires only when the query also names its topic
# ("window" means "within 7 days" for returns but "3-6 business days" for
# delivery), so a word in one topic never pulls in the passage of another. An
# expansion counts at half weight and never replaces the customer's own words.
# This is a keyword aid, not semantic search.
_RETURN_TOPIC = ("return", "returns", "returned", "returnable", "replace", "replacement", "exchange",
                 "damaged", "wrong")
_DELIVERY_TOPIC = ("delivery", "deliver", "delivered", "shipping", "shipment", "shipped", "ship",
                   "courier", "dispatch")
_REFUND_TOPIC = ("refund", "refunds", "reimbursement")
_WINDOW_WORDS = ("window", "deadline", "period", "timeframe", "timeline", "duration")

# (query words that trigger, topic words one of which must also be in the query, expansions)
_BRIDGE = (
    (_WINDOW_WORDS, _RETURN_TOPIC, ("within", "days", "eligible")),
    (_WINDOW_WORDS, _DELIVERY_TOPIC, ("business", "days")),
    (_WINDOW_WORDS, _REFUND_TOPIC, ("within", "business", "days")),
    (("cost", "costs", "fee", "fees"), _DELIVERY_TOPIC, ("charge", "fee")),
)
_EXPANSION_WEIGHT = 0.5


def _stem(token: str) -> str:
    """Light suffix stripping so charge/charges and cancel/cancelled/cancellation meet."""
    for suffix in _SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 4:
            token = token[: -len(suffix)]
            break
    if len(token) >= 5 and token[-1] == token[-2] and token[-1] not in "aeiou":
        token = token[:-1]  # cancell -> cancel
    return token


def tokenize(text: str) -> list[str]:
    return [_stem(t) for t in _TOKEN_RE.findall(text.lower()) if t not in _STOP]


def query_weights(query: str) -> dict[str, float]:
    """Query terms with weights: the customer's words at 1.0, expansions at 0.5."""
    weights: dict[str, float] = {}
    raw = set(_TOKEN_RE.findall(query.lower()))
    for term in tokenize(query):
        weights[term] = 1.0
    for triggers, topic, expansions in _BRIDGE:
        fired = raw.intersection(triggers)
        if fired and raw.intersection(topic):
            # The generic word ("timeline", "window") is a stand-in for the facts the
            # documents state in other words, so it must not out-vote the topic word.
            for word in fired:
                for term in tokenize(word):
                    weights[term] = min(weights.get(term, _EXPANSION_WEIGHT), _EXPANSION_WEIGHT)
            for extra in expansions:
                for term in tokenize(extra):
                    weights.setdefault(term, _EXPANSION_WEIGHT)
    return weights


def load_chunks(source_dir: Optional[Path] = None) -> list[Chunk]:
    """Parse the FAQ into Q&A chunks and the policies into section chunks.

    ``source_dir`` defaults to ``SOURCE_DIR`` looked up at call time, so tests can
    point it at a temporary folder.

    Raises:
        KnowledgeBaseError: a source file is missing or unreadable, or either source
            yields no entries. Nothing is guessed or substituted.
    """
    source_dir = SOURCE_DIR if source_dir is None else Path(source_dir)
    try:
        faq = (source_dir / "FAQ.md").read_text(encoding="utf-8")
        policies = (source_dir / "Policies.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise KnowledgeBaseError("the FAQ or policy source could not be read") from exc
    faq_chunks: list[Chunk] = []
    for match in _QA_RE.finditer(faq):
        number, question, answer = match.groups()
        faq_chunks.append(Chunk("FAQ.md", number, question.strip(),
                                f"{question.strip()} {answer.strip()}"))
    policy_chunks: list[Chunk] = []
    headings = list(_SECTION_RE.finditer(policies))
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(policies)
        body = policies[heading.end():end].replace("---", "").strip()
        title = heading.group(1).strip()
        policy_chunks.append(Chunk("Policies.md", title, title, f"{title}. {body}"))
    if not faq_chunks or not policy_chunks:
        raise KnowledgeBaseError("the FAQ or policy source contains no usable entries")
    return faq_chunks + policy_chunks


class KnowledgeBase:
    """In-memory BM25 index over a list of chunks."""

    def __init__(self, chunks: Iterable[Chunk]) -> None:
        self.chunks = list(chunks)
        self._docs = [tokenize(c.text) + tokenize(c.title) for c in self.chunks]
        self._avg = (sum(len(d) for d in self._docs) / len(self._docs)) if self._docs else 0.0
        document_frequency: dict[str, int] = {}
        for doc in self._docs:
            for term in set(doc):
                document_frequency[term] = document_frequency.get(term, 0) + 1
        total = len(self._docs)
        self._idf = {
            term: math.log(1 + (total - df + 0.5) / (df + 0.5))
            for term, df in document_frequency.items()
        }

    def search(self, query: str, k: int = 3, min_score: float = 1.0) -> list[SearchHit]:
        """Return up to *k* chunks scoring at least *min_score*, best first."""
        weights = query_weights(query) if isinstance(query, str) else {}
        if not weights or not self.chunks:
            return []
        hits: list[SearchHit] = []
        for chunk, doc in zip(self.chunks, self._docs):
            length = len(doc) or 1
            score = 0.0
            matched_own_word = False
            for term, weight in weights.items():
                frequency = doc.count(term)
                if frequency:
                    matched_own_word = matched_own_word or weight == 1.0
                    norm = frequency * (_K1 + 1) / (
                        frequency + _K1 * (1 - _B + _B * length / (self._avg or 1))
                    )
                    score += weight * self._idf.get(term, 0.0) * norm
            # A passage that matches only the vocabulary bridge, and none of the
            # customer's own words, is not a hit ("the window of my bedroom").
            if matched_own_word and score >= min_score:
                hits.append(SearchHit(chunk, score))
        hits.sort(key=lambda h: (-h.score, h.chunk.source, h.chunk.section))
        return hits[:k]


# Injected embedding configuration (tests and offline evaluation only). ``None`` means
# production: OpenAI embeddings, which require OPENAI_API_KEY.
_embedding_factory: Optional[Callable[[Sequence[Chunk]], Any]] = None
_embedding_max_distance: Optional[float] = None
_embedding_label: Optional[str] = None
PRODUCTION_EMBEDDING_LABEL = "openai:text-embedding-3-small (production; requires OPENAI_API_KEY)"


EmbeddingState = tuple[Optional[Callable[[Sequence[Chunk]], Any]], Optional[float], Optional[str]]


def configure_embeddings(factory: Optional[Callable[[Sequence[Chunk]], Any]] = None, *,
                         max_distance: Optional[float] = None, label: Optional[str] = None) -> EmbeddingState:
    """Inject a Chroma embedding function for ``default_knowledge_base``; ``None`` restores production.

    ``factory`` receives the source chunks and returns a Chroma embedding function
    (``app.rag.local_embeddings.factory`` is the deterministic offline one).
    ``max_distance`` replaces the cosine-distance cut-off, which is calibrated for the
    production embedding. The cached knowledge base is discarded. Returns the previous
    configuration so a caller can put it back with ``restore_embeddings``.
    """
    global _embedding_factory, _embedding_max_distance, _embedding_label
    previous: EmbeddingState = (_embedding_factory, _embedding_max_distance, _embedding_label)
    release_default_knowledge_base()
    _embedding_factory = factory
    _embedding_max_distance = max_distance if factory is not None else None
    _embedding_label = label if factory is not None else None
    return previous


def restore_embeddings(state: EmbeddingState) -> None:
    """Put back a configuration returned by ``configure_embeddings``."""
    configure_embeddings(state[0], max_distance=state[1], label=state[2])


_built: list[Any] = []  # knowledge bases created by default_knowledge_base, so they can be released


def release_default_knowledge_base() -> None:
    """Forget the cached knowledge base and close its Chroma client (idempotent, never raises)."""
    while _built:
        instance = _built.pop()
        try:
            instance.close()
        except Exception:  # noqa: BLE001 - releasing resources must never mask a result
            pass
    default_knowledge_base.cache_clear()


def embedding_configuration() -> str:
    """Which embeddings retrieval will use, without creating anything or needing a key."""
    if _embedding_factory is None:
        return PRODUCTION_EMBEDDING_LABEL
    return f"injected: {_embedding_label or 'custom embedding function'}"


@lru_cache(maxsize=1)
def default_knowledge_base() -> ChromaKnowledgeBase:
    from app.rag.vector_index import ChromaKnowledgeBase

    configured_directory = os.environ.get("VECTOR_STORE_DIR")
    if configured_directory:
        persist_directory = Path(configured_directory)
        if not persist_directory.is_absolute():
            persist_directory = PROJECT_ROOT / persist_directory
    else:
        persist_directory = (
            PROJECT_ROOT / "app" / "db" / "data" / "generated" / "vector_store"
        )

    chunks = load_chunks()
    if _embedding_factory is not None:
        options: dict[str, Any] = {"embedding_function": _embedding_factory(chunks)}
        if _embedding_max_distance is not None:
            options["max_distance"] = _embedding_max_distance
        knowledge_base = ChromaKnowledgeBase(chunks, persist_directory, **options)
    else:
        knowledge_base = ChromaKnowledgeBase(
            chunks,
            persist_directory,
            api_key=os.environ.get("OPENAI_API_KEY"),
        )
    _built.append(knowledge_base)
    return knowledge_base
