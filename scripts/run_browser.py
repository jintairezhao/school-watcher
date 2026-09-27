"""Run the private browser executor; configuration is shared with the desktop launcher."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if __name__ == '__main__':
    import os
    import subprocess
    import time
    from dotenv import load_dotenv
    from filelock import FileLock
    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / '.env')
    if '--supervise' in sys.argv:
        data = Path(os.environ.get('WATCHER_DATA_DIR', str(root / 'data')))
        data.mkdir(parents=True, exist_ok=True)
        with FileLock(str(data / 'browser-service.lock'), timeout=0):
            while True:
                result = subprocess.run([sys.executable, str(Path(__file__).resolve())], cwd=root,
                    stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                if result.returncode == 0:
                    break
                time.sleep(5)
    else:
        from backend.browser_service.server import main
        main()
