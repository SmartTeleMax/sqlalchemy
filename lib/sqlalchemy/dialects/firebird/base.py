# firebird.py
# Copyright (C) 2005, 2006, 2007, 2008, 2009 Michael Bayer mike_mp@zzzcomputing.com
#
# This module is part of SQLAlchemy and is released under
# the MIT License: http://www.opensource.org/licenses/mit-license.php

"""
Support for the Firebird database.

Connectivity is usually supplied via the kinterbasdb_
DBAPI module.

Firebird dialects
-----------------

Firebird offers two distinct dialects_ (not to be confused with a
SQLAlchemy ``Dialect``):

dialect 1
  This is the old syntax and behaviour, inherited from Interbase pre-6.0.

dialect 3
  This is the newer and supported syntax, introduced in Interbase 6.0.

The SQLAlchemy Firebird dialect detects these versions and
adjusts its representation of SQL accordingly.  However,
support for dialect 1 is not well tested and probably has
incompatibilities.

Firebird Locking Behavior
-------------------------

Firebird locks tables aggressively.  For this reason, a DROP TABLE may
hang until other transactions are released.  SQLAlchemy does its best
to release transactions as quickly as possible.  The most common cause
of hanging transactions is a non-fully consumed result set, i.e.::

    result = engine.execute("select * from table")
    row = result.fetchone()
    return

Where above, the ``ResultProxy`` has not been fully consumed.  The
connection will be returned to the pool and the transactional state
rolled back once the Python garbage collector reclaims the objects
which hold onto the connection, which often occurs asynchronously.
The above use case can be alleviated by calling ``first()`` on the
``ResultProxy`` which will fetch the first row and immediately close
all remaining cursor/connection resources.

RETURNING support
-----------------

Firebird 2.0 supports returning a result set from inserts, and 2.1 extends
that to deletes and updates.

To use this pass the column/expression list to the ``firebird_returning``
parameter when creating the queries::

  raises = tbl.update(empl.c.sales > 100, values=dict(salary=empl.c.salary * 1.1),
                      firebird_returning=[empl.c.id, empl.c.salary]).execute().fetchall()


.. [#] Well, that is not the whole story, as the client may still ask
       a different (lower) dialect...

.. _dialects: http://mc-computing.com/Databases/Firebird/SQL_Dialect.html
.. _kinterbasdb: http://sourceforge.net/projects/kinterbasdb

"""


import datetime, decimal, re

from sqlalchemy import schema as sa_schema
from sqlalchemy import exc, types as sqltypes, sql, util
from sqlalchemy.sql import expression
from sqlalchemy.engine import base, default, reflection
from sqlalchemy.sql import compiler

from sqlalchemy.types import (BIGINT, BLOB, BOOLEAN, CHAR, DATE,
                               FLOAT, INTEGER, NUMERIC, SMALLINT,
                               TEXT, TIME, TIMESTAMP, VARCHAR)


