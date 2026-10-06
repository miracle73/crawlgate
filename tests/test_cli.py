"""Every CLI command must at least import and render help (catches syntax errors the unit tests never load)."""

import pytest
from typer.testing import CliRunner

from crawlgate.cli import app


@pytest.mark.parametrize("args", [["--help"], ["crawl", "--help"], ["check", "--help"], ["report", "--help"],
                                  ["baseline", "update", "--help"], ["propose", "--help"]])
def test_cli_help(args: list[str]) -> None:
    assert CliRunner().invoke(app, args).exit_code == 0
