"""Runs pytest inside PyStudio's debugger (Test Explorer > Debug)."""
import os
import sys

sys.path.insert(0, os.getcwd())     # the project folder, so tests can import its modules
import pytest  # noqa: E402

sys.exit(pytest.main(sys.argv[1:]))
