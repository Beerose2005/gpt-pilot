import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from core.cli.helpers import list_projects_json
from core.db.models import Branch, Project
from core.state.state_manager import StateManager

from .factories import create_project_state


@pytest.mark.asyncio
async def test_get_by_id_requires_valid_uuid(testdb):
    with pytest.raises(ValueError):
        await Project.get_by_id(testdb, "invalid-uuid")


@pytest.mark.asyncio
async def test_get_by_id_no_match(testdb):
    fake_id = uuid4().hex
    result = await Project.get_by_id(testdb, fake_id)
    assert result is None


@pytest.mark.asyncio
async def test_get_by_id(testdb):
    project = Project(name="test", project_type="node")
    testdb.add(project)
    await testdb.commit()

    p = await Project.get_by_id(testdb, project.id)
    assert p == project


@pytest.mark.asyncio
async def test_delete_by_id(testdb):
    project = Project(name="test", project_type="node")
    testdb.add(project)
    await testdb.commit()

    await Project.delete_by_id(testdb, project.id)
    await testdb.commit()
    assert await Project.get_by_id(testdb, project.id) is None


@pytest.mark.asyncio
async def test_get_branch_no_match(testdb):
    project = Project(name="test", project_type="node")
    testdb.add(project)
    await testdb.commit()

    b = await project.get_branch()
    assert b is None


@pytest.mark.asyncio
async def test_get_branch(testdb):
    project = Project(name="test", project_type="node")
    branch = Branch(project=project)
    testdb.add(project)
    testdb.add(branch)
    await testdb.commit()

    b = await project.get_branch()
    assert b == branch


@pytest.mark.asyncio
async def test_get_branch_no_session():
    project = Project(name="test", project_type="node")

    with pytest.raises(ValueError):
        await project.get_branch()


@pytest.mark.asyncio
async def test_get_all_projects(testdb, capsys):
    state1 = create_project_state(project_name="Test Project 1")
    state2 = create_project_state(project_name="Test Project 2")

    testdb.add(state1)
    testdb.add(state2)
    await testdb.commit()  # Ensure changes are committed

    # Set folder names for the test
    folder_name1 = "folder1"
    folder_name2 = "folder2"

    sm = StateManager(testdb)
    sm.list_projects = AsyncMock(
        return_value=[
            (
                MagicMock(hex=state1.branch.project.id.hex),
                state1.branch.project.name,
                datetime(2021, 1, 1),
                folder_name1,
            ),
            (
                MagicMock(hex=state2.branch.project.id.hex),
                state2.branch.project.name,
                datetime(2021, 1, 2),
                folder_name2,
            ),
        ]
    )

    with patch("core.cli.helpers.StateManager", return_value=sm):
        await list_projects_json(testdb)

    captured = capsys.readouterr()
    data = json.loads(captured.out)

    expected_output = [
        {
            "id": state1.branch.project.id.hex,
            "name": "Test Project 1",
            "folder_name": folder_name1,
            "updated_at": "2021-01-01T00:00:00",
        },
        {
            "id": state2.branch.project.id.hex,
            "name": "Test Project 2",
            "folder_name": folder_name2,
            "updated_at": "2021-01-02T00:00:00",
        },
    ]

    assert data == expected_output


@pytest.mark.asyncio
async def test_default_folder_name(testdb):
    project = Project(name="test project", project_type="node")
    testdb.add(project)
    await testdb.commit()

    assert project.folder_name == "test-project"


@pytest.mark.parametrize(
    ("project_name", "expected_folder_name"),
    [
        ("Test", "test"),
        ("with space", "with-space"),
        ("with   many   spaces", "with-many-spaces"),
        ("w00t? with,interpunction!", "w00t-with-interpunction"),
        ("With special / * and ☺️ emojis", "with-special-and-emojis"),
        ("Šašavi niño & mädchen", "sasavi-nino-madchen"),
    ],
)
def test_get_folder_from_project_name(project_name, expected_folder_name):
    folder_name = Project.get_folder_from_project_name(project_name)
    assert folder_name == expected_folder_name
