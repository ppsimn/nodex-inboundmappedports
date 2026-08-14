# src/api.py
import os
import time
import json
import logging
import requests
from urllib.parse import quote

class APIManager:
    """
    APIManager main responsibilities:
    - Manages persistent sessions with TTL-based reuse
    - Handles request timeouts
    - URL-encodes sensitive fields like email and client_id
    """

    def __init__(self, net_opts=None):
        self.sessions = {}  # Maps server identity to requests.Session
        self.net_opts = net_opts or {}
        self.timeout = int(self.net_opts.get("request_timeout", 10))
        # Tracks last successful validation timestamp for each server identity
        self._last_valid = {}  # session_key -> timestamp
        self._validate_ttl = int(
            os.getenv("NET_VALIDATE_TTL_SECONDS", str(self.net_opts.get("validate_ttl_seconds", 60)))
        )

    # ---------------------- Session Management ----------------------
    def _base_url(self, server_or_url) -> str:
        if isinstance(server_or_url, dict):
            return server_or_url["url"].rstrip("/")
        return str(server_or_url).rstrip("/")

    def _session_key(self, server_or_url) -> str:
        base = self._base_url(server_or_url)
        if not isinstance(server_or_url, dict):
            return base
        username = server_or_url.get("username") or server_or_url.get("api_username") or ""
        state_key = server_or_url.get("state_key") or server_or_url.get("name") or ""
        return f"{base}|{username}|{state_key}"

    def _get_session(self, server_or_url) -> requests.Session:
        """
        Returns a persistent session for the given server identity.
        Creates a new session if one does not exist.
        """
        key = self._session_key(server_or_url)
        s = self.sessions.get(key)
        if s is None:
            s = requests.Session()
            s.headers.update({
                "User-Agent": "dds-sync-worker/0.1",
                "Accept": "application/json, text/plain, */*",
            })
            self.sessions[key] = s
        return s

    def _validate_session(self, key: str, base: str, s: requests.Session) -> bool:
        """
        Validates the session for the given base_url using TTL:
        - If last validation was within TTL, returns True.
        - Otherwise, performs a GET request to /panel/api/inbounds/list and checks for success.
        """
        now = time.time()
        ts = self._last_valid.get(key)
        if ts and (now - ts) < self._validate_ttl:
            return True

        try:
            r = s.get(f"{base}/panel/api/inbounds/list", timeout=self.timeout)
            if r.status_code != 200:
                return False
            jr = r.json()
            ok = bool(jr.get("success"))
            if ok:
                self._last_valid[key] = now
            return ok
        except Exception:
            return False

    # ---------------------- Authentication ----------------------
    def login(self, server: dict) -> requests.Session:
        """
        Logs in to the server and returns a session.
        If a valid session exists (within TTL), it is reused.
        Expects server dict with keys: url, username, password.
        """
        base = self._base_url(server)
        key = self._session_key(server)
        s = self._get_session(server)

        # Reuse session if still valid (no need to call /login)
        if self._validate_session(key, base, s):
            logging.info(f"Reusing session for {base}")
            return s

        payload = {"username": server.get("username", ""), "password": server.get("password", "")}
        try:
            r = s.post(f"{base}/login", json=payload, timeout=self.timeout)
            r.raise_for_status()
            jr = r.json()
            if jr.get("success"):
                self._last_valid[key] = time.time()
                logging.info(f"Logged in via /login for {base}")
                return s
            raise RuntimeError(f"Login failed: {jr.get('msg', 'unknown error')}")
        except Exception as e:
            logging.error(f"Login request error for {base}: {e}")
            raise

    # ---------------------- Inbounds Management ----------------------
    def get_inbounds(self, server: dict, session: requests.Session):
        """
        Retrieves the list of inbounds from the server.
        Returns an empty list on error.
        """
        base = self._base_url(server)
        s = self._get_session(server)
        try:
            r = s.get(f"{base}/panel/api/inbounds/list", timeout=self.timeout)
            r.raise_for_status()
            jr = r.json()
            return jr.get("obj") or []
        except Exception as e:
            logging.error(f"Error fetching inbounds from {base}: {e}")
            return []

    def add_inbound(self, server: dict, session: requests.Session, inbound: dict) -> None:
        """
        Adds a new inbound to the server.
        Logs an error if the operation fails.
        """
        base = self._base_url(server)
        s = self._get_session(server)
        try:
            r = s.post(f"{base}/panel/api/inbounds/add", json=inbound, timeout=self.timeout)
            r.raise_for_status()
            jr = r.json()
            if not jr.get("success"):
                logging.error(f"Failed to add inbound {inbound.get('id')} on {base}: {jr.get('msg', 'No message')}")
        except Exception as e:
            logging.error(f"Error adding inbound {inbound.get('id')} on {base}: {e}")

    def update_inbound(self, server: dict, session: requests.Session, inbound_id: int, inbound: dict) -> None:
        """
        Updates an existing inbound on the server.
        Logs an error if the operation fails.
        """
        base = self._base_url(server)
        s = self._get_session(server)
        try:
            r = s.post(f"{base}/panel/api/inbounds/update/{inbound_id}", json=inbound, timeout=self.timeout)
            r.raise_for_status()
            jr = r.json()
            if not jr.get("success"):
                logging.error(f"Failed to update inbound {inbound_id} on {base}: {jr.get('msg', 'No message')}")
        except Exception as e:
            logging.error(f"Error updating inbound {inbound_id} on {base}: {e}")

    def delete_inbound(self, server: dict, session: requests.Session, inbound_id: int) -> None:
        """
        Deletes an inbound from the server.
        Logs an error if the operation fails.
        """
        base = self._base_url(server)
        s = self._get_session(server)
        try:
            r = s.post(f"{base}/panel/api/inbounds/del/{inbound_id}", timeout=self.timeout)
            r.raise_for_status()
            jr = r.json()
            if not jr.get("success"):
                logging.error(f"Failed to delete inbound {inbound_id} on {base}: {jr.get('msg', 'No message')}")
        except Exception as e:
            logging.error(f"Error deleting inbound {inbound_id} on {base}: {e}")

    # ---------------------- Client Management ----------------------
    def add_client(self, server: dict, session: requests.Session, inbound_id: int, client: dict) -> None:
        """
        Adds a new client to the specified inbound.
        Logs an error if the operation fails.
        """
        base = self._base_url(server)
        s = self._get_session(server)
        payload = {"id": inbound_id, "settings": json.dumps({"clients": [client]})}
        try:
            r = s.post(f"{base}/panel/api/inbounds/addClient", json=payload, timeout=self.timeout)
            r.raise_for_status()
            jr = r.json()
            if not jr.get("success"):
                logging.error(f"Failed to add client {client.get('email')} on {base}: {jr.get('msg', 'No message')}")
        except Exception as e:
            logging.error(f"Error adding client {client.get('email')} on {base}: {e}")

    def update_client(self, server: dict, session: requests.Session, client_id, inbound_id: int, client: dict) -> None:
        """
        Updates an existing client for the specified inbound.
        Logs an error if the operation fails.
        """
        base = self._base_url(server)
        s = self._get_session(server)
        safe_id = quote(str(client_id), safe="")
        url = f"{base}/panel/api/inbounds/updateClient/{safe_id}"
        payload = {"id": inbound_id, "settings": json.dumps({"clients": [client]})}
        try:
            r = s.post(url, json=payload, timeout=self.timeout)
            r.raise_for_status()
            jr = r.json()
            if not jr.get("success"):
                logging.error(f"Failed to update client {client_id} on {base}: {jr.get('msg', 'No message')}")
        except Exception as e:
            logging.error(f"Error updating client {client_id} on {base}: {e}")

    def delete_client(self, server: dict, session: requests.Session, inbound_id: int, client_id) -> None:
        """
        Deletes a client from the specified inbound.
        Logs an error if the operation fails.
        """
        base = self._base_url(server)
        s = self._get_session(server)
        safe_id = quote(str(client_id), safe="")
        url = f"{base}/panel/api/inbounds/{inbound_id}/delClient/{safe_id}"
        try:
            r = s.post(url, timeout=self.timeout)
            r.raise_for_status()
            jr = r.json()
            if not jr.get("success"):
                logging.error(f"Failed to delete client {client_id} on {base}: {jr.get('msg', 'No message')}")
        except Exception as e:
            logging.error(f"Error deleting client {client_id} on {base}: {e}")

    # ---------------------- Traffic Management ----------------------
    def get_client_traffic(self, server: dict, session: requests.Session, email: str):
        """
        Retrieves upload and download traffic statistics for the specified client email.
        Returns (0, 0) on error.
        """
        base = self._base_url(server)
        s = self._get_session(server)
        safe_email = quote(email, safe="")
        url = f"{base}/panel/api/inbounds/getClientTraffics/{safe_email}"
        try:
            r = s.get(url, timeout=self.timeout)
            r.raise_for_status()
            jr = r.json()
            if jr.get("success"):
                obj = jr.get("obj") or {}
                up = int(obj.get("up", 0) or 0)
                down = int(obj.get("down", 0) or 0)
                return up, down
            return (0, 0)
        except Exception as e:
            logging.error(f"Error fetching traffic for {email} on {base}: {e}")
            return (0, 0)

    def update_client_traffic(self, server: dict, session: requests.Session, email: str, up: int, down: int) -> None:
        """
        Updates the traffic statistics for the specified client email.
        This endpoint may not be supported by all panels; errors are logged.
        """
        base = self._base_url(server)
        s = self._get_session(server)
        safe_email = quote(email, safe="")
        url = f"{base}/panel/api/inbounds/updateClientTraffic/{safe_email}"
        payload = {"upload": int(up), "download": int(down)}
        try:
            r = s.post(url, json=payload, timeout=self.timeout)
            r.raise_for_status()
            jr = r.json()
            if not jr.get("success"):
                logging.error(f"Failed to update traffic for {email} on {base}: {jr.get('msg', 'No message')}")
        except Exception as e:
            logging.error(f"Error updating traffic for {email} on {base}: {e}")
