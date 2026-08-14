import logging, os
from logging.handlers import RotatingFileHandler

def setup_logging(data_dir: str, level: str = "INFO"):
    log_level = getattr(logging, (level or "INFO").upper(), logging.INFO)
    logger = logging.getLogger()
    logger.setLevel(log_level)

    # Always log to stdout (container standard)
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logger.handlers = [sh]

    # File logging only if needed
    if os.getenv("ENABLE_FILE_LOG", "0") == "1":
        os.makedirs(data_dir, exist_ok=True)
        log_path = os.path.join(data_dir, "sync.log")
        fh = RotatingFileHandler(
            filename=log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        fh.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
        logger.addHandler(fh)

    return logger
