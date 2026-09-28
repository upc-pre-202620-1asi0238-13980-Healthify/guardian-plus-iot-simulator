"""Entrypoint del simulador. Equivale a `python simulator/cli.py serve`."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cli import main as cli_main  # noqa: E402


def main():
    cli_main(["serve", *sys.argv[1:]])


if __name__ == "__main__":
    main()
