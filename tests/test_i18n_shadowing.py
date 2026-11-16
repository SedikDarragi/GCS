"""Guard against local names hiding the ``t`` translation helper.

Python decides that a name is local to a function when it is assigned anywhere
in that function.  A loop such as ``for t in (...):`` therefore turns every
earlier ``t("key")`` call in the same function into an ``UnboundLocalError``.
"""

import ast
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
MODULES = ("main_app.py", "main.py", "database_maintenance.py", "accounts.py")


def _target_names(target):
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            yield from _target_names(element)
    elif isinstance(target, ast.Starred):
        yield from _target_names(target.value)


def _own_scope_nodes(function):
    """Yield nodes in *function* itself without entering nested scopes."""
    stack = list(function.body)
    while stack:
        node = stack.pop()
        if isinstance(
            node,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef),
        ):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _bound_names(nodes):
    names = set()
    for node in nodes:
        if isinstance(node, (ast.For, ast.AsyncFor)):
            names.update(_target_names(node.target))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                names.update(_target_names(target))
        elif isinstance(node, ast.AnnAssign):
            names.update(_target_names(node.target))
        elif isinstance(node, ast.AugAssign):
            names.update(_target_names(node.target))
        elif isinstance(node, ast.NamedExpr):
            names.update(_target_names(node.target))
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
    return names


def _shadowing_functions(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        nodes = list(_own_scope_nodes(node))
        if "t" not in _bound_names(nodes):
            continue
        calls_helper = any(
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id == "t"
            for child in nodes
        )
        if calls_helper:
            offenders.append(f"{path.name}:{node.lineno} {node.name}")
    return offenders


def test_translation_helper_is_never_shadowed():
    offenders = []
    for name in MODULES:
        path = BASE / name
        if path.exists():
            offenders.extend(_shadowing_functions(path))
    assert offenders == []
