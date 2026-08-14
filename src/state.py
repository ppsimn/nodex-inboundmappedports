import sqlite3
import threading
import time
import logging

class TrafficStateManager:
    def __init__(self, db_file='traffic_state.db', db_opts=None):
        self.db_file = db_file
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(self.db_file, check_same_thread=False, isolation_level=None)
        self.conn.execute("PRAGMA foreign_keys=ON;")
        # PRAGMAs
        if db_opts:
            if db_opts.get('wal', True):
                self.conn.execute("PRAGMA journal_mode=WAL;")
            sync_mode = db_opts.get('synchronous', 'NORMAL').upper()
            if sync_mode not in ('FULL', 'NORMAL', 'OFF'):
                sync_mode = 'NORMAL'
            self.conn.execute(f"PRAGMA synchronous={sync_mode};")
            cache_mb = int(db_opts.get('cache_size_mb', 20))
            self.conn.execute(f"PRAGMA cache_size=-{cache_mb * 1024};")  # negative => KB
            self.conn.execute("PRAGMA temp_store=MEMORY;")
        self.init_db()

    def init_db(self):
        with self.lock, self.conn:
            c = self.conn.cursor()
            # مجموع کل کاربر در سیکل
            c.execute('''
                CREATE TABLE IF NOT EXISTS client_totals (
                    email TEXT PRIMARY KEY,
                    total_up INTEGER NOT NULL DEFAULT 0,
                    total_down INTEGER NOT NULL DEFAULT 0,
                    cycle_started_at INTEGER
                )
            ''')
            # baseline هر سرور (آخرین عدد کل نوشته‌شده روی آن سرور)
            c.execute('''
                CREATE TABLE IF NOT EXISTS server_counters (
                    email TEXT NOT NULL,
                    server_url TEXT NOT NULL,
                    last_up INTEGER NOT NULL DEFAULT 0,
                    last_down INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (email, server_url)
                )
            ''')
            # <<< جدید: مصرف انباشته‌ی هر نود از ابتدای سیکل جاری >>>
            c.execute('''
                CREATE TABLE IF NOT EXISTS node_totals (
                    email TEXT NOT NULL,
                    server_url TEXT NOT NULL,
                    up_total INTEGER NOT NULL DEFAULT 0,
                    down_total INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (email, server_url)
                )
            ''')
            c.execute("CREATE INDEX IF NOT EXISTS idx_node_totals_email ON node_totals(email)")

    # ---- total getters/setters ----
    def get_total(self, email):
        with self.lock:
            row = self.conn.execute(
                "SELECT total_up,total_down FROM client_totals WHERE email=?", (email,)
            ).fetchone()
            return (row[0], row[1]) if row else (0, 0)

    def set_total(self, email, up, down):
        # idempotent write; only write if changed
        with self.lock, self.conn:
            row = self.conn.execute(
                "SELECT total_up,total_down FROM client_totals WHERE email=?", (email,)
            ).fetchone()
            if row and row[0] == up and row[1] == down:
                return False  # no change
            self.conn.execute("""
                INSERT INTO client_totals(email,total_up,total_down,cycle_started_at)
                VALUES(?,?,?,COALESCE((SELECT cycle_started_at FROM client_totals WHERE email=?), NULL))
                ON CONFLICT(email) DO UPDATE
                SET total_up=excluded.total_up, total_down=excluded.total_down
            """, (email, up, down, email))
            return True

    def set_cycle_started_at(self, email, ts):
        with self.lock, self.conn:
            self.conn.execute("""
                INSERT INTO client_totals(email,total_up,total_down,cycle_started_at)
                VALUES(?,?,?,?)
                ON CONFLICT(email) DO UPDATE SET cycle_started_at=excluded.cycle_started_at
            """, (email, 0, 0, ts))

    # ---- per-server baseline getters/setters ----
    def get_last_counter(self, email, server_url):
        with self.lock:
            row = self.conn.execute("""
                SELECT last_up,last_down FROM server_counters
                WHERE email=? AND server_url=?
            """, (email, server_url)).fetchone()
            return (row[0], row[1]) if row else None

    def set_last_counter(self, email, server_url, up, down):
        with self.lock, self.conn:
            # only write if changed
            row = self.conn.execute("""
                SELECT last_up,last_down FROM server_counters
                WHERE email=? AND server_url=?
            """, (email, server_url)).fetchone()
            if row and row[0] == up and row[1] == down:
                return False
            self.conn.execute("""
                INSERT INTO server_counters(email,server_url,last_up,last_down)
                VALUES(?,?,?,?)
                ON CONFLICT(email,server_url) DO UPDATE
                SET last_up=excluded.last_up, last_down=excluded.last_down
            """, (email, server_url, up, down))
            return True

    def set_last_counters_batch(self, email, items):
        # items: Iterable[(server_url, up, down)]
        with self.lock, self.conn:
            self.conn.executemany("""
              INSERT INTO server_counters(email,server_url,last_up,last_down)
              VALUES(?,?,?,?)
              ON CONFLICT(email,server_url) DO UPDATE
              SET last_up=excluded.last_up,last_down=excluded.last_down
            """, [(email, srv, up, down) for (srv, up, down) in items])

    # ---- per-node accumulation (جدید) ----
    def add_node_delta(self, email: str, server_url: str, du: int, dd: int) -> None:
        """انباشتن دلتاهای مصرف برای نود مشخص (از ابتدای سیکل جاری)."""
        if not du and not dd:
            return
        with self.lock, self.conn:
            self.conn.execute("""
                INSERT INTO node_totals(email, server_url, up_total, down_total)
                VALUES(?,?,?,?)
                ON CONFLICT(email, server_url) DO UPDATE SET
                  up_total  = node_totals.up_total  + excluded.up_total,
                  down_total= node_totals.down_total+ excluded.down_total
            """, (email, server_url, int(du or 0), int(dd or 0)))

    def reset_node_totals(self, email: str) -> None:
        """در شروع سیکل جدید، per-node مربوط به کاربر را صفر می‌کند."""
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM node_totals WHERE email=?", (email,))

    def reset_cycle(self, email, currents_by_server, central_url):
        """
        شروع سیکل جدید:
          - total کاربر را برابر مقدار فعلی سرور مرکزی می‌گذارد
          - baseline تمام سرورها را به مقدار فعلی‌شان تنظیم می‌کند
          - و per-node را صفر می‌کند (node_totals DELETE)
        """
        with self.lock, self.conn:
            now_ts = int(time.time())
            cup, cdown = currents_by_server.get(central_url, (0, 0))
            # صفر کردن per-node برای این کاربر
            self.conn.execute("DELETE FROM node_totals WHERE email=?", (email,))
            # ثبت total و زمان شروع سیکل
            self.conn.execute("""
                INSERT INTO client_totals(email,total_up,total_down,cycle_started_at)
                VALUES(?,?,?,?)
                ON CONFLICT(email) DO UPDATE
                SET total_up=excluded.total_up,total_down=excluded.total_down,cycle_started_at=excluded.cycle_started_at
            """, (email, cup, cdown, now_ts))
            # به‌روز کردن baseline همه‌ی سرورها
            self.conn.executemany("""
                INSERT INTO server_counters(email,server_url,last_up,last_down)
                VALUES(?,?,?,?)
                ON CONFLICT(email,server_url) DO UPDATE
                SET last_up=excluded.last_up,last_down=excluded.last_down
            """, [(email, srv, up, down) for srv, (up, down) in currents_by_server.items()])
            logging.info(f"Cycle reset for {email}: total set to central ({cup},{cdown}); baselines updated; node_totals cleared.")
