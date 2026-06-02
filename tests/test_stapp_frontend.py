"""Regression tests for the Streamlit frontend command wiring."""

from __future__ import annotations

import ast
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _stapp_tree() -> ast.Module:
    source = (PROJECT_ROOT / "zero_agent" / "frontends" / "stapp.py").read_text()
    return ast.parse(source)


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function not found: {name}")


def _is_startswith_btw(test: ast.expr) -> bool:
    return _is_startswith_literal(test, "/btw")


def _is_startswith_literal(test: ast.expr, literal: str) -> bool:
    if not isinstance(test, ast.Call):
        return False
    func = test.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "startswith"
        and isinstance(func.value, ast.Name)
        and func.value.id == "cmd"
        and len(test.args) == 1
        and isinstance(test.args[0], ast.Constant)
        and test.args[0].value == literal
    )


def test_stapp_btw_uses_agent_runner_adapter() -> None:
    handle_cmd = _function(_stapp_tree(), "_handle_slash_cmd")
    btw_branch = next(
        (
            node
            for node in ast.walk(handle_cmd)
            if isinstance(node, ast.If) and _is_startswith_btw(node.test)
        ),
        None,
    )
    assert btw_branch is not None

    btw_calls = [
        node
        for node in ast.walk(btw_branch)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "btw_handle"
    ]

    assert len(btw_calls) == 1
    assert isinstance(btw_calls[0].args[0], ast.Name)
    assert btw_calls[0].args[0].id == "runner"


def test_stapp_continue_uses_agent_runner_adapter() -> None:
    handle_cmd = _function(_stapp_tree(), "_handle_slash_cmd")
    continue_branch = next(
        (
            node
            for node in ast.walk(handle_cmd)
            if isinstance(node, ast.If) and _is_startswith_literal(node.test, "/continue")
        ),
        None,
    )
    assert continue_branch is not None

    calls = [
        node
        for node in ast.walk(continue_branch)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "handle_frontend_command"
    ]

    assert len(calls) == 1
    assert isinstance(calls[0].args[0], ast.Name)
    assert calls[0].args[0].id == "runner"


def test_stapp_export_uses_agent_runner_adapter() -> None:
    handle_cmd = _function(_stapp_tree(), "_handle_slash_cmd")
    export_branch = next(
        (
            node
            for node in ast.walk(handle_cmd)
            if isinstance(node, ast.If) and _is_startswith_literal(node.test, "/export")
        ),
        None,
    )
    assert export_branch is not None

    calls = [
        node
        for node in ast.walk(export_branch)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "last_assistant_text"
    ]

    assert len(calls) == 1
    assert isinstance(calls[0].args[0], ast.Name)
    assert calls[0].args[0].id == "runner"


def test_stapp_session_history_uses_agent_runner_adapter() -> None:
    history_renderer = _function(_stapp_tree(), "_render_session_history")
    calls = [
        node
        for node in ast.walk(history_renderer)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "handle_frontend_command"
    ]

    assert len(calls) == 1
    assert isinstance(calls[0].args[0], ast.Name)
    assert calls[0].args[0].id == "runner"
