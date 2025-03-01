import builtins
import importlib
import inspect
import os
import re
import sys
import traceback
from ast import AST

from funcparserlib.parser import NoParseError

import hy.compiler
from hy._compat import PY3_11
from hy.errors import (
    HyLanguageError,
    HyMacroExpansionError,
    HyRequireError,
    HyTypeError,
)
from hy.model_patterns import whole
from hy.models import Expression, Symbol, as_model, is_unpack, replace_hy_obj
from hy.reader import mangle

EXTRA_MACROS = ["hy.core.result_macros", "hy.core.macros"]


def macro(name):
    """Decorator to define a macro called `name`."""
    return lambda fn: install_macro(name, fn, fn)


def reader_macro(name, fn):
    fn = rename_function(fn, name)
    fn.__globals__.setdefault("_hy_reader_macros", {})[name] = fn


def pattern_macro(names, pattern, shadow=None):
    pattern = whole(pattern)
    py_version_required = None
    if isinstance(names, tuple):
        py_version_required, names = names

    def dec(fn):
        def wrapper_maker(name):
            def wrapper(hy_compiler, *args):

                if shadow and any(is_unpack("iterable", x) for x in args):
                    # Try a shadow function call with this name instead.
                    return Expression(
                        [Expression(map(Symbol, [".", "hy", "pyops", name])), *args]
                    ).replace(hy_compiler.this)

                expr = hy_compiler.this

                if py_version_required and sys.version_info < py_version_required:
                    raise hy_compiler._syntax_error(
                        expr,
                        "`{}` requires Python {} or later".format(
                            name, ".".join(map(str, py_version_required))
                        ),
                    )

                try:
                    parse_tree = pattern.parse(args)
                except NoParseError as e:
                    raise hy_compiler._syntax_error(
                        expr[min(e.state.pos + 1, len(expr) - 1)],
                        "parse error for pattern macro '{}': {}".format(
                            name, e.msg.replace("end of input", "end of macro call")
                        ),
                    )
                return fn(hy_compiler, expr, name, *parse_tree)

            return wrapper

        for name in [names] if isinstance(names, str) else names:
            install_macro(name, wrapper_maker(name), fn)
        return fn

    return dec


def install_macro(name, fn, module_of):
    name = mangle(name)
    fn = rename_function(fn, name)
    module_of.__globals__.setdefault("_hy_macros", {})[name] = fn
    return fn


def _same_modules(source_module, target_module):
    """Compare the filenames associated with the given modules names.

    This tries to not actually load the modules.
    """
    if not (source_module or target_module):
        return False

    if target_module is source_module:
        return True

    def get_filename(module):
        if inspect.ismodule(module):
            return inspect.getfile(module)
        elif (
                (spec := importlib.util.find_spec(module)) and
                isinstance(spec.loader, importlib.machinery.SourceFileLoader)):
            return spec.loader.get_filename()

    try:
        return os.path.samefile(
            get_filename(source_module),
            get_filename(target_module))
    except (ValueError, TypeError, ImportError, FileNotFoundError):
        return False


def derive_target_module(target_module, parent_frame):
    if target_module is None:
        target_namespace = parent_frame.f_globals
        target_module = target_namespace.get("__name__", None)
    elif isinstance(target_module, str):
        target_module = importlib.import_module(target_module)
        target_namespace = target_module.__dict__
    elif inspect.ismodule(target_module):
        target_namespace = target_module.__dict__
    else:
        raise HyTypeError(
            "`target_module` is not a recognized type: {}".format(type(target_module))
        )
    return target_module, target_namespace


def import_module_from_string(module_name, package_module):
    package = None
    if module_name.startswith("."):
        source_dirs = module_name.split(".")
        target_dirs = getattr(package_module, "__name__", package_module).split(".")
        while len(source_dirs) > 1 and source_dirs[0] == "" and target_dirs:
            source_dirs.pop(0)
            target_dirs.pop()
        package = ".".join(target_dirs + source_dirs[:-1])
    try:
        return importlib.import_module(module_name, package)
    except ImportError as e:
        raise HyRequireError(e.args[0]).with_traceback(None)