RESERVED_WORDS = set(
   ["action", "active", "add", "admin", "after", "all", "alter", "and", "any",
    "as", "asc", "ascending", "at", "auto", "autoddl", "avg", "based", "basename",
    "base_name", "before", "begin", "between", "bigint", "blob", "blobedit", "buffer",
    "by", "cache", "cascade", "case", "cast", "char", "character", "character_length",
    "char_length", "check", "check_point_len", "check_point_length", "close", "collate",
    "collation", "column", "commit", "committed", "compiletime", "computed", "conditional",
    "connect", "constraint", "containing", "continue", "count", "create", "cstring",
    "current", "current_connection", "current_date", "current_role", "current_time",
    "current_timestamp", "current_transaction", "current_user", "cursor", "database",
    "date", "day", "db_key", "debug", "dec", "decimal", "declare", "default", "delete",
    "desc", "descending", "describe", "descriptor", "disconnect", "display", "distinct",
    "do", "domain", "double", "drop", "echo", "edit", "else", "end", "entry_point",
    "escape", "event", "exception", "execute", "exists", "exit", "extern", "external",
    "extract", "fetch", "file", "filter", "float", "for", "foreign", "found", "free_it",
    "from", "full", "function", "gdscode", "generator", "gen_id", "global", "goto",
    "grant", "group", "group_commit_", "group_commit_wait", "having", "help", "hour",
    "if", "immediate", "in", "inactive", "index", "indicator", "init", "inner", "input",
    "input_type", "insert", "int", "integer", "into", "is", "isolation", "isql", "join",
    "key", "lc_messages", "lc_type", "left", "length", "lev", "level", "like", "logfile",
    "log_buffer_size", "log_buf_size", "long", "manual", "max", "maximum", "maximum_segment",
    "max_segment", "merge", "message", "min", "minimum", "minute", "module_name", "month",
    "names", "national", "natural", "nchar", "no", "noauto", "not", "null", "numeric",
    "num_log_buffers", "num_log_bufs", "octet_length", "of", "on", "only", "open", "option",
    "or", "order", "outer", "output", "output_type", "overflow", "page", "pagelength",
    "pages", "page_size", "parameter", "password", "plan", "position", "post_event",
    "precision", "prepare", "primary", "privileges", "procedure", "protected", "public",
    "quit", "raw_partitions", "rdb$db_key", "read", "real", "record_version", "recreate",
    "references", "release", "release", "reserv", "reserving", "restrict", "retain",
    "return", "returning_values", "returns", "revoke", "right", "role", "rollback",
    "row_count", "runtime", "savepoint", "schema", "second", "segment", "select",
    "set", "shadow", "shared", "shell", "show", "singular", "size", "smallint",
    "snapshot", "some", "sort", "sqlcode", "sqlerror", "sqlwarning", "stability",
    "starting", "starts", "statement", "static", "statistics", "sub_type", "sum",
    "suspend", "table", "terminator", "then", "time", "timestamp", "to", "transaction",
    "translate", "translation", "trigger", "trim", "type", "uncommitted", "union",
    "unique", "update", "upper", "user", "using", "value", "values", "varchar",
    "variable", "varying", "version", "view", "wait", "wait_time", "weekday", "when",
    "whenever", "where", "while", "with", "work", "write", "year", "yearday" ])


class _FBBoolean(sqltypes.Boolean):
    def result_processor(self, dialect):
        def process(value):
            if value is None:
                return None
            return value and True or False
        return process

    def bind_processor(self, dialect):
        def process(value):
            if value is True:
                return 1
            elif value is False:
                return 0
            elif value is None:
                return None
            else:
                return value and True or False
        return process


colspecs = {
    sqltypes.Boolean: _FBBoolean,
}

ischema_names = {
      'SHORT': SMALLINT,
       'LONG': BIGINT,
       'QUAD': FLOAT,
      'FLOAT': FLOAT,
       'DATE': DATE,
       'TIME': TIME,
       'TEXT': TEXT,
      'INT64': NUMERIC,
     'DOUBLE': FLOAT,
  'TIMESTAMP': TIMESTAMP,
    'VARYING': VARCHAR,
    'CSTRING': CHAR,
       'BLOB': BLOB,
    }


# TODO: date conversion types (should be implemented as _FBDateTime, _FBDate, etc.
# as bind/result functionality is required)

class FBTypeCompiler(compiler.GenericTypeCompiler):
    def visit_boolean(self, type_):
        return self.visit_SMALLINT(type_)

    def visit_datetime(self, type_):
        return self.visit_TIMESTAMP(type_)

    def visit_TEXT(self, type_):
        return "BLOB SUB_TYPE 1"

    def visit_BLOB(self, type_):
        return "BLOB SUB_TYPE 0"


