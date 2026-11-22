from pathlib import Path

import pytest

from kide.i18n import FALLBACK_LOCALE, LOCALES_DIR, LocaleError, Translator
from kide.i18n.translator import load_locale_file, normalize_locale, placeholders

LOCALE_FILES = sorted(LOCALES_DIR.glob("*.yaml"))
REFERENCE = load_locale_file(LOCALES_DIR / f"{FALLBACK_LOCALE}.yaml")


# --- Shipped locale files -------------------------------------------------


def test_spanish_and_english_are_shipped():
    assert {path.stem for path in LOCALE_FILES} >= {"en", "es"}


@pytest.mark.parametrize("path", LOCALE_FILES, ids=lambda p: p.stem)
def test_every_locale_has_the_same_keys(path: Path):
    keys = set(load_locale_file(path))
    assert keys - set(REFERENCE) == set(), f"{path.name} has keys missing from en.yaml"
    assert set(REFERENCE) - keys == set(), f"{path.name} is missing keys"


@pytest.mark.parametrize("path", LOCALE_FILES, ids=lambda p: p.stem)
def test_every_locale_uses_the_same_placeholders(path: Path):
    catalog = load_locale_file(path)
    mismatched = {
        key: (placeholders(REFERENCE[key]), placeholders(text))
        for key, text in catalog.items()
        if key in REFERENCE and placeholders(text) != placeholders(REFERENCE[key])
    }
    assert mismatched == {}


@pytest.mark.parametrize("path", LOCALE_FILES, ids=lambda p: p.stem)
def test_no_empty_texts(path: Path):
    assert [key for key, text in load_locale_file(path).items() if not text.strip()] == []


def test_real_catalog_translates():
    translator = Translator.from_directory(default_locale="es")
    assert translator.t("checkin.question") == "¿Qué toca hoy?"
    assert translator.t("checkin.question", locale="en") == "What's on today?"
    assert translator.t("common.yes") == "Sí"


# --- Translator behaviour (invented catalogs) -----------------------------

CATALOGS = {
    "en": {"greet": "Hello {name}", "only_en": "English only"},
    "es": {"greet": "Hola {name}"},
}


def test_default_locale_from_profile():
    assert Translator(CATALOGS, "es").t("greet", name="Ane") == "Hola Ane"


def test_explicit_locale_overrides_default():
    assert Translator(CATALOGS, "es").t("greet", locale="en", name="Ane") == "Hello Ane"


def test_missing_key_falls_back_to_english():
    assert Translator(CATALOGS, "es").t("only_en") == "English only"


def test_unknown_key_returns_key(caplog):
    assert Translator(CATALOGS, "es").t("nope.missing") == "nope.missing"
    assert "nope.missing" in caplog.text


def test_unknown_default_locale_falls_back_to_english():
    assert Translator(CATALOGS, "eu").default_locale == "en"


def test_region_tags_are_normalized():
    assert normalize_locale("es_ES") == "es"
    assert Translator(CATALOGS, "es-ES").default_locale == "es"


def test_missing_parameter_keeps_placeholder():
    assert Translator(CATALOGS).t("greet", other="x") == "Hello {name}"


def test_english_catalog_is_required():
    with pytest.raises(LocaleError, match="fallback"):
        Translator({"es": {"a": "b"}})


# --- Malformed files --------------------------------------------------------


def test_non_text_value_is_rejected(tmp_path):
    bad = tmp_path / "en.yaml"
    bad.write_text("count: 3\n", encoding="utf-8")
    with pytest.raises(LocaleError, match="'count' must be text"):
        load_locale_file(bad)


def test_unquoted_yes_key_is_rejected(tmp_path):
    bad = tmp_path / "en.yaml"
    bad.write_text("common:\n  yes: Yes\n", encoding="utf-8")
    with pytest.raises(LocaleError, match="must be quoted"):
        load_locale_file(bad)
