"""Restore pre-generated AI-derived artifacts from a local snapshot."""

from __future__ import annotations

import csv
import io
import json
import tarfile
import time
import uuid
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text

from ..db import get_session_factory
from ..models import (
    Anomaly,
    Conversation,
    ConversationAnalysis,
    ConversationIssue,
    IssueCatalogEntry,
    KnowledgeDocumentRecord,
    PipelineRun,
)
from ..repositories import PipelineRunRepository
from .progress import ProgressCallback, ProgressReporter

IMPORT_DERIVED_STAGE_KEY = "import_derived"
IMPORT_DERIVED_STAGE_LABEL = "Import Pre-generated AI Dataset"
SNAPSHOT_FORMAT = "cxintel-derived-v1"

_TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "conversation_analyses": (
        "conversation_id",
        "model",
        "model_version",
        "prompt_version",
        "processed_at",
        "analysis_json",
    ),
    "conversation_issues": (
        "id",
        "conversation_id",
        "canonical_name",
        "customer_description",
        "severity",
        "confidence",
        "customer_impact",
        "product",
        "symptoms",
        "catalog_matched",
        "catalog_confidence",
        "resolution_status",
        "resolution_summary",
        "created_at",
    ),
    "issue_catalog": (
        "canonical_name",
        "description",
        "first_seen_day",
        "example_count",
        "representative_examples",
        "created_at",
    ),
    "anomalies": (
        "id",
        "day",
        "observation_date",
        "baseline_date",
        "issue",
        "severity",
        "delta",
        "description",
        "slack_message",
        "signals",
        "metrics",
        "recommended_action",
        "created_at",
    ),
    "knowledge_documents": (
        "id",
        "conversation_id",
        "issue",
        "product",
        "document",
        "knowledge_text",
        "embedding",
        "embedding_model",
        "created_at",
    ),
}

_COPY_ORDER = (
    "conversation_analyses",
    "conversation_issues",
    "issue_catalog",
    "anomalies",
    "knowledge_documents",
)

_EXPORT_SQL: dict[str, str] = {
    "conversation_analyses": """
        COPY (
            SELECT
                conversation_id,
                model,
                model_version,
                prompt_version,
                processed_at,
                analysis_json::text AS analysis_json
            FROM conversation_analyses
            ORDER BY conversation_id
        ) TO STDOUT WITH (FORMAT csv, HEADER true)
    """,
    "conversation_issues": """
        COPY (
            SELECT
                id,
                conversation_id,
                canonical_name,
                customer_description,
                severity,
                confidence,
                customer_impact,
                product,
                symptoms::text AS symptoms,
                catalog_matched,
                catalog_confidence,
                resolution_status,
                resolution_summary,
                created_at
            FROM conversation_issues
            ORDER BY conversation_id, canonical_name, id
        ) TO STDOUT WITH (FORMAT csv, HEADER true)
    """,
    "issue_catalog": """
        COPY (
            SELECT
                canonical_name,
                description,
                first_seen_day,
                example_count,
                representative_examples::text AS representative_examples,
                created_at
            FROM issue_catalog
            ORDER BY canonical_name
        ) TO STDOUT WITH (FORMAT csv, HEADER true)
    """,
    "anomalies": """
        COPY (
            SELECT
                id,
                day,
                observation_date,
                baseline_date,
                issue,
                severity,
                delta,
                description,
                slack_message,
                signals::text AS signals,
                metrics::text AS metrics,
                recommended_action,
                created_at
            FROM anomalies
            ORDER BY day, issue, id
        ) TO STDOUT WITH (FORMAT csv, HEADER true)
    """,
    "knowledge_documents": """
        COPY (
            SELECT
                id,
                conversation_id,
                issue,
                product,
                document::text AS document,
                knowledge_text,
                embedding::text AS embedding,
                embedding_model,
                created_at
            FROM knowledge_documents
            ORDER BY conversation_id, issue, id
        ) TO STDOUT WITH (FORMAT csv, HEADER true)
    """,
}

_TABLE_MODELS = {
    "conversation_analyses": ConversationAnalysis,
    "conversation_issues": ConversationIssue,
    "issue_catalog": IssueCatalogEntry,
    "anomalies": Anomaly,
    "knowledge_documents": KnowledgeDocumentRecord,
}

