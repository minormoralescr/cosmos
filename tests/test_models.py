"""Tests de integridad del esquema (metadata + compilacion DDL PostgreSQL).

Verifica que los modelos:
  - registran todas las tablas esperadas
  - resuelven todas las FK (sin referencias sueltas)
  - persisten enums por VALOR (legibilidad en DB)
  - generan DDL PostgreSQL valido (compila sin DB real)

NO usa sqlite: compila DDL contra el dialecto postgresql directamente.
"""
from __future__ import annotations

from sqlalchemy import create_mock_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.db.base import Base
import app.db.models  # noqa: F401  — registra modelos en Base.metadata
from app.db.models import AppointmentStatus

EXPECTED_TABLES = {
    "agent", "user_account", "client", "service", "staff", "staff_service",
    "availability", "appointment", "conversation", "message", "reminder",
}


def test_all_expected_tables_registered():
    tables = set(Base.metadata.tables.keys())
    missing = EXPECTED_TABLES - tables
    assert not missing, f"Faltan tablas en el metadata: {missing}"


def test_all_foreign_keys_resolve():
    """Toda FK apunta a una tabla existente en el metadata (no referencias rotas)."""
    tables = Base.metadata.tables
    for table in tables.values():
        for fk in table.foreign_keys:
            assert fk.column.table.name in tables, (
                f"FK rota en {table.name}: -> {fk.column.table.name} no existe"
            )


def test_enum_columns_persist_by_value():
    """Los StrEnum se persisten por VALOR (lowercase), no por nombre."""
    agent_type = Base.metadata.tables["agent"].columns["type"].type
    assert "human" in agent_type.enums
    assert "ai" in agent_type.enums
    assert "HUMAN" not in agent_type.enums  # no el nombre del miembro

    appt_status = Base.metadata.tables["appointment"].columns["status"].type
    assert set(appt_status.enums) == {s.value for s in AppointmentStatus}


def test_postgres_ddl_compiles():
    """Compila el DDL de cada tabla contra dialecto PostgreSQL (sin DB real).

    table.create(mock_engine) recorre la generacion completa de DDL (CREATE TABLE,
    CREATE TYPE para enums, constraints, FK, cascadas). Si un modelo tiene un tipo
    invalido o FK rota, lanza ANTES de llegar al executor. Llegar al executor
    prueba que el DDL se genero sin error.
    """
    dialect = postgresql.dialect()
    emitted: list[str] = []

    def executor(sql, *a, **kw):
        if sql is not None:
            # No stringificamos: CreateEnumType falla con str() (bug de SA).
            # Contar la emision es prueba suficiente de compilacion exitosa.
            emitted.append(type(sql).__name__)

    mock_engine = create_mock_engine("postgresql+psycopg2://u:p@h/db", executor=executor)
    for table in Base.metadata.sorted_tables:
        # 1) compilacion directa del CREATE TABLE contra el dialecto postgresql
        CreateTable(table).compile(dialect=dialect)
        # 2) emision completa via mock engine (ejercita enums + constraints + FK)
        table.create(mock_engine, checkfirst=False)

    create_table_count = emitted.count("CreateTable")
    assert create_table_count >= len(EXPECTED_TABLES), (
        f"Falta DDL de tablas: {create_table_count} CREATE TABLEs, "
        f"esperadas >= {len(EXPECTED_TABLES)}"
    )


def test_cascade_delete_on_core_relations():
    """Cancelar/borrar cita o cliente debe propagar (reminders, messages)."""
    tables = Base.metadata.tables
    reminder_fks = tables["reminder"].columns["appointment_id"].foreign_keys
    assert any(fk.ondelete == "CASCADE" for fk in reminder_fks), (
        "reminder.appointment_id debe tener ON DELETE CASCADE"
    )
    appt_fks = tables["appointment"].columns["client_id"].foreign_keys
    assert any(fk.ondelete == "CASCADE" for fk in appt_fks), (
        "appointment.client_id debe tener ON DELETE CASCADE"
    )
