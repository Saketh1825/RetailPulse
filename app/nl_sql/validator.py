"""VALIDATE the SQL a language model generates, before it ever touches the database.

Design (see docs/NL_SQL_SECURITY.md for the full write-up and rationale):

This is layer 2 of 4 in the NL-to-SQL defense: (1) the prompt asks for one read-only SELECT,
(2) THIS validator independently checks that, (3) the query then runs under a PostgreSQL role
that is granted SELECT on exactly three tables and nothing else, (4) that role's session also has
a statement_timeout. A failure of this layer alone is not fatal, and a failure of any single layer
should not be either -- that is the point of defense in depth.

What this validator is: a lightweight, deliberately conservative lexical/regex check on top of
sqlparse's statement classification. It is NOT a full SQL parser or a formal proof of safety.
Where a construct is ambiguous or hard to analyze cheaply (old-style comma joins, OFFSET, schema-
qualified names outside `public`), it REJECTS rather than tries to handle it -- fail closed.

Checks, in order (first failure wins, exactly one reason per rejection):
  1  sql_too_long           - hard length cap, independent of anything else
  2  empty_query            - nothing to run
  3  multiple_statements     - more than one non-empty SQL statement
  4  not_a_select            - the (only) statement is not a SELECT (covers WITH ... SELECT too,
                                since sqlparse classifies that as SELECT already)
  5  contains_comment        - `--` or `/* */` anywhere (predictable > clever)
  6  select_into             - `SELECT ... INTO` would create a table
  7  dangerous_function      - a blocklisted function/keyword appears anywhere in the text
  8  comma_join_not_supported - a bare comma inside a FROM clause (old-style join); the validator
                                cannot cheaply enumerate every table in that form, so it refuses
                                the whole class rather than risk missing one
  9  non_whitelisted_table   - a FROM/JOIN target (after resolving CTE names) is not one of the
                                three whitelisted tables, or is schema-qualified outside `public`
 10  offset_not_supported     - pagination is out of scope for v1
On success, a LIMIT is enforced: a missing LIMIT is added; an existing one larger than the cap is
reduced. The returned SQL asks for one extra row beyond the cap so the caller can detect truncation
without a second query.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlparse
from sqlparse.tokens import Comment

WHITELISTED_TABLES = frozenset({"items", "sales", "forecasts"})

MAX_SQL_CHARS = 4000

# Substring/keyword blocklist, checked case-insensitively anywhere in the (string-stripped) SQL.
# Not exhaustive by design -- it is one of four layers, not the only one. See docs/NL_SQL_SECURITY.md.
DANGEROUS_TOKENS = (
    "pg_sleep", "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file",
    "pg_read_server_files", "lo_import", "lo_export", "lo_get", "lo_put",
    "dblink", "dblink_connect", "pg_terminate_backend", "pg_cancel_backend",
    "pg_reload_conf", "set_config", "setval", "nextval", "pg_execute_server_program",
    "copy", "xp_cmdshell",
)

_STRING_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")
_COMMENT_TAIL_RE = None  # comments are detected via sqlparse tokens, not regex -- see _has_comment
_FROM_JOIN_RE = re.compile(r"\b(?:FROM|JOIN)\s+([a-zA-Z_][\w.]*)", re.IGNORECASE)
_FROM_KEYWORD_RE = re.compile(r"\bFROM\b", re.IGNORECASE)
_STOP_KEYWORDS_RE = re.compile(
    r"\b(?:WHERE|GROUP\s+BY|ORDER\s+BY|LIMIT|JOIN|HAVING|WINDOW|UNION)\b", re.IGNORECASE)
_CTE_NAME_RE = re.compile(r"\b(\w+)\s+AS\s*\(", re.IGNORECASE)
_INTO_RE = re.compile(r"\bSELECT\b.*?\bINTO\b", re.IGNORECASE | re.DOTALL)
_OFFSET_RE = re.compile(r"\bOFFSET\b", re.IGNORECASE)
_TRAILING_LIMIT_RE = re.compile(r"(?is)\bLIMIT\s+(\d+)\s*;?\s*$")


@dataclass
class ValidationResult:
    ok: bool
    sql: str | None = None            # final, executable SQL (probe LIMIT baked in) if ok
    limit_applied: int | None = None  # the cap actually enforced, for reporting to the caller
    notes: list[str] = field(default_factory=list)
    reject_reason: str | None = None
    reject_detail: str | None = None

    @classmethod
    def reject(cls, reason: str, detail: str) -> "ValidationResult":
        return cls(ok=False, reject_reason=reason, reject_detail=detail)


def _strip_strings(sql: str) -> str:
    """Blank out string-literal contents so keywords/table names inside them can't confuse
    the regex checks below (e.g. WHERE name = 'FROM sales')."""
    return _STRING_LITERAL_RE.sub("''", sql)


def _has_comment(statement: sqlparse.sql.Statement) -> bool:
    return any(tok.ttype in Comment for tok in statement.flatten())


def _cte_names(stripped_sql: str) -> set[str]:
    return {m.group(1).lower() for m in _CTE_NAME_RE.finditer(stripped_sql)}


def _referenced_tables(stripped_sql: str) -> list[str]:
    return [m.group(1).lower() for m in _FROM_JOIN_RE.finditer(stripped_sql)]


def _has_comma_join(stripped_sql: str) -> bool:
    """True if some FROM clause contains a bare, depth-0 comma (an old-style join) before the
    clause ends -- either at a stop keyword or by exiting the parenthesis that contains it
    (e.g. the closing ')' of a CTE definition, which is not a comma-join at all)."""
    n = len(stripped_sql)
    for from_match in _FROM_KEYWORD_RE.finditer(stripped_sql):
        depth = 0
        i = from_match.end()
        while i < n:
            ch = stripped_sql[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth < 0:
                    break  # exited the parenthesis enclosing this FROM; not a comma-join
            elif depth == 0:
                if ch == ",":
                    return True
                if _STOP_KEYWORDS_RE.match(stripped_sql, i):
                    break
            i += 1
    return False


def validate_and_limit(raw_sql: str, max_rows: int) -> ValidationResult:
    """The single entry point. Never raises -- always returns a ValidationResult."""
    try:
        return _validate(raw_sql, max_rows)
    except Exception as exc:  # noqa: BLE001 - the validator itself must never crash the request
        return ValidationResult.reject("unparseable", f"could not be analyzed safely: {exc!r}")


def _validate(raw_sql: str, max_rows: int) -> ValidationResult:
    sql = (raw_sql or "").strip()
    if len(sql) > MAX_SQL_CHARS:
        return ValidationResult.reject("sql_too_long", f"generated SQL exceeds {MAX_SQL_CHARS} characters")
    if not sql:
        return ValidationResult.reject("empty_query", "the model returned no SQL")

    statements = [s for s in sqlparse.parse(sql) if s.token_first(skip_cm=True) is not None]
    if len(statements) != 1:
        return ValidationResult.reject(
            "multiple_statements", f"expected exactly one SQL statement, found {len(statements)}")
    statement = statements[0]

    if statement.get_type() != "SELECT":
        return ValidationResult.reject(
            "not_a_select", f"only SELECT is allowed, got statement type '{statement.get_type()}'")

    if _has_comment(statement):
        return ValidationResult.reject("contains_comment", "SQL comments are not allowed")

    stripped = _strip_strings(sql)

    if _INTO_RE.search(stripped):
        return ValidationResult.reject("select_into", "SELECT ... INTO is not allowed")

    lowered = stripped.lower()
    for bad in DANGEROUS_TOKENS:
        if re.search(rf"\b{re.escape(bad)}\b", lowered):
            return ValidationResult.reject("dangerous_function", f"use of '{bad}' is not allowed")

    if _has_comma_join(stripped):
        return ValidationResult.reject(
            "comma_join_not_supported",
            "old-style comma joins in FROM are not supported; use explicit JOIN")

    allowed = WHITELISTED_TABLES | _cte_names(stripped)
    for table in _referenced_tables(stripped):
        schema, dot, name = table.rpartition(".")
        if dot and schema != "public":
            return ValidationResult.reject("non_whitelisted_table", f"schema '{schema}' is not allowed")
        check_name = name if dot else table
        if check_name not in allowed:
            return ValidationResult.reject("non_whitelisted_table", f"table '{check_name}' is not allowed")

    if _OFFSET_RE.search(stripped):
        return ValidationResult.reject("offset_not_supported", "OFFSET/pagination is not supported")

    return _apply_limit(sql, max_rows)


def _apply_limit(sql: str, max_rows: int) -> ValidationResult:
    notes: list[str] = []
    m = _TRAILING_LIMIT_RE.search(sql)
    if m is None:
        effective_limit = max_rows
        notes.append(f"no LIMIT clause found; capped result to {max_rows} rows")
        final_sql = f"{sql.rstrip().rstrip(';')} LIMIT {max_rows + 1}"
    else:
        requested = int(m.group(1))
        effective_limit = min(requested, max_rows)
        if requested > max_rows:
            notes.append(f"requested LIMIT {requested} exceeds the cap; reduced to {max_rows}")
        final_sql = _TRAILING_LIMIT_RE.sub(f"LIMIT {effective_limit + 1}", sql.rstrip())
    return ValidationResult(ok=True, sql=final_sql, limit_applied=effective_limit, notes=notes)
