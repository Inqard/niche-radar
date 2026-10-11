import random

from niche_radar.clusters import Doc, cluster_docs, video_terms
from niche_radar.config import load_config

CFG = load_config()
STOP, GENERIC = set(CFG["discovery"]["stopwords"]), set(CFG["discovery"]["generic_words"])
CC = dict(CFG["clusters"], max_df_share=0.5)  # small corpus, see test_pipeline_offline


def doc(vid, ch, title, tags=(), views=1000):
    return Doc(vid, ch, views, video_terms(title, list(tags), STOP, GENERIC, CC["max_title_ngram"]))


def corpus():
    docs = []
    for i in range(4):
        docs.append(doc(f"w{i}", f"WA{i % 2}", f"The Origin of the Word {['Boss', 'Cool', 'Nerd', 'Dude'][i]}",
                        ["word origins", "etymology"], views=10_000 * (i + 1)))
    for i in range(4):
        docs.append(doc(f"r{i}", f"RO{i % 2}", f"How Romans Built {['Roads', 'Aqueducts', 'Walls', 'Domes'][i]}",
                        ["roman engineering", "ancient rome"], views=20_000 * (i + 1)))
    fillers = ["Cooking Pasta Tonight", "Football Match Recap", "Guitar Lesson Basics"]
    for i, title in enumerate(fillers):  # unrelated, share no terms -> must not form a sub-niche
        docs.append(doc(f"x{i}", f"X{i}", title, views=500))
    return docs


def test_video_terms_keep_tags_and_title_ngrams_without_clickbait():
    terms = video_terms("Crazy Facts: How Romans Built Roads", ["roman engineering"], STOP, GENERIC, 2)
    assert {"roman engineering", "romans", "romans built", "roads", "built roads"} <= terms
    assert "crazy facts" not in terms and "crazy" not in terms and "how" not in terms


def test_topics_form_separate_clusters():
    clusters = cluster_docs(corpus(), CC)
    by_term = {c.label: c for c in clusters}
    words = next(c for c in clusters if "word origins" in c.terms)
    rome = next(c for c in clusters if "roman engineering" in c.terms)
    assert set(words.video_ids) == {"w0", "w1", "w2", "w3"}
    assert set(rome.video_ids) == {"r0", "r1", "r2", "r3"}
    assert words.channels == 2 and words.term_stats["word origins"] == (2, 100_000)
    assert len(by_term) == len(clusters)          # keys are unique
    assert all(not v.startswith("x") for c in clusters for v in c.video_ids)  # singletons dropped


def test_label_avoids_clickbait_words():
    docs = [doc(f"g{i}", f"G{i}", f"Complete Game Lore Part {i}", ["game lore"], views=5000 * (i + 1))
            for i in range(4)] + corpus()
    lore = next(c for c in cluster_docs(docs, CC, GENERIC) if "game lore" in c.terms)
    assert lore.label == "game lore"


def test_single_channel_topic_is_not_a_subniche():
    docs = [doc(f"s{i}", "SOLO", f"Solo Topic Part {i}", ["solo topic"]) for i in range(4)] + corpus()
    assert all(not any(v.startswith("s") for v in c.video_ids) for c in cluster_docs(docs, CC))


def test_single_channel_topic_is_kept_apart_on_request_without_shifting_other_keys():
    # live 11.10: 49% of window views sat outside multi-channel clusters, incl. 22M on a 12K-sub channel
    docs = [doc(f"s{i}", "SOLO", f"Solo Topic Part {i}", ["solo topic"]) for i in range(4)] + corpus()
    plain = cluster_docs(docs, CC)
    both = cluster_docs(docs, CC, keep_single_channel=True)
    solo = [c for c in both if c.channels == 1]
    assert len(solo) == 1 and solo[0].key.startswith("solo:") and set(solo[0].video_ids) == {f"s{i}" for i in range(4)}
    assert [(c.key, c.video_ids) for c in both if c.channels > 1] == [(c.key, c.video_ids) for c in plain]
    assert all(not v.startswith("x") for c in both for v in c.video_ids)   # 1-video "groups" are still no topic


def test_result_does_not_depend_on_input_order():
    base = cluster_docs(corpus(), CC)
    for seed in range(5):
        shuffled = corpus()
        random.Random(seed).shuffle(shuffled)
        again = cluster_docs(shuffled, CC)
        assert [(c.key, c.video_ids, c.terms) for c in again] == [(c.key, c.video_ids, c.terms) for c in base]
