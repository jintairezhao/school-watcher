"""Run selected durable worker roles; PostgreSQL supports several processes."""
import argparse
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / '.env')
from backend import create_app
from backend.core.config import DATA_DIR
from backend.worker import run


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--roles', default='http,browser,directory,scheduler',
                        help='Comma-separated http,browser,directory,scheduler roles')
    parser.add_argument('--concurrency', type=int, default=None)
    parser.add_argument('--worker-id', default=None)
    args = parser.parse_args()
    handler = RotatingFileHandler(DATA_DIR / 'worker.log', maxBytes=2 * 1024 * 1024, backupCount=3, encoding='utf-8')
    logging.basicConfig(level=logging.INFO, handlers=[handler], format='%(asctime)s %(levelname)s %(message)s')
    run(create_app(), once=args.once, roles=[role.strip() for role in args.roles.split(',') if role.strip()],
        concurrency=args.concurrency, worker_id=args.worker_id)
