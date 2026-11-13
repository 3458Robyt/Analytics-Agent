"""Recoverable BigQuery page collection for the durable graph runtime.

Callers must validate the SQL and run the existing date-scope and dry-run
checks before preparing an operation. This module does not make policy decisions.
"""
from __future__ import annotations

from typing import Any

from .bigquery_adapter import bigquery_request_scope
from .execution_store import ExecutionStore


def collect_prepared_query(store: ExecutionStore, adapter: Any, *, run_id: str,
                           token: str, operation_index: int) -> dict[str, Any]:
    """Collect a prepared operation, resuming from its last committed row.

    The job identity is reserved before this function is called. Replaying a
    submission uses BigQuery's deterministic job ID; page writes and offsets
    commit together. The execution lease must remain valid during the call.
    """
    operation = store.query_result(run_id, operation_index)
    if operation is None:
        raise ValueError('La operación no existe o no pertenece al usuario')
    if operation['status'] == 'expired':
        raise ValueError('Los resultados caducaron; esta ejecución no volverá a enviar la consulta')
    if operation['status'] == 'completed':
        return operation
    store.require_lease(run_id, token)
    client_project = str(getattr(getattr(adapter, 'client', None), 'project', '') or '')
    if client_project != operation['project']:
        raise ValueError('El proyecto del adaptador no coincide con la operación preparada')
    offset = operation['rows_saved']
    request_id = f'{run_id}:{operation_index}'
    if offset:
        page = adapter.resume_query(request_id, operation['sql'], project=operation['project'],
                                    location=operation['location'], parameters=operation['parameters'],
                                    start_index=offset, query_id=operation['query_id'])
    else:
        with bigquery_request_scope(request_id):
            page = adapter.execute(operation['sql'], location=operation['location'],
                                   parameters=operation['parameters'])
    metadata = operation['metadata']
    while True:
        if not metadata:
            metadata = {'columns': list(page.columns), 'column_types': dict(page.column_types),
                        'total_rows': page.total_rows, 'bytes_processed': page.bytes_processed,
                        'job_id': page.job_id, 'job_project': page.job_project, 'location': page.location}
        if page.job_id != operation['job_id']:
            raise ValueError('El trabajo devuelto no coincide con la operación preparada')
        if page.job_project != operation['project']:
            raise ValueError('El proyecto del trabajo no coincide con la operación preparada')
        if page.location.lower() != operation['location'].lower():
            raise ValueError('La región del trabajo no coincide con la operación preparada')
        expected = metadata.get('total_rows')
        if not page.has_next_page and expected is not None and offset + len(page.rows) != expected:
            raise ValueError('La cantidad de filas recuperadas no coincide con la informada por BigQuery')
        offset = store.save_page(run_id, token, operation_index=operation_index, start_index=offset,
                                 rows=page.rows, is_last=not page.has_next_page,
                                 metadata=metadata)
        if not page.has_next_page:
            break
        store.require_lease(run_id, token)
        page = adapter.read_page(page.query_id)
    result = store.query_result(run_id, operation_index)
    assert result is not None
    return result
