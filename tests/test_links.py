import pytest

from niche_radar.links import parse_link, parse_many

CH = "UC" + "a" * 22


@pytest.mark.parametrize("text,expected", [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", ("video", "dQw4w9WgXcQ")),
    ("https://www.youtube.com/watch?app=desktop&v=dQw4w9WgXcQ&t=42s", ("video", "dQw4w9WgXcQ")),
    ("https://youtu.be/dQw4w9WgXcQ?si=abc", ("video", "dQw4w9WgXcQ")),
    ("https://www.youtube.com/shorts/dQw4w9WgXcQ", ("video", "dQw4w9WgXcQ")),
    ("https://www.youtube.com/live/dQw4w9WgXcQ?feature=share", ("video", "dQw4w9WgXcQ")),
    ("dQw4w9WgXcQ", ("video", "dQw4w9WgXcQ")),
    (f"https://www.youtube.com/channel/{CH}/videos", ("channel", CH)),
    (CH, ("channel", CH)),
    ("https://www.youtube.com/@Word.Nerd-42/videos", ("handle", "Word.Nerd-42")),
    ("@archaeo", ("handle", "archaeo")),
    ("https://www.youtube.com/@archaeo", ("handle", "archaeo")),
    ("https://example.com/watch?v=short", None),
    ("hello world", None),
    ("dQw4w9WgXcQX", None),  # 12 chars is not a video id
])
def test_parse_link(text, expected):
    assert parse_link(text) == expected


def test_parse_many_dedupes_and_reports_rejects():
    links, rejected = parse_many([
        "https://youtu.be/dQw4w9WgXcQ https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "", "  @archaeo  ", "not-a-link",
    ])
    assert links == [("video", "dQw4w9WgXcQ"), ("handle", "archaeo")]
    assert rejected == ["not-a-link"]
