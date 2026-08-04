"""The CLI-to-MCP contract, enforced.

SPEC section 2 says eight rules, and that breaking any of them turns the MCP
port into a rewrite. Rules are only rules if something checks them, so this
file checks them: naming, annotations, layering, and the promise that every
behaviour reachable from the CLI is reachable from MCP.

None of these tests touch the network or a real snapshot.
"""

from __future__ import annotations

import asyncio
import inspect as pyinspect

import pytest
import typer

from stl_transit.cli import main as cli
from stl_transit.core import service
from stl_transit.mcp import server


@pytest.fixture(scope="module")
def tools():
    return asyncio.run(server.mcp.list_tools())


@pytest.fixture(scope="module")
def by_name(tools):
    return {t.name: t for t in tools}


# ------------------------------------------------------- 2.2 name mapping --

def test_every_tool_uses_the_stl_prefix(tools):
    """SPEC 2.2: this server will sit alongside others in one client. An
    unprefixed `departures` collides with every other transit tool a user
    might install, and the model picks whichever it saw last."""
    assert tools, "no tools registered at all"
    offenders = [t.name for t in tools if not t.name.startswith("stl_")]
    assert offenders == []


def test_tool_names_are_lowercase_snake_case(tools):
    import re

    bad = [t.name for t in tools if not re.fullmatch(r"stl_[a-z0-9_]+", t.name)]
    assert bad == []


def test_tool_names_are_unique(tools):
    names = [t.name for t in tools]
    assert len(names) == len(set(names))


def test_every_tool_resolves_to_a_core_function(by_name):
    """SPEC 2.1: mcp/ is a registration file over core/, nothing more. A tool
    whose logic lives in the wrapper is a behaviour the CLI cannot reach."""
    from stl_transit.core import oracle

    # The oracle group is the one place the tool name and the core function
    # name diverge, because `oracle.cases` would shadow the CASES constant.
    oracle_map = {
        "stl_oracle_cases": "list_cases",
        "stl_oracle_generate": "generate",
        "stl_oracle_verify": "verify",
    }
    for name in by_name:
        if name in oracle_map:
            assert hasattr(oracle, oracle_map[name]), name
            continue
        stem = name[len("stl_"):]
        assert hasattr(service, stem), f"{name} has no core counterpart named {stem}"


# --------------------------------------------------------- 2.3 annotations --

def test_every_tool_carries_all_four_annotations(by_name):
    """A client that cannot tell a read from a write cannot protect the user
    from an agent that guessed wrong."""
    for name, tool in by_name.items():
        ann = tool.model_dump().get("annotations")
        assert ann, f"{name} has no annotations"
        for hint in ("read_only_hint", "destructive_hint",
                     "idempotent_hint", "open_world_hint"):
            assert ann.get(hint) is not None, f"{name} is missing {hint}"


def test_nothing_in_this_server_is_destructive(by_name):
    """Every tool here reads a feed or writes a generated artifact. If one ever
    becomes genuinely destructive, this test should fail and make someone
    think about it rather than let the annotation quietly drift."""
    for name, tool in by_name.items():
        assert tool.model_dump()["annotations"]["destructive_hint"] is False, name


def test_network_tools_are_marked_open_world(by_name):
    """Anything that leaves the machine must say so: it is the difference
    between a cached answer and a request against a public agency's servers."""
    for name in ("stl_snapshot_fetch", "stl_web_capture"):
        ann = by_name[name].model_dump()["annotations"]
        assert ann["open_world_hint"] is True, name
        assert ann["read_only_hint"] is False, name


def test_read_only_tools_are_not_marked_open_world(by_name):
    reads_local = ["stl_gtfs_departures", "stl_gtfs_query", "stl_assert_run",
                   "stl_diff_summary", "stl_report_brief", "stl_support_repro"]
    for name in reads_local:
        ann = by_name[name].model_dump()["annotations"]
        assert ann["read_only_hint"] is True, name
        assert ann["open_world_hint"] is False, name


# ---------------------------------------------------------------- schemas --

def test_every_tool_has_an_input_schema(by_name):
    for name, tool in by_name.items():
        schema = tool.model_dump().get("input_schema")
        assert schema and schema.get("type") == "object", f"{name} has no input schema"


def test_every_tool_has_a_description_worth_reading(by_name):
    """Tool selection accuracy is a function of description quality. A one-line
    description on a 45-tool server is how the model picks the wrong one."""
    for name, tool in by_name.items():
        desc = (tool.description or "").strip()
        assert len(desc) >= 60, f"{name} description is too thin: {desc!r}"


def test_tools_with_arguments_document_them(by_name):
    """An argument the model cannot interpret is an argument it will guess."""
    for name, tool in by_name.items():
        props = tool.model_dump()["input_schema"].get("properties") or {}
        interesting = [p for p in props if p not in ("snapshot", "limit", "offset")]
        if not interesting:
            continue
        desc = tool.description or ""
        assert "Args:" in desc, f"{name} takes {interesting} but documents no arguments"


