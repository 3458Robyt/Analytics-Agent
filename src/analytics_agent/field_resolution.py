"""Conservative name resolution against a verified schema, including SQL scopes."""
from __future__ import annotations

from difflib import SequenceMatcher
from typing import Mapping

from sqlglot import exp, parse_one
from sqlglot.optimizer.scope import traverse_scope

from .schema import SchemaCatalog, TableSchema
from .semantics import MetricDefinitionError, load_metric_definitions


class UnknownFieldError(MetricDefinitionError):
    def __init__(self, table_id: str, field: str, candidates: list[str]):
        self.table_id = table_id
        self.field = field
        super().__init__(
            f"El campo `{field}` no existe en el esquema comprobado de `{table_id}`. "
            + (f"Candidatos reales: {', '.join(candidates)}. " if candidates else "")
            + "No lo des por cierto porque aparezca en la pregunta. Usa describe_tables; si hay varias equivalencias, aclara cuál corresponde."
        )


def resolve_field(field: str, table: TableSchema, aliases: Mapping[str, str] | None = None, *, allow_typo: bool = True) -> str:
    fields = {name.lower(): col.name for name, col in table.columns.items()}
    if field.lower() in fields:
        return fields[field.lower()]
    target = (aliases or {}).get(field.lower())
    if target and target.lower() in fields:
        return fields[target.lower()]
    ranked = sorted(
        ((SequenceMatcher(None, field.lower(), name.lower()).ratio(), name) for name in fields),
        reverse=True,
    )
    if allow_typo and ranked and ranked[0][0] >= 0.88 and (len(ranked) == 1 or ranked[0][0] - ranked[1][0] >= 0.08):
        return fields[ranked[0][1]]
    candidates = [fields[name] for score, name in ranked[:4] if score >= 0.35]
    raise UnknownFieldError(table.table_id, field, candidates)


def resolve_sql_fields(sql: str, catalog: SchemaCatalog, *, allow_typo: bool = True) -> tuple[str, list[str]]:
    expression = parse_one(sql, read="bigquery")
    notices: list[str] = []
    known_aliases: dict[str, dict[str, set[str]]] = {}
    for definition in load_metric_definitions().values():
        table_aliases = known_aliases.setdefault(definition["table_id"].lower(), {})
        for field, config in definition.get("dimensions", {}).items():
            if isinstance(config, Mapping) and config.get("alias"):
                table_aliases.setdefault(config["alias"].lower(), set()).add(field)
    for scope in traverse_scope(expression):
        physical: dict[str, TableSchema] = {}
        for alias, (_, source) in scope.selected_sources.items():
            if isinstance(source, exp.Table):
                table_id = ".".join((source.catalog, source.db, source.name)).lower()
                table = catalog.tables.get(table_id)
                if table is not None and table.metadata_loaded and "*" not in table_id:
                    physical[alias.lower()] = table
        for column in scope.columns:
            if column.is_star:
                continue
            # ORDER/GROUP/HAVING may legally reference a SELECT output alias.
            if column.find_ancestor(exp.Order, exp.Group, exp.Having, exp.Qualify) and column.name in scope.expression.named_selects:
                continue
            if column.table:
                table = physical.get(column.table.lower())
                if table is None:
                    continue  # derived source or nested field: BigQuery resolves it
            elif len(physical) == len(scope.selected_sources) == 1:
                table = next(iter(physical.values()))
            else:
                continue  # joins/CTEs can make an unqualified name genuinely ambiguous
            field = column.name
            # Preserve nested paths, rather than replacing a STRUCT parent by a typo match.
            parts = [part.name for part in column.parts]
            if len(parts) > 2 or (not column.table and "." in field):
                continue
            aliases = {alias: next(iter(targets)) for alias, targets in known_aliases.get(table.table_id.lower(), {}).items()
                       if len(targets) == 1}
            corrected = resolve_field(field, table, aliases, allow_typo=allow_typo)
            if corrected != field:
                column.set("this", exp.to_identifier(corrected, quoted=True))
                notices.append(f"Interpreté `{field}` como el campo comprobado `{corrected}`.")
    return expression.sql(dialect="bigquery"), list(dict.fromkeys(notices))