_FK_TABLES = ("conversation_analyses", "conversation_issues", "knowledge_documents")

_TRUNCATE_DERIVED_SQL = text(
    """
    TRUNCATE TABLE
        conversation_issues,
        conversation_analyses,
        issue_catalog,
        anomalies,
        knowledge_documents
    RESTART IDENTITY CASCADE
    """
)


class DerivedImportError(Exception):
    """Raised when a derived snapshot cannot be safely restored."""


@dataclass(frozen=True)
class SnapshotTable:
    """Validated snapshot table payload."""

    table: str
    path: str
    rows: int
    csv_text: str


def _record_finish(
    run_id: uuid.UUID,
    trigger: str,
    started_at: datetime,
    duration_seconds: float,
    *,
    summary: str | None = None,
    error: str | None = None,
) -> None:
    with get_session_factory()() as session:
        repo = PipelineRunRepository(session)
        run = repo.get(run_id)
        if run is None:
            run = PipelineRun(
                id=run_id,
                stage_key=IMPORT_DERIVED_STAGE_KEY,
                status="running",
                trigger=trigger,
                started_at=started_at,
            )
            repo.add(run)
        run.status = "succeeded" if error is None else "failed"
        run.finished_at = datetime.now(tz=UTC)
        run.duration_seconds = duration_seconds
        run.summary = summary
        run.error = error
        session.commit()


def _table_path(table: str, spec: object) -> str:
    if isinstance(spec, dict):
        value = spec.get("path")
        if isinstance(value, str) and value:
            return value
    return f"tables/{table}.csv"


def _table_expected_rows(table: str, spec: object) -> int | None:
    value: object = spec.get("rows") if isinstance(spec, dict) else spec
    if value is None:
        return None
    if not isinstance(value, int) or value < 0:
        raise DerivedImportError(f"manifest row count for {table} must be a non-negative integer")
    return value


def _read_manifest(snapshot: zipfile.ZipFile) -> dict[str, Any]:
    try:
        raw = snapshot.read("manifest.json")
    except KeyError as exc:
        raise DerivedImportError("snapshot is missing manifest.json") from exc
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DerivedImportError("manifest.json is not valid UTF-8 JSON") from exc
    if not isinstance(manifest, dict):
        raise DerivedImportError("manifest.json must contain a JSON object")
    if manifest.get("format") != SNAPSHOT_FORMAT:
        raise DerivedImportError(
            f"manifest format must be {SNAPSHOT_FORMAT!r}, got {manifest.get('format')!r}"
        )
    tables = manifest.get("tables")
    if not isinstance(tables, dict):
        raise DerivedImportError("manifest.json must contain a tables object")
    unknown = sorted(set(tables) - set(_TABLE_COLUMNS))
    if unknown:
        raise DerivedImportError(f"manifest contains unsupported table(s): {', '.join(unknown)}")
    missing = [table for table in _COPY_ORDER if table not in tables]
    if missing:
        raise DerivedImportError(f"manifest is missing table(s): {', '.join(missing)}")
    return manifest


def _validate_csv(table: str, csv_text: str, expected_rows: int | None) -> int:
    reader = csv.reader(io.StringIO(csv_text))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise DerivedImportError(f"{table} CSV is empty") from exc
    expected = list(_TABLE_COLUMNS[table])
    if header != expected:
        raise DerivedImportError(f"{table} CSV header mismatch; expected {expected}, got {header}")
    rows = sum(1 for _ in reader)
    if expected_rows is not None and rows != expected_rows:
        raise DerivedImportError(
            f"{table} row count mismatch; manifest says {expected_rows}, CSV has {rows}"
        )
    return rows


def _load_snapshot_zip(snapshot: zipfile.ZipFile) -> list[SnapshotTable]:
    manifest = _read_manifest(snapshot)
    manifest_tables = manifest["tables"]
    tables: list[SnapshotTable] = []
    for table in _COPY_ORDER:
        spec = manifest_tables[table]
        csv_path = _table_path(table, spec)
        try:
            csv_text = snapshot.read(csv_path).decode("utf-8-sig")
        except KeyError as exc:
            raise DerivedImportError(f"snapshot is missing {csv_path}") from exc
        except UnicodeDecodeError as exc:
            raise DerivedImportError(f"{csv_path} is not valid UTF-8") from exc
        rows = _validate_csv(table, csv_text, _table_expected_rows(table, spec))
        tables.append(SnapshotTable(table=table, path=csv_path, rows=rows, csv_text=csv_text))
    return tables


