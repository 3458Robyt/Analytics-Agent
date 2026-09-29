"""Read-only analytical assistant for BigQuery."""

from .agent import AnalyticsAgent
from .bigquery_adapter import (
    BigQueryAdapter,
    BigQueryError,
    DiscoveryReport,
    DryRunResult,
    QueryPage,
)
from .llm import LLMError, OpenAICompatibleProvider
from .models import AgentAnswer, AgentSettings, SummaryTable
from .schema import (
    ColumnSchema,
    DictionaryError,
    SchemaCatalog,
    TableSchema,
    load_dictionary,
    merge_bigquery_schema,
)

__all__ = [
    "AgentAnswer",
    "AgentSettings",
    "AnalyticsAgent",
    "BigQueryAdapter",
    "BigQueryError",
    "ColumnSchema",
    "DictionaryError",
    "DiscoveryReport",
    "DryRunResult",
    "LLMError",
    "OpenAICompatibleProvider",
    "QueryPage",
    "SchemaCatalog",
    "SummaryTable",
    "TableSchema",
    "load_dictionary",
    "merge_bigquery_schema",
]
