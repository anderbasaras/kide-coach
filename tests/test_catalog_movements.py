import pytest

from kide.catalog import CatalogError, Movement, MovementCatalog, load_movements, normalize_name
from kide.catalog.movements import Category, Unit, default_movements
from kide.i18n import Translator

BRANDS = ("crossfit", "hyrox")


@pytest.fixture(scope="module")
def catalog():
    return default_movements()


def test_shipped_catalog_is_valid_and_big_enough(catalog):
    assert len(catalog) >= 80


def test_every_movement_has_a_name_in_every_bot_language(catalog):
    locales = set(Translator.from_directory().available_locales)
    for movement in catalog:
        assert set(movement.names) == locales, movement.id


def test_no_brand_names_in_ids_or_display_names(catalog):
    for movement in catalog:
        texts = [movement.id, *movement.names.values()]
        assert not any(brand in normalize_name(text) for text in texts for brand in BRANDS)


def test_hybrid_race_stations_are_covered(catalog):
    stations = {
        "run",
        "ski",
        "sled_push",
        "sled_pull",
        "burpee_broad_jump",
        "row",
        "farmers_carry",
        "sandbag_lunge",
        "wall_ball",
    }
    assert stations <= set(catalog.ids)


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("T2B", "toes_to_bar"),
        ("Toes-to-bar", "toes_to_bar"),
        ("C&J", "clean_and_jerk"),
        ("HSPU", "handstand_push_up"),
        ("Pull-ups", "pull_up"),
        ("Wall Balls", "wall_ball"),
        ("burpees", "burpee"),
        ("Flexiones", "push_up"),
        ("Zancadas con saco", "sandbag_lunge"),
        ("dobles", "double_under"),
        ("DU", "double_under"),
        ("DB snatches", "dumbbell_snatch"),
        ("SkiErg", "ski"),
        ("peso muerto", "deadlift"),
        ("Sumo Deadlift High Pull", "sumo_deadlift_high_pull"),
        ("Saltos al cajón", "box_jump"),
        ("farmers_carry", "farmers_carry"),
    ],
)
def test_resolve_board_spellings(catalog, written, expected):
    movement = catalog.resolve(written)
    assert movement is not None, written
    assert movement.id == expected


@pytest.mark.parametrize("written", ["", "sentadilla", "5 burpees", "bike", "something else"])
def test_ambiguous_or_unknown_spellings_resolve_to_nothing(catalog, written):
    assert catalog.resolve(written) is None


def test_display_name_falls_back_to_english(catalog):
    movement = catalog.get("deadlift")
    assert movement.name("es") == "Peso muerto"
    assert movement.name("de") == "Deadlift"
    assert catalog.get("nope") is None
    assert "deadlift" in catalog


def test_normalize_name():
    assert normalize_name("  Zancada  con SACO! ") == "zancada con saco"
    assert normalize_name("Salto al CAJÓN") == "salto al cajon"
    assert normalize_name("C&J") == "c&j"


def _movement(movement_id: str, *aliases: str) -> Movement:
    return Movement(
        id=movement_id,
        names={"en": movement_id},
        category=Category.BARBELL,
        unit=Unit.REPS,
        aliases=aliases,
    )


def test_an_alias_shared_by_two_movements_is_rejected():
    with pytest.raises(CatalogError, match="points to both"):
        MovementCatalog([_movement("a_one", "x"), _movement("b_two", "X!")])


def test_duplicate_ids_are_rejected():
    with pytest.raises(CatalogError, match="duplicate"):
        MovementCatalog([_movement("same"), _movement("same")])


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("movements: [", "invalid YAML"),
        ("schema_version: 2\nmovements: []", "movements.yaml"),
        (
            "schema_version: 1\nmovements:\n  - {id: Bad-Id, names: {en: x}, category: barbell,"
            " unit: reps}",
            "snake_case",
        ),
        (
            "schema_version: 1\nmovements:\n  - {id: ok, names: {en: x}, category: barbell,"
            " unit: reps, muscles: []}",
            "Extra inputs",
        ),
    ],
)
def test_invalid_files_give_clear_errors(tmp_path, content, message):
    path = tmp_path / "movements.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(CatalogError, match=message):
        load_movements(path)


def test_missing_file(tmp_path):
    with pytest.raises(CatalogError, match="cannot read"):
        load_movements(tmp_path / "nope.yaml")
