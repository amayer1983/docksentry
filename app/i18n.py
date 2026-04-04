#!/usr/bin/env python3
"""Internationalization - loads translations from JSON files in lang/ directory."""

import json
import os

LANG_DIR = os.path.join(os.path.dirname(__file__), "lang")

# Minimal English fallback (in case lang/en.json is missing)
_FALLBACK = {}

_cache = {}


def _load_language(lang):
    """Load a language file from lang/ directory."""
    if lang in _cache:
        return _cache[lang]

    lang_file = os.path.join(LANG_DIR, f"{lang}.json")
    if os.path.isfile(lang_file):
        with open(lang_file, encoding="utf-8") as f:
            strings = json.load(f)
        _cache[lang] = strings
        return strings
    return None


def available_languages():
    """Return list of available language codes."""
    langs = []
    if os.path.isdir(LANG_DIR):
        for f in sorted(os.listdir(LANG_DIR)):
            if f.endswith(".json"):
                langs.append(f[:-5])
    return langs


def get_translator(language="en"):
    """Return a translation function for the given language."""
    lang = language.lower()[:2]
    strings = _load_language(lang)
    en_strings = _load_language("en") or _FALLBACK

    if not strings:
        strings = en_strings

    def t(key, **kwargs):
        text = strings.get(key, en_strings.get(key, key))
        if kwargs:
            try:
                text = text.format(**kwargs)
            except (KeyError, IndexError):
                pass
        return text

    return t