def require_reader(source_module, target_module, assignments):
    target_module, target_namespace = derive_target_module(
        target_module, inspect.stack()[1][0]
    )

    if _same_modules(source_module, target_module):
        return False

    if not inspect.ismodule(source_module):
        source_module = import_module_from_string(source_module, target_module)

    source_macros = source_module.__dict__.setdefault("_hy_reader_macros", {})
    target_macros = target_namespace.setdefault("_hy_reader_macros", {})

    assignments = (
        source_macros.keys() if assignments == "ALL" else assignments
    )

    for name in assignments:
        if name in source_module._hy_reader_macros:
            target_macros[name] = source_macros[name]
        else:
            raise HyRequireError(f"Could not require name {name} from {source_module}")

    return True


def enable_readers(module, reader, names):
    _, namespace = derive_target_module(module, inspect.stack()[1][0])
    names = (
        namespace["_hy_reader_macros"].keys() if names == "ALL" else names
    )
    for name in names:
        if name not in namespace["_hy_reader_macros"]:
            raise NameError(f"reader {name} is not defined")
        reader.reader_macros[name] = namespace["_hy_reader_macros"][name]


def require(source_module, target, assignments, prefix="", target_module_name=None, compiler=None):
    """Load macros from a module. Return a `bool` indicating whether
    macros were actually transferred.

    - `target` can be a a string (naming a module), a module object,
      a dictionary, or `None` (meaning the calling module).
    - `assignments` can be "ALL", "EXPORTS", or a list of (macro
      name, alias) pairs."""

    if type(target) is dict:
        target_module = None
    else:
        target_module, target_namespace = derive_target_module(
            target, inspect.stack()[1][0]
        )
        # Let's do a quick check to make sure the source module isn't actually
        # the module being compiled (e.g. when `runpy` executes a module's code
        # in `__main__`).
        # We use the module's underlying filename for this (when they exist), since
        # it's the most "fixed" attribute.
        if _same_modules(source_module, target_module):
            return False

    if not inspect.ismodule(source_module):
        source_module = import_module_from_string(source_module,
           target_module_name or target_module or '')

    source_macros = source_module.__dict__.setdefault("_hy_macros", {})
    source_exports = getattr(
        source_module,
        "_hy_export_macros",
        [k for k in source_macros.keys() if not k.startswith("_")],
    )

    if not source_module._hy_macros:
        if assignments in ("ALL", "EXPORTS"):
            return False
        for name, alias in assignments:
            try:
                require(
                    f"{source_module.__name__}.{mangle(name)}",
                    target_module or target,
                    "ALL",
                    prefix=alias,
                )
            except HyRequireError as e:
                raise HyRequireError(
                    f"Cannot import name '{name}'"
                    f" from '{source_module.__name__}'"
                    f" ({source_module.__file__})"
                )
        return True

    target_macros = target_namespace.setdefault("_hy_macros", {}) if target_module else target

    if prefix:
        prefix += "."

    for name, alias in (
        assignments
        if assignments not in ("ALL", "EXPORTS")
        else (
            (k, k)
            for k in source_macros.keys()
            if assignments == "ALL" or k in source_exports
        )
    ):
        _name = mangle(name)
        if compiler:
            compiler.warn_on_core_shadow(prefix + alias)
        alias = mangle(prefix + alias)
        if _name in source_module._hy_macros:
            target_macros[alias] = source_macros[_name]
        else:
            raise HyRequireError(
                "Could not require name {} from {}".format(_name, source_module)
            )

    return True


def load_macros(module):
    """Load the hy builtin macros into module `module_name`,
    removing any prior macros set.
    It is an error to call this on any module in `hy.core`.
    """
    builtin_macros = EXTRA_MACROS
    module._hy_macros = {}
    module._hy_reader_macros = {}

    for builtin_mod_name in builtin_macros:
        builtin_mod = importlib.import_module(builtin_mod_name)

        # This may overwrite macros in the module.
        if hasattr(builtin_mod, "_hy_macros"):
            module._hy_macros.update(getattr(builtin_mod, "_hy_macros", {}))

        if hasattr(builtin_mod, "_hy_reader_macros"):
            module._hy_reader_macros.update(
                getattr(builtin_mod, "_hy_reader_macros", {})
            )


