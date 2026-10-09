from pathlib import Path
from tempfile import TemporaryDirectory

from legacy_csv import (
    EXPECTED_HEADERS,
    LEGACY_ENCODING,
    count_legacy_records,
    iter_legacy_dicts,
    iter_legacy_rows,
    parse_legacy_csv_text,
    validate_legacy_csv,
)


def assert_equal(actual, expected, label):
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def main():
    sample = (
        "id;name;note\r\n"
        '1;"ACME; Italia";"prima riga\r\nseconda riga con ; e euro €"\r\n'
        '2;"Caffè ""Roma""";"virgolette e apostrofo smart ’"\r\n'
    )

    with TemporaryDirectory() as directory:
        path = Path(directory) / "sample.csv"
        path.write_bytes(sample.encode(LEGACY_ENCODING))

        rows = list(iter_legacy_rows(path))
        assert_equal(rows[1][1], "ACME; Italia", "quoted semicolon")
        assert_equal(rows[1][2], "prima riga\r\nseconda riga con ; e euro €", "multiline field")
        assert_equal(rows[2][1], 'Caffè "Roma"', "cp1252 accents and quotes")
        assert_equal(count_legacy_records(path), 2, "record count")

        dicts = list(iter_legacy_dicts(path))
        assert_equal(dicts[0]["note"], "prima riga\r\nseconda riga con ; e euro €", "dict reader")
        assert_equal(parse_legacy_csv_text(sample)[1][2], "prima riga\r\nseconda riga con ; e euro €", "text parser")

    export_dir = Path("legacy/export/sinergia-export")
    if export_dir.exists():
        for name in EXPECTED_HEADERS:
            count = validate_legacy_csv(export_dir / name)
            print(f"{name}: {count} records")

    print("CSV parser validation passed")


if __name__ == "__main__":
    main()
