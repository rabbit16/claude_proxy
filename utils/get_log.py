import logging
import multiprocessing
import os
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler

LOG_DIR = os.path.join(os.getcwd(), "logs")
ACCESS_LOG_NAME = "access"
BILLING_LOG_NAME = "billing"
BILLING_AUDIT_LOG_NAME = "billing_audit"
PROCESS_LOG_NAME = "process"
DEEPSEEK_SLOW_LOG_NAME = "deepseek_slow"

access_log = logging.getLogger(ACCESS_LOG_NAME)
billing_log = logging.getLogger(BILLING_LOG_NAME)
billing_audit_log = logging.getLogger(BILLING_AUDIT_LOG_NAME)
process_log = logging.getLogger(PROCESS_LOG_NAME)
# DeepSeek 上游首字超过阈值的单独留档，便于事后按供应商分析慢请求。
deepseek_slow_log = logging.getLogger(DEEPSEEK_SLOW_LOG_NAME)

log_queue = multiprocessing.Queue()
listener = None
_mode = None  # "queue" | "file"


class _DispatchHandler(logging.Handler):
    def __init__(self, handlers_by_name, default_handler):
        super().__init__()
        self.handlers_by_name = handlers_by_name
        self.default_handler = default_handler

    def emit(self, record):
        handler = self.handlers_by_name.get(record.name, self.default_handler)
        handler.emit(record)


def _log_dir():
    os.makedirs(LOG_DIR, exist_ok=True)
    return LOG_DIR


def _rotating_handler(filename):
    handler = RotatingFileHandler(
        os.path.join(_log_dir(), filename),
        maxBytes=20 * 1024 * 1024,
        backupCount=10,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(name)s | %(levelname)s | %(message)s")
    )
    return handler


def _build_file_handlers():
    access_handler = _rotating_handler("access.log")
    billing_handler = _rotating_handler("billing.log")
    billing_audit_handler = _rotating_handler("billing_audit.log")
    process_handler = _rotating_handler("process.log")
    deepseek_slow_handler = _rotating_handler("deepseek_slow.log")
    return {
        ACCESS_LOG_NAME: access_handler,
        BILLING_LOG_NAME: billing_handler,
        BILLING_AUDIT_LOG_NAME: billing_audit_handler,
        PROCESS_LOG_NAME: process_handler,
        DEEPSEEK_SLOW_LOG_NAME: deepseek_slow_handler,
        "system": process_handler,
        "check_user": process_handler,
        "common_util": process_handler,
    }, process_handler


def setup_standalone_logging():
    """uvicorn / python main.py 单进程写文件。gunicorn worker 已走 queue 时跳过。"""
    global _mode
    if _mode:
        return
    _mode = "file"
    handlers, _ = _build_file_handlers()
    for name, handler in (
        (ACCESS_LOG_NAME, handlers[ACCESS_LOG_NAME]),
        (BILLING_LOG_NAME, handlers[BILLING_LOG_NAME]),
        (BILLING_AUDIT_LOG_NAME, handlers[BILLING_AUDIT_LOG_NAME]),
        (PROCESS_LOG_NAME, handlers[PROCESS_LOG_NAME]),
        (DEEPSEEK_SLOW_LOG_NAME, handlers[DEEPSEEK_SLOW_LOG_NAME]),
    ):
        logger = logging.getLogger(name)
        logger.setLevel(logging.INFO)
        logger.handlers = [handler]
        logger.propagate = False

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(isinstance(item, logging.StreamHandler) and not isinstance(item, RotatingFileHandler) for item in root.handlers):
        console = logging.StreamHandler()
        console.setFormatter(
            logging.Formatter("%(asctime)s | %(name)s | %(levelname)s | %(message)s")
        )
        root.addHandler(console)


def setup_listener():
    global listener, _mode
    _mode = "queue"
    handlers, default_handler = _build_file_handlers()
    listener = QueueListener(log_queue, _DispatchHandler(handlers, default_handler))
    listener.start()


def setup_worker_logger():
    global _mode
    _mode = "queue"
    queue_handler = QueueHandler(log_queue)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers = [queue_handler]
    for name in (
        ACCESS_LOG_NAME, BILLING_LOG_NAME, BILLING_AUDIT_LOG_NAME, PROCESS_LOG_NAME,
        DEEPSEEK_SLOW_LOG_NAME, "system", "check_user",
    ):
        logger = logging.getLogger(name)
        logger.setLevel(logging.INFO)
        logger.handlers = []
        logger.propagate = True
