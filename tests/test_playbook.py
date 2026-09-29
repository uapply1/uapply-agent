"""The packaged playbook: server instructions, prompts and the generated Claude Code plugin."""
from importlib.resources import files

import pytest

from uapply_agent import playbook


def test_playbook_sections_load():
    assert "case_status" in playbook.instructions()
    assert playbook.prompt("run") and playbook.prompt("status")
    with pytest.raises(KeyError):
        playbook.prompt("missing")


def test_playbook_ships_inside_the_package():
    assert files("uapply_agent").joinpath("SOURCE.md").is_file()


def test_every_prompt_has_a_section_and_a_plugin_command():
    generated = playbook.plugin_files("1.0.0")
    for name, description in playbook.PROMPTS.items():
        assert playbook.prompt(name)
        assert generated[f"commands/{name}.md"].startswith(f"---\ndescription: {description}\n---")
