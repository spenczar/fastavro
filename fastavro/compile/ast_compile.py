from typing import Dict, Any, Union, List, Callable, IO, Optional, Iterator, DefaultDict
import random
import platform
import collections
import re
from fastavro.read import block_reader
from fastavro._schema_common import PRIMITIVES
from fastavro.schema import expand_schema
from fastavro.compile._graph import find_recursive_types
import sys

from ast import (
    AST,
    Assign,
    Attribute,
    Call,
    Compare,
    Constant,
    Dict as DictLiteral,
    Eq,
    Expr,
    For,
    FunctionDef,
    If,
    IfExp,
    Import,
    ImportFrom,
    Index,
    List as ListLiteral,
    Load,
    Lt,
    Module,
    Name,
    NotEq,
    Return,
    Store,
    Subscript,
    USub,
    UnaryOp,
    While,
    alias,
    arg,
    arguments,
    fix_missing_locations,
    keyword,
    stmt,
    dump,
)

if sys.version_info >= (3, 9):
    from ast import unparse

    unparse_available = True
else:

    def unparse(x):
        return ""

    unparse_available = False


PRIMITIVE_READERS = {
    "string": "read_utf8",
    "int": "read_long",
    "long": "read_long",
    "float": "read_float",
    "double": "read_double",
    "boolean": "read_boolean",
    "bytes": "read_bytes",
    "null": "read_null",
}

LOGICAL_READERS = {
    "decimal": "read_decimal",
    "uuid": "read_uuid",
    "date": "read_date",
    "time-millis": "read_time_millis",
    "time-micros": "read_time_micros",
    "timestamp-millis": "read_timestamp_millis",
    "timestamp-micros": "read_timestamp_micros",
}


SchemaType = Union[
    str,  # Primitives
    List[Any],  # Unions
    Dict[str, Any],  # Complex types
]


def read_file(fo: IO[bytes]) -> Iterator[Any]:
    """
    Open an Avro Container Format file. Read its header to find the schema,
    compile the schema, and use it to deserialize records, yielding them out.
    """
    blocks = block_reader(fo, reader_schema=None, return_record_name=False)
    if blocks.writer_schema is None:
        raise ValueError("missing write schema")
    sp = SchemaParser(blocks.writer_schema)
    reader = sp.compile()

    for block in blocks:
        for _ in range(block.num_records):
            yield reader(block.bytes_)


