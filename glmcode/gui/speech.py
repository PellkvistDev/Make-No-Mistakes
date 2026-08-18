"""Which speech engine and voice the app is currently set to.

A leaf on purpose: both the event sink (which reads replies aloud) and the
voice API (which previews and pre-warms them) need this answer, and neither
should have to import the other to get it.
"""

from __future__ import annotations


def _tts_engine_voice(cfg) -> tuple[str, str]:
    """The active TTS engine and its voice from config. Each engine keeps its
    own voice (tts_voice for Kokoro, piper_voice for Piper), so switching
    engines never lands on a voice the other one doesn't have."""
    engine = (getattr(cfg, "tts_engine", "kokoro") if cfg else "kokoro") or "kokoro"
    if engine == "piper":
        return engine, (getattr(cfg, "piper_voice", "") if cfg else "") or "en_US-amy-medium"
    return "kokoro", (getattr(cfg, "tts_voice", "") if cfg else "") or "af_heart"
