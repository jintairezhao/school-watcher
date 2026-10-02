"""Run the same local desktop application from source as from an installer."""
from desktop.entry import main


if __name__ == '__main__':
    from multiprocessing import freeze_support
    freeze_support()
    raise SystemExit(main())