class SchemaParser:
    schema: SchemaType
    variable_name_counts: DefaultDict[str, int]

    # List of schemas which are defined recursively. This is needed to generate
    # separate functions for each of these types so that they can call
    # themselves recursively when reading.
    recursive_types: List[Dict]

    pure_python: bool

    file_reader: Optional[Callable[[IO[bytes]], Any]]
    schemaless_reader: Optional[Callable[[IO[bytes]], Any]]

    def __init__(self, schema: SchemaType):
        self.schema = expand_schema(schema)  # type: ignore
        self.variable_name_counts = collections.defaultdict(int)
        self.recursive_types = find_recursive_types(self.schema)
        self.pure_python = platform.python_implementation() != "CPython"
        print(f"found recursive types: {self.recursive_types}")

    def new_variable(self, name: str) -> str:
        """
        Returns a new name for a variable which is guaranteed to be unique.
        """
        count = self.variable_name_counts[name]
        self.variable_name_counts[name] = count + 1
        if count == 0:
            return f"{name}"
        return f"{name}{count}"

    def compile(
        self, populate_linecache=unparse_available
    ) -> Callable[[IO[bytes]], Any]:
        """
        Compile the schema and return a callable function which will read from a
        file-like byte source and produce a value determined by schema.
        """
        module = self.generate_module()

        if populate_linecache:
            if not unparse_available:
                raise NotImplementedError(
                    "cannot provide source code in Python < 3.9 because AST cannot be unparsed"
                )
            import linecache

            source_code = unparse(module)
            print(source_code)
            filename = f"autogenerated_file_{_rand_str(8)}.py"
            compiled = compile(source_code, filename, mode="exec")
            source_lines = source_code.splitlines()
            linecache.cache[filename] = (  # type: ignore
                len(source_lines),
                None,
                source_lines,
                filename,
            )
        else:
            print(dump(module))
            filename = "<generated>"
            compiled = compile(module, filename, mode="exec")
        namespace = {}  # type: ignore
        exec(compiled, namespace)
        self.schemaless_reader = namespace["reader"]
        return self.schemaless_reader  # type: ignore

    def generate_module(self) -> Module:
        body: List[stmt] = [Import(names=[alias(name="decimal")])]

        # Add import statements of low-level reader functions
        import_from_fastavro_read = []
        for reader in PRIMITIVE_READERS.values():
            import_from_fastavro_read.append(alias(name=reader))
        for reader in LOGICAL_READERS.values():
            import_from_fastavro_read.append(alias(name=reader))

        if self.pure_python:
            body.append(
                ImportFrom(
                    module="fastavro._read_py",
                    names=import_from_fastavro_read,
                    level=0,
                )
            )
            body.append(
                ImportFrom(
                    module="fastavro.io.binary_decoder",
                    names=[alias(name="BinaryDecoder")],
                    level=0,
                )
            )
        else:
            body.append(
                ImportFrom(
                    module="fastavro._read",
                    names=import_from_fastavro_read,
                    level=0,
                )
            )

        body.append(self.generate_reader_func(self.schema, "reader"))

        for recursive_type in self.recursive_types:
            body.append(
                self.generate_reader_func(
                    name=self._named_type_reader_name(recursive_type["name"]),
                    schema=recursive_type,
                )
            )

        module = Module(
            body=body,
            type_ignores=[],
        )
        module = fix_missing_locations(module)
        return module

    @staticmethod
    def _named_type_reader_name(name: str) -> str:
        return "_read_" + _clean_name(name)

    def generate_reader_func(self, schema: SchemaType, name: str) -> FunctionDef:
        """
        Returns an AST describing a function which can read an Avro message from a
        IO[bytes] source. The message is parsed according to the given schema.
        """
        src_var = Name(id="src", ctx=Load())
        result_var = Name(id=self.new_variable("result"), ctx=Store())
        func = FunctionDef(
            name=name,
            args=arguments(
                args=[arg(arg="src")],
                posonlyargs=[],
                kwonlyargs=[],
                kw_defaults=[],
                defaults=[],
            ),
            body=[],
            decorator_list=[],
        )

        if platform.python_implementation() == "PyPy" and name == "reader":
            # In PyPy, we'll need to wrap the input stream with a decoder. This
            # should only be done once, at the very top level
            func.body.append(
                Assign(
                    targets=[Name(id="src", ctx=Store())],
                    value=Call(
                        func=Name(id="BinaryDecoder", ctx=Load()),
                        args=[src_var],
                        keywords=[],
                    ),
                )
            )

        func.body.extend(self._gen_reader(schema, src_var, result_var))
        func.body.append(Return(value=Name(id=result_var.id, ctx=Load())))
        return func

    def _gen_reader(self, schema: SchemaType, src: Name, dest: AST) -> List[stmt]:
        """
        Returns a sequence of statements which will read data from src and write
        the deserialized value into dest.
        """
        if isinstance(schema, str):
            if schema in PRIMITIVES:
                return self._gen_primitive_reader(
                    primitive_type=schema, src=src, dest=dest
                )
            else:
                # Named type reference. Could be recursion?
                if schema in set(t["name"] for t in self.recursive_types):
                    # Yep, recursion. Just generate a function call - we'll have
                    # a separate function to handle this type.
                    return self._gen_recursive_reader_call(schema, src, dest)
        if isinstance(schema, list):
            return self._gen_union_reader(
                options=schema,
                src=src,
                dest=dest,
            )
        if isinstance(schema, dict):
            if "logicalType" in schema:
                return self._gen_logical_reader(
                    schema=schema,
                    src=src,
                    dest=dest,
                )
            schema_type = schema["type"]
            if schema_type in PRIMITIVE_READERS.keys():
                return self._gen_primitive_reader(
                    primitive_type=schema_type,
                    src=src,
                    dest=dest,
                )
            if schema_type == "record":
                return self._gen_record_reader(
                    schema=schema,
                    src=src,
                    dest=dest,
                )
            if schema_type == "array":
                return self._gen_array_reader(
                    item_schema=schema["items"],
                    src=src,
                    dest=dest,
                )
            if schema_type == "map":
                return self._gen_map_reader(
                    value_schema=schema["values"],
                    src=src,
                    dest=dest,
                )
            if schema_type == "fixed":
                return self._gen_fixed_reader(
                    size=schema["size"],
                    src=src,
                    dest=dest,
                )
            if schema_type == "enum":
                return self._gen_enum_reader(
                    symbols=schema["symbols"],
                    default=schema.get("default"),
                    src=src,
                    dest=dest,
                )

        raise NotImplementedError(f"Schema type not implemented: {schema}")

    def _gen_union_reader(
        self, options: List[SchemaType], src: Name, dest: AST
    ) -> List[stmt]:

        # Special case: fields like '["null", "long"] which represent an
        # optional field.
        if len(options) == 2:
            if options[0] == "null":
                return self._gen_optional_reader(1, options[1], src, dest)
            if options[1] == "null":
                return self._gen_optional_reader(0, options[0], src, dest)

        statements: List[stmt] = []
        # Read a long to figure out which option in the union is chosen.
        idx_var = self.new_variable("union_choice")
        idx_var_dest = Name(id=idx_var, ctx=Store())
        statements.extend(self._gen_primitive_reader("long", src, idx_var_dest))

        idx_var_ref = Name(id=idx_var, ctx=Load())
        prev_if = None
        for idx, option in enumerate(options):
            if_idx_matches = Compare(
                left=idx_var_ref, ops=[Eq()], comparators=[Constant(idx)]
            )
            if_stmt = If(
                test=if_idx_matches,
                body=self._gen_reader(option, src, dest),
                orelse=[],
            )

            if prev_if is None:
                statements.append(if_stmt)
            else:
                prev_if.orelse = [if_stmt]
            prev_if = if_stmt
        return statements

    def _gen_optional_reader(
        self, idx: int, schema: SchemaType, src: Name, dest: AST
    ) -> List[stmt]:
        statements: List[stmt] = []
        is_populated = Compare(
            left=Call(func=Name(id="read_long", ctx=Load()), args=[src], keywords=[]),
            ops=[Eq()],
            comparators=[Constant(idx)],
        )

        if isinstance(schema, str) and schema in PRIMITIVE_READERS:
            # We can read the value in one line, so we can do something like:
            #  v1["optional_long"] = read_long(src) if idx == 1 else None

            if_expr = IfExp(
                test=is_populated,
                body=Call(
                    func=Name(id=PRIMITIVE_READERS[schema], ctx=Load()),
                    args=[src],
                    keywords=[],
                ),
                orelse=Constant(None),
            )
            assignment = Assign(
                targets=[dest],
                value=if_expr,
            )
            statements.append(assignment)
        else:
            # It takes more than one line to read the value, so we need a real if block.
            if_stmt = If(
                test=is_populated,
                body=self._gen_reader(schema, src, dest),
                orelse=[Assign(targets=[dest], value=Constant(None))],
            )
            statements.append(if_stmt)
        return statements

    def _gen_record_reader(self, schema: Dict, src: Name, dest: AST) -> List[stmt]:
        statements: List[stmt] = []

        # Construct a new empty dictionary to hold the record contents.
        value_name = self.new_variable(_clean_name(schema["name"]))
        empty_dict = DictLiteral(keys=[], values=[])
        statements.append(
            Assign(
                targets=[Name(id=value_name, ctx=Store())],
                value=empty_dict,
                lineno=0,
            ),
        )
        value_reference = Name(id=value_name, ctx=Load())

        # Write statements to populate all the fields of the record.
        for field in schema["fields"]:
            # Make an AST node that references an entry in the record dict,
            # using the field name as a key.
            field_dest = Subscript(
                value=value_reference,
                slice=Index(value=Constant(value=field["name"])),
                ctx=Store(),
            )

            # Generate the statements required to read that field's type, and to
            # store it into field_dest.
            read_statements = self._gen_reader(field["type"], src, field_dest)
            statements.extend(read_statements)

        # Now that we have a fully constructed record, write it into the
        # destination provided.
        statements.append(
            Assign(
                targets=[dest],
                value=value_reference,
                lineno=0,
            )
        )
        return statements

    def _gen_array_reader(
        self, item_schema: SchemaType, src: Name, dest: AST
    ) -> List[stmt]:
        """
        Returns a sequence of statements which will deserialize an array of given
        type from src into dest.
        """
        statements: List[stmt] = []

        # Create a new list to hold the values we'll read.
        name = "array_"
        if isinstance(item_schema, dict):
            if "name" in item_schema:
                name += item_schema["name"]
            elif "type" in item_schema and isinstance(item_schema["type"], str):
                name += item_schema["type"]
        elif isinstance(item_schema, str):
            name += item_schema
        name = _clean_name(name)

        list_varname = self.new_variable(name)

        assign_stmt = Assign(
            targets=[Name(id=list_varname, ctx=Store())],
            value=ListLiteral(elts=[], ctx=Load()),
        )
        statements.append(assign_stmt)

        # For each message in the array...
        for_each_message: List[stmt] = []

        # ... read a value...
        value_varname = self.new_variable("array_val")
        value_dest = Name(id=value_varname, ctx=Store())
        read_statements = self._gen_reader(item_schema, src, value_dest)
        for_each_message.extend(read_statements)

        # ... and append it to the list.
        list_append_method = Attribute(
            value=Name(id=list_varname, ctx=Load()),
            attr="append",
            ctx=Load(),
        )
        list_append_method_call = Expr(
            Call(
                func=list_append_method,
                args=[Name(id=value_varname, ctx=Load())],
                keywords=[],
            )
        )
        for_each_message.append(list_append_method_call)

        statements.extend(self._gen_block_reader(for_each_message, src))

        # Finally, assign the list we have constructed into the destination AST node.
        assign_result = Assign(
            targets=[dest],
            value=Name(id=list_varname, ctx=Load()),
        )
        statements.append(assign_result)
        return statements

    def _gen_map_reader(
        self, value_schema: SchemaType, src: Name, dest: AST
    ) -> List[stmt]:
        """
        Returns a sequence of statements which will deserialize a map with given
        value type from src into dest.
        """
        statements: List[stmt] = []

        name = "map_"
        if isinstance(value_schema, dict):
            if "name" in value_schema:
                name += value_schema["name"]
            elif "type" in value_schema and isinstance(value_schema["type"], str):
                name += value_schema["type"]
        elif isinstance(value_schema, str):
            name += value_schema
        name = _clean_name(name)

        map_varname = self.new_variable(name)
        assign_stmt = Assign(
            targets=[Name(id=map_varname, ctx=Store())],
            value=DictLiteral(keys=[], values=[]),
        )
        statements.append(assign_stmt)

        # For each message in a block...
        for_each_message = []

        # ... read a string key...
        key_varname = self.new_variable("key")
        key_dest = Name(id=key_varname, ctx=Store())
        for_each_message.extend(self._gen_primitive_reader("string", src, key_dest))
        # ... and read the corresponding value.
        value_dest = Subscript(
            value=Name(id=map_varname, ctx=Load()),
            slice=Index(Name(id=key_varname, ctx=Load())),
            ctx=Store(),
        )
        for_each_message.extend(self._gen_reader(value_schema, src, value_dest))

        statements.extend(self._gen_block_reader(for_each_message, src))

        # Finally, assign our resulting map to the destination target.
        statements.append(
            Assign(
                targets=[dest],
                value=Name(id=map_varname, ctx=Load()),
            )
        )
        return statements

    def _gen_block_reader(self, for_each_message: List[stmt], src: Name) -> List[stmt]:
        """
        Returns a series of statements which represent iteration over an Avro record
        block, like are used for arrays and maps.

        Blocks are a series of records. The block is prefixed with a long that
        indicates the number of records in the block. A zero-length block
        indicates the end of the array or map.

        If a block's count is negative, its absolute value is used, and the
        count is followed immediately by a long block size indicating the number
        of bytes in the block

        for_each_message is a series of statements that will be injected and
        called for every message in the block.
        """
        statements: List[stmt] = []

        # Read the blocksize to figure out how many messages to read.
        blocksize_varname = self.new_variable("blocksize")
        blocksize_dest = Name(id=blocksize_varname, ctx=Store())
        statements.extend(self._gen_primitive_reader("long", src, blocksize_dest))

        # For each nonzero-sized block...
        while_loop = While(
            test=Compare(
                left=Name(id=blocksize_varname, ctx=Load()),
                ops=[NotEq()],
                comparators=[Constant(value=0)],
            ),
            body=[],
            orelse=[],
        )

        # ... handle negative block sizes...
        if_negative_blocksize = If(
            test=Compare(
                left=Name(id=blocksize_varname, ctx=Load()),
                ops=[Lt()],
                comparators=[Constant(value=0)],
            ),
            body=[],
            orelse=[],
        )
        flip_blocksize_sign = Assign(
            targets=[Name(id=blocksize_varname, ctx=Store())],
            value=UnaryOp(op=USub(), operand=Name(id=blocksize_varname, ctx=Load())),
        )
        if_negative_blocksize.body.append(flip_blocksize_sign)
        # Just discard the byte size of the block.
        read_a_long = Expr(
            Call(func=Name(id="read_long", ctx=Load()), args=[src], keywords=[])
        )
        if_negative_blocksize.body.append(read_a_long)
        while_loop.body.append(if_negative_blocksize)

        # Do a 'for _ in range(blocksize)' loop
        read_loop = For(
            target=Name(id="_", ctx=Store()),
            iter=Call(
                func=Name(id="range", ctx=Load()),
                args=[Name(id=blocksize_varname, ctx=Load())],
                keywords=[],
            ),
            body=for_each_message,
            orelse=[],
        )

        while_loop.body.append(read_loop)

        # If we've finished the block, read another long into blocksize.
        #
        # If it's zero, then we're done reading the array, and the loop test
        # will exit.
        #
        # If it's nonzero, then there are more messages to go.
        while_loop.body.extend(self._gen_primitive_reader("long", src, blocksize_dest))

        statements.append(while_loop)
        return statements

    def _gen_enum_reader(
        self, symbols: List[str], default: Optional[str], src: Name, dest: AST
    ) -> List[stmt]:
        statements: List[stmt] = []

        # Construct a literal dictionary which maps integers to symbols.
        enum_map = DictLiteral(keys=[], values=[])
        for i, sym in enumerate(symbols):
            enum_map.keys.append(Constant(value=i))
            enum_map.values.append(Constant(value=sym))

        # Call dict.get(read_long(src), default=default)
        call = Call(
            func=Attribute(
                value=enum_map,
                attr="get",
                ctx=Load(),
            ),
            args=[
                Call(
                    func=Name(id="read_long", ctx=Load()),
                    args=[src],
                    keywords=[],
                )
            ],
            keywords=[],
        )

        if default is not None:
            call.args.append(Constant(value=default))

        statements.append(
            Assign(
                targets=[dest],
                value=call,
            )
        )
        return statements

    def _gen_fixed_reader(self, size: int, src: Name, dest: AST) -> List[stmt]:
        # Call dest = src.read(size).
        if self.pure_python:
            read = Call(
                func=Attribute(value=src, attr="read_fixed", ctx=Load()),
                args=[Constant(value=size)],
                keywords=[],
            )
        else:
            read = Call(
                func=Attribute(value=src, attr="read", ctx=Load()),
                args=[Constant(value=size)],
                keywords=[],
            )

        return [
            Assign(
                targets=[dest],
                value=read,
            )
        ]

    def _gen_primitive_reader(
        self, primitive_type: str, src: Name, dest: AST
    ) -> List[stmt]:
        """
        Returns a sequence of statements which will deserialize a given primitive
        type from src into dest.
        """
        if primitive_type == "null":
            statement = Assign(
                targets=[dest],
                value=Constant(value=None),
            )
            return [statement]

        reader_func_name = PRIMITIVE_READERS[primitive_type]
        value = Call(
            func=Name(id=reader_func_name, ctx=Load()),
            args=[src],
            keywords=[],
        )
        statement = Assign(
            targets=[dest],
            value=value,
        )
        return [statement]

    def _gen_logical_reader(
        self, schema: Dict[str, Any], src: Name, dest: AST
    ) -> List[stmt]:
        try:
            lt = schema["logicalType"]
            if lt == "decimal":
                return self._gen_decimal_reader(schema, src, dest)
            if lt == "uuid":
                return self._gen_uuid_reader(schema, src, dest)
            if lt == "date":
                return self._gen_date_reader(schema, src, dest)
            if lt == "time-millis":
                return self._gen_time_millis_reader(schema, src, dest)
            if lt == "time-micros":
                return self._gen_time_micros_reader(schema, src, dest)
            if lt == "timestamp-millis":
                return self._gen_timestamp_millis_reader(schema, src, dest)
            if lt == "timestamp-micros":
                return self._gen_timestamp_micros_reader(schema, src, dest)
            raise LogicalTypeError("unknown logical type")
        except LogicalTypeError:
            # If a logical type is unknown, or invalid, then we should fall back
            # and use the underlying Avro type. We do this by clearing the
            # logicalType field of the schema and calling self._gen_reader.
            schema = schema.copy()
            del schema["logicalType"]
            return self._gen_reader(schema, src, dest)

    def _gen_decimal_reader(
        self, schema: Dict[str, Any], src: Name, dest: AST
    ) -> List[stmt]:
        scale = schema.get("scale", 0)
        precision = schema.get("precision", 0)
        if precision <= 0 or scale < 0 or scale > precision:
            raise LogicalTypeError("invalid decimal")

        statements: List[stmt] = []

        # Read the raw bytes. They can be either 'fixed' or 'bytes'
        raw_bytes_varname = self.new_variable("raw_decimal")
        raw_bytes_dest = Name(id=raw_bytes_varname, ctx=Store())
        if schema["type"] == "bytes":
            statements.extend(self._gen_primitive_reader("bytes", src, raw_bytes_dest))
        elif schema["type"] == "fixed":
            size: int = schema["size"]
            statements.extend(self._gen_fixed_reader(size, src, raw_bytes_dest))
        else:
            raise LogicalTypeError("unexpected type for decimal")

        # Parse the bytes.
        parse = Call(
            func=Name(id="read_decimal", ctx=Load()),
            args=[Name(id=raw_bytes_varname, ctx=Load())],
            keywords=[
                keyword(
                    arg="writer_schema",
                    value=DictLiteral(
                        keys=[Constant("precision"), Constant("scale")],
                        values=[Constant(precision), Constant(scale)],
                    ),
                )
            ],
        )
        statements.append(Assign(targets=[dest], value=parse))
        return statements

    def _gen_uuid_reader(
        self, schema: Dict[str, Any], src: Name, dest: AST
    ) -> List[stmt]:
        if schema["type"] != "string":
            raise LogicalTypeError("unexpected type for uuid")
        return self._call_fastavro_logical_reader("string", "read_uuid", src, dest)

    def _gen_date_reader(
        self, schema: Dict[str, Any], src: Name, dest: AST
    ) -> List[stmt]:
        if schema["type"] != "int":
            raise LogicalTypeError("unexpected type for date")
        return self._call_fastavro_logical_reader("int", "read_date", src, dest)

    def _gen_time_millis_reader(
        self, schema: Dict[str, Any], src: Name, dest: AST
    ) -> List[stmt]:
        if schema["type"] != "int":
            raise LogicalTypeError("unexpected type for time-millis")
        return self._call_fastavro_logical_reader("int", "read_time_millis", src, dest)

    def _gen_time_micros_reader(
        self, schema: Dict[str, Any], src: Name, dest: AST
    ) -> List[stmt]:
        if schema["type"] != "long":
            raise LogicalTypeError("unexpected type for time-micros")
        return self._call_fastavro_logical_reader("long", "read_time_micros", src, dest)

    def _gen_timestamp_millis_reader(
        self, schema: Dict[str, Any], src: Name, dest: AST
    ) -> List[stmt]:
        if schema["type"] != "long":
            raise LogicalTypeError("unexpected type for timestamp-millis")
        return self._call_fastavro_logical_reader(
            "long", "read_timestamp_millis", src, dest
        )

    def _gen_timestamp_micros_reader(
        self, schema: Dict, src: Name, dest: AST
    ) -> List[stmt]:
        if schema["type"] != "long":
            raise LogicalTypeError("unexpected type for timestamp-micros")
        return self._call_fastavro_logical_reader(
            "long", "read_timestamp_micros", src, dest
        )

    def _gen_recursive_reader_call(
        self, recursive_type_name: str, src: Name, dest: AST
    ) -> List[stmt]:
        funcname = self._named_type_reader_name(recursive_type_name)
        return [
            Assign(
                targets=[dest],
                value=Call(
                    func=Name(id=funcname, ctx=Load()),
                    args=[src],
                    keywords=[],
                ),
            )
        ]

    def _call_fastavro_logical_reader(
        self, primitive_type: str, parser: str, src: Name, dest: AST
    ) -> List[stmt]:
        """
        Read a value of primitive type from src, and then call parser on it,
        assigning into dest.
        """
        statements: List[stmt] = []
        # Read the raw value.
        raw_varname = self.new_variable("raw_" + primitive_type)
        raw_dest = Name(id=raw_varname, ctx=Store())
        statements.extend(self._gen_primitive_reader(primitive_type, src, raw_dest))

        # Call the fastavro parser for the logical type.
        parse = Call(
            func=Name(id=parser, ctx=Load()),
            args=[Name(id=raw_varname, ctx=Load())],
            keywords=[],
        )
        statements.append(Assign(targets=[dest], value=parse))
        return statements


def _rand_str(length: int) -> str:
    alphabet = "0123456789abcdef"
    return "".join(random.choices(alphabet, k=length))


def _clean_name(name: str) -> str:
    """
        Clean a name so it can be used as a python identifier.
    p"""
    if not re.match("[a-zA-Z_]", name[0]):
        name = "_" + name
    name = re.sub("[^0-9a-zA-Z_]+", "_", name)
    if all(c == "_" for c in name):
        name = "v"
    return name


class LogicalTypeError(Exception):
    pass