def _load_snapshot_from_tarball(path: Path) -> list[SnapshotTable]:
    try:
        with tarfile.open(path, "r:gz") as bundle:
            member = next(
                (
                    item
                    for item in bundle.getmembers()
                    if item.isfile() and Path(item.name).name == "derived-ai-dataset.zip"
                ),
                None,
            )
            if member is None:
                raise DerivedImportError(
                    "data artifacts bundle does not contain derived-ai-dataset.zip; "
                    "run 'make data-artifacts' to refresh it"
                )
            extracted = bundle.extractfile(member)
            if extracted is None:
                raise DerivedImportError("could not read derived-ai-dataset.zip from bundle")
            with zipfile.ZipFile(io.BytesIO(extracted.read())) as snapshot:
                return _load_snapshot_zip(snapshot)
    except tarfile.TarError as exc:
        raise DerivedImportError(f"derived dataset is not a valid tar.gz file: {path}") from exc
    except zipfile.BadZipFile as exc:
        raise DerivedImportError("embedded derived-ai-dataset.zip is invalid") from exc


def _load_snapshot(path: Path) -> list[SnapshotTable]:
    if not path.exists():
        raise DerivedImportError(f"derived dataset not found at {path}")
    if not path.is_file():
        raise DerivedImportError(f"derived dataset path is not a file: {path}")

    if path.suffix == ".tgz" or path.name.endswith(".tar.gz"):
        return _load_snapshot_from_tarball(path)

    try:
        with zipfile.ZipFile(path) as snapshot:
            return _load_snapshot_zip(snapshot)
    except zipfile.BadZipFile as exc:
        raise DerivedImportError(f"derived dataset is not a valid zip file: {path}") from exc


def _conversation_ids(table: SnapshotTable) -> set[uuid.UUID]:
    if table.table not in _FK_TABLES:
        return set()
    reader = csv.DictReader(io.StringIO(table.csv_text))
    ids: set[uuid.UUID] = set()
    for line_number, row in enumerate(reader, start=2):
        value = row.get("conversation_id")
        if not value:
            raise DerivedImportError(f"{table.table}:{line_number} has an empty conversation_id")
        try:
            ids.add(uuid.UUID(value))
        except ValueError as exc:
            raise DerivedImportError(
                f"{table.table}:{line_number} has invalid conversation_id {value!r}"
            ) from exc
    return ids


