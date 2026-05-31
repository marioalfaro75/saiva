"""A small, dependency-free Multinomial Naive Bayes categoriser that learns from
the household's own labelled transactions and corrections (PRD R12).

Deliberately simple and fast so it trains on every request and is fully testable;
the categorisation service treats it as a pluggable component behind the rule engine.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict

_TOKEN = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall((text or "").lower())


class NaiveBayesCategoriser:
    def __init__(self) -> None:
        self._reset()

    def _reset(self) -> None:
        self.class_counts: dict[str, int] = defaultdict(int)
        self.token_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.class_token_totals: dict[str, int] = defaultdict(int)
        self.vocab: set[str] = set()
        self.total_docs: int = 0
        self.trained: bool = False

    def train(self, samples: list[tuple[str, str]]) -> None:
        """Train from (text, category_id) pairs."""
        self._reset()
        for text, category_id in samples:
            if not category_id:
                continue
            self.class_counts[category_id] += 1
            self.total_docs += 1
            for tok in tokenize(text):
                self.token_counts[category_id][tok] += 1
                self.class_token_totals[category_id] += 1
                self.vocab.add(tok)
        # Need at least two classes and some data for a meaningful prediction.
        self.trained = self.total_docs > 0 and len(self.class_counts) > 1

    def predict(self, text: str) -> tuple[str | None, float]:
        """Return (category_id, confidence in [0, 1]) or (None, 0.0)."""
        tokens = tokenize(text)
        if not self.trained or not tokens:
            return None, 0.0

        vocab_size = len(self.vocab) or 1
        scores: dict[str, float] = {}
        for category_id, class_count in self.class_counts.items():
            log_prob = math.log(class_count / self.total_docs)
            denom = self.class_token_totals[category_id] + vocab_size
            counts = self.token_counts[category_id]
            for tok in tokens:
                log_prob += math.log((counts.get(tok, 0) + 1) / denom)
            scores[category_id] = log_prob

        best = max(scores, key=lambda c: scores[c])
        # Softmax over log-probs for a pseudo-confidence.
        top = max(scores.values())
        exps = {c: math.exp(s - top) for c, s in scores.items()}
        total = sum(exps.values())
        confidence = exps[best] / total if total else 0.0
        return best, confidence