class FBCompiler(sql.compiler.SQLCompiler):
    """Firebird specific idiosincrasies"""

    def visit_mod(self, binary, **kw):
        # Firebird lacks a builtin modulo operator, but there is
        # an equivalent function in the ib_udf library.
        return "mod(%s, %s)" % (self.process(binary.left), self.process(binary.right))

    def visit_alias(self, alias, asfrom=False, **kwargs):
        if self.dialect._version_two:
            return super(FBCompiler, self).visit_alias(alias, asfrom=asfrom, **kwargs)
        else:
            # Override to not use the AS keyword which FB 1.5 does not like
            if asfrom:
                alias_name = isinstance(alias.name, expression._generated_label) and \
                                self._truncated_identifier("alias", alias.name) or alias.name

                return self.process(alias.original, asfrom=asfrom, **kwargs) + " " + \
                            self.preparer.format_alias(alias, alias_name)
            else:
                return self.process(alias.original, **kwargs)

    def visit_substring_func(self, func, **kw):
        s = self.process(func.clauses.clauses[0])
        start = self.process(func.clauses.clauses[1])
        if len(func.clauses.clauses) > 2:
            length = self.process(func.clauses.clauses[2])
            return "SUBSTRING(%s FROM %s FOR %s)" % (s, start, length)
        else:
            return "SUBSTRING(%s FROM %s)" % (s, start)

    def visit_length_func(self, function, **kw):
        if self.dialect._version_two:
            return "char_length" + self.function_argspec(function)
        else:
            return "strlen" + self.function_argspec(function)

    visit_char_length_func = visit_length_func

    def function_argspec(self, func):
        if func.clauses:
            return self.process(func.clause_expr)
        else:
            return ""

    def default_from(self):
        return " FROM rdb$database"

    def visit_sequence(self, seq):
        return "gen_id(%s, 1)" % self.preparer.format_sequence(seq)

    def get_select_precolumns(self, select):
        """Called when building a ``SELECT`` statement, position is just
        before column list Firebird puts the limit and offset right
        after the ``SELECT``...
        """

        result = ""
        if select._limit:
            result += "FIRST %d "  % select._limit
        if select._offset:
            result +="SKIP %d "  %  select._offset
        if select._distinct:
            result += "DISTINCT "
        return result

    def limit_clause(self, select):
        """Already taken care of in the `get_select_precolumns` method."""

        return ""

    def _append_returning(self, text, stmt):
        returning_cols = stmt.kwargs["firebird_returning"]
        def flatten_columnlist(collist):
            for c in collist:
                if isinstance(c, sql.expression.Selectable):
                    for co in c.columns:
                        yield co
                else:
                    yield c
        columns = [self.process(c, within_columns_clause=True)
                   for c in flatten_columnlist(returning_cols)]
        text += ' RETURNING ' + ', '.join(columns)
        return text

    def visit_update(self, update_stmt):
        text = super(FBCompiler, self).visit_update(update_stmt)
        if "firebird_returning" in update_stmt.kwargs:
            return self._append_returning(text, update_stmt)
        else:
            return text

    def visit_insert(self, insert_stmt):
        text = super(FBCompiler, self).visit_insert(insert_stmt)
        if "firebird_returning" in insert_stmt.kwargs:
            return self._append_returning(text, insert_stmt)
        else:
            return text

    def visit_delete(self, delete_stmt):
        text = super(FBCompiler, self).visit_delete(delete_stmt)
        if "firebird_returning" in delete_stmt.kwargs:
            return self._append_returning(text, delete_stmt)
        else:
            return text


class FBDDLCompiler(sql.compiler.DDLCompiler):
    """Firebird syntactic idiosincrasies"""

    def visit_create_sequence(self, create):
        """Generate a ``CREATE GENERATOR`` statement for the sequence."""

        if self.dialect._version_two:
            return "CREATE SEQUENCE %s" % self.preparer.format_sequence(create.element)
        else:
            return "CREATE GENERATOR %s" % self.preparer.format_sequence(create.element)

    def visit_drop_sequence(self, drop):
        """Generate a ``DROP GENERATOR`` statement for the sequence."""

        if self.dialect._version_two:
            return "DROP SEQUENCE %s" % self.preparer.format_sequence(drop.element)
        else:
            return "DROP GENERATOR %s" % self.preparer.format_sequence(drop.element)


class FBDefaultRunner(base.DefaultRunner):
    """Firebird specific idiosincrasies"""

    def visit_sequence(self, seq):
        """Get the next value from the sequence using ``gen_id()``."""

        return self.execute_string("SELECT gen_id(%s, 1) FROM rdb$database" % \
            self.dialect.identifier_preparer.format_sequence(seq))


