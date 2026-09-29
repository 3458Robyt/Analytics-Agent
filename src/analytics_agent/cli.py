from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import unquote, urlsplit

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from .agent import AnalyticsAgent
from .bigquery_adapter import BigQueryAdapter, BigQueryError
from .llm import LLMError, OpenAICompatibleProvider
from .models import AgentAnswer, AgentSettings
from .schema import DictionaryError, SchemaCatalog, load_dictionary


DEFAULT_WORKBOOK_URI = (
    "gs://buckets-analytics-staging/proyectos/librerias/diccionario_tablas.xlsx"
)
DEFAULT_SEED_PROJECTS = ("centralizacion-datos", "sbscol-dbreplication-prd")


@dataclass(frozen=True)
class RuntimeConfig:
    settings: AgentSettings
    wire_api: str
    store_responses: bool
    seed_projects: tuple[str, ...]
    workbook_gcs_uri: str
    workbook_path: str
    page_size: int

    @classmethod
    def from_env(cls) -> "RuntimeConfig":
        settings = AgentSettings.from_env()
        wire_api = os.environ.get("LLM_WIRE_API", "responses").strip().lower()
        if wire_api not in {"responses", "chat_completions"}:
            raise ValueError("LLM_WIRE_API debe ser 'responses' o 'chat_completions'")
        store_responses = _as_bool(os.environ.get("LLM_STORE_RESPONSES", "false"))
        seed_value = os.environ.get("BQ_SEED_PROJECTS", ",".join(DEFAULT_SEED_PROJECTS))
        seed_projects = tuple(dict.fromkeys(part.strip() for part in seed_value.split(",") if part.strip()))
        page_size = int(os.environ.get("BQ_PAGE_SIZE", "500"))
        if page_size < 1:
            raise ValueError("BQ_PAGE_SIZE debe ser mayor que cero")
        return cls(
            settings=settings,
            wire_api=wire_api,
            store_responses=store_responses,
            seed_projects=seed_projects,
            workbook_gcs_uri=os.environ.get("SCHEMA_WORKBOOK_GCS_URI", DEFAULT_WORKBOOK_URI).strip(),
            workbook_path=os.environ.get("SCHEMA_WORKBOOK", "").strip(),
            page_size=page_size,
        )


def _as_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "si", "sí", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError("LLM_STORE_RESPONSES debe ser true o false")


def _download_gcs_object(uri: str, destination: Path) -> Path:
    parsed = urlsplit(uri)
    object_name = unquote(parsed.path.lstrip("/"))
    if parsed.scheme != "gs" or not parsed.netloc or not object_name or parsed.query or parsed.fragment:
        raise DictionaryError("La URI del diccionario debe tener el formato gs://bucket/ruta/archivo.xlsx")
    try:
        from google.cloud import storage
    except ImportError as exc:  # pragma: no cover - optional runtime dependency
        raise DictionaryError("Instala google-cloud-storage para leer el diccionario desde GCS") from exc
    try:
        storage.Client().bucket(parsed.netloc).blob(object_name).download_to_filename(str(destination))
    except Exception as exc:
        raise DictionaryError(f"No se pudo descargar el diccionario desde GCS ({type(exc).__name__})") from exc
    return destination


def _load_catalog(config: RuntimeConfig, console: Console) -> SchemaCatalog:
    catalog = SchemaCatalog(tables={})
    try:
        if config.workbook_path:
            workbook = Path(config.workbook_path).expanduser()
        elif config.workbook_gcs_uri:
            workbook = Path(tempfile.gettempdir()) / "analytics_agent_dictionary.xlsx"
            _download_gcs_object(config.workbook_gcs_uri, workbook)
        else:
            return catalog
        catalog = load_dictionary(workbook)
        for warning in catalog.warnings:
            console.print(f"[yellow]Aviso del diccionario:[/yellow] {warning}", highlight=False)
    except Exception as exc:
        console.print(
            "[yellow]No se pudo cargar el diccionario; continuaré con metadatos de BigQuery:[/yellow] "
            f"{type(exc).__name__}",
            highlight=False,
        )
    return catalog


def _build_agent(config: RuntimeConfig, console: Console) -> tuple[AnalyticsAgent, BigQueryAdapter]:
    catalog = _load_catalog(config, console)
    try:
        bigquery = BigQueryAdapter.from_default_credentials(
            project=config.settings.bigquery_project,
            location=config.settings.bigquery_location,
            page_size=config.page_size,
        )
    except Exception as exc:
        raise BigQueryError(
            "No se pudo iniciar el cliente de BigQuery; verifica ADC y la identidad del runtime "
            f"({type(exc).__name__})"
        ) from exc
    llm = OpenAICompatibleProvider(
        base_url=config.settings.llm_base_url,
        model=config.settings.llm_model,
        api_key=config.settings.llm_api_key,
        wire_api=config.wire_api,
        store_responses=config.store_responses,
    )
    agent = AnalyticsAgent(catalog=catalog, llm=llm, bigquery=bigquery, settings=config.settings)
    return agent, bigquery


def _discover(agent: AnalyticsAgent, config: RuntimeConfig, console: Console) -> dict[str, Any]:
    console.print("[dim]Consultando inventario y metadatos accesibles en BigQuery…[/dim]")
    report = agent.discover(config.seed_projects)
    for warning in report["warnings"]:
        console.print(f"[yellow]Aviso de BigQuery:[/yellow] {warning}", highlight=False)
    return report


