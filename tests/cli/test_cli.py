import json
from argparse import ArgumentParser, ArgumentTypeError
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.cli.helpers import (
    init,
    list_projects_branches_states,
    list_projects_json,
    load_config,
    load_project,
    parse_arguments,
    parse_llm_endpoint,
    parse_llm_key,
    show_config,
)
from core.cli.main import async_main
from core.config import Config, LLMProvider, loader


def write_test_config(tmp_path):
    cfg = {
        "fs": {"workspace_root": str(tmp_path)},
        "db": {"url": f"sqlite+aiosqlite:///{tmp_path.as_posix()}/test.db"},
    }
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(cfg), encoding="utf-8")
    return config_file


@patch("core.cli.helpers.ArgumentParser")
def test_parse_arguments(mock_ArgumentParser):
    parser = mock_ArgumentParser.return_value

    parse_arguments()

    flags = set(call[0][0] for call in parser.add_argument.call_args_list)
    assert flags == {
        "--config",
        "--show-config",
        "--level",
        "--database",
        "--local-ipc-port",
        "--local-ipc-host",
        "--enable-api-server",
        "--local-api-server-host",
        "--local-api-server-port",
        "--version",
        "--list",
        "--list-json",
        "--project",
        "--delete",
        "--branch",
        "--step",
        "--project-state-id",
        "--llm-endpoint",
        "--llm-key",
        "--import-v0",
        "--email",
        "--extension-version",
        "--use-git",
        "--access-token",
        "--no-auto-confirm-breakdown",
        "--initial-prompt",
    }

    parser.parse_args.assert_called_once_with()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("openai:https://api.openai.com", (LLMProvider.OPENAI, "https://api.openai.com")),
        ("noprovider:https://example.com", ArgumentTypeError),
        ("https://example.com", ArgumentTypeError),
        ("whatever", ArgumentTypeError),
    ],
)
def test_parse_llm_endpoint(value, expected):
    if isinstance(expected, type) and issubclass(expected, Exception):
        with pytest.raises(expected):
            parse_llm_endpoint(value)
    else:
        parsed_args = parse_llm_endpoint(value)
        assert parsed_args == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("openai:sk-abc", (LLMProvider.OPENAI, "sk-abc")),
        ("noprovider:fake-secret-key", ArgumentTypeError),
        ("sk-abc", ArgumentTypeError),
    ],
)
def test_parse_llm_key(value, expected):
    if isinstance(expected, type) and issubclass(expected, Exception):
        with pytest.raises(expected):
            parse_llm_key(value)
    else:
        parsed_args = parse_llm_key(value)
        assert parsed_args == expected


@patch("core.cli.helpers.import_from_dotenv")
def test_load_config_not_found(mock_import_from_dotenv, tmp_path, capsys):
    config_file = tmp_path / "config.json"
    mock_import_from_dotenv.return_value = False
    config = load_config(MagicMock(config=config_file))

    assert config == Config()
    captured = capsys.readouterr()
    assert f"Configuration file not found: {config_file}" in captured.err
    mock_import_from_dotenv.assert_called_once_with(config_file)


def test_load_config_not_json(tmp_path, capsys):
    config_file = tmp_path / "config.json"
    config_file.write_text("not really a JSON file", encoding="utf-8")

    config = load_config(MagicMock(config=config_file))

    assert config is None
    captured = capsys.readouterr()
    assert f"Error parsing config file {config_file}" in captured.err


