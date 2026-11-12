import asyncio
import re
from collections import Counter

from scribe.application.pipeline_ports import Summarizer
from scribe.domain.transcript import Summary, Transcription

MODEL = "extractive-v1"

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+")
_WORD = re.compile(r"[^\W\d_]{3,}")
_ACTION = re.compile(
    r"\b(need to|needs to|should|must|have to|let's|action item|follow up|todo|to-do)\b"
    r"|\b(потрібно|треба|необхідно|маємо|повинен|повинні|давайте|домовилися)\b",
    re.IGNORECASE,
)
_STOPWORDS = frozenset(
    [
        "the",
        "and",
        "for",
        "that",
        "this",
        "with",
        "from",
        "have",
        "has",
        "had",
        "will",
        "would",
        "can",
        "could",
        "should",
        "may",
        "might",
        "must",
        "are",
        "was",
        "were",
        "been",
        "being",
        "not",
        "but",
        "you",
        "your",
        "our",
        "their",
        "they",
        "them",
        "his",
        "her",
        "its",
        "what",
        "which",
        "who",
        "whom",
        "when",
        "where",
        "why",
        "how",
        "there",
        "here",
        "than",
        "then",
        "also",
        "into",
        "over",
        "after",
        "before",
        "about",
        "just",
        "very",
        "що",
        "це",
        "як",
        "чи",
        "для",
        "про",
        "при",
        "під",
        "над",
        "між",
        "так",
        "але",
        "яка",
        "який",
        "які",
        "цей",
        "той",
        "ця",
        "ці",
        "вони",
        "ми",
        "ви",
        "він",
        "вона",
        "було",
        "були",
        "буде",
        "бути",
        "вже",
        "ще",
        "тут",
        "там",
        "коли",
        "чому",
    ]
)


class ExtractiveSummarizer(Summarizer):
    """Dependency-free default: top sentences by word frequency, frequent words as topics,
    trigger phrases as action items. An LLM-based summarizer can replace it behind the port."""

    async def summarize(self, transcription: Transcription) -> Summary:
        return await asyncio.to_thread(self._summarize, transcription)

    @staticmethod
    def _summarize(transcription: Transcription) -> Summary:
        text = " ".join(" ".join(s.text.split()) for s in transcription.segments)
        sentences = [s for s in (p.strip() for p in _SENTENCE_SPLIT.split(text)) if s]
        if not sentences:
            return Summary(summary="", topics=[], action_items=[], model=MODEL)

        content_words = [
            [w for w in (x.lower() for x in _WORD.findall(s)) if w not in _STOPWORDS]
            for s in sentences
        ]
        freq = Counter(w for words in content_words for w in words)

        def score(i: int) -> float:
            words = content_words[i]
            return sum(freq[w] for w in words) / (len(words) ** 0.5) if words else 0.0

        best = sorted(sorted(range(len(sentences)), key=score, reverse=True)[:3])
        action_items = list(dict.fromkeys(s[:300] for s in sentences if _ACTION.search(s)))[:10]
        return Summary(
            summary=" ".join(sentences[i] for i in best),
            topics=[w for w, _ in freq.most_common(5)],
            action_items=action_items,
            model=MODEL,
        )