class MacroExceptions:
    """wrap non ``HyLanguageError``'s in ``HyMacroExpansionError`` preserving stack trace

    used in lieu of ``@contextmanager`` to ensure stack trace contains only internal hy
    modules for consistent filtering.
    """

    def __init__(self, module, macro_tree, compiler=None):
        self.module = module
        self.macro_tree = macro_tree
        self.compiler = compiler

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, exc_traceback):
        if exc_type is None:
            return True
        elif not issubclass(exc_type, HyLanguageError):
            if self.compiler:
                filename = self.compiler.filename
                source = self.compiler.source
            else:
                filename = None
                source = None

            exc_msg = "  ".join(
                traceback.format_exception_only(sys.exc_info()[0], sys.exc_info()[1])
            )

            msg = "expanding macro {}\n  ".format(str(self.macro_tree[0]))
            msg += exc_msg

            raise HyMacroExpansionError(msg, self.macro_tree, filename, source)
        else:
            return False


def macroexpand(tree, module, compiler=None, once=False, result_ok=True):
    """Expand the toplevel macros for the given Hy AST tree.

    Load the macros from the given `module`, then expand the (top-level) macros
    in `tree` until we no longer can. This doesn't work on local macros.

    `Expression` resulting from macro expansions are assigned the module in
    which the macro function is defined (determined using `inspect.getmodule`).
    If the resulting `Expression` is itself macro expanded, then the namespace
    of the assigned module is checked first for a macro corresponding to the
    expression's head/car symbol.  If the head/car symbol of such a `Expression`
    is not found among the macros of its assigned module's namespace, the
    outer-most namespace--e.g.  the one given by the `module` parameter--is used
    as a fallback.

    Args:
        tree (Union[Object, list]): Hy AST tree.
        module (Union[str, ModuleType]): Module used to determine the local
            namespace for macros.
        compiler (Optional[HyASTCompiler] ): The compiler object passed to
            expanded macros. Defaults to None
        once (bool): Only expand the first macro in `tree`. Defaults to False
        result_ok (bool): Whether or not it's okay to return a compiler `Result` instance.
            Defaults to True.

    Returns:
        Union[Object, Result]: A mutated tree with macros expanded.
    """
    if not inspect.ismodule(module):
        module = importlib.import_module(module)

    assert not compiler or compiler.module == module

    while isinstance(tree, Expression) and tree:

        fn = tree[0]
        if isinstance(fn, Expression) and fn and fn[0] == Symbol("."):
            fn = ".".join(map(mangle, fn[1:]))
        elif isinstance(fn, Symbol):
            fn = mangle(fn)
        else:
            break

        # Choose the first namespace with the macro.
        m = ((compiler and next(
                (d['macros'][fn]
                    for d in reversed(compiler.local_state_stack)
                    if fn in d['macros']),
                None)) or
            next(
                (mod._hy_macros[fn]
                    for mod in (module, builtins)
                    if fn in getattr(mod, "_hy_macros", ())),
                None))
        if not m:
            break

        with MacroExceptions(module, tree, compiler):
            if compiler:
                compiler.this = tree
            obj = m(compiler, *tree[1:])
            if isinstance(obj, (hy.compiler.Result, AST)):
                return obj if result_ok else tree

            tree = replace_hy_obj(obj, tree)

        if once:
            break

    tree = as_model(tree)
    return tree


def macroexpand_1(tree, module, compiler=None):
    """Expand the toplevel macro from `tree` once, in the context of
    `compiler`."""
    return macroexpand(tree, module, compiler, once=True)


def rename_function(f, new_name):
    """Create a copy of a function, but with a new name."""
    f = type(f)(
        f.__code__.replace(
            co_name=new_name,
            **(
                {
                    "co_qualname": re.sub(
                        r"\.[^.+]\Z", "." + new_name, f.__code__.co_qualname
                    )
                    if "." in f.__code__.co_qualname
                    else new_name
                }
                if PY3_11
                else {}
            ),
        ),
        f.__globals__,
        str(new_name),
        f.__defaults__,
        f.__closure__,
    )
    f.__dict__.update(f.__dict__)
    return f
