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
from .llm import (
    AGENT_SYSTEM_PROMPT_VERSION,
    LLMError,
    OpenAICompatibleProvider,
    load_default_agent_system_prompt,
)
from .memory import SessionStore
from .models import AgentAnswer, AgentSettings
from .schema import DictionaryError, SchemaCatalog, load_dictionary
from .evaluation import evaluate_prompt, load_evaluation_cases


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
    state_dir: str = ""

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
            state_dir=os.environ.get("ANALYTICS_AGENT_STATE_DIR", "").strip(),
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
            "[yellow]No se pudo cargar el diccionario; continuaré con las tablas conocidas y SQL directo:[/yellow] "
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
    if answer.detail_table and answer.detail_table.columns:
        console.print()
        detail_table = Table(title=answer.detail_table.title, show_lines=False, header_style="bold cyan")
        for column in answer.detail_table.columns:
            detail_table.add_column(column, overflow="fold")
        for row in answer.detail_table.rows:
            detail_table.add_row(*(_format_cell(row.get(column)) for column in answer.detail_table.columns))
        console.print(detail_table)
    if answer.assumptions:
        console.print(Panel("\n".join(f"• {item}" for item in answer.assumptions), title="Supuestos", border_style="yellow"))
    if answer.learning_updates:
        console.print(Panel("\n".join(f"• {item}" for item in answer.learning_updates),
                            title="Aprendizaje actualizado", border_style="green"))
    stats: list[str] = []
    if answer.query_count:
        stats.append(f"Consultas: {answer.query_count}")
    if answer.tables_used:
        stats.append("Tablas: " + ", ".join(answer.tables_used))
    if answer.bytes_processed:
        stats.append(f"Bytes procesados: {answer.bytes_processed:,}")
    if answer.session_id:
        stats.append("Sesión: " + answer.session_id)
    if stats:
        console.print("[dim]" + " · ".join(stats) + "[/dim]", highlight=False)
    if answer.query_jobs:
        jobs = [job for job in answer.query_jobs if job.get("job_id")]
        if jobs:
            console.print("[dim]Trabajos BigQuery: " + ", ".join(
                ":".join(part for part in (job.get("project", ""), job["job_id"]) if part)
                + (f" ({job['location']})" if job.get("location") else "")
                for job in jobs
            ) + "[/dim]", highlight=False)


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
    with SessionStore(config.state_dir or None) as store:
        session_id = store.create_session(question)
        answer = agent.answer(
            question,
            session_store=store,
            session_id=session_id,
            on_progress=lambda message: progress.print(f"[dim]{message}[/dim]"),
        )
    _render_answer(answer, console)
    return 0


def _run_chat(config: RuntimeConfig, console: Console, progress: Console, resume_id: str = "") -> int:
    agent, _ = _build_agent(config, console)
    with SessionStore(config.state_dir or None) as store:
        if resume_id:
            if not store.session_exists(resume_id):
                raise ValueError("La sesión indicada no existe en la cuenta actual")
            session_id = resume_id
        else:
            session_id = store.create_session()
        console.print(Panel(
            "Escribe una pregunta. Usa [bold]/new[/bold] o [bold]/clear[/bold] para iniciar otra sesión, "
            "[bold]/sessions[/bold] para listar sesiones o [bold]/exit[/bold] para salir.",
            title=f"Analytics Agent · sesión {session_id}",
            border_style="cyan",
        ))
        while True:
            try:
                question = input("Tú: ").strip()
            except EOFError:
                console.print()
                return 0
            command = question.lower()
            if command in {"/exit", "/quit", "salir", "exit", "quit"}:
                return 0
            if command in {"/clear", "/new"}:
                session_id = store.create_session()
                console.print(f"[dim]Nueva sesión: {session_id}[/dim]")
                continue
            if command == "/sessions":
                _render_sessions(store, console)
                continue
            if not question:
                continue
            try:
                answer = agent.answer(
                    question,
                    session_store=store,
                    session_id=session_id,
                    on_progress=lambda message: progress.print(f"[dim]{message}[/dim]"),
                )
                _render_answer(answer, console)
            except (BigQueryError, LLMError, ValueError) as exc:
                console.print(f"[red]No se pudo responder:[/red] {exc}", highlight=False)


def _render_sessions(store: SessionStore, console: Console) -> None:
    rows = store.list_sessions()
    if not rows:
        console.print("[dim]No hay sesiones guardadas en esta cuenta.[/dim]")
        return
    table = Table(title="Sesiones guardadas", header_style="bold cyan")
    table.add_column("ID", overflow="fold")
    table.add_column("Conversación", overflow="fold")
    table.add_column("Turnos", justify="right")
    table.add_column("Actualizada")
    for row in rows:
        table.add_row(row["session_id"], row["title"], str(row["turns"]), row["updated_at"])
    console.print(table)


