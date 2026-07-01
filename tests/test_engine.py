from app.game import engine


def test_sequence_length_grows_and_caps():
    assert engine.sequence_length(1, max_len=8) == 3
    assert engine.sequence_length(2, max_len=8) == 4
    assert engine.sequence_length(100, max_len=8) == 8


def test_generate_sequence_has_correct_length_and_valid_words():
    seq = engine.generate_sequence(round_number=3, max_len=8)
    assert len(seq) == 5
    from app.game.words import WORDS
    assert all(w in WORDS for w in seq)


def test_generate_sequence_never_exceeds_word_pool():
    from app.game.words import WORDS
    # even with an absurd max_len, sampling must not raise ValueError
    seq = engine.generate_sequence(round_number=1000, max_len=10_000)
    assert len(seq) == len(WORDS)


def test_normalize_lowercases_strips_punctuation_and_fillers():
    assert engine.normalize("Apple, tiger and river!") == ["apple", "tiger", "river"]


def test_normalize_drops_leading_filler_phrases():
    assert engine.normalize("um the answer is apple tiger") == ["answer", "apple", "tiger"]
