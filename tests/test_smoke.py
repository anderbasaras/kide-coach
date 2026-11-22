import pytest

import kide
from kide.cli import main


def test_package_has_version():
    assert kide.__version__


def test_cli_requires_a_command(capsys):
    with pytest.raises(SystemExit) as info:
        main([])
    assert info.value.code == 2
    assert "usage: kide" in capsys.readouterr().err
