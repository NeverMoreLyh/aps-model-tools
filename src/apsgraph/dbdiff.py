"""db-diff: compare APS metadata model (via ddl-gen) against a live database schema.

Expected schema: reuse ddlgen.generate_all_ddl -> sqlglot parse -> canonical model.
Actual schema: read-only information_schema / data-dictionary views.

Severity rules (table-granular output):
  ERROR:   table missing/extra, column missing/extra, type family mismatch,
           precision/scale shrink, nullable mismatch, default mismatch,
           primary key mismatch, index missing/extra/type or column mismatch.
  WARNING: column order, comment mismatch, precision/scale expansion,
           integer display width, index renamed with same columns,
           case-only identifier difference, identity/auto-increment not verifiable.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, List, Optional, Sequence, Tuple

ERROR = "ERROR"
WARNING = "WARNING"

# Canonical type families.
_F_STRING = "string"
_F_TEXT = "text"
_F_BINARY = "binary"
_F_INT = "int"
_F_BIGINT = "bigint"
_F_DECIMAL = "decimal"
_F_FLOAT = "float"
_F_BOOL = "bool"
_F_DATE = "date"
_F_TIME = "time"
_F_DATETIME = "datetime"
_F_TIMESTAMP = "timestamp"


@dataclass
class ExpectedColumn:
    name: str
    family: str
    dtype: str
    length: Optional[int] = None
    precision: Optional[int] = None
    scale: Optional[int] = None
    nullable: bool = True
    default: Optional[str] = None
    identity: bool = False
    comment: Optional[str] = None


@dataclass
class ExpectedIndex:
    name: str
    unique: bool
    columns: Tuple[str, ...]


@dataclass
class ExpectedTable:
    name: str
    columns: List[ExpectedColumn] = dc_field(default_factory=list)
    primary_key: Tuple[str, ...] = ()
    indexes: List[ExpectedIndex] = dc_field(default_factory=list)
    comment: Optional[str] = None
    temporary: bool = False


@dataclass
class ActualColumn:
    name: str
    family: str
    dtype: str
    length: Optional[int] = None
    precision: Optional[int] = None
    scale: Optional[int] = None
    nullable: bool = True
    default: Optional[str] = None
    identity: bool = False
    comment: Optional[str] = None
    ordinal: int = 0


@dataclass
class ActualIndex:
    name: str
    unique: bool
    columns: Tuple[str, ...]


@dataclass
class ActualTable:
    name: str
    columns: List[ActualColumn] = dc_field(default_factory=list)
    primary_key: Tuple[str, ...] = ()
    indexes: List[ActualIndex] = dc_field(default_factory=list)
    comment: Optional[str] = None
    temporary: bool = False


@dataclass
class DiffIssue:
    severity: str
    kind: str
    object: str
    detail: str


@dataclass
class TableDiff:
    table: str
    status: str  # OK / ERROR / WARNING / MISSING_TABLE / EXTRA_TABLE
    errors: List[DiffIssue] = dc_field(default_factory=list)
    warnings: List[DiffIssue] = dc_field(default_factory=list)


# ---------------------------------------------------------------------------
# Canonical type parsing
# ---------------------------------------------------------------------------

_MYSQL_INT_DEFAULTS = {"tinyint": 4, "smallint": 6, "mediumint": 9, "int": 11, "bigint": 20}


def _parse_args(args_str: str) -> List[str]:
    return [a.strip() for a in args_str.split(",") if a.strip()] if args_str else []


def canonicalize_dtype(raw: str, dialect: str) -> Dict[str, Any]:
    """Normalize a raw type string to {family, length, precision, scale}."""
    t = (raw or "").strip().lower()
    t = re.sub(r"\s+(unsigned|zerofill)$", "", t)
    m = re.match(r"^([a-z0-9_ ]+?)\s*(?:\(([^)]*)\))?$", t)
    if not m:
        return {"family": t, "length": None, "precision": None, "scale": None}
    base, args = m.group(1).strip(), m.group(2)
    parts = _parse_args(args or "")

    def num(i: int) -> Optional[int]:
        try:
            return int(parts[i]) if len(parts) > i else None
        except ValueError:
            return None

    length = num(0)
    precision = scale = None
    if base in ("char", "varchar", "varchar2", "nvarchar", "nvarchar2", "nchar"):
        return {"family": _F_STRING, "length": length, "precision": None, "scale": None}
    if base in ("text", "tinytext", "mediumtext", "longtext", "clob", "nclob"):
        return {"family": _F_TEXT, "length": None, "precision": None, "scale": None}
    if base in ("blob", "tinyblob", "mediumblob", "longblob", "bytea", "raw", "long raw"):
        return {"family": _F_BINARY, "length": None, "precision": None, "scale": None}
    if base == "boolean":
        return {"family": _F_BOOL, "length": None, "precision": None, "scale": None}
    if base == "tinyint":
        if length == 1:
            return {"family": _F_BOOL, "length": 1, "precision": None, "scale": None}
        return {"family": _F_INT, "length": length, "precision": None, "scale": None}
    if base in ("smallint", "mediumint", "int", "integer"):
        return {"family": _F_INT, "length": length, "precision": None, "scale": None}
    if base == "bigint":
        return {"family": _F_BIGINT, "length": length, "precision": None, "scale": None}
    if base == "float":
        return {"family": _F_FLOAT, "length": None, "precision": None, "scale": None}
    if base in ("double", "real"):
        return {"family": _F_FLOAT, "length": None, "precision": None, "scale": None}
    if base in ("decimal", "numeric", "number"):
        if base == "number" and not parts and dialect == "oracle":
            # NUMBER without precision: treat as decimal with generous precision.
            return {"family": _F_DECIMAL, "length": None, "precision": 38, "scale": None}
        return {"family": _F_DECIMAL, "length": None, "precision": num(0), "scale": num(1)}
    if base == "date":
        return {"family": _F_DATE, "length": None, "precision": None, "scale": None}
    if base == "time":
        return {"family": _F_TIME, "length": None, "precision": None, "scale": None}
    if base in ("datetime",):
        return {"family": _F_DATETIME, "length": None, "precision": None, "scale": None}
    if base in ("timestamp", "timestamptz", "timestamp with time zone"):
        return {"family": _F_TIMESTAMP, "length": None, "precision": None, "scale": None}
    return {"family": base, "length": length, "precision": precision, "scale": scale}


def normalize_default(value: Any) -> Optional[str]:
    """Normalize a default value literal for cross-db comparison."""
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            value = value.decode("utf-8", errors="replace")
    if isinstance(value, memoryview):
        value = bytes(value).decode("utf-8", errors="replace")
    s = str(value).strip()
    if not s:
        return None
    # Strip trailing NULL marker some drivers append.
    if s.upper().endswith("::text"):
        s = s[: -len("::text")]
    # Oracle char defaults may be padded with spaces.
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1]
    su = s.upper()
    if su in ("CURRENT_TIMESTAMP", "SYSDATE", "SYSTIMESTAMP", "LOCALTIMESTAMP", "NOW()", "CURRENT_DATE"):
        return "CURRENT_TIMESTAMP"
    if su in ("NULL",):
        return None
    return s


# ---------------------------------------------------------------------------
# Expected schema from ddl-gen output (sqlglot)
# ---------------------------------------------------------------------------

def parse_expected_schema(ddl_sql: str, dialect: str) -> Dict[str, ExpectedTable]:
    import sqlglot
    from sqlglot import exp

    read = {"mysql": "mysql", "oracle": "oracle", "postgresql": "postgres"}[dialect]
    tables: Dict[str, ExpectedTable] = {}
    statements = [s for s in sqlglot.parse(ddl_sql, read=read, error_level=None) if s is not None]

    for stmt in statements:
        if isinstance(stmt, exp.Create):
            kind = (stmt.args.get("kind") or "").upper()
            if kind != "TABLE":
                continue
            schema = stmt.this
            tbl_expr = schema.this if isinstance(schema, exp.Schema) else schema
            name = tbl_expr.name.lower() if tbl_expr else None
            if not name:
                continue
            table = ExpectedTable(name=name)
            props = stmt.args.get("properties")
            temporary = False
            if props:
                for prop in props.expressions:
                    if isinstance(prop, exp.TemporaryProperty):
                        temporary = True
                    elif isinstance(prop, exp.SchemaCommentProperty):
                        val = prop.this
                        table.comment = val.this if isinstance(val, exp.Literal) else str(val)
            table.temporary = temporary
            if isinstance(schema, exp.Schema):
                for expr in schema.expressions:
                    if isinstance(expr, exp.ColumnDef):
                        col = _expected_column_from_def(expr)
                        if col:
                            table.columns.append(col)
                    elif isinstance(expr, exp.PrimaryKey):
                        cols = [c.name.lower() for c in expr.expressions if hasattr(c, "name")]
                        if cols:
                            table.primary_key = tuple(cols)
            tables[name] = table
        elif isinstance(stmt, exp.Alter) and (stmt.args.get("kind") or "").upper() == "TABLE":
            tname = stmt.this.name.lower()
            table = tables.get(tname)
            if table is None:
                continue
            for action in stmt.args.get("actions") or []:
                pk = None
                if isinstance(action, exp.PrimaryKey):
                    pk = action
                elif hasattr(action, "expressions"):
                    # AddConstraint wraps Constraint(name, PrimaryKey)
                    for child in action.expressions if isinstance(action.expressions, list) else []:
                        if isinstance(child, exp.PrimaryKey):
                            pk = child
                        elif hasattr(child, "expressions"):
                            for grand in child.expressions if isinstance(child.expressions, list) else []:
                                if isinstance(grand, exp.PrimaryKey):
                                    pk = grand
                if pk is not None:
                    cols = [c.name.lower() for c in pk.expressions
                            if hasattr(c, "name") and not isinstance(c, exp.IndexParameters)]
                    if cols:
                        table.primary_key = tuple(cols)
        elif isinstance(stmt, exp.Comment):
            kind = (stmt.args.get("kind") or "").upper()
            target = stmt.this
            comment_val = stmt.args.get("expression")
            text = None
            if isinstance(comment_val, exp.Literal):
                text = comment_val.this
            elif comment_val is not None:
                text = str(comment_val)
            if kind == "TABLE":
                obj_name = target.name.lower() if hasattr(target, "name") else None
                if obj_name in tables:
                    tables[obj_name].comment = text
            elif kind == "COLUMN" and isinstance(target, exp.Column):
                col_name = target.this.name.lower() if hasattr(target.this, "name") else None
                tbl_ident = target.args.get("table")
                tbl_name = tbl_ident.name.lower() if tbl_ident is not None and hasattr(tbl_ident, "name") else None
                table = tables.get(tbl_name)
                if table and col_name:
                    for col in table.columns:
                        if col.name == col_name:
                            col.comment = text
    return tables


def _expected_column_from_def(coldef) -> Optional[ExpectedColumn]:
    from sqlglot import exp

    name = coldef.name.lower()
    dtype_node = coldef.args.get("kind")
    raw = dtype_node.sql() if dtype_node is not None else ""
    canon = canonicalize_dtype(raw, "expected")
    col = ExpectedColumn(
        name=name,
        family=canon["family"],
        dtype=raw,
        length=canon["length"],
        precision=canon["precision"],
        scale=canon["scale"],
    )
    # constraints
    for constraint in coldef.args.get("constraints") or []:
        kind = constraint.args.get("kind")
        if isinstance(kind, exp.NotNullColumnConstraint):
            col.nullable = False
        elif isinstance(kind, exp.PrimaryKeyColumnConstraint):
            col.identity = col.identity  # no-op; pk handled at table level
        elif isinstance(kind, exp.GeneratedAsIdentityColumnConstraint):
            col.identity = True
        elif isinstance(kind, exp.AutoIncrementColumnConstraint):
            col.identity = True
        elif isinstance(kind, exp.DefaultColumnConstraint):
            val = kind.this
            if isinstance(val, exp.Literal):
                col.default = val.this if val.is_string else str(val.this)
            elif isinstance(val, exp.Column):
                col.default = val.name
            elif isinstance(val, exp.Null):
                col.default = None
            elif isinstance(val, (exp.CurrentTimestamp, exp.CurrentDate)):
                col.default = "CURRENT_TIMESTAMP"
            else:
                col.default = val.sql()
        elif isinstance(kind, exp.CommentColumnConstraint):
            val = kind.this
            col.comment = val.this if isinstance(val, exp.Literal) else str(val)
    return col


def expected_schema_from_db(conn, dialect: str, config=None) -> Tuple[Dict[str, ExpectedTable], List[str]]:
    """Generate expected DDL via ddlgen and parse it. Returns (tables, ddlgen_errors)."""
    from .ddlgen import DdlGenConfig, generate_all_ddl

    cfg = config or DdlGenConfig(dialect=dialect)
    cfg.dialect = dialect
    report = generate_all_ddl(conn, cfg)
    tables = parse_expected_schema(report.sql, dialect)
    # attach index info from ddlgen report internals is not exposed; re-derive minimal:
    # indexes are emitted as CREATE INDEX statements — capture them here.
    _attach_indexes_from_sql(report.sql, tables, dialect)
    return tables, list(report.errors)


def _attach_indexes_from_sql(sql: str, tables: Dict[str, ExpectedTable], dialect: str) -> None:
    import sqlglot
    from sqlglot import exp

    read = {"mysql": "mysql", "oracle": "oracle", "postgresql": "postgres"}[dialect]
    for stmt in sqlglot.parse(sql, read=read, error_level=None):
        if stmt is None or not isinstance(stmt, exp.Create):
            continue
        if (stmt.args.get("kind") or "").upper() != "INDEX":
            continue
        idx_node = stmt.this
        if idx_node is None or not hasattr(idx_node, "name"):
            continue
        idx_name = idx_node.name.lower()
        tbl = idx_node.args.get("table")
        tname = tbl.name.lower() if tbl is not None and hasattr(tbl, "name") else None
        if not idx_name or not tname or tname not in tables:
            continue
        params = idx_node.args.get("params")
        cols: List[str] = []
        if params is not None:
            for c in params.args.get("columns") or []:
                inner = c.this if hasattr(c, "this") else c
                cname = inner.name.lower() if hasattr(inner, "name") else None
                if cname:
                    cols.append(cname)
        unique = bool(stmt.args.get("unique"))
        tables[tname].indexes.append(ExpectedIndex(name=idx_name, unique=unique, columns=tuple(cols)))


# ---------------------------------------------------------------------------
# Actual schema readers (read-only)
# ---------------------------------------------------------------------------

def _connect_mysql(dsn: str):
    import pymysql

    m = re.match(r"^mysql://([^:/@]+):([^@]*)@([^:/]+):(\d+)/(.+)$", dsn)
    if not m:
        raise ValueError("mysql dsn must be mysql://user:pass@host:port/db")
    user, pwd, host, port, db = m.group(1), m.group(2), m.group(3), int(m.group(4)), m.group(5)
    return pymysql.connect(host, port, user, pwd, db,
                           charset="utf8mb4", connect_timeout=15, read_timeout=120)


def _connect_oracle(dsn: str):
    import oracledb

    m = re.match(r"^oracle://([^:/@]+):([^@]*)@(.+)$", dsn)
    if not m:
        raise ValueError("oracle dsn must be oracle://user:pass@host:port/service")
    return oracledb.connect(m.group(1), m.group(2), m.group(3))


def fetch_actual_mysql(dsn: str, table_filter: Optional[Sequence[str]] = None) -> Dict[str, ActualTable]:
    conn = _connect_mysql(dsn)
    try:
        cur = conn.cursor()
        cur.execute("SELECT DATABASE()")
        row = cur.fetchone()
        if not row:
            raise RuntimeError("cannot determine current database")
        schema = row[0]
        where_t = ""
        params: List[Any] = [schema]
        if table_filter:
            marks = ",".join(["%s"] * len(table_filter))
            where_t = f" AND table_name IN ({marks})"
            params.extend(table_filter)

        tables: Dict[str, ActualTable] = {}

        cur.execute(
            f"SELECT table_name, table_comment FROM information_schema.tables "
            f"WHERE table_schema=%s AND table_type='BASE TABLE'{where_t} ORDER BY table_name", params)
        for tname, tcomment in cur.fetchall():
            key = tname.lower()
            tables[key] = ActualTable(name=tname, comment=tcomment or None)

        cur.execute(
            f"SELECT table_name, column_name, data_type, "
            f"IF(data_type IN ('tinyint','smallint','mediumint','int','bigint'), NULL, character_maximum_length), "
            f"numeric_precision, numeric_scale, is_nullable, column_default, extra, column_comment, ordinal_position "
            f"FROM information_schema.columns WHERE table_schema=%s{where_t} "
            f"ORDER BY table_name, ordinal_position", params)
        for tname, cname, dtype, char_len, num_prec, num_scale, is_null, cdefault, extra, ccomment, ordinal in cur.fetchall():
            key = tname.lower()
            table = tables.get(key)
            if table is None:
                continue
            if dtype.lower() in ("tinyint", "smallint", "mediumint", "int", "bigint"):
                # display width comes back in numeric_precision for integer types in mysql
                canon = canonicalize_dtype(f"{dtype}({num_prec})" if num_prec else dtype, "mysql")
            else:
                raw = f"{dtype}({char_len})" if char_len is not None else (
                    f"{dtype}({num_prec},{num_scale})" if num_prec is not None and num_scale is not None else dtype)
                canon = canonicalize_dtype(raw, "mysql")
            identity = "auto_increment" in (extra or "").lower()
            table.columns.append(ActualColumn(
                name=cname.lower(), family=canon["family"], dtype=dtype,
                length=canon["length"], precision=canon["precision"], scale=canon["scale"],
                nullable=(is_null == "YES"), default=normalize_default(cdefault),
                identity=identity, comment=ccomment or None, ordinal=int(ordinal)))

        cur.execute(
            f"SELECT table_name, index_name, non_unique, column_name, seq_in_index "
            f"FROM information_schema.statistics WHERE table_schema=%s{where_t} "
            f"ORDER BY table_name, index_name, seq_in_index", params)
        idx_cols: Dict[Tuple[str, str], List[str]] = {}
        idx_unique: Dict[Tuple[str, str], bool] = {}
        for tname, iname, non_unique, cname, _seq in cur.fetchall():
            k = (tname.lower(), iname.lower())
            idx_cols.setdefault(k, []).append(cname.lower())
            idx_unique[k] = (int(non_unique) == 0)
        for (tname, iname), cols in idx_cols.items():
            table = tables.get(tname)
            if table is None:
                continue
            if iname == "primary":
                table.primary_key = tuple(cols)
            else:
                table.indexes.append(ActualIndex(name=iname, unique=idx_unique[(tname, iname)], columns=tuple(cols)))
        return tables
    finally:
        conn.close()


def fetch_actual_oracle(dsn: str, table_filter: Optional[Sequence[str]] = None) -> Dict[str, ActualTable]:
    conn = _connect_oracle(dsn)
    try:
        cur = conn.cursor()
        tables: Dict[str, ActualTable] = {}
        where_t = ""
        params: List[Any] = []
        if table_filter:
            marks = ",".join([":" + str(i + 1) for i in range(len(table_filter))])
            where_t = f" AND table_name IN ({marks})"
            params.extend(t.upper() for t in table_filter)

        cur.execute(
            f"SELECT tc.table_name, tc.temporary, c.comments "
            f"FROM user_tables tc LEFT JOIN user_tab_comments c ON c.table_name = tc.table_name"
            f"{where_t.replace('table_name', 'tc.table_name') if where_t else ''} ORDER BY tc.table_name", params)
        for tname, temporary, tcomment in cur.fetchall():
            key = tname.lower()
            tables[key] = ActualTable(name=tname, temporary=(temporary == "Y"),
                                      comment=(tcomment or None) if tcomment else None)

        cur.execute(
            f"SELECT c.table_name, c.column_name, c.data_type, c.char_length, c.data_length, "
            f"c.data_precision, c.data_scale, c.nullable, c.data_default, c.identity_column, "
            f"cc.comments, c.column_id "
            f"FROM user_tab_columns c LEFT JOIN user_col_comments cc "
            f"ON cc.table_name = c.table_name AND cc.column_name = c.column_name"
            f"{where_t.replace('table_name', 'c.table_name') if where_t else ''} "
            f"ORDER BY c.table_name, c.column_id", params)
        for tname, cname, dtype, char_len, data_len, prec, scale, nullable, ddefault, ident, ccomment, col_id in cur.fetchall():
            key = tname.lower()
            table = tables.get(key)
            if table is None:
                continue
            dt = dtype.lower()
            if dt in ("varchar2", "nvarchar2", "char", "nchar"):
                raw = f"{dtype}({char_len})"
            elif dt == "number":
                raw = f"number({prec},{scale})" if prec is not None else ("number" if scale is None else f"number(*,{scale})")
            elif dt == "raw":
                raw = f"raw({data_len})"
            else:
                raw = dtype
            canon = canonicalize_dtype(raw, "oracle")
            table.columns.append(ActualColumn(
                name=cname.lower(), family=canon["family"], dtype=dtype,
                length=canon["length"], precision=canon["precision"], scale=canon["scale"],
                nullable=(nullable == "Y"), default=normalize_default(ddefault),
                identity=(ident == "YES"), comment=ccomment or None, ordinal=int(col_id)))

        cur.execute(
            f"SELECT c.table_name, c.constraint_name, cc.column_name, cc.position "
            f"FROM user_constraints c JOIN user_cons_columns cc "
            f"ON cc.constraint_name = c.constraint_name "
            f"WHERE c.constraint_type='P'{where_t.replace('table_name', 'c.table_name') if where_t else ''} "
            f"ORDER BY c.table_name, c.constraint_name, cc.position", params)
        pk_cols: Dict[str, List[str]] = {}
        for tname, _cname, colname, _pos in cur.fetchall():
            pk_cols.setdefault(tname.lower(), []).append(colname.lower())
        for tname, cols in pk_cols.items():
            if tname in tables:
                tables[tname].primary_key = tuple(cols)

        cur.execute(
            f"SELECT i.table_name, i.index_name, i.uniqueness, ic.column_name, ic.column_position "
            f"FROM user_indexes i JOIN user_ind_columns ic ON ic.index_name = i.index_name"
            f"{where_t.replace('table_name', 'i.table_name') if where_t else ''} "
            f"ORDER BY i.table_name, i.index_name, ic.column_position", params)
        idx_cols: Dict[Tuple[str, str], List[str]] = {}
        idx_unique: Dict[Tuple[str, str], bool] = {}
        for tname, iname, uniq, cname, _pos in cur.fetchall():
            k = (tname.lower(), iname.lower())
            idx_cols.setdefault(k, []).append(cname.lower())
            idx_unique[k] = (uniq == "UNIQUE")
        for (tname, iname), cols in idx_cols.items():
            table = tables.get(tname)
            if table is None:
                continue
            table.indexes.append(ActualIndex(name=iname, unique=idx_unique[(tname, iname)], columns=tuple(cols)))
        return tables
    finally:
        conn.close()


ACTUAL_FETCHERS = {
    "mysql": fetch_actual_mysql,
    "oracle": fetch_actual_oracle,
}


def dump_actual_json(tables: Dict[str, ActualTable], path, dialect: str) -> None:
    from dataclasses import asdict
    from pathlib import Path as _Path

    p = _Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"dialect": dialect, "tables": {name: asdict(t) for name, t in tables.items()}}
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_actual_json(path) -> Tuple[str, Dict[str, ActualTable]]:
    from pathlib import Path as _Path

    payload = json.loads(_Path(path).read_text(encoding="utf-8"))
    dialect = payload.get("dialect", "mysql")
    tables: Dict[str, ActualTable] = {}
    for name, t in (payload.get("tables") or {}).items():
        table = ActualTable(
            name=t["name"],
            primary_key=tuple(t.get("primary_key") or ()),
            comment=t.get("comment"),
            temporary=bool(t.get("temporary")),
        )
        for c in t.get("columns") or []:
            table.columns.append(ActualColumn(
                name=c["name"], family=c["family"], dtype=c["dtype"],
                length=c.get("length"), precision=c.get("precision"), scale=c.get("scale"),
                nullable=bool(c.get("nullable", True)), default=c.get("default"),
                identity=bool(c.get("identity")), comment=c.get("comment"),
                ordinal=int(c.get("ordinal") or 0)))
        for i in t.get("indexes") or []:
            table.indexes.append(ActualIndex(name=i["name"], unique=bool(i["unique"]),
                                             columns=tuple(i.get("columns") or ())))
        tables[name] = table
    return dialect, tables


# ---------------------------------------------------------------------------
# Diff engine
# ---------------------------------------------------------------------------

def _type_compatible(exp_col: ExpectedColumn, act_col: ActualColumn) -> Tuple[str, str]:
    """Return (severity, detail). severity in {OK, ERROR, WARNING}."""
    ef, af = exp_col.family, act_col.family
    if ef == af:
        if ef == _F_STRING:
            el, al = exp_col.length, act_col.length
            if el is not None and al is not None:
                if el == al:
                    return "OK", ""
                if al >= el:
                    return WARNING, f"长度放宽 期望 varchar({el}) 实际 varchar({al})"
                return ERROR, f"长度收窄 期望 varchar({el}) 实际 varchar({al})"
            return "OK", ""
        if ef == _F_DECIMAL:
            ep, es = exp_col.precision, exp_col.scale
            ap, as_ = act_col.precision, act_col.scale
            if ep is not None and ap is not None and (ep, es) != (ap, as_):
                if ap >= ep and (as_ or 0) >= (es or 0):
                    return WARNING, f"精度放宽 期望 decimal({ep},{es or 0}) 实际 decimal({ap},{as_ or 0})"
                return ERROR, f"精度收窄 期望 decimal({ep},{es or 0}) 实际 decimal({ap},{as_ or 0})"
            return "OK", ""
        if ef in (_F_INT, _F_BIGINT):
            el, al = exp_col.length, act_col.length
            if el is not None and al is not None and el != al:
                return WARNING, f"整数显示宽度不同 期望 {exp_col.dtype} 实际 {act_col.dtype}"
            return "OK", ""
        return "OK", ""
    # Cross-family tolerance: datetime/date share storage on some platforms.
    if {ef, af} <= {_F_DATETIME, _F_DATE, _F_TIMESTAMP}:
        return WARNING, f"日期时间族差异 期望 {exp_col.dtype} 实际 {act_col.dtype}"
    if {ef, af} <= {_F_BOOL, _F_STRING}:
        # boolean <-> char(1): common legacy pattern, warn only.
        if (ef == _F_BOOL and af == _F_STRING and act_col.length == 1) or \
           (af == _F_BOOL and ef == _F_STRING and exp_col.length == 1):
            return WARNING, f"布尔/字符互转 期望 {exp_col.dtype} 实际 {act_col.dtype}"
    return ERROR, f"类型不匹配 期望 {exp_col.dtype} 实际 {act_col.dtype}"


def compare_table(table_name: str, exp: ExpectedTable, act: ActualTable) -> TableDiff:
    diff = TableDiff(table=table_name, status="OK")
    exp_cols = {c.name: c for c in exp.columns}
    act_cols = {c.name: c for c in act.columns}

    # Column count summary goes into issues implicitly via missing/extra.
    for name in exp_cols:
        if name not in act_cols:
            diff.errors.append(DiffIssue(ERROR, "column_missing", name,
                                         f"数据库缺少字段（期望类型 {exp_cols[name].dtype}）"))
    for name in act_cols:
        if name not in exp_cols:
            diff.errors.append(DiffIssue(ERROR, "column_extra", name,
                                         f"数据库多出字段（模型未定义，实际类型 {act_cols[name].dtype}）"))

    for name, ec in exp_cols.items():
        ac = act_cols.get(name)
        if ac is None:
            continue
        sev, detail = _type_compatible(ec, ac)
        if sev == ERROR:
            diff.errors.append(DiffIssue(ERROR, "type_mismatch", name, detail))
        elif sev == WARNING:
            diff.warnings.append(DiffIssue(WARNING, "type_variant", name, detail))
        if ec.nullable != ac.nullable:
            detail = "期望 NOT NULL，实际可空" if not ec.nullable else "期望可空，实际 NOT NULL"
            diff.errors.append(DiffIssue(ERROR, "nullable_mismatch", name, detail))
        ed, ad = normalize_default(ec.default), normalize_default(ac.default)
        if ed != ad:
            diff.errors.append(DiffIssue(ERROR, "default_mismatch", name,
                                         f"默认值不一致 期望 {ed!r} 实际 {ad!r}"))
        if (ec.comment or None) is not None and (ec.comment or "") != (ac.comment or ""):
            diff.warnings.append(DiffIssue(WARNING, "comment_mismatch", name,
                                           f"注释不一致 期望 {ec.comment!r} 实际 {ac.comment!r}"))
        if ec.identity and not ac.identity:
            diff.warnings.append(DiffIssue(WARNING, "identity_unverified", name,
                                           "模型定义自增/identity，无法从数据字典确认，请人工核对"))

    # Column order
    exp_order = [c.name for c in exp.columns]
    act_order = [c.name for c in sorted(act.columns, key=lambda c: c.ordinal)]
    common_exp = [c for c in exp_order if c in act_cols]
    common_act = [c for c in act_order if c in exp_cols]
    if common_exp != common_act:
        first_diff = next((i for i, (a, b) in enumerate(zip(common_exp, common_act)) if a != b), len(common_exp) - 1)
        detail = (f"字段顺序不一致（共 {len(common_exp)} 个公共字段），"
                  f"首个差异位置 {first_diff + 1}：期望 {common_exp[first_diff]} 实际 {common_act[first_diff]}")
        diff.warnings.append(DiffIssue(WARNING, "column_order", "(表级)", detail))

    # Primary key
    if exp.primary_key:
        if tuple(exp.primary_key) != tuple(act.primary_key):
            diff.errors.append(DiffIssue(ERROR, "primary_key_mismatch", "(表级)",
                                         f"主键不一致 期望 {list(exp.primary_key)} 实际 {list(act.primary_key)}"))
    if act.primary_key and not exp.primary_key:
        diff.warnings.append(DiffIssue(WARNING, "primary_key_extra", "(表级)",
                                       f"模型未定义主键，数据库主键为 {list(act.primary_key)}"))

    # Indexes
    exp_idx = {i.name: i for i in exp.indexes}
    act_idx = {i.name: i for i in act.indexes}
    act_by_cols = {}
    for ai in act.indexes:
        act_by_cols.setdefault(ai.columns, []).append(ai)
    for name, ei in exp_idx.items():
        ai = act_idx.get(name)
        if ai is None:
            same_cols = act_by_cols.get(tuple(ei.columns))
            if same_cols:
                alt = same_cols[0]
                diff.warnings.append(DiffIssue(WARNING, "index_renamed", name,
                                               f"索引列相同但名称不同，实际索引名 {alt.name}"
                                               + ("，唯一性不同" if alt.unique != ei.unique else "")))
            else:
                diff.errors.append(DiffIssue(ERROR, "index_missing", name,
                                             f"数据库缺少索引，期望列 {list(ei.columns)}"
                                             + ("（唯一索引）" if ei.unique else "")))
            continue
        if ai.unique != ei.unique:
            diff.errors.append(DiffIssue(ERROR, "index_type_mismatch", name,
                                         f"唯一性不一致 期望 {'UNIQUE' if ei.unique else 'INDEX'} "
                                         f"实际 {'UNIQUE' if ai.unique else 'INDEX'}"))
        if tuple(ai.columns) != tuple(ei.columns):
            diff.errors.append(DiffIssue(ERROR, "index_columns_mismatch", name,
                                         f"索引列不一致 期望 {list(ei.columns)} 实际 {list(ai.columns)}"))
    for name, ai in act_idx.items():
        if name not in exp_idx:
            diff.errors.append(DiffIssue(ERROR, "index_extra", name,
                                         f"数据库多出索引，列 {list(ai.columns)}"
                                         + ("（唯一索引）" if ai.unique else "")))

    # Table comment
    if (exp.comment or "") != (act.comment or ""):
        diff.warnings.append(DiffIssue(WARNING, "table_comment_mismatch", "(表级)",
                                       f"表注释不一致 期望 {exp.comment!r} 实际 {act.comment!r}"))

    if diff.errors:
        diff.status = ERROR
    elif diff.warnings:
        diff.status = WARNING
    return diff


def run_diff(expected: Dict[str, ExpectedTable], actual: Dict[str, ActualTable],
             strict_extra_tables: bool = False) -> Dict[str, Any]:
    results: List[TableDiff] = []
    for name in sorted(expected):
        act = actual.get(name)
        if act is None:
            td = TableDiff(table=name, status="MISSING_TABLE")
            td.errors.append(DiffIssue(ERROR, "table_missing", name, "数据库中不存在该表"))
            results.append(td)
            continue
        results.append(compare_table(name, expected[name], act))

    extra_tables = sorted(set(actual) - set(expected))
    for name in extra_tables:
        td = TableDiff(table=name, status="EXTRA_TABLE")
        if strict_extra_tables:
            td.errors.append(DiffIssue(ERROR, "table_extra", name, "数据库存在模型未定义的表"))
        else:
            td.warnings.append(DiffIssue(WARNING, "table_extra", name, "数据库存在模型未定义的表"))
        results.append(td)

    n_err_tables = sum(1 for r in results if r.status == ERROR or r.status == "MISSING_TABLE"
                       or (r.status == "EXTRA_TABLE" and strict_extra_tables))
    n_warn_tables = sum(1 for r in results if r.status in (WARNING,))
    total_errors = sum(len(r.errors) for r in results)
    total_warnings = sum(len(r.warnings) for r in results)
    return {
        "summary": {
            "tables_compared": len(results),
            "tables_ok": sum(1 for r in results if r.status == "OK"),
            "tables_error": n_err_tables,
            "tables_warning": n_warn_tables,
            "total_errors": total_errors,
            "total_warnings": total_warnings,
            "extra_tables": len(extra_tables),
        },
        "tables": results,
    }


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------

def render_markdown(report: Dict[str, Any], meta: Dict[str, Any]) -> str:
    s = report["summary"]
    lines = [
        "# APS 元数据模型 vs 数据库结构 差异报告",
        "",
        f"- 方言: `{meta.get('dialect')}`",
        f"- 数据库: `{meta.get('dsn_redacted')}`",
        f"- 生成时间: {meta.get('generated_at')}",
        f"- 表比对数: {s['tables_compared']}（一致 {s['tables_ok']} / 错误 {s['tables_error']} / 警告 {s['tables_warning']}）",
        f"- 错误项合计: {s['total_errors']}，警告项合计: {s['total_warnings']}",
        "",
        "## 差异明细（按表维度）",
        "",
    ]
    ordered = sorted(report["tables"], key=lambda r: (r.status not in ("MISSING_TABLE", ERROR, "EXTRA_TABLE"), r.table))
    for r in ordered:
        if r.status == "OK":
            continue
        lines.append(f"### 表 `{r.table}` — {r.status}")
        if r.errors:
            lines.append("")
            lines.append("**错误类：**")
            lines.append("")
            for issue in r.errors:
                lines.append(f"- `{issue.object}` [{issue.kind}] {issue.detail}")
        if r.warnings:
            lines.append("")
            lines.append("**警告类：**")
            lines.append("")
            for issue in r.warnings:
                lines.append(f"- `{issue.object}` [{issue.kind}] {issue.detail}")
        lines.append("")
    ok_count = s["tables_ok"]
    if ok_count:
        lines.append(f"其余 {ok_count} 张表结构完全一致，未列出。")
    return "\n".join(lines) + "\n"


def redact_dsn(dsn: str) -> str:
    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", dsn)