# ---------------------------------------------------------------- layering --

def test_core_never_imports_the_cli_or_the_server():
    """SPEC 2.1: cli/ and mcp/ are siblings, both thin, neither importing the
    other, and core/ importing either would invert the whole arrangement."""
    import pathlib

    core = pathlib.Path(service.__file__).parent
    offenders = []
    for path in core.rglob("*.py"):
        text = path.read_text()
        if "from ..cli" in text or "from ..mcp" in text or "import cli" in text:
            offenders.append(path.name)
    assert offenders == []


def test_core_service_never_prints():
    """SPEC 2.1: core never prints, exits, or prompts. A print in core is
    output the MCP client cannot see and the CLI cannot format."""
    import pathlib
    import re

    path = pathlib.Path(service.__file__)
    lines = [
        (n, line) for n, line in enumerate(path.read_text().splitlines(), 1)
        if re.match(r"\s*(print\(|sys\.exit|input\()", line)
    ]
    assert lines == [], f"core/service.py has console side effects at {lines}"


# -------------------------------------------------------------- CLI parity --

CLI_GROUPS = ["snapshot", "gtfs", "rt", "oracle", "support",
              "assert", "diff", "web", "bundle", "report"]


def test_every_group_is_registered_on_the_cli():
    registered = {g.name for g in cli.app.registered_groups}
    assert set(CLI_GROUPS) <= registered, f"missing: {set(CLI_GROUPS) - registered}"


@pytest.mark.parametrize("group", CLI_GROUPS)
def test_each_cli_group_has_commands(group):
    found = next(g for g in cli.app.registered_groups if g.name == group)
    assert found.typer_instance.registered_commands, f"{group} has no commands"


def test_mcp_surface_is_a_subset_of_what_the_cli_can_do(by_name):
    """SPEC 2.1 again: 'if a behaviour exists only in cli/, MCP won't have it.'
    The converse is the failure this catches -- a tool exposed over MCP that
    has no CLI equivalent cannot be debugged from a terminal, which is where
    it will need debugging."""
    cli_commands = set()
    for group in cli.app.registered_groups:
        for cmd in group.typer_instance.registered_commands:
            name = (cmd.name or cmd.callback.__name__).replace("-", "_")
            cli_commands.add(f"{group.name}_{name}")
    for cmd in cli.app.registered_commands:
        cli_commands.add((cmd.name or cmd.callback.__name__).replace("-", "_"))

    # Known, deliberate aliases where the CLI verb and the tool noun differ.
    aliases = {
        "stl_rt_schema_census": "rt_schema",
        "stl_gtfs_stop_resolve": "gtfs_stop_resolve",
        "stl_snapshot_sources": "snapshot_sources",
        "stl_support_diff_device": "support_diff_device",
    }
    missing = []
    for name in by_name:
        stem = aliases.get(name, name[len("stl_"):])
        if stem not in cli_commands:
            missing.append(name)
    assert missing == [], f"MCP tools with no CLI equivalent: {missing}"


# ------------------------------------------------------- error containment --

def test_call_wrapper_returns_structured_errors_not_tracebacks():
    """SPEC 5: every error carries a remedy. In MCP that is the difference
    between the model recovering and the model giving up."""
    from stl_transit.errors import StopNotFound

    def boom(**_):
        raise StopNotFound("nope", remedy="try stl gtfs stops --search")

    out = server._call(boom)
    assert '"ok": false' in out
    assert "STOP_NOT_FOUND" in out
    assert "remedy" in out
    assert "Traceback" not in out


def test_call_wrapper_contains_unexpected_exceptions_too():
    """An unhandled exception must not reach the client as a traceback: the
    model cannot act on a stack trace, and it may carry local paths."""
    def boom(**_):
        raise ZeroDivisionError("division by zero")

    out = server._call(boom)
    assert '"ok": false' in out
    assert "UNEXPECTED" in out
    assert "remedy" in out


def test_every_tool_is_an_async_callable():
    for name in dir(server):
        fn = getattr(server, name)
        if name.startswith("stl_") and callable(fn):
            assert pyinspect.iscoroutinefunction(fn), f"{name} is not async"


def test_server_instructions_orient_a_cold_start():
    """45 tools is a lot to choose between. The instructions carry a
    question-to-tool index precisely so the model does not have to read all 45
    descriptions to find the entry point."""
    text = server.INSTRUCTIONS
    assert "WHERE TO START" in text
    for anchor in ("stl_report_brief", "stl_assert_run", "stl_gtfs_query",
                   "stl_gtfs_stop_resolve", "24:12:00"):
        assert anchor in text, f"instructions never mention {anchor}"