def test_load_config_defaults(tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text("{}", encoding="utf-8")

    config = load_config(MagicMock(config=config_file, level=None, database=None, local_ipc_port=None))

    assert config.log.level == "DEBUG"
    assert config.db.url == "sqlite+aiosqlite:///data/database/pythagora.db"
    assert config.ui.type == "plain"


def test_load_config_overridden(tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text("{}", encoding="utf-8")

    args = MagicMock(
        config=config_file,
        level="warning",
        database="postgresql+asyncpg://localhost/mydb",
        local_ipc_port=1234,
        local_ipc_host="localhost",
        llm_endpoint=[(LLMProvider.OPENAI, "https://test.openai.com")],
        llm_key=[(LLMProvider.ANTHROPIC, "sk-test")],
    )
    config = load_config(args)

    assert config.log.level == "WARNING"
    assert config.db.url == "postgresql+asyncpg://localhost/mydb"
    assert config.ui.type == "ipc-client"
    assert config.ui.port == 1234
    assert config.llm[LLMProvider.OPENAI].base_url == "https://test.openai.com"
    assert config.llm[LLMProvider.ANTHROPIC].api_key == "sk-test"


def test_show_default_config(capsys):
    loader.config = Config()
    show_config()
    captured = capsys.readouterr()
    assert Config.model_validate_json(captured.out) == Config()


@pytest.mark.asyncio
@patch("core.cli.helpers.StateManager")
async def test_list_projects_json(mock_StateManager, capsys):
    sm = mock_StateManager.return_value

    project_id1 = MagicMock(hex="abcd")
    project_id2 = MagicMock(hex="efgh")

    created_at1 = datetime(2021, 1, 1)
    created_at2 = datetime(2021, 1, 2)

    projects_data = [
        (project_id1, "project1", created_at1, "folder1"),
        (project_id2, "project2", created_at2, "folder2"),
    ]

    sm.list_projects = AsyncMock(return_value=projects_data)
    await list_projects_json(None)

    mock_StateManager.assert_called_once_with(None)
    sm.list_projects.assert_awaited_once_with()

    captured = capsys.readouterr().out
    data = json.loads(captured)

    assert data == [
        {"id": "abcd", "name": "project1", "folder_name": "folder1", "updated_at": "2021-01-01T00:00:00"},
        {"id": "efgh", "name": "project2", "folder_name": "folder2", "updated_at": "2021-01-02T00:00:00"},
    ]


@pytest.mark.asyncio
@patch("core.cli.helpers.StateManager")
async def test_list_projects(mock_StateManager, capsys):
    sm = mock_StateManager.return_value

    branch = MagicMock(
        id="1234",
        states=[
            MagicMock(step_index=1),
            MagicMock(step_index=2),
        ],
    )
    branch.name = "branch1"

    project = MagicMock(
        id="abcd",
        project_type="node",
        branches=[branch],
    )
    project.name = "project1"

    sm.list_projects_with_branches_states = AsyncMock(return_value=[project])
    await list_projects_branches_states(None)

    mock_StateManager.assert_called_once_with(None)
    sm.list_projects_with_branches_states.assert_awaited_once_with()

    data = capsys.readouterr().out

    assert "* project1 (abcd)" in data
    assert "- branch1 (1234)" in data


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "kwargs", "retval", "should_succeed"),
    [
        (["abc", None, None, None], dict(project_id="abc", step_index=None, project_state_id=None), MagicMock(), True),
        (["abc", None, None, None], dict(project_id="abc", step_index=None, project_state_id=None), None, False),
        (["abc", "def", None, None], dict(branch_id="def", step_index=None, project_state_id=None), MagicMock(), True),
        (["abc", "def", None, None], dict(branch_id="def", step_index=None, project_state_id=None), None, False),
        (["abc", None, 123, None], dict(project_id="abc", step_index=123, project_state_id=None), MagicMock(), True),
        (["abc", "def", 123, None], dict(branch_id="def", step_index=123, project_state_id=None), None, False),
        (
            ["abc", None, None, "xyz"],
            dict(project_id="abc", step_index=None, project_state_id="xyz"),
            MagicMock(),
            True,
        ),
        (["abc", "def", None, "xyz"], dict(branch_id="def", step_index=None, project_state_id="xyz"), None, False),
    ],
)
async def test_load_project(args, kwargs, retval, should_succeed, capsys):
    sm = MagicMock(load_project=AsyncMock(return_value=retval))

    result = await load_project(sm, *args)

    if should_succeed:
        assert result is not None
    else:
        assert result is None
        data = capsys.readouterr().err
        assert "not found" in data

    sm.load_project.assert_awaited_once_with(**kwargs)


def test_init(tmp_path):
    config_file = write_test_config(tmp_path)

    class MockArgumentParser(ArgumentParser):
        def parse_args(self):
            return super().parse_args(["--config", str(config_file)])

    with patch("core.cli.helpers.ArgumentParser", new=MockArgumentParser):
        ui, db, args = init()

    assert ui is not None
    assert db is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "run_orchestrator", "retval"),
    [
        (["--list"], False, True),
        (["--list-json"], False, True),
        (["--show-config"], False, True),
        (["--project", "ca7a0cc9-767f-472a-aefb-0c8d3377c9bc"], False, False),
        (["--branch", "ca7a0cc9-767f-472a-aefb-0c8d3377c9bc"], False, False),
        (["--step", "123"], False, False),
        (["--project-state", "ca7a0cc9-767f-472a-aefb-0c8d3377c9bc"], False, False),
        ([], True, True),
    ],
)
@patch("core.cli.main.Orchestrator")
async def test_main(mock_Orchestrator, args, run_orchestrator, retval, tmp_path):
    config_file = write_test_config(tmp_path)

    class MockArgumentParser(ArgumentParser):
        def parse_args(self):
            return super().parse_args(["--config", str(config_file)] + args)

    with patch("core.cli.helpers.ArgumentParser", new=MockArgumentParser):
        ui, db, args = init()

    # Create a mock with a string value for the button attribute
    mock_response = MagicMock()
    mock_response.text = "test"
    mock_response.cancelled = False
    # Set a string value for the button attribute
    mock_response.button = "node"  # or whatever valid project type you want to use

    ui.ask_question = AsyncMock(return_value=mock_response)

    mock_orca = mock_Orchestrator.return_value
    mock_orca.run = AsyncMock(return_value=True)

    success = await async_main(ui, db, args)
    assert success is retval

    if run_orchestrator:
        assert ui.ask_question.call_count == 1
        mock_Orchestrator.assert_called_once()
        mock_orca.run.assert_awaited_once_with()


@pytest.mark.asyncio
@patch("core.cli.main.Orchestrator")
async def test_main_handles_crash(mock_Orchestrator, tmp_path):
    config_file = write_test_config(tmp_path)

    class MockArgumentParser(ArgumentParser):
        def parse_args(self):
            return super().parse_args(["--config", str(config_file)])

    with patch("core.cli.helpers.ArgumentParser", new=MockArgumentParser):
        ui, db, args = init()

    # Create a mock response with a string value for the button attribute
    mock_response = MagicMock(text="test", cancelled=False)
    mock_response.button = "test_project_type"  # Set a string value for project_type
    ui.ask_question = AsyncMock(return_value=mock_response)
    ui.send_fatal_error = AsyncMock()

    mock_orca = mock_Orchestrator.return_value
    mock_orca.run = AsyncMock(side_effect=RuntimeError("test error"))

    success = await async_main(ui, db, args)

    assert success is False
    ui.send_fatal_error.assert_called_once()
    assert "test error" in ui.send_fatal_error.call_args[0][0]
