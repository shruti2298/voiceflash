import random
import re
from dataclasses import dataclass
from typing import List

from app.game.words import WORDS

FILLER = {"um", "uh", "the", "a", "an", "and", "then", "was", "is", "please", "okay"}


def sequence_length(round_number: int, max_len: int = 8) -> int:
    return min(2 + round_number, max_len)


def generate_sequence(round_number: int, max_len: int = 8) -> List[str]:
    # cap length at the pool size so random.sample never raises ValueError
    # even if MAX_SEQUENCE_LENGTH is misconfigured larger than the word pool
    n = min(sequence_length(round_number, max_len), len(WORDS))
    # sample without replacement so the sequence has no repeats (easier to say/hear)
    return random.sample(WORDS, n)


def normalize(text: str) -> List[str]:
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)          # drop punctuation
    tokens = [t for t in text.split() if t and t not in FILLER]
    return tokens


@dataclass
class Evaluation:
    is_correct: bool
    points: int
    expected: List[str]
    heard: List[str]


def score(sequence_length_: int) -> int:
    return 10 * sequence_length_


def evaluate(expected: List[str], transcript: str) -> Evaluation:
    heard = normalize(transcript)
    is_correct = heard == expected
    return Evaluation(
        is_correct=is_correct,
        points=score(len(expected)) if is_correct else 0,
        expected=expected,
        heard=heard,
    )
