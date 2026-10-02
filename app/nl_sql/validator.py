"""VALIDATE the SQL a language model generates, before it ever touches the database.

Design (see docs/NL_SQL_SECURITY.md for the full write-up, including the red-team audit that
shaped this version):

This is layer 2 of 4 in the NL-to-SQL defense: (1) the prompt asks for one read-only SELECT,
(2) THIS validator independently checks that, (3) the query then runs under a PostgreSQL role
that is granted SELECT on exactly three tables and nothing else, (4) that role's session also has
a statement_timeout. A failure of this layer alone is not fatal -- that is the point of defense in
depth -- but this layer is still written to DENY BY DEFAULT: anything it cannot positively classify
as a plain analytic SELECT is rejected.

What this validator is: a conservative lexical checker on top of sqlparse's statement count. It is
NOT a full SQL grammar or a formal proof of safety, and it is not claimed to be. Its main defense
against its own limits is refusing to guess:

  * ONE tokenizer, consistent with PostgreSQL. String literals and quoted identifiers are replaced by
    placeholders before any keyword/table/function analysis, so text inside a string can never be
    mistaken for code (or vice versa). Syntax whose tokenization differs between this checker and
    PostgreSQL -- backslash escapes (E'..\\'..'), dollar quoting, non-ASCII outside quotes, control
    characters, unbalanced quotes -- is rejected outright rather than interpreted.
  * Function calls are an ALLOWLIST (aggregates, window, math, string, date functions). Anything
    else -- pg_*, query_to_xml, to_regclass, current_setting, version, ... -- is rejected, so new
    dangerous functions need no blocklist update.
  * Every FROM/JOIN target must be a plain (optionally quoted, optionally public-qualified) table
    name on the whitelist or a CTE defined in the query's leading WITH; parenthesised joins and
    anything else it cannot classify is rejected.
  * Statement keywords that can write or change state are rejected ANYWHERE in the query, not just
    at the start -- including inside CTEs (WITH d AS (DELETE ...) SELECT ...).

Checks, in order (first failure wins, exactly one reason per rejection):
  sql_too_long, empty_query          - size / emptiness
  unsupported_syntax, unbalanced_quotes - tokenization must be unambiguous (see above)
  multiple_statements                - more than one statement / stray ';'
  not_a_select                       - not a SELECT / WITH ... SELECT
  contains_comment                   - `--` or `/* */` outside string literals
  select_into                        - SELECT ... INTO would create a table
  dangerous_function                 - specific known-bad names get a specific message
  comma_join_not_supported           - old-style FROM a, b (cannot cheaply enumerate its tables)
  non_whitelisted_table / unrecognized_from_clause - FROM/JOIN target rules above
  forbidden_keyword                  - write/DDL/session keywords, current_user & friends, anywhere
  function_not_allowed               - any function call not on the allowlist
  offset_not_supported               - pagination is out of scope for v1
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

# Known-bad names get a SPECIFIC rejection message. This is no longer the security boundary (the
# function allowlist below is); it exists so the common attacks produce a clear, exact reason.
DANGEROUS_TOKENS = (
    "pg_sleep", "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file",
    "pg_read_server_files", "lo_import", "lo_export", "lo_get", "lo_put",
    "dblink", "dblink_connect", "pg_terminate_backend", "pg_cancel_backend",
    "pg_reload_conf", "set_config", "setval", "nextval", "pg_execute_server_program",
    "copy", "xp_cmdshell",
)

# Words that must not appear ANYWHERE in the (string-stripped) query.
FORBIDDEN_WORDS = (
    "insert", "update", "delete", "merge", "drop", "alter", "create", "truncate", "grant", "revoke",
    "call", "do", "execute", "prepare", "deallocate", "listen", "notify", "unlisten", "vacuum",
    "reindex", "cluster", "refresh", "lock", "set", "reset", "show", "explain", "analyze", "analyse",
    "discard", "load", "import", "table", "returning", "fetch",
    "current_user", "session_user", "user", "current_role", "current_catalog", "current_schema",
    "pg_catalog", "information_schema",
)
_FORBIDDEN_RE = re.compile(
    r"\b(?:" + "|".join(FORBIDDEN_WORDS) + r")\b|\bfor\s+(?:no\s+key\s+update|key\s+share|share)\b")

# Deny-by-default function list: pure analytic functions, none with side effects or catalog access.
ALLOWED_FUNCTIONS = frozenset({
    # aggregates
    "count", "sum", "avg", "min", "max", "stddev", "stddev_pop", "stddev_samp", "variance", "var_pop",
    "var_samp", "string_agg", "array_agg", "bool_and", "bool_or", "corr", "covar_pop", "covar_samp",
    "percentile_cont", "percentile_disc", "mode",
    # window
    "row_number", "rank", "dense_rank", "percent_rank", "cume_dist", "ntile", "lag", "lead",
    "first_value", "last_value", "nth_value",
    # math
    "abs", "ceil", "ceiling", "floor", "round", "trunc", "sqrt", "power", "exp", "ln", "log", "mod",
    "sign", "div", "greatest", "least",
    # string
    "lower", "upper", "initcap", "length", "char_length", "trim", "ltrim", "rtrim", "btrim",
    "substring", "substr", "left", "right", "replace", "concat", "concat_ws", "position", "strpos",
    "split_part", "lpad", "rpad", "reverse", "repeat", "overlay", "to_char",
    # date / time
    "date_trunc", "date_part", "extract", "age", "to_date", "make_date", "now", "date",
    # conditional / cast
    "coalesce", "nullif", "cast",
    # type names that take a (precision) argument, e.g. ::numeric(10,2)
    "numeric", "decimal", "varchar", "char", "character", "timestamp", "timestamptz", "time", "float",
})

# Keywords that legitimately sit directly before "(" -- these are syntax, not function calls.
_NON_FUNCTION_WORDS = frozenset({
    "in", "as", "from", "join", "on", "and", "or", "not", "where", "having", "values", "over", "filter",
    "within", "group", "using", "select", "with", "union", "intersect", "except", "exists", "any", "all",
    "some", "like", "ilike", "between", "case", "when", "then", "else", "by", "partition", "order",
    "rows", "range", "distinct", "materialized", "system", "bernoulli", "repeatable", "tablesample",
    "rollup", "cube", "grouping", "is", "limit", "recursive", "lateral",
})
_EXTRACT_LIKE = frozenset({"extract", "substring", "trim", "overlay", "position"})

_QUOTED_RE = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_QNAME_RE = re.compile(r"[a-z_][a-z0-9_]*(?:\s*\.\s*[a-z_][a-z0-9_]*)*")
_FUNC_CALL_RE = re.compile(r"((?:[a-z_][a-z0-9_]*\s*\.\s*)*)([a-z_][a-z0-9_]*)\s*\(")
_FROM_JOIN_RE = re.compile(r"\b(from|join)\b")
_CTE_DEF_RE = re.compile(r"\s*([a-z_][a-z0-9_]*)\s+as\s+(?:not\s+)?(?:materialized\s+)?\(")
_INTO_RE = re.compile(r"\bselect\b.*?\binto\b", re.DOTALL)
_OFFSET_RE = re.compile(r"\boffset\b")
_TRAILING_LIMIT_RE = re.compile(r"(?is)\bLIMIT\s+(\d+)\s*;?\s*$")
_STOP_KEYWORDS_RE = re.compile(
    r"\b(?:where|group\s+by|order\s+by|limit|join|having|window|union)\b")
_FROM_KEYWORD_RE = re.compile(r"\bfrom\b")


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


def _normalize(sql: str) -> tuple[str, dict[str, str]]:
    """Return (text, quoted) where every string literal is replaced by the word `__s__` and every
    quoted identifier by a unique `__qN__` word (its real, case-sensitive name is kept in `quoted`).
    After this, all remaining analysis sees only plain words, punctuation and placeholders -- no
    quoting rules left to disagree with PostgreSQL about. The text is lower-cased."""
    quoted: dict[str, str] = {}

    def repl(m: re.Match) -> str:
        s = m.group(0)
        if s[0] == "'":
            return " __s__ "
        key = f"__q{len(quoted)}__"
        quoted[key] = s[1:-1].replace('""', '"')
        return f" {key} "

    return _QUOTED_RE.sub(repl, sql).lower(), quoted


def _has_comment(statement: sqlparse.sql.Statement) -> bool:
    return any(tok.ttype in Comment for tok in statement.flatten())


def _leading_cte_names(text: str) -> set[str]:
    """Names defined by the query's LEADING `WITH [RECURSIVE] a AS (...), b AS (...)` clause only.
    A name that merely LOOKS like `x AS (` elsewhere is not trusted as a CTE (fail closed)."""
    m = re.match(r"\s*with\s+(?:recursive\s+)?", text)
    if not m:
        return set()
    names: set[str] = set()
    pos = m.end()
    while True:
        d = _CTE_DEF_RE.match(text, pos)
        if not d:
            break
        names.add(d.group(1))
        depth, i = 1, d.end()
        while i < len(text) and depth:
            depth += {"(": 1, ")": -1}.get(text[i], 0)
            i += 1
        rest = re.match(r"\s*,", text[i:])
        if not rest:
            break
        pos = i + rest.end()
    return names


def _has_comma_join(text: str) -> bool:
    """True if some FROM clause contains a bare, depth-0 comma (an old-style join) before the
    clause ends -- either at a stop keyword or by exiting the parenthesis that contains it
    (e.g. the closing ')' of a CTE definition, which is not a comma-join at all)."""
    n = len(text)
    for from_match in _FROM_KEYWORD_RE.finditer(text):
        if _from_is_not_a_table_ref(text, from_match.start()):
            continue
        depth = 0
        i = from_match.end()
        while i < n:
            ch = text[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth < 0:
                    break  # exited the parenthesis enclosing this FROM; not a comma-join
            elif depth == 0:
                if ch == ",":
                    return True
                if _STOP_KEYWORDS_RE.match(text, i):
                    break
            i += 1
    return False


def _from_is_not_a_table_ref(text: str, pos: int) -> bool:
    """FROM also appears inside EXTRACT(x FROM y), SUBSTRING(x FROM n), TRIM(.. FROM x),
    OVERLAY(..) and `IS DISTINCT FROM`; there it is not followed by a table."""
    if re.search(r"\bdistinct\s*$", text[:pos]):
        return True
    stack: list[str | None] = []
    for idx in range(pos):
        ch = text[idx]
        if ch == "(":
            m = re.search(r"([a-z_][a-z0-9_]*)\s*$", text[:idx])
            stack.append(m.group(1) if m else None)
        elif ch == ")" and stack:
            stack.pop()
    return bool(stack) and stack[-1] in _EXTRACT_LIKE


def _check_tables(text: str, quoted: dict[str, str], ctes: set[str]) -> ValidationResult | None:
    allowed = WHITELISTED_TABLES | ctes
    for m in _FROM_JOIN_RE.finditer(text):
        if m.group(1) == "from" and _from_is_not_a_table_ref(text, m.start()):
            continue
        rest = text[m.end():].lstrip()
        if rest.startswith("("):
            inner = rest.lstrip("( \t\r\n")
            if re.match(r"(?:select|with|values)\b", inner):
                continue  # a subquery; its own FROM/JOINs are checked in turn
            return ValidationResult.reject(
                "unrecognized_from_clause",
                "parenthesised table references are not supported; use plain table names and explicit JOINs")
        q = _QNAME_RE.match(rest)
        if not q:
            return ValidationResult.reject(
                "unrecognized_from_clause", "could not recognise the table name after FROM/JOIN")
        parts = [quoted.get(p.strip(), p.strip()) for p in q.group(0).split(".")]
        if len(parts) > 2:
            return ValidationResult.reject("non_whitelisted_table", "database-qualified names are not allowed")
        if len(parts) == 2:
            if parts[0] != "public":
                return ValidationResult.reject("non_whitelisted_table", f"schema '{parts[0]}' is not allowed")
            parts = parts[1:]
        if parts[0] not in allowed:
            return ValidationResult.reject("non_whitelisted_table", f"table '{parts[0]}' is not allowed")
    return None


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

    # --- unambiguous tokenization: refuse syntax whose meaning this checker and PostgreSQL might
    #     disagree about, instead of trying to interpret it -------------------------------------
    if "\\" in sql or _CONTROL_RE.search(sql):
        return ValidationResult.reject(
            "unsupported_syntax", "backslashes and control characters are not supported")
    text, quoted = _normalize(sql)
    if "'" in text or '"' in text:
        return ValidationResult.reject("unbalanced_quotes", "unterminated string literal or quoted identifier")
    if "$" in text:
        return ValidationResult.reject("unsupported_syntax", "dollar quoting and parameters are not supported")
    if not text.isascii():
        return ValidationResult.reject("unsupported_syntax", "non-ASCII characters are only allowed inside quotes")

    statements = [s for s in sqlparse.parse(sql) if s.token_first(skip_cm=True) is not None]
    if len(statements) != 1 or ";" in text.rstrip().rstrip(";"):
        found = len(statements) if len(statements) != 1 else 2
        return ValidationResult.reject(
            "multiple_statements", f"expected exactly one SQL statement, found {found}")
    statement = statements[0]

    if statement.get_type() != "SELECT" or not re.match(r"\s*\(*\s*(?:select|with)\b", text):
        return ValidationResult.reject(
            "not_a_select", f"only SELECT is allowed, got statement type '{statement.get_type()}'")

    if _has_comment(statement) or "--" in text or "/*" in text:
        return ValidationResult.reject("contains_comment", "SQL comments are not allowed")

    if _INTO_RE.search(text):
        return ValidationResult.reject("select_into", "SELECT ... INTO is not allowed")

    for bad in DANGEROUS_TOKENS:
        if re.search(rf"\b{re.escape(bad)}\b", text):
            return ValidationResult.reject("dangerous_function", f"use of '{bad}' is not allowed")

    if _has_comma_join(text):
        return ValidationResult.reject(
            "comma_join_not_supported",
            "old-style comma joins in FROM are not supported; use explicit JOIN")

    rejected = _check_tables(text, quoted, _leading_cte_names(text))
    if rejected:
        return rejected

    kw = _FORBIDDEN_RE.search(text)
    if kw:
        return ValidationResult.reject(
            "forbidden_keyword", f"'{re.sub(chr(32), ' ', kw.group(0))}' is not allowed anywhere in the query")

    for m in _FUNC_CALL_RE.finditer(text):
        qualifier, name = m.group(1), m.group(2)
        if qualifier:
            return ValidationResult.reject(
                "function_not_allowed", f"schema-qualified function call '{qualifier.strip()}{name}' is not allowed")
        if name in _NON_FUNCTION_WORDS:
            continue
        if name not in ALLOWED_FUNCTIONS:
            return ValidationResult.reject("function_not_allowed", f"function '{name}' is not on the allowlist")

    if _OFFSET_RE.search(text):
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
