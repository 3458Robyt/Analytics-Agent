from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Mapping


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Falta la variable de entorno {name}")
    return value


def csv_env(name: str) -> frozenset[str]:
    return frozenset(value.strip().lower() for value in os.environ.get(name, "").split(",") if value.strip())


def teams_owner(tenant_id: str, user_id: str) -> str:
    tenant = tenant_id.strip().lower()
    user = user_id.strip().lower()
    if not tenant or not user:
        raise ValueError("La actividad de Teams no incluye tenantId y aadObjectId")
    return f"teams:{tenant}:{user}"


def stable_request_id(tenant_id: str, user_id: str, activity_id: str) -> str:
    source = "\0".join((tenant_id.strip().lower(), user_id.strip().lower(), activity_id.strip()))
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:40]


def value_at(value: Any, *names: str, default: Any = "") -> Any:
    """Read an SDK model field or a plain dictionary, accepting camel and snake case."""
    current = value
    for name in names:
        if current is None:
            return default
        candidates = (name, _camel_to_snake(name), _snake_to_camel(name))
        if name == "from_":
            candidates = (*candidates, "from")
        found = False
        for candidate in candidates:
            if isinstance(current, Mapping) and candidate in current:
                current = current[candidate]
                found = True
                break
            if hasattr(current, candidate):
                current = getattr(current, candidate)
                found = True
                break
        if not found:
            return default
    return current if current is not None else default


def _camel_to_snake(value: str) -> str:
    chars: list[str] = []
    for char in value:
        if char.isupper():
            chars.extend(("_", char.lower()))
        else:
            chars.append(char)
    return "".join(chars).lstrip("_")


def _snake_to_camel(value: str) -> str:
    first, *rest = value.split("_")
    return first + "".join(part[:1].upper() + part[1:] for part in rest)


def json_value(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def answer_record(answer: Any) -> dict[str, Any]:
    """Convert AgentAnswer into a JSON-safe payload without dropping result rows."""
    return json_value({
        "answer": answer.answer,
        "assumptions": answer.assumptions,
        "summary_table": answer.summary_table,
        "detail_table": answer.detail_table,
        "query_count": answer.query_count,
        "tables_used": answer.tables_used,
        "bytes_processed": answer.bytes_processed,
        "session_id": answer.session_id,
        "query_jobs": answer.query_jobs,
        "status": answer.status,
        "clarification": answer.clarification,
        "audit": answer.audit,
        "timings": answer.timings,
        "turn_id": answer.turn_id,
    })


def render_cell(value: Any, *, numeric: bool = False) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "Sí" if value else "No"
    if numeric or isinstance(value, (int, float, Decimal)):
        try:
            rendered = f"{Decimal(str(value)):,.2f}" if Decimal(str(value)).as_tuple().exponent < 0 else f"{Decimal(str(value)):,.0f}"
            return rendered.replace(",", "§").replace(".", ",").replace("§", ".")
        except Exception:
            pass
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, default=str)
    text = str(value).replace("\n", " ").strip()
    return text if len(text) <= 180 else text[:177] + "..."


def result_page(
    record: Mapping[str, Any], page: int, *, page_size: int = 10,
    column_page: int = 0, column_page_size: int = 5, table_kind: str = "summary",
) -> dict[str, Any]:
    if page < 0 or column_page < 0:
        raise ValueError("La página debe ser cero o un número positivo")
    if table_kind not in {"summary", "detail"}:
        raise ValueError("table_kind debe ser summary o detail")
    table = record.get(f"{table_kind}_table")
    if not table:
        table_kind = "detail" if table_kind == "summary" else "summary"
        table = record.get(f"{table_kind}_table")
    table = table or {}
    rows = table.get("rows", []) if isinstance(table, Mapping) else []
    all_columns = table.get("columns", []) if isinstance(table, Mapping) else []
    numeric_columns = set(table.get("numeric_columns", [])) if isinstance(table, Mapping) else set()
    column_start = column_page * column_page_size
    selected_columns = all_columns[column_start:column_start + column_page_size]
    start = page * page_size
    end = min(start + page_size, len(rows))
    return {
        "title": str(table.get("title") or "Resultados"),
        "columns": [str(column) for column in selected_columns],
        "rows": [
            {str(column): render_cell(row.get(column), numeric=str(column) in numeric_columns) for column in selected_columns}
            for row in rows[start:end]
        ],
        "row_count": len(rows),
        "page": page,
        "page_size": page_size,
        "has_next": end < len(rows),
        "shown_from": start + 1 if end else 0,
        "shown_to": end,
        "column_page": column_page,
        "has_next_columns": column_start + column_page_size < len(all_columns),
        "has_previous_columns": column_start > 0,
        "column_start": column_start + 1 if selected_columns else 0,
        "column_end": column_start + len(selected_columns),
        "column_count": len(all_columns),
        "table_kind": table_kind,
    }
