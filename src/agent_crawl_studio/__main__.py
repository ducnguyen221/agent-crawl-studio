import sys

from .cli import main


def run():
    sys.exit(main(sys.argv[1:]))


if __name__ == "__main__":  # pragma: no cover
    run()
