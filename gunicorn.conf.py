from settings.capacity import CAPACITY
from utils.get_log import setup_listener, setup_worker_logger

bind = "0.0.0.0:4000"
worker_class = "uvicorn.workers.UvicornWorker"
workers = CAPACITY["workers"]
timeout = 0
graceful_timeout = 30
keepalive = 5
backlog = 2048
worker_connections = max(200, CAPACITY["limit_concurrency"] * 4)
accesslog = "-"
limit_request_field_size = 16384


def on_starting(server):
    setup_listener()


def post_fork(server, worker):
    setup_worker_logger()
    from settings.database import async_engine

    async_engine.sync_engine.dispose()
