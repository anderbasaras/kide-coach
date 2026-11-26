# data/

Versioned catalogs only (movements, exercises, rotation templates).
User data never goes here: it lives in `DATA_DIR` (`./var/` by default).

- `movements.yaml`: box and hybrid-race movements with a display name per bot language
  and the abbreviations found on whiteboards. Used to normalise parsed WODs; the load
  engine (phase 4) adds muscle groups to each entry. Ids are stable: never rename one,
  add an alias instead. `tests/test_catalog_movements.py` checks ids, aliases (one
  movement per alias), names for every bot language and that no brand is used as a name.