def _chunks(values: Sequence[uuid.UUID], size: int = 1000) -> list[Sequence[uuid.UUID]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _validate_conversation_ids(tables: Sequence[SnapshotTable]) -> None:
    ids = list(set().union(*(_conversation_ids(table) for table in tables)))
    if not ids:
        return
    with get_session_factory()() as session:
        found: set[uuid.UUID] = set()
        for chunk in _chunks(ids):
            found.update(
                session.execute(select(Conversation.id).where(Conversation.id.in_(chunk))).scalars()
            )
    missing = [str(value) for value in ids if value not in found]
    if missing:
        preview = ", ".join(missing[:5])
        suffix = "" if len(missing) <= 5 else f" (+{len(missing) - 5} more)"
        raise DerivedImportError(
            f"snapshot references conversation_id values that are not imported: {preview}{suffix}"
        )


def _copy_table(cursor: Any, table: SnapshotTable) -> None:
    columns = ", ".join(_TABLE_COLUMNS[table.table])
    sql = f"COPY {table.table} ({columns}) FROM STDIN WITH (FORMAT csv, HEADER true)"
    with cursor.copy(sql) as copy:
        copy.write(table.csv_text)


def _count_rows(session: Any, table: str) -> int:
    model = _TABLE_MODELS[table]
    return int(session.execute(select(func.count()).select_from(model)).scalar_one())


def _copy_to_csv(cursor: Any, table: str) -> str:
    chunks: list[str] = []
    with cursor.copy(_EXPORT_SQL[table]) as copy:
        while data := copy.read():
            if isinstance(data, str):
                chunks.append(data)
            else:
                chunks.append(bytes(data).decode("utf-8"))
    return "".join(chunks)


def export_derived_data(path: Path) -> str:
    """Write a derived-artifact snapshot zip from the current database."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tables: dict[str, str] = {}
    manifest_tables: dict[str, dict[str, object]] = {}
    with get_session_factory()() as session:
        raw_connection = session.connection().connection.driver_connection
        assert raw_connection is not None
        with raw_connection.cursor() as cursor:
            for table in _COPY_ORDER:
                csv_text = _copy_to_csv(cursor, table)
                rows = _validate_csv(table, csv_text, expected_rows=None)
                table_path = f"tables/{table}.csv"
                tables[table_path] = csv_text
                manifest_tables[table] = {"path": table_path, "rows": rows}

    manifest = {
        "format": SNAPSHOT_FORMAT,
        "created_at": datetime.now(tz=UTC).isoformat(),
        "tables": manifest_tables,
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as snapshot:
        snapshot.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
        for table_path, csv_text in tables.items():
            snapshot.writestr(table_path, csv_text)
    return f"Exported derived AI dataset snapshot to {path}."


def _restore_snapshot(
    tables: Sequence[SnapshotTable], reporter: ProgressReporter
) -> dict[str, int]:
    counts: dict[str, int] = {}
    with get_session_factory()() as session:
        reporter.report(message="Replacing derived AI artifacts…")
        session.execute(_TRUNCATE_DERIVED_SQL)

        raw_connection = session.connection().connection.driver_connection
        assert raw_connection is not None
        with raw_connection.cursor() as cursor:
            for table in tables:
                reporter.report(message=f"Importing {table.table}…", current_item=table.path)
                _copy_table(cursor, table)
                reporter.advance(current_item=table.path, message=f"Imported {table.table}.")

        reporter.report(message="Verifying imported row counts…")
        for table_name in _COPY_ORDER:
            count = _count_rows(session, table_name)
            expected = next(
                snapshot_table.rows
                for snapshot_table in tables
                if snapshot_table.table == table_name
            )
            if count != expected:
                raise DerivedImportError(
                    f"{table_name} row count mismatch after import; "
                    f"expected {expected}, got {count}"
                )
            counts[table_name] = count
        session.commit()
    return counts


def import_derived_data(
    path: Path,
    *,
    progress: ProgressCallback | None = None,
    trigger: str = "api",
) -> str:
    """Restore a pre-generated derived AI dataset and record an audit row."""
    from alembic import command
    from alembic.config import Config

    progress_callback = progress or (lambda _update: None)
    reporter = ProgressReporter(
        stage_key=IMPORT_DERIVED_STAGE_KEY,
        stage_label=IMPORT_DERIVED_STAGE_LABEL,
        progress=progress_callback,
        total_work=len(_COPY_ORDER) + 3,
        message="Preparing derived dataset import…",
    )

    reporter.report(message="Applying database migrations…")
    command.upgrade(Config("alembic.ini"), "head")
    reporter.advance(message="Migrations applied.")

    run_id = uuid.uuid4()
    started_at = datetime.now(tz=UTC)
    started = time.monotonic()

    with get_session_factory()() as session:
        PipelineRunRepository(session).add(
            PipelineRun(
                id=run_id,
                stage_key=IMPORT_DERIVED_STAGE_KEY,
                status="running",
                trigger=trigger,
                started_at=started_at,
            )
        )
        session.commit()

    try:
        reporter.report(message=f"Validating derived dataset at {path}…", current_item=str(path))
        tables = _load_snapshot(path)
        _validate_conversation_ids(tables)
        reporter.advance(current_item=str(path), message="Snapshot validated.")

        counts = _restore_snapshot(tables, reporter)
        reporter.advance(message="Import verified.")
        summary = (
            "Imported pre-generated AI dataset: "
            f"{counts['conversation_analyses']} analyses, "
            f"{counts['conversation_issues']} issues, "
            f"{counts['issue_catalog']} catalog entries, "
            f"{counts['anomalies']} anomalies, "
            f"{counts['knowledge_documents']} knowledge documents."
        )
    except Exception as exc:
        _record_finish(
            run_id,
            trigger,
            started_at,
            time.monotonic() - started,
            error=str(exc),
        )
        raise

    _record_finish(
        run_id,
        trigger,
        started_at,
        time.monotonic() - started,
        summary=summary,
    )
    return summary
