"""Command-line entry point for the raw TCP data collector."""

import logging

from .config import Config
from .server import run_server


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    run_server(Config.from_environ())


if __name__ == "__main__":
    main()
