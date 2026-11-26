from kide.bot.summaries import format_duration_min, format_number, summarize_activity, summarize_wod
from kide.catalog.movements import default_movements
from kide.i18n import Translator
from kide.llm.schemas import Activity, Wod, apply_catalog

TRANSLATOR = Translator.from_directory()
CATALOG = default_movements()


def t_for(locale):
    return lambda key, **params: TRANSLATOR.t(key, locale=locale, **params)


def fran_with_strength():
    return Wod.model_validate(
        {
            "title": "Fran",
            "format": "mixed",
            "confidence": "high",
            "scaling": "rx",
            "result": "4:32",
            "blocks": [
                {
                    "label": "A",
                    "format": "strength",
                    "sets": 5,
                    "load_pct_1rm": 75,
                    "movements": [{"name": "BS", "movement_id": "back_squat", "reps": 5}],
                },
                {
                    "label": "B",
                    "format": "for_time",
                    "time_cap_min": 10,
                    "movements": [
                        {
                            "name": "Thruster",
                            "movement_id": "thruster",
                            "reps_per_round": [21, 15, 9],
                            "load_kg": 42.5,
                        },
                        {
                            "name": "Pull-up",
                            "movement_id": "pull_up",
                            "reps_per_round": [21, 15, 9],
                        },
                        {
                            "name": "DB snatch",
                            "movement_id": "dumbbell_snatch",
                            "reps": 10,
                            "load_kg": 22.5,
                            "implements": 2,
                        },
                        {
                            "name": "Box jump",
                            "movement_id": "box_jump",
                            "reps": 15,
                            "height_cm": 60,
                        },
                        {"name": "Zumba", "duration_s": 30},
                        {"name": "Row", "movement_id": "row", "calories": 15},
                        {"name": "Run", "movement_id": "run", "distance_m": 400},
                    ],
                },
            ],
        }
    )


def test_wod_summary_in_spanish():
    text = summarize_wod(fran_with_strength(), t_for("es"), "es", CATALOG)
    assert text.splitlines() == [
        "Fran · Mixto",
        "A) Fuerza · al 75 % del 1RM",
        "• 5×5 Sentadilla trasera",  # noqa: RUF001 - multiplication sign intended
        "B) For time · límite 10 min",
        "• 21-15-9 Thruster · 42,5 kg",
        "• 21-15-9 Dominada",
        "• 10 Arrancada con mancuerna · 2×22,5 kg",  # noqa: RUF001
        "• 15 Salto al cajón · 60 cm",
        "• 30 s Zumba (?)",
        "• 15 cal Remo (ergómetro)",
        "• 400 m Carrera",
        "Nivel: Rx · Resultado: 4:32",
    ]


def test_wod_summary_in_english_with_warnings():
    wod = Wod.model_validate(
        {
            "format": "amrap",
            "confidence": "low",
            "unreadable": ["second movement"],
            "notes": "Partner WOD",
            "blocks": [
                {
                    "format": "rounds",
                    "is_warmup": True,
                    "rounds": 2,
                    "movements": [{"name": "PVC"}],
                },
                {
                    "format": "amrap",
                    "duration_min": 12,
                    "movements": [{"name": "WB", "reps": 20, "load_text": "9/6"}],
                },
                {
                    "format": "emom",
                    "duration_min": 10,
                    "every_min": 2,
                    "movements": [{"name": "T2B", "reps": 8}],
                },
                {
                    "format": "intervals",
                    "rounds": 8,
                    "work_s": 20,
                    "rest_s": 10,
                    "movements": [{"name": "air bike", "notes": "max cal"}],
                },
            ],
        }
    )
    wod = apply_catalog(wod, CATALOG)
    lines = summarize_wod(wod, t_for("en"), "en", CATALOG).splitlines()
    assert lines[0] == "WOD · Mixed"
    assert lines[1] == "Warm-up · 2 rounds"
    assert lines[2] == "• PVC (?)"
    assert "• 20 Wall ball · 9/6" in lines
    assert "EMOM · 10 min · every 2 min" in lines
    assert "Intervals · 8 rounds · 20 s on / 10 s off" in lines
    assert "• Air bike · max cal" in lines
    assert "Notes: Partner WOD" in lines
    assert "Couldn't read: second movement" in lines
    assert lines[-1].startswith("⚠️")


def test_activity_summary():
    run = Activity.model_validate(
        {
            "type": "run",
            "name": "Txorierri",
            "duration_min": 52.5,
            "distance_km": 10.2,
            "avg_hr_bpm": 152,
            "elevation_gain_m": 120,
            "rpe": 6,
            "confidence": "high",
        }
    )
    assert summarize_activity(run, t_for("es")).splitlines() == [
        "Carrera · Txorierri",
        "10,2 km · 52,5 min · 5:09 /km · +120 m · FC 152 ppm · RPE 6/10",
    ]
    hike = Activity.model_validate(
        {"type": "hike", "duration_min": 185, "confidence": "low", "notes": "con barro"}
    )
    assert summarize_activity(hike, t_for("en")).splitlines() == [
        "Hike",
        "3 h 05 min",
        "Notes: con barro",
        "⚠️ I'm not sure about this reading: please check it.",
    ]
    unknown = Activity.model_validate({"type": "other", "confidence": "high"})
    assert summarize_activity(unknown, t_for("es")).splitlines()[1] == "duración ?"


def test_number_formatting():
    es, en = t_for("es"), t_for("en")
    assert format_number(42.5, es) == "42,5"
    assert format_number(42.0, es) == "42"
    assert format_number(1.256, en) == "1.26"
    assert format_duration_min(59.6, en) == "59.6 min"
    assert format_duration_min(120, es) == "2 h 00 min"
