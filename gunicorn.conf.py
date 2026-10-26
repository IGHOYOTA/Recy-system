"""Run durable recovery while the Free web instance is awake; a standalone worker is also supported."""
import os


def post_worker_init(worker):
    if os.environ.get('RECY_WORKER_ENABLED')=='1':
        from recy.worker import start_background_worker
        start_background_worker()