def _run_sessions(args: argparse.Namespace, state_dir: str, console: Console) -> int:
    with SessionStore(state_dir or None) as store:
        if args.sessions_action == "list":
            _render_sessions(store, console)
            return 0
        record = store.get_session(args.session_id)
        if record is None:
            console.print("[red]La sesión no existe en la cuenta actual.[/red]")
            return 1
        console.print(Panel(record["session"]["title"], title=f"Sesión {args.session_id}"))
        for turn in record["turns"]:
            console.print(Panel(turn["question"], title="Pregunta", border_style="cyan"))
            if turn["answer"]:
                console.print(Markdown(turn["answer"]))
            plan_items = [item["result"].get("plan") for item in turn["actions"] if item["action"] == "plan"]
            if plan_items:
                console.print(Panel(json.dumps(plan_items[0], ensure_ascii=False, indent=2), title="Plan de análisis"))
            sql_items = [item["sql_text"] for item in turn["actions"] if item["sql_text"]]
            for sql in sql_items:
                console.print(Panel(sql, title="SQL", border_style="yellow"))
            for action in turn["actions"]:
                result = action["result"]
                if action["bq_job_id"]:
                    console.print(f"[dim]Trabajo BigQuery: {action['bq_job_id']}[/dim]")
                aggregate_rows = result.get("aggregate_rows")
                columns = result.get("columns") or []
                if aggregate_rows and columns:
                    evidence_table = Table(title="Evidencia agregada guardada", header_style="bold cyan")
                    for column in columns:
                        evidence_table.add_column(str(column), overflow="fold")
                    for row in aggregate_rows:
                        evidence_table.add_row(*(_format_cell(row.get(column)) for column in columns))
                    console.print(evidence_table)
    return 0


def _run_learning(args: argparse.Namespace, state_dir: str, console: Console) -> int:
    with SessionStore(state_dir or None) as store:
        if args.learning_action == "list":
            rows = store.list_learning(status=args.status or "")
            if not rows:
                console.print("[dim]No hay aprendizajes guardados con ese estado.[/dim]")
                return 0
            table = Table(title="Aprendizajes", header_style="bold cyan")
            table.add_column("ID", overflow="fold")
            table.add_column("Tipo")
            table.add_column("Estado")
            table.add_column("Enseñanza", overflow="fold")
            table.add_column("Evidencia", justify="right")
            table.add_column("Verificada", justify="right")
            for row in rows:
                table.add_row(
                    row["learning_id"],
                    row["kind"],
                    row["status"],
                    row["title"],
                    str(row["evidence_count"]),
                    str(row["verified_evidence_count"]),
                )
            console.print(table)
            return 0
        if args.learning_action == "show":
            record = store.get_learning(args.learning_id)
            if record is None:
                console.print("[red]Ese aprendizaje no existe en la cuenta actual.[/red]")
                return 1
            console.print(Panel(json.dumps(record, ensure_ascii=False, indent=2),
                                title=f"Aprendizaje {args.learning_id}"))
            return 0
        if args.learning_action == "disable":
            changed = store.disable_learning(args.learning_id)
            message = "Aprendizaje desactivado"
        else:
            changed = store.enable_learning(args.learning_id)
            message = "Aprendizaje reactivado; las propuestas de negocio siguen pendientes"
        if not changed:
            console.print("[red]Ese aprendizaje no existe o ya está en ese estado.[/red]")
            return 1
        console.print(f"[green]{message}:[/green] {args.learning_id}")
        return 0


