import os
import time
import signal
import shutil
from .logging_setup import setup_logging
from .config import ConfigManager
from .state import TrafficStateManager
from .api import APIManager
from .sync import SyncManager

HEARTBEAT_FILE = ".heartbeat"

def migrate_db_if_needed(logger, new_db_path, legacy_candidates):
    """If the new DB does not exist and one of the legacy paths exists, copy it (with wal/shm)."""
    if os.path.exists(new_db_path):
        return
    for old in legacy_candidates:
        if old and os.path.exists(old):
            logger.info(f"Migrating legacy SQLite DB from {old} -> {new_db_path}")
            os.makedirs(os.path.dirname(new_db_path), exist_ok=True)
            shutil.copy2(old, new_db_path)
            for suffix in ("-wal", "-shm"):
                old_side, new_side = old + suffix, new_db_path + suffix
                if os.path.exists(old_side):
                    shutil.copy2(old_side, new_side)
            logger.info("DB migration completed.")
            return

def write_heartbeat(path):
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(int(time.time())))
    except Exception as e:
        # Log to root logger (stdout)
        import logging
        logging.error(f"Failed to write heartbeat: {e}")

def main():
    data_dir = os.getenv("DATA_DIR", "/app/data")
    os.makedirs(data_dir, exist_ok=True)

    logger = setup_logging(data_dir, os.getenv("LOG_LEVEL","INFO"))
    logger.info("Starting sync-worker v0.1 (dockerized + safe patches)")

    cfg_file = os.getenv("CONFIG_FILE", "/app/config/config.json")
    config_manager = ConfigManager(config_file=cfg_file)

    # DB path: can be overridden by ENV
    db_path = os.getenv("DB_FILE", os.path.join(data_dir, "traffic_state.db"))
    legacy_dbs = [
        "/app/traffic_state.db",
        "/app/src/traffic_state.db",
    ]
    migrate_db_if_needed(logger, db_path, legacy_dbs)

    traffic_state_manager = TrafficStateManager(
        db_file=db_path,
        db_opts=config_manager.db()
    )
    api_manager = APIManager(net_opts=config_manager.net())
    sync_manager = SyncManager(api_manager, config_manager, traffic_state_manager)

    # interval from config (or ENV override)
    interval_min_env = os.getenv("SYNC_INTERVAL_MINUTES")
    if interval_min_env is not None:
        try:
            interval_sec = max(1, int(interval_min_env)) * 60
        except Exception:
            interval_sec = max(1, int(config_manager.get_interval())) * 60
    else:
        interval_sec = max(1, int(config_manager.get_interval())) * 60

    stop = {"flag": False}
    def _graceful(signum, frame):
        logger.info(f"Received signal {signum}; shutting down gracefully...")
        stop["flag"] = True
    signal.signal(signal.SIGINT, _graceful)
    signal.signal(signal.SIGTERM, _graceful)

    hb_path = os.path.join(data_dir, HEARTBEAT_FILE)

    while not stop["flag"]:
        # v0.1: heartbeat is always updated (service health is independent of cycle success)
        write_heartbeat(hb_path)
        try:
            logger.info("Starting sync cycle")
            sync_manager.sync_inbounds_and_clients()
            sync_manager.sync_traffic()
            logger.info("Sync cycle completed successfully")
        except Exception as e:
            logger.error(f"Sync cycle failed: {e}")
        time.sleep(interval_sec)

    logger.info("Exited cleanly.")

if __name__ == "__main__":
    main()

