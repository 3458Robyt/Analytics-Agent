from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from sqlglot import exp, parse
from sqlglot.errors import ParseError

from .models import ProposalError


EXTERNAL_FUNCTIONS = {
    "EXTERNAL_QUERY",
    "AI.GENERATE_TEXT",
    "AI.GENERATE",
    "AI.GENERATE_EMBEDDING",
    "ML.PREDICT",
}


@dataclass(frozen=True)
class ValidatedSql:
    sql: str
    sql_sha256: str
    tables: tuple[str, ...]


def _full_table_id(table: exp.Table) -> str:
    parts = [table.catalog, table.db, table.name]
    if any(not part for part in parts):
        return ""
    return ".".join(str(part).replace(":", ".").lower() for part in parts)


def extract_table_ids(sql: str) -> tuple[str, ...]:
    """Return fully qualified physical table references, excluding CTE names."""
    try:
        statements = [statement for statement in parse(sql, read="bigquery") if statement is not None]
    except ParseError as exc:
        raise ProposalError("BigQuery SQL no se pudo analizar") from exc
    if len(statements) != 1:
        raise ProposalError("Se acepta exactamente una sentencia SQL")
    expression = statements[0]
    cte_names = {cte.alias_or_name.lower() for cte in expression.find_all(exp.CTE)}
    table_ids = set()
    for table in expression.find_all(exp.Table):
        if not table.catalog and not table.db and table.name.lower() in cte_names:
            continue
        table_id = _full_table_id(table)
        if not table_id:
            raise ProposalError(f"La tabla `{table.sql()}` debe tener project.dataset.table completos")
        table_ids.add(table_id)
    return tuple(sorted(table_ids))


def validate_sql(sql: str, catalog=None) -> ValidatedSql:
    """Accept exactly one GoogleSQL SELECT and reject known external execution."""
    sql = sql.strip()
    if not sql:
        raise ProposalError("El modelo no generó SQL")
    try:
        statements = [statement for statement in parse(sql, read="bigquery") if statement is not None]
    except ParseError as exc:
        raise ProposalError("BigQuery SQL no se pudo analizar") from exc
    if len(statements) != 1:
        raise ProposalError("Se acepta exactamente una sentencia SQL")
    expression = statements[0]
    if not isinstance(expression, (exp.Select, exp.Union)):
        raise ProposalError("Solo se permite una consulta SELECT de solo lectura")
    if expression.args.get("into") or expression.args.get("locking"):
        raise ProposalError("La consulta SELECT contiene una operación de escritura o bloqueo")

    for function in expression.find_all(exp.Func):
        name = (getattr(function, "name", "") or function.sql_name() or "").upper()
        rendered = function.sql(dialect="bigquery").upper()
        if name in EXTERNAL_FUNCTIONS or any(token in rendered for token in EXTERNAL_FUNCTIONS):
            raise ProposalError(f"La función externa `{name or rendered}` no se permite en consultas")

    table_ids = extract_table_ids(sql)
    normalized_sql = expression.sql(dialect="bigquery")
    return ValidatedSql(
        sql=normalized_sql,
        sql_sha256=sha256(normalized_sql.encode("utf-8")).hexdigest(),
        tables=table_ids,
    )