def _run_evaluation(args: argparse.Namespace, config: RuntimeConfig, console: Console) -> int:
    candidate_path = Path(args.candidate_prompt).expanduser()
    candidate_prompt = candidate_path.read_text(encoding="utf-8").strip()
    if not candidate_prompt:
        raise ValueError("El prompt candidato está vacío")
    cases = load_evaluation_cases(args.cases or "")
    if not cases:
        raise ValueError("La batería de evaluación no contiene casos")
    settings = config.settings
    baseline_name, baseline = evaluate_prompt(
        f"{AGENT_SYSTEM_PROMPT_VERSION} actual", load_default_agent_system_prompt(), cases, settings,
        wire_api=config.wire_api, store_responses=config.store_responses,
    )
    candidate_name, candidate = evaluate_prompt(
        "candidato", candidate_prompt, cases, settings,
        wire_api=config.wire_api, store_responses=config.store_responses,
    )
    table = Table(title="Comparación de prompts con BigQuery simulado", header_style="bold cyan")
    table.add_column("Caso")
    table.add_column(baseline_name, justify="center")
    table.add_column(candidate_name, justify="center")
    for base_result, candidate_result in zip(baseline, candidate):
        table.add_row(
            base_result.case_id,
            "[green]OK[/green]" if base_result.passed else "[red]FALLÓ[/red]",
            "[green]OK[/green]" if candidate_result.passed else "[red]FALLÓ[/red]",
        )
    console.print(table)
    for variant, results in ((baseline_name, baseline), (candidate_name, candidate)):
        passed = sum(result.passed for result in results)
        tokens = sum(result.total_tokens for result in results)
        console.print(f"{variant}: {passed}/{len(results)} casos correctos · {tokens:,} tokens")
        for result in results:
            if result.failures:
                console.print(Panel("\n".join(f"• {failure}" for failure in result.failures),
                                    title=f"{variant} · {result.case_id}", border_style="red"))
    baseline_passed = sum(result.passed for result in baseline)
    candidate_passed = sum(result.passed for result in candidate)
    console.print("[dim]La evaluación solo compara resultados; no activa ni reemplaza el prompt vigente.[/dim]")
    return 0 if candidate_passed >= baseline_passed and candidate_passed > 0 else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="analytics-agent", description="Consulta BigQuery en lenguaje natural.")
    parser.add_argument("--env-file", default=".env", help="Archivo local de configuración (por defecto: .env).")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Revisa configuración y acceso a metadatos; no ejecuta SQL.")
    ask = commands.add_parser("ask", help="Responde una pregunta y ejecuta las consultas necesarias.")
    ask.add_argument("question", nargs="+", help="Pregunta de negocio.")
    chat = commands.add_parser("chat", help="Abre una conversación persistente en la terminal.")
    chat.add_argument("--resume", help="Continúa una sesión guardada por su identificador.")
    sessions = commands.add_parser("sessions", help="Lista o muestra sesiones de la cuenta actual.")
    sessions_commands = sessions.add_subparsers(dest="sessions_action", required=True)
    sessions_commands.add_parser("list", help="Lista las sesiones guardadas.")
    show = sessions_commands.add_parser("show", help="Muestra una sesión con SQL y referencias de BigQuery.")
    show.add_argument("session_id")
    learning = commands.add_parser("learn", help="Consulta o desactiva aprendizajes automáticos.")
    learning_commands = learning.add_subparsers(dest="learning_action", required=True)
    learning_list = learning_commands.add_parser("list", help="Lista aprendizajes y propuestas del usuario actual.")
    learning_list.add_argument("--status", choices=("candidate", "active", "proposed", "disabled"), default="",
                               help="Filtra por estado; por defecto muestra todos.")
    learning_show = learning_commands.add_parser("show", help="Muestra el contenido y evidencia de un aprendizaje.")
    learning_show.add_argument("learning_id")
    learning_disable = learning_commands.add_parser("disable", help="Desactiva un aprendizaje activo o candidato.")
    learning_disable.add_argument("learning_id")
    learning_enable = learning_commands.add_parser("enable", help="Restaura un aprendizaje desactivado por el usuario.")
    learning_enable.add_argument("learning_id")
    evaluate = commands.add_parser("evaluate", help="Compara un prompt candidato con BigQuery simulado.")
    evaluate.add_argument("--candidate-prompt", required=True, help="Archivo de texto con el prompt candidato completo.")
    evaluate.add_argument("--cases", default="", help="Archivo JSON opcional de casos de evaluación.")
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
        if args.command == "doctor":
            config = RuntimeConfig.from_env()
            return _run_doctor(config, console)
        if args.command == "ask":
            config = RuntimeConfig.from_env()
            return _run_ask(" ".join(args.question).strip(), config, console, progress)
        if args.command == "sessions":
            return _run_sessions(args, os.environ.get("ANALYTICS_AGENT_STATE_DIR", "").strip(), console)
        if args.command == "learn":
            return _run_learning(args, os.environ.get("ANALYTICS_AGENT_STATE_DIR", "").strip(), console)
        config = RuntimeConfig.from_env()
        if args.command == "evaluate":
            return _run_evaluation(args, config, console)
        return _run_chat(config, console, progress, args.resume or "")
    except KeyboardInterrupt:
        console.print("\n[dim]Interrumpido.[/dim]")
        return 130
    except (BigQueryError, LLMError, RuntimeError, ValueError, OSError) as exc:
        console.print(f"[red]Error:[/red] {exc}", highlight=False)
        return 1


if __name__ == "__main__":  # pragma: no cover - exercised as the console command
    sys.exit(main())
