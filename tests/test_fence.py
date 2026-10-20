"""Tests for the fence a rollout runs under.

A rollout is a program being run against someone's repository with the
operator's authority. Three things could leave the workspace without
touching a path -- the operator's MCP servers, Code Mode's route to those
servers, and OpenCode's own session and worktree tools. Each is denied
here, and the denial is pinned so it cannot quietly disappear.

These tests build the configuration the runtime would use; they do not
call a model.
"""

import json

from harness2.substrate.opencode import opencode_command


def fence_env(workspace, allow_mcp=False):
    command = opencode_command(
        prompt="p",
        workspace=workspace,
        model="mistral/mistral-large-latest",
        allow_mcp=allow_mcp,
    )
    return json.loads(command.env["OPENCODE_CONFIG_CONTENT"])


def rules(document, action):
    return [r for r in document["permissions"] if r["action"] == action]


def test_the_inherited_mcp_servers_are_not_inherited(tmp_path):
    """A rollout must not inherit the operator's MCP servers.

    Those servers may hold credentials (a GitHub token, a private API), and
    they are reachable from the rollout with no path involved at all.
    """
    document = fence_env(tmp_path)
    assert document["mcp"] == {"servers": {}}


def test_code_mode_is_denied(tmp_path):
    """Code Mode is a second route to every MCP tool, so it is denied too."""
    document = fence_env(tmp_path)
    denies = rules(document, "execute")
    assert denies and all(r["effect"] == "deny" for r in denies)


def test_paths_outside_the_workspace_are_denied(tmp_path):
    document = fence_env(tmp_path)
    denies = rules(document, "external_directory")
    assert denies and all(r["effect"] == "deny" for r in denies)


def test_the_denial_is_not_a_prompt(tmp_path):
    """A denial, not an "ask": nothing is there to answer the prompt."""
    document = fence_env(tmp_path)
    for rule in document["permissions"]:
        assert rule["effect"] == "deny", rule


def test_opencodes_own_session_and_worktree_tools_are_denied(tmp_path):
    """These cross the workspace boundary without touching a path.

    ``worktree_create`` registers a worktree of whatever repository the
    session belongs to; ``session_move`` relocates the session. Together
    they are how a rollout ends up writing into a project it was never
    given.
    """
    document = fence_env(tmp_path)
    for tool in ("opencode_worktree_create", "opencode_session_move"):
        denies = rules(document, tool)
        assert denies, f"{tool} is not denied"
        assert all(r["effect"] == "deny" for r in denies)


def test_allowing_mcp_keeps_the_path_fence(tmp_path):
    """Opting into MCP gives up the servers, not the path boundary."""
    document = fence_env(tmp_path, allow_mcp=True)
    assert "mcp" not in document
    denies = rules(document, "external_directory")
    assert denies and all(r["effect"] == "deny" for r in denies)


def test_allowing_mcp_still_denies_the_escape_hatch(tmp_path):
    """OpenCode's own tools are not one of the operator's integrations."""
    document = fence_env(tmp_path, allow_mcp=True)
    for tool in ("opencode_worktree_create", "opencode_session_move"):
        assert rules(document, tool), f"{tool} is not denied"


def test_the_fence_is_valid_json_with_the_shape_v2_expects(tmp_path):
    document = fence_env(tmp_path)
    assert set(document) == {"mcp", "permissions"}
    for rule in document["permissions"]:
        assert set(rule) == {"action", "resource", "effect"}
        assert isinstance(rule["resource"], str)
