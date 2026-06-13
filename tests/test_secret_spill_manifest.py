from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "benchmarks" / "secret-spill-v1" / "manifest.yaml"


def test_secret_spill_prompt_mentions_cache_and_dump() -> None:
    text = MANIFEST.read_text(encoding="utf-8")

    assert "缓存" in text
    assert "stale cache" in text
    assert "dump" in text