class FBIdentifierPreparer(sql.compiler.IdentifierPreparer):
    """Install Firebird specific reserved words."""

    reserved_words = RESERVED_WORDS

    def __init__(self, dialect):
        super(FBIdentifierPreparer, self).__init__(dialect, omit_schema=True)


class FBDialect(default.DefaultDialect):
    """Firebird dialect"""

    name = 'firebird'

    max_identifier_length = 31
    supports_sequences = True
    sequences_optional = False
    supports_default_values = True
    supports_empty_insert = False
    preexecute_pk_sequences = True
    supports_pk_autoincrement = False
    requires_name_normalize = True

    statement_compiler = FBCompiler
    ddl_compiler = FBDDLCompiler
    defaultrunner = FBDefaultRunner
    preparer = FBIdentifierPreparer
    type_compiler = FBTypeCompiler

    colspecs = colspecs
    ischema_names = ischema_names

    # defaults to dialect ver. 3,
    # will be autodetected off upon
    # first connect
    _version_two = True

    def initialize(self, connection):
        super(FBDialect, self).initialize(connection)
        self._version_two = self.server_version_info > (2, )
        if not self._version_two:
            # TODO: whatever other pre < 2.0 stuff goes here
            self.ischema_names = ischema_names.copy()
            self.ischema_names['TIMESTAMP'] = sqltypes.DATE
            self.colspecs = {
                sqltypes.DateTime: sqltypes.DATE
            }

    def normalize_name(self, name):
        # Remove trailing spaces: FB uses a CHAR() type,
        # that is padded with spaces
        name = name and name.rstrip()
        if name is None:
            return None
        elif name.upper() == name and \
            not self.identifier_preparer._requires_quotes(name.lower()):
            return name.lower()
        else:
            return name

    def denormalize_name(self, name):
        if name is None:
            return None
        elif name.lower() == name and \
            not self.identifier_preparer._requires_quotes(name.lower()):
            return name.upper()
        else:
            return name

    def has_table(self, connection, table_name, schema=None):
        """Return ``True`` if the given table exists, ignoring the `schema`."""

        tblqry = """
        SELECT 1 FROM rdb$database
        WHERE EXISTS (SELECT rdb$relation_name
                      FROM rdb$relations
                      WHERE rdb$relation_name=?)
        """
        c = connection.execute(tblqry, [self.denormalize_name(table_name)])
        return c.first() is not None

    def has_sequence(self, connection, sequence_name):
        """Return ``True`` if the given sequence (generator) exists."""

        genqry = """
        SELECT 1 FROM rdb$database
        WHERE EXISTS (SELECT rdb$generator_name
                      FROM rdb$generators
                      WHERE rdb$generator_name=?)
        """
        c = connection.execute(genqry, [self.denormalize_name(sequence_name)])
        return c.first() is not None

    def table_names(self, connection, schema):
        s = """
        SELECT DISTINCT rdb$relation_name
        FROM rdb$relation_fields
        WHERE rdb$system_flag=0 AND rdb$view_context IS NULL
        """
        return [self.normalize_name(row[0]) for row in connection.execute(s)]

    @reflection.cache
    def get_table_names(self, connection, schema=None, **kw):
        return self.table_names(connection, schema)

    @reflection.cache
    def get_view_names(self, connection, schema=None, **kw):
        s = """
        SELECT distinct rdb$view_name
        FROM rdb$view_relations
        """
        return [self.normalize_name(row[0]) for row in connection.execute(s)]

    @reflection.cache
    def get_view_definition(self, connection, view_name, schema=None, **kw):
        qry = """
        SELECT rdb$view_source AS view_source
        FROM rdb$relations
        WHERE rdb$relation_name=?
        """
        rp = connection.execute(qry, [self.denormalize_name(view_name)])
        row = rp.first()
        if row:
            return row['view_source']
        else:
            return None

    @reflection.cache
    def get_primary_keys(self, connection, table_name, schema=None, **kw):
        # Query to extract the PK/FK constrained fields of the given table
        keyqry = """
        SELECT se.rdb$field_name AS fname
        FROM rdb$relation_constraints rc
             JOIN rdb$index_segments se ON rc.rdb$index_name=se.rdb$index_name
        WHERE rc.rdb$constraint_type=? AND rc.rdb$relation_name=?
        """
        tablename = self.denormalize_name(table_name)
        # get primary key fields
        c = connection.execute(keyqry, ["PRIMARY KEY", tablename])
        pkfields = [self.normalize_name(r['fname']) for r in c.fetchall()]
        return pkfields

    @reflection.cache
    def get_column_sequence(self, connection, table_name, column_name, schema=None, **kw):
        tablename = self.denormalize_name(table_name)
        colname = self.denormalize_name(column_name)
        # Heuristic-query to determine the generator associated to a PK field
        genqry = """
        SELECT trigdep.rdb$depended_on_name AS fgenerator
        FROM rdb$dependencies tabdep
             JOIN rdb$dependencies trigdep
                  ON tabdep.rdb$dependent_name=trigdep.rdb$dependent_name
                     AND trigdep.rdb$depended_on_type=14
                     AND trigdep.rdb$dependent_type=2
             JOIN rdb$triggers trig ON trig.rdb$trigger_name=tabdep.rdb$dependent_name
        WHERE tabdep.rdb$depended_on_name=?
          AND tabdep.rdb$depended_on_type=0
          AND trig.rdb$trigger_type=1
          AND tabdep.rdb$field_name=?
          AND (SELECT count(*)
               FROM rdb$dependencies trigdep2
               WHERE trigdep2.rdb$dependent_name = trigdep.rdb$dependent_name) = 2
        """
        genc = connection.execute(genqry, [tablename, colname])
        genr = genc.fetchone()
        if genr is not None:
            return dict(name=self.normalize_name(genr['fgenerator']))

    @reflection.cache
    def get_columns(self, connection, table_name, schema=None, **kw):
        # Query to extract the details of all the fields of the given table
        tblqry = """
        SELECT DISTINCT r.rdb$field_name AS fname,
                        r.rdb$null_flag AS null_flag,
                        t.rdb$type_name AS ftype,
                        f.rdb$field_sub_type AS stype,
                        f.rdb$field_length AS flen,
                        f.rdb$field_precision AS fprec,
                        f.rdb$field_scale AS fscale,
                        COALESCE(r.rdb$default_source, f.rdb$default_source) AS fdefault
        FROM rdb$relation_fields r
             JOIN rdb$fields f ON r.rdb$field_source=f.rdb$field_name
             JOIN rdb$types t
                  ON t.rdb$type=f.rdb$field_type AND t.rdb$field_name='RDB$FIELD_TYPE'
        WHERE f.rdb$system_flag=0 AND r.rdb$relation_name=?
        ORDER BY r.rdb$field_position
        """
        # get the PK, used to determine the eventual associated sequence
        pkey_cols = self.get_primary_keys(connection, table_name)

        tablename = self.denormalize_name(table_name)
        # get all of the fields for this table
        c = connection.execute(tblqry, [tablename])
        cols = []
        while True:
            row = c.fetchone()
            if row is None:
                break
            name = self.normalize_name(row['fname'])
            # get the data type

            colspec = row['ftype'].rstrip()
            coltype = self.ischema_names.get(colspec)
            if coltype is None:
                util.warn("Did not recognize type '%s' of column '%s'" %
                          (colspec, name))
                coltype = sqltypes.NULLTYPE
            elif colspec == 'INT64':
                coltype = coltype(precision=row['fprec'], scale=row['fscale'] * -1)
            elif colspec in ('VARYING', 'CSTRING'):
                coltype = coltype(row['flen'])
            elif colspec == 'TEXT':
                coltype = TEXT(row['flen'])
            elif colspec == 'BLOB':
                if row['stype'] == 1:
                    coltype = TEXT()
                else:
                    coltype = BLOB()
            else:
                coltype = coltype(row)

            # does it have a default value?
            defvalue = None
            if row['fdefault'] is not None:
                # the value comes down as "DEFAULT 'value'"
                assert row['fdefault'].upper().startswith('DEFAULT '), row
                defvalue = row['fdefault'][8:]
            col_d = {
                'name' : name,
                'type' : coltype,
                'nullable' :  not bool(row['null_flag']),
                'default' : defvalue
            }

            # if the PK is a single field, try to see if its linked to
            # a sequence thru a trigger
            if len(pkey_cols)==1 and name==pkey_cols[0]:
                seq_d = self.get_column_sequence(connection, tablename, name)
                if seq_d is not None:
                    col_d['sequence'] = seq_d

            cols.append(col_d)
        return cols

    @reflection.cache
    def get_foreign_keys(self, connection, table_name, schema=None, **kw):
        # Query to extract the details of each UK/FK of the given table
        fkqry = """
        SELECT rc.rdb$constraint_name AS cname,
               cse.rdb$field_name AS fname,
               ix2.rdb$relation_name AS targetrname,
               se.rdb$field_name AS targetfname
        FROM rdb$relation_constraints rc
             JOIN rdb$indices ix1 ON ix1.rdb$index_name=rc.rdb$index_name
             JOIN rdb$indices ix2 ON ix2.rdb$index_name=ix1.rdb$foreign_key
             JOIN rdb$index_segments cse ON cse.rdb$index_name=ix1.rdb$index_name
             JOIN rdb$index_segments se
                  ON se.rdb$index_name=ix2.rdb$index_name
                     AND se.rdb$field_position=cse.rdb$field_position
        WHERE rc.rdb$constraint_type=? AND rc.rdb$relation_name=?
        ORDER BY se.rdb$index_name, se.rdb$field_position
        """
        tablename = self.denormalize_name(table_name)

        c = connection.execute(fkqry, ["FOREIGN KEY", tablename])
        fks = util.defaultdict(lambda:{
            'name' : None,
            'constrained_columns' : [],
            'referred_schema' : None,
            'referred_table' : None,
            'referred_columns' : []
        })

        for row in c:
            cname = self.normalize_name(row['cname'])
            fk = fks[cname]
            if not fk['name']:
                fk['name'] = cname
                fk['referred_table'] = self.normalize_name(row['targetrname'])
            fk['constrained_columns'].append(self.normalize_name(row['fname']))
            fk['referred_columns'].append(
                            self.normalize_name(row['targetfname']))
        return fks.values()

    @reflection.cache
    def get_indexes(self, connection, table_name, schema=None, **kw):
        qry = """
        SELECT ix.rdb$index_name AS index_name,
               ix.rdb$unique_flag AS unique_flag,
               ic.rdb$field_name AS field_name
        FROM rdb$indices ix
             JOIN rdb$index_segments ic
                  ON ix.rdb$index_name=ic.rdb$index_name
             LEFT OUTER JOIN rdb$relation_constraints
                  ON rdb$relation_constraints.rdb$index_name = ic.rdb$index_name
        WHERE ix.rdb$relation_name=? AND ix.rdb$foreign_key IS NULL
          AND rdb$relation_constraints.rdb$constraint_type IS NULL
        ORDER BY index_name, field_name
        """
        c = connection.execute(qry, [self.denormalize_name(table_name)])

        indexes = util.defaultdict(dict)
        for row in c:
            indexrec = indexes[row['index_name']]
            if 'name' not in indexrec:
                indexrec['name'] = self.normalize_name(row['index_name'])
                indexrec['column_names'] = []
                indexrec['unique'] = bool(row['unique_flag'])

            indexrec['column_names'].append(self.normalize_name(row['field_name']))

        return indexes.values()

    def do_execute(self, cursor, statement, parameters, **kwargs):
        # kinterbase does not accept a None, but wants an empty list
        # when there are no arguments.
        cursor.execute(statement, parameters or [])

    def do_rollback(self, connection):
        # Use the retaining feature, that keeps the transaction going
        connection.rollback(True)

    def do_commit(self, connection):
        # Use the retaining feature, that keeps the transaction going
        connection.commit(True)
