import kide
from kide.cli import main


def test_package_has_version():
    assert kide.__version__


def test_cli_runs():
    assert main() == 0
