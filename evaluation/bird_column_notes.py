"""Database-level notes for the pinned public BIRD Mini-Dev SQLite snapshot."""
from pathlib import Path

from app.auto_column_notes import AutoColumnNotes
from app.column_notes import load_column_notes, render_column_notes

VERSION = "bird-column-notes-1"
MAX_NOTES = 8
MAX_CHARS = 1300

# Encoding corrections come from the public metadata or read-only aggregate checks.
# Keys use database, table, and column identifiers, independent of benchmark questions.
VERIFIED = {
    "california_schools": {
        ("frpm", "CDSCode"): (
            "School CDS identifier stored as digit text, sometimes with leading zeroes. "
            "For a numeric code supplied without padding, compare CAST(CDSCode AS INTEGER). Keep the original key for joins.",
            "cds school code identifier leading zero enrollment county",
        ),
        ("schools", "CDSCode"): (
            "School CDS identifiers can contain leading zeroes. Preserve the text code for joins.",
            "cds school code identifier leading zero",
        ),
        ("schools", "DOCType"): (
            "District ownership or charter category. 'Statewide Benefit Charter' is a DOCType value. FundingType records a separate funding category.",
            "statewide benefit charter authorization category district ownership school",
        ),
        ("frpm", "Percent (%) Eligible Free (Ages 5-17)"): (
            "Stored precomputed free-meal eligibility proportion. The corresponding free-meal count and enrollment columns can be used to compute the ratio from counts.",
            "free meal eligibility proportion fraction percentage enrollment",
        ),
    },
    "debit_card_specializing": {
        ("yearmonth", "Date"): (
            "Month stored as six digits YYYYMM, e.g. 201301. Compare month ranges in this representation.",
            "date year month monthly January November consumption",
        ),
    },
    "financial": {
        ("district", "A2"): (
            "District name as text. A3 is the region.",
            "district name region branch",
        ),
        ("district", "A4"): (
            "Population (number of inhabitants) stored as digit text. Cast to INTEGER for numeric filtering, sorting, arithmetic, or output.",
            "population inhabitants residents populous per capita",
        ),
        ("district", "A11"): (
            "Average salary as a numeric value, separate from the population in A4.",
            "average salary wage income",
        ),
        ("trans", "k_symbol"): (
            "Payment purpose may be NULL, empty, or only spaces. A stated purpose requires TRIM(k_symbol) <> ''. SIPO denotes household bill payments.",
            "payment purpose transaction characterization household sipo blank",
        ),
    },
    "codebase_community": {
        ("posts", "CreaionDate"): (
            "Creation timestamp of the post. Use the spelling CreaionDate exactly as declared.",
            "post creation timestamp first date",
        ),
        ("postLinks", "CreationDate"): (
            "Creation timestamp of the link record. Use it when filtering or grouping the time a post received a link.",
            "link creation timestamp linked receive month date",
        ),
    },
    "formula_1": {
        ("qualifying", "q3"): (
            "Final qualifying time is text. Missing Q3 may be NULL or the empty string. Exclude both before comparing qualifying times.",
            "qualifying q3 final session fastest time blank",
        ),
    },
    "superhero": {
        ("superhero", "weight_kg"): (
            "Weight in kilograms. NULL or 0 represents an unknown weight. Use positive values for averages of recorded weight.",
            "weight kilograms kg",
        ),
        ("superhero", "height_cm"): (
            "Height in centimetres. NULL or 0 represents an unknown height. Use positive values for averages of recorded height.",
            "height tall centimetres centimeters cm",
        ),
    },
    "thrombosis_prediction": {
        ("Laboratory", "U-PRO"): (
            "Urine protein stored as text. Codes include older grades such as '3' and '4', plain numeric amounts, "
            "numeric lower bounds such as '>=300', and values with a parenthesized amount. '-' and 'TR' are negative and trace.",
            "urine urinary protein proteinuria u-pro grade lab scale mg dl",
        ),
        ("Laboratory", "DNA"): (
            "Anti-DNA readings are recorded in DNA. The readings are text, so cast to a numeric type when comparing levels.",
            "anti DNA antibody antibody level result highest reading",
        ),
        ("Laboratory", "DNA-II"): (
            "Separate anti-DNA field; it has no non-NULL observations in this snapshot. Recorded anti-DNA levels are in DNA.",
            "anti DNA antibody antibody level result highest reading",
        ),
        ("Patient", "SEX"): (
            "Sex code. An unrecorded value may be NULL or an empty string.",
            "sex missing unrecorded patient registry blank",
        ),
    },
    "card_games": {
        ("legalities", "status"): (
            "Legality values are case-sensitive literals 'Legal', 'Banned', and 'Restricted' in the stored data.",
            "legal legality status banned restricted format",
        ),
    },
}


def _load_metadata(db_path: Path):
    return {
        path.stem.casefold(): load_column_notes(path)
        for path in (db_path.parent / "database_description").glob("*.csv")
    }


class AutoBirdColumnNotes(AutoColumnNotes):
    def __init__(self, db_path: Path, *, value_hints: bool = False):
        super().__init__(db_path, _load_metadata(db_path), value_hints=value_hints)


class BirdColumnNotes:
    def __init__(self, db_path: Path):
        self._tables = _load_metadata(db_path)
        self._verified = VERIFIED.get(db_path.stem, {})

    def render(self, selected_columns: dict[str, set[str]], query: str) -> list[str]:
        notes = {table: self._tables.get(table.casefold(), {}) for table in selected_columns}
        return render_column_notes(
            selected_columns, notes, query, self._verified,
            max_notes=MAX_NOTES, max_chars=MAX_CHARS,
        )