def _format_cell(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "Sí" if value else "No"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, (float, Decimal)):
        return f"{value:,.2f}"
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def _render_answer(answer: AgentAnswer, console: Console) -> None:
    console.print()
    if answer.answer:
        console.print(Markdown(answer.answer))
    if answer.summary_table and answer.summary_table.columns:
        console.print()
        result_table = Table(title=answer.summary_table.title, show_lines=False, header_style="bold cyan")
        for column in answer.summary_table.columns:
            result_table.add_column(column, overflow="fold")
        for row in answer.summary_table.rows:
            result_table.add_row(*(_format_cell(row.get(column)) for column in answer.summary_table.columns))
        console.print(result_table)
    if answer.assumptions:
        console.print(Panel("\n".join(f"• {item}" for item in answer.assumptions), title="Supuestos", border_style="yellow"))
    stats: list[str] = []
    if answer.query_count:
        stats.append(f"Consultas: {answer.query_count}")
    if answer.tables_used:
        stats.append("Tablas: " + ", ".join(answer.tables_used))
    if answer.bytes_processed:
        stats.append(f"Bytes procesados: {answer.bytes_processed:,}")
    if stats:
        console.print("[dim]" + " · ".join(stats) + "[/dim]", highlight=False)


def _answer_context(answer: AgentAnswer) -> str:
    sections = [answer.answer]
    if answer.summary_table:
        sections.append(
            answer.summary_table.title
            + "\n"
            + json.dumps(
                {"columns": answer.summary_table.columns, "rows": answer.summary_table.rows},
                ensure_ascii=False,
                default=str,
            )
        )
    if answer.assumptions:
        sections.append("Supuestos: " + "; ".join(answer.assumptions))
    return "\n\n".join(section for section in sections if section)


def _run_doctor(config: RuntimeConfig, console: Console) -> int:
    agent, _ = _build_agent(config, console)
    report = _discover(agent, config, console)
    details = Table(title="Diagnóstico", header_style="bold cyan")
    details.add_column("Componente")
    details.add_column("Estado")
    details.add_row("Modelo", f"Configurado · {config.settings.llm_model}")
    details.add_row("Proyecto de ejecución", config.settings.bigquery_project)
    details.add_row("Proyectos revisados", str(len(report["projects"])))
    details.add_row("Datasets visibles", str(report["datasets"]))
    details.add_row("Tablas y vistas visibles", str(report["tables"]))
    details.add_row("Diccionario", "Opcional; los avisos aparecen arriba si no se pudo leer")
    console.print(details)
    if not report["tables"]:
        console.print("[red]No se pudo confirmar acceso al inventario de BigQuery.[/red]")
        return 1
    console.print("[green]Configuración y acceso a metadatos verificados. No se ejecutó SQL ni se llamó al modelo.[/green]")
    return 0


def _run_ask(question: str, config: RuntimeConfig, console: Console, progress: Console) -> int:
    agent, _ = _build_agent(config, console)
    _discover(agent, config, console)
    answer = agent.answer(question, on_progress=lambda message: progress.print(f"[dim]{message}[/dim]"))
    _render_answer(answer, console)
    return 0


def _run_chat(config: RuntimeConfig, console: Console, progress: Console) -> int:
    agent, _ = _build_agent(config, console)
    _discover(agent, config, console)
    history: list[str] = []
    console.print(Panel(
        "Escribe una pregunta. Usa [bold]/clear[/bold] para borrar el contexto o [bold]/exit[/bold] para salir.",
        title="Analytics Agent",
        border_style="cyan",
    ))
    while True:
        try:
            question = input("Tú: ").strip()
        except EOFError:
            console.print()
            return 0
        if question.lower() in {"/exit", "/quit", "salir", "exit", "quit"}:
            return 0
        if question.lower() == "/clear":
            history.clear()
            console.print("[dim]Contexto de conversación borrado.[/dim]")
            continue
        if not question:
            continue
        try:
            answer = agent.answer(
                question,
                context="\n\n".join(history[-6:]),
                on_progress=lambda message: progress.print(f"[dim]{message}[/dim]"),
            )
            _render_answer(answer, console)
            history.append(f"Usuario: {question}\nAgente: {_answer_context(answer)}")
        except (BigQueryError, LLMError, ValueError) as exc:
            console.print(f"[red]No se pudo responder:[/red] {exc}", highlight=False)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="analytics-agent", description="Consulta BigQuery en lenguaje natural.")
    parser.add_argument("--env-file", default=".env", help="Archivo local de configuración (por defecto: .env).")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Revisa configuración y acceso a metadatos; no ejecuta SQL.")
    ask = commands.add_parser("ask", help="Responde una pregunta y ejecuta las consultas necesarias.")
    ask.add_argument("question", nargs="+", help="Pregunta de negocio.")
    commands.add_parser("chat", help="Abre una conversación interactiva en la terminal.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    console = Console()
    progress = Console(stderr=True)
    try:
        try:
            from dotenv import load_dotenv
        except ImportError as exc:  # pragma: no cover - packaged dependency
            raise RuntimeError("Instala python-dotenv para cargar el archivo .env") from exc
        env_file = Path(args.env_file).expanduser()
        if env_file.is_file():
            load_dotenv(env_file, override=False)
        elif args.env_file != ".env":
            raise FileNotFoundError(f"No existe el archivo de configuración: {env_file}")
        config = RuntimeConfig.from_env()
        if args.command == "doctor":
            return _run_doctor(config, console)
        if args.command == "ask":
            return _run_ask(" ".join(args.question).strip(), config, console, progress)
        return _run_chat(config, console, progress)
    except KeyboardInterrupt:
        console.print("\n[dim]Interrumpido.[/dim]")
        return 130
    except (BigQueryError, LLMError, RuntimeError, ValueError, OSError) as exc:
        console.print(f"[red]Error:[/red] {exc}", highlight=False)
        return 1


if __name__ == "__main__":  # pragma: no cover - exercised as the console command
    sys.exit(main())
