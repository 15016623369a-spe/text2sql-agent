"""Format a retrieved schema and its optional field notes."""
from app import auto_column_notes
from app.schema_linking import SchemaIndex


def render_scope(index: SchemaIndex, selection: dict, query: str, renderer) -> tuple[str, list[str]]:
    tables = selection["tables"]
    schema = "\n\n".join(index.ddl[table] for table in tables)
    if renderer is None:
        return schema, []
    selected = {table: index.columns[table] for table in tables}
    if isinstance(renderer, auto_column_notes.AutoColumnNotes):
        notes = renderer.render(
            selected, query, foreign_keys=index.foreign_keys,
            value_matched_columns=selection["value_matched_columns"],
        )
    else:
        notes = renderer.render(selected, query)
    if notes:
        schema += "\n\nColumn metadata (use the exact identifiers in the DDL):\n" + "\n".join(notes)
    return schema, notes
