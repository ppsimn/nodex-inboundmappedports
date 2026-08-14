# src/config.py
import os
import json
import logging
import re
from urllib.parse import urlsplit, urlunsplit

def _parse_bool(val, default=False):
    if val is None:
        return default
    v = str(val).strip().lower()
    return v in ("1", "true", "yes", "on")

def _parse_int(val, default):
    if val is None:
        return default
    try:
        return int(str(val).strip())
    except Exception:
        return default

def _clean_env(val):
    if val is None:
        return None
    val = str(val).strip()
    return val if val else None

def _parse_mapping_port(val):
    try:
        return int(str(val).strip())
    except Exception:
        raise ValueError(f"Invalid inbound port '{val}'")

class ConfigManager:
    # Used by main.py: config_file is passed in as an argument
    def __init__(self, config_file='config.json'):
        self.config_file = config_file
        self.config = self.load_config()

    def load_config(self):
        try:
            with open(self.config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
                if not isinstance(config, dict):
                    raise ValueError("Config root must be a JSON object")

                # --- Set default values if missing ---
                config.setdefault('central_server', {})
                config.setdefault('nodes', [])
                config.setdefault('sync_interval_minutes', 1)
                config.setdefault('net', {})
                config.setdefault('db', {})
                config['net'].setdefault('parallel_node_calls', True)
                config['net'].setdefault('max_workers', 8)
                config['net'].setdefault('request_timeout', 10)
                config['net'].setdefault('connect_pool_size', 50)
                # NEW: TTL for session validation
                config['net'].setdefault('validate_ttl_seconds', 60)

                config['db'].setdefault('wal', True)
                config['db'].setdefault('synchronous', 'NORMAL')  # Options: FULL/NORMAL/OFF
                config['db'].setdefault('cache_size_mb', 20)

                # --- Override config values with environment variables ---
                # sync interval
                config['sync_interval_minutes'] = _parse_int(
                    os.getenv("SYNC_INTERVAL_MINUTES"),
                    config['sync_interval_minutes']
                )

                # network settings
                config['net']['parallel_node_calls'] = _parse_bool(
                    os.getenv("NET_PARALLEL_NODE_CALLS"),
                    config['net']['parallel_node_calls']
                )
                config['net']['max_workers'] = _parse_int(
                    os.getenv("NET_MAX_WORKERS"),
                    config['net']['max_workers']
                )
                config['net']['request_timeout'] = _parse_int(
                    os.getenv("NET_REQUEST_TIMEOUT"),
                    config['net']['request_timeout']
                )
                config['net']['connect_pool_size'] = _parse_int(
                    os.getenv("NET_CONNECT_POOL_SIZE"),
                    config['net']['connect_pool_size']
                )
                # NEW: TTL override from ENV
                config['net']['validate_ttl_seconds'] = _parse_int(
                    os.getenv("NET_VALIDATE_TTL_SECONDS"),
                    config['net']['validate_ttl_seconds']
                )

                # database settings
                db_wal_env = os.getenv("DB_WAL")
                if db_wal_env is not None:
                    config['db']['wal'] = _parse_bool(db_wal_env, config['db']['wal'])

                db_sync_env = os.getenv("DB_SYNCHRONOUS")
                if db_sync_env is not None:
                    sync_mode = str(db_sync_env).strip().upper()
                    if sync_mode in ("FULL", "NORMAL", "OFF"):
                        config['db']['synchronous'] = sync_mode
                    else:
                        logging.warning(f"Invalid DB_SYNCHRONOUS='{db_sync_env}', keeping '{config['db']['synchronous']}'")

                config['db']['cache_size_mb'] = _parse_int(
                    os.getenv("DB_CACHE_SIZE_MB"),
                    config['db']['cache_size_mb']
                )

                self._apply_server_env(config)
                self._normalize_servers(config)
                self._validate_config(config)

                return config

        except FileNotFoundError:
            logging.error(f"Config file {self.config_file} not found")
            raise
        except json.JSONDecodeError:
            logging.error(f"Invalid JSON in {self.config_file}")
            raise
        except ValueError as e:
            logging.error(f"Config error: {e}")
            raise

    def _apply_server_env(self, config):
        config['central_server'] = dict(config.get('central_server') or {})
        self._apply_one_server_env(config['central_server'], "CENTRAL")

        nodes = [dict(node or {}) for node in config.get('nodes', [])]
        node_count_env = _clean_env(os.getenv("NODE_COUNT"))
        node_count = None
        if node_count_env is not None:
            node_count = _parse_int(node_count_env, len(nodes))
            if node_count < 0:
                node_count = 0
            nodes = nodes[:node_count]
            while len(nodes) < node_count:
                nodes.append({})

        indices = set(range(1, len(nodes) + 1))
        for key in os.environ:
            m = re.match(r"NODE_(\d+)_", key)
            if m:
                indices.add(int(m.group(1)))

        if indices:
            while len(nodes) < max(indices):
                nodes.append({})

        global_mappings = _clean_env(os.getenv("INBOUND_MAPPINGS"))
        for idx, node in enumerate(nodes, start=1):
            self._apply_one_server_env(node, f"NODE_{idx}")
            if "inbound_mappings" not in node and global_mappings is not None:
                node["inbound_mappings"] = global_mappings

        if node_count is not None:
            nodes = nodes[:node_count]

        config['nodes'] = nodes

    def _apply_one_server_env(self, server, prefix):
        env_to_key = {
            "URL": "url",
            "HOST": "host",
            "ADDRESS": "host",
            "SCHEME": "scheme",
            "WEB_PATH": "web_path",
            "WEBPATH": "web_path",
            "API_PORT": "api_port",
            "API_ACCESS_PORT": "api_port",
            "PORT": "api_port",
            "USERNAME": "username",
            "PASSWORD": "password",
            "API_USERNAME": "username",
            "API_PASSWORD": "password",
            "LOGIN_USERNAME": "username",
            "LOGIN_PASSWORD": "password",
            "STATE_KEY": "state_key",
            "NAME": "name",
            "EMAIL_SUFFIX": "email_suffix",  # <--- NEW MAPPING ADDED HERE
        }
        for suffix, key in env_to_key.items():
            val = _clean_env(os.getenv(f"{prefix}_{suffix}"))
            if val is not None:
                server[key] = val

        mappings = (
            _clean_env(os.getenv(f"{prefix}_INBOUND_MAPPINGS")) or
            _clean_env(os.getenv(f"{prefix}_INBOUND_MAP")) or
            _clean_env(os.getenv(f"{prefix}_SYNC_INBOUNDS"))
        )
        if mappings is not None:
            server["inbound_mappings"] = mappings

    def _normalize_servers(self, config):
        config['central_server'] = self._normalize_server(config.get('central_server') or {}, "central_server")
        normalized_nodes = []
        for idx, node in enumerate(config.get('nodes', []), start=1):
            normalized_nodes.append(self._normalize_server(node or {}, f"nodes[{idx - 1}]", allow_inbounds=True))
        config['nodes'] = normalized_nodes

    def _normalize_server(self, server, label, allow_inbounds=False):
        server = dict(server or {})
        if "api_username" in server and not server.get("username"):
            server["username"] = server.get("api_username")
        if "api_password" in server and not server.get("password"):
            server["password"] = server.get("api_password")
        if "api_access_port" in server and not server.get("api_port"):
            server["api_port"] = server.get("api_access_port")

        url = _clean_env(server.get("url"))
        api_port = _clean_env(server.get("api_port"))
        if api_port is not None:
            api_port = str(_parse_mapping_port(api_port))
            server["api_port"] = int(api_port)

        if url:
            server["url"] = self._url_with_port(url, api_port) if api_port else url.rstrip("/")
        else:
            host = _clean_env(server.get("host") or server.get("address"))
            if host:
                scheme = _clean_env(server.get("scheme")) or "http"
                web_path = _clean_env(server.get("web_path") or server.get("path")) or ""
                if web_path and not web_path.startswith("/"):
                    web_path = "/" + web_path
                netloc = host
                if api_port is not None:
                    netloc = self._host_with_port(host, api_port)
                server["url"] = f"{scheme}://{netloc}{web_path}".rstrip("/")

        if allow_inbounds:
            server["inbound_mappings"] = self._normalize_inbound_mappings(server.get("inbound_mappings"))
        return server

    def _url_with_port(self, url, port):
        parts = urlsplit(url.strip())
        if not parts.scheme or not parts.netloc:
            raise ValueError(f"Invalid URL '{url}'")
        host = parts.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if parts.username:
            auth = parts.username
            if parts.password:
                auth += f":{parts.password}"
            host = f"{auth}@{host}"
        netloc = self._host_with_port(host, port)
        return urlunsplit((parts.scheme, netloc, parts.path.rstrip("/"), parts.query, parts.fragment)).rstrip("/")

    def _host_with_port(self, host, port):
        auth = ""
        host_part = host
        if "@" in host:
            auth, host_part = host.rsplit("@", 1)
            auth += "@"
        host = host_part
        if host.startswith("["):
            base = host.split("]", 1)[0] + "]"
            return f"{auth}{base}:{port}"
        if ":" in host and host.count(":") == 1:
            host = host.split(":", 1)[0]
        return f"{auth}{host}:{port}"

    def _normalize_inbound_mappings(self, raw):
        if raw in (None, "", []):
            return []
        items = []
        if isinstance(raw, dict):
            items = [{"central_port": k, "node_port": v} for k, v in raw.items()]
        elif isinstance(raw, str):
            items = [part.strip() for part in raw.split(",") if part.strip()]
        elif isinstance(raw, list):
            items = raw
        else:
            raise ValueError("inbound_mappings must be a list, object, or comma-separated string")

        normalized = []
        seen_central = set()
        seen_node = set()
        for item in items:
            if isinstance(item, dict):
                central = (
                    item.get("central_port") or item.get("source_port") or
                    item.get("main_port") or item.get("from") or item.get("src") or
                    item.get("inbound_port")
                )
                node = (
                    item.get("node_port") or item.get("target_port") or
                    item.get("to") or item.get("dst") or central
                )
            else:
                text = str(item).strip()
                if ":" in text:
                    central, node = text.split(":", 1)
                elif "=" in text:
                    central, node = text.split("=", 1)
                else:
                    central, node = text, text

            central_port = _parse_mapping_port(central)
            node_port = _parse_mapping_port(node)
            if central_port in seen_central:
                logging.warning(f"Duplicate inbound mapping for central port {central_port}; keeping the last entry")
                normalized = [m for m in normalized if m["central_port"] != central_port]
            if node_port in seen_node:
                logging.warning(f"Duplicate inbound mapping for node port {node_port}; multiple central inbounds target the same node port")
            normalized.append({"central_port": central_port, "node_port": node_port})
            seen_central.add(central_port)
            seen_node.add(node_port)
        return normalized

    def _validate_config(self, config):
        if not config.get('central_server') or not config['central_server'].get('url'):
            raise ValueError("Missing central_server url/host in config")
        if not config.get('nodes'):
            raise ValueError("Missing nodes in config")
        for key in ("username", "password"):
            if config['central_server'].get(key) is None:
                config['central_server'][key] = ""
        for idx, node in enumerate(config.get('nodes', []), start=1):
            if not node.get('url'):
                raise ValueError(f"Missing url/host for node {idx}")
            for key in ("username", "password"):
                if node.get(key) is None:
                    node[key] = ""

    def get_central_server(self):
        return self.config.get('central_server', {})

    def get_nodes(self):
        return self.config.get('nodes', [])

    def get_interval(self):
        return self.config.get('sync_interval_minutes', 1)

    def net(self):
        return self.config.get('net', {})

    def db(self):
        return self.config.get('db', {})
