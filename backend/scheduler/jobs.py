"""Compatibility entrypoints: scheduling belongs to scripts/run_worker.py."""


def start_scheduler(app=None):
    raise RuntimeError('Start scripts/run_worker.py as a separate process; the website does not run a scheduler')


def restart_scheduler():
    # The independent worker reloads persisted interval settings every 30 seconds.
    return None


def stop_scheduler():
    return None
