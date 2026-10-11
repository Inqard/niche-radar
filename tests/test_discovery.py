from niche_radar.config import load_config
from niche_radar.discovery import SourceVideo, find_candidates, phrases_of

CFG = load_config()["discovery"]


def sv(vid, ch, title, tags=(), views=100_000, query="etymology", direction="languages", parent=5.0):
    return SourceVideo(vid, ch, title, list(tags), views, query, direction, parent)


STOP, GENERIC = set(CFG["stopwords"]), set(CFG["generic_words"])


def test_phrases_skip_stopword_edges_and_numbers():
    _, found = phrases_of(sv("v", "c", "The History of Word Origins 2026"), STOP, GENERIC, 2, 4)
    assert "word origins" in found
    assert "history of word origins" in found
    assert not any(p.startswith("the ") or p.endswith(" of") for p in found)
    assert not any("2026" in p for p in found)


def test_phrase_needs_two_channels():
    videos = [sv("v1", "A", "x", tags=["word origins"]), sv("v2", "A", "y", tags=["word origins"])]
    assert find_candidates(videos, set(), CFG) == []
    videos.append(sv("v3", "B", "z", tags=["word origins"]))
    cands = find_candidates(videos, set(), CFG)
    assert [c.phrase for c in cands] == ["word origins"]
    assert cands[0].channels == 2 and cands[0].direction == "languages" and cands[0].parent == "etymology"


def test_low_view_sources_and_fragments_of_existing_queries_are_ignored():
    videos = [sv("v1", "A", "x", tags=["game lore"]), sv("v2", "B", "y", tags=["game lore"])]
    assert find_candidates(videos, {"game lore explained"}, CFG) == []
    weak = [sv("v1", "A", "x", tags=["word origins"], views=100),
            sv("v2", "B", "y", tags=["word origins"], views=100)]
    assert find_candidates(weak, set(), CFG) == []


def test_clickbait_phrases_are_never_candidates():
    titles = ["Things You Never Knew", "Facts Nobody Knew", "Crazy Facts You Never Knew"]
    videos = [sv(f"v{i}", f"C{i}", t, tags=["crazy facts"]) for i, t in enumerate(titles)]
    assert find_candidates(videos, set(), CFG) == []


def test_queries_never_start_with_a_hype_modifier_but_genre_words_are_fine():
    videos = [sv(f"v{i}", f"C{i}", f"Complete Game Lore {i}", tags=["actually works", "space facts",
                                                                     "weird animals", "secret societies"])
              for i in range(3)]
    phrases = {c.phrase for c in find_candidates(videos, set(), CFG)}
    assert {"space facts", "game lore", "weird animals", "secret societies"} <= phrases
    assert not {"complete game", "actually works", "complete game lore"} & phrases


def test_title_only_phrase_needs_one_more_channel_than_tag_phrase():
    two = [sv("v1", "A", "Ancient Rome Engineering"), sv("v2", "B", "Ancient Rome Engineering Secrets")]
    assert find_candidates(two, set(), CFG) == []
    three = two + [sv("v3", "C", "How Ancient Rome Engineering Worked")]
    assert [c.phrase for c in find_candidates(three, set(), CFG)] == ["ancient rome engineering"]


def test_one_direction_cannot_take_all_new_candidates():
    esl = [sv(f"e{i}", f"E{i % 3}", "x", tags=[f"english topic{i}"], direction="education") for i in range(30)]
    esl += [sv(f"e{i}b", f"E{(i + 1) % 3}", "y", tags=[f"english topic{i}"], direction="education")
            for i in range(30)]
    other = [sv(f"a{i}", f"A{i}", "z", tags=["deep sea creatures"], direction="animals", views=1000 + 30000)
             for i in range(2)]
    cands = find_candidates(esl + other, set(), CFG)
    by_dir = {d: sum(c.direction == d for c in cands) for d in ("education", "animals")}
    assert by_dir["education"] == CFG["max_new_per_direction"]
    assert by_dir["animals"] == 1


def test_narrower_phrase_than_existing_query_is_kept():
    videos = [sv("v1", "A", "x", tags=["english etymology"]), sv("v2", "B", "y", tags=["english etymology"])]
    assert [c.phrase for c in find_candidates(videos, {"etymology"}, CFG)] == ["english etymology"]
