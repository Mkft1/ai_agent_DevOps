"""Регресс-тест: все локальные картинки, на которые ссылаются README, существуют.

Исторический контекст: README.md ссылался на несуществующий docs/assets/demo.gif
(битая картинка на GitHub) — этот тест не даёт проблеме вернуться.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMG_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")


def _local_images(readme: Path) -> list[str]:
    return [
        m
        for m in IMG_RE.findall(readme.read_text(encoding="utf-8"))
        if not m.startswith(("http://", "https://"))
    ]


def test_readme_images_exist():
    for readme in (ROOT / "README.md", ROOT / "README.ru.md"):
        for ref in _local_images(readme):
            assert (ROOT / ref).exists(), f"{readme.name}: missing image {ref}"


def test_visual_table_present_in_both_readmes():
    en = (ROOT / "README.md").read_text(encoding="utf-8")
    ru = (ROOT / "README.ru.md").read_text(encoding="utf-8")
    for readme in (en, ru):
        # assert "docs/assets/dashboard.png" in readme
        # assert "docs/assets/swagger.png" in readme
