import json
import logging
import time
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor, as_completed

class SyncManager:
    def __init__(self, api_manager, config_manager, traffic_state_manager):
        self.api_manager = api_manager
        self.config_manager = config_manager
        self.traffic_state_manager = traffic_state_manager

    @staticmethod
    def _to_int(val, default=0):
        try:
            if val is None:
                return default
            return int(val)
        except Exception:
            try:
                return int(str(val).strip())
            except Exception:
                return default

    @staticmethod
    def _now_ms():
        return int(time.time() * 1000)

    # --- Client identity helpers (protocol-aware) ---
    def _client_key(self, c, protocol: str):
        p = (protocol or "").lower()
        if not isinstance(c, dict):
            return None
        if p == "trojan":
            # Trojan: password is the unique identifier
            return c.get("password") or c.get("email") or c.get("id")
        elif p == "shadowsocks":
            # Shadowsocks: clientId is email
            return c.get("email")
        else:
            # vmess/vless: id or email
            return c.get("id") or c.get("email")

    def _client_id_for_api(self, c, protocol: str):
        p = (protocol or "").lower()
        if not isinstance(c, dict):
            return None
        if p == "trojan":
            return c.get("password")
        elif p == "shadowsocks":
            return c.get("email")
        else:
            return c.get("id")

    def _is_safu_fresh(self, c):
        """
        A fresh SAFU client is waiting for first use: startAfterFirstUse=True and expiryTime<=0
        """
        if not isinstance(c, dict):
            return False
        safu = bool(c.get('startAfterFirstUse'))
        exp = self._to_int(c.get('expiryTime'), 0)
        return safu and exp <= 0

    def _is_active_started(self, c, now_ms):
        """Client is active if expiryTime is in the future."""
        exp = self._to_int(c.get('expiryTime'), 0)
        return exp > now_ms

    def _is_ended(self, c, now_ms):
        """Client has ended if expiryTime is in the past or negative."""
        exp = self._to_int(c.get('expiryTime'), 0)
        return (exp > 0 and exp <= now_ms) or exp < 0

    def _server_state_key(self, server):
        return server.get('state_key') or server.get('url')

    def _inbound_port(self, inbound):
        if not isinstance(inbound, dict):
            return None
        return self._to_int(inbound.get('port'), None)

    def _parse_inbound_clients(self, inbound):
        try:
            settings = json.loads(inbound.get('settings') or '{}') or {}
            clients = settings.get('clients', [])
            return clients if isinstance(clients, list) else []
        except Exception:
            return []

    def _extract_inbound_emails(self, inbound):
        emails = set()
        for client in inbound.get('clientStats') or []:
            if client and 'email' in client:
                emails.add(client['email'])
        for client in self._parse_inbound_clients(inbound):
            email = client.get('email') if isinstance(client, dict) else None
            if email:
                emails.add(email)
        return emails

    def _inbound_mappings(self, node):
        mappings = node.get('inbound_mappings') or []
        return mappings if isinstance(mappings, list) else []

    # def _node_inbound_payload(self, central_inbound, node_port, node_inbound=None):
    #     payload = deepcopy(central_inbound)
    #     payload['port'] = int(node_port)
    #     if node_inbound and node_inbound.get('id') is not None:
    #         payload['id'] = node_inbound['id']
    #     else:
    #         payload.pop('id', None)
    #     return payload

    def _node_inbound_payload(self, central_inbound, node_port, node_inbound=None, node=None):
        payload = deepcopy(central_inbound)
        payload['port'] = int(node_port)
        if node_inbound and node_inbound.get('id') is not None:
            payload['id'] = node_inbound['id']
        else:
            payload.pop('id', None)
            
        # NEW: Inject suffix to all clients in the payload settings
        if node:
            suffix = node.get("email_suffix", "")
            if suffix and 'settings' in payload:
                try:
                    settings_dict = json.loads(payload['settings'])
                    for cl in settings_dict.get('clients', []):
                        if 'email' in cl:
                            cl['email'] = f"{cl['email']}{suffix}"
                    payload['settings'] = json.dumps(settings_dict)
                except Exception:
                    pass
                    
        return payload

    def _sync_legacy_node_inbounds(self, node, node_session, parsed_central, node_inbounds):
        node_inbound_map = {inbound['id']: inbound for inbound in node_inbounds if inbound.get('id') is not None}
        shape_changed = False

        # for central_inbound, _ in parsed_central:
        #     cid = central_inbound['id']
        #     if cid not in node_inbound_map:
        #         self.api_manager.add_inbound(node, node_session, central_inbound)
        #         shape_changed = True
        #     else:
        #         self.api_manager.update_inbound(node, node_session, cid, central_inbound)
        #         node_inbound_map.pop(cid, None)
        for central_inbound, _ in parsed_central:
            cid = central_inbound['id']
            # NEW: Generate payload with the node object attached to trigger suffix
            payload = self._node_inbound_payload(central_inbound, central_inbound.get('port'), None, node)
            
            if cid not in node_inbound_map:
                self.api_manager.add_inbound(node, node_session, payload)
                shape_changed = True
            else:
                payload['id'] = cid
                self.api_manager.update_inbound(node, node_session, cid, payload)
                node_inbound_map.pop(cid, None)

        for inbound_id in list(node_inbound_map.keys()):
            self.api_manager.delete_inbound(node, node_session, inbound_id)
            shape_changed = True

        if shape_changed:
            node_inbounds = self.api_manager.get_inbounds(node, node_session)

        node_by_id = {inbound.get('id'): inbound for inbound in node_inbounds}
        targets = []
        for central_inbound, c_clients in parsed_central:
            cid = central_inbound['id']
            targets.append({
                "central_inbound": central_inbound,
                "central_clients": c_clients,
                "central_id": cid,
                "node_inbound": node_by_id.get(cid),
                "node_id": cid,
            })
        return targets

    def _sync_mapped_node_inbounds(self, node, node_session, parsed_central, node_inbounds):
        mappings = self._inbound_mappings(node)
        central_by_port = {}
        for central_inbound, c_clients in parsed_central:
            port = self._inbound_port(central_inbound)
            if port is not None:
                central_by_port[port] = (central_inbound, c_clients)

        node_by_port = {}
        for inbound in node_inbounds:
            port = self._inbound_port(inbound)
            if port is not None:
                node_by_port[port] = inbound

        added = False
        for mapping in mappings:
            central_port = mapping.get("central_port")
            node_port = mapping.get("node_port")
            central_pair = central_by_port.get(central_port)
            if not central_pair:
                logging.warning(f"Mapped central inbound port {central_port} was not found; node {node['url']} skipped for this inbound")
                continue

            central_inbound, _ = central_pair
            node_inbound = node_by_port.get(node_port)
            # payload = self._node_inbound_payload(central_inbound, node_port, node_inbound)
            payload = self._node_inbound_payload(central_inbound, node_port, node_inbound, node)
            if node_inbound and node_inbound.get('id') is not None:
                self.api_manager.update_inbound(node, node_session, node_inbound['id'], payload)
            else:
                self.api_manager.add_inbound(node, node_session, payload)
                added = True

        if added:
            node_inbounds = self.api_manager.get_inbounds(node, node_session)
            node_by_port = {}
            for inbound in node_inbounds:
                port = self._inbound_port(inbound)
                if port is not None:
                    node_by_port[port] = inbound

        targets = []
        for mapping in mappings:
            central_port = mapping.get("central_port")
            node_port = mapping.get("node_port")
            central_pair = central_by_port.get(central_port)
            if not central_pair:
                continue
            node_inbound = node_by_port.get(node_port)
            if not node_inbound or node_inbound.get('id') is None:
                logging.error(f"Mapped node inbound port {node_port} was not found on {node['url']}; client sync skipped for central port {central_port}")
                continue
            central_inbound, c_clients = central_pair
            targets.append({
                "central_inbound": central_inbound,
                "central_clients": c_clients,
                "central_id": central_inbound['id'],
                "node_inbound": node_inbound,
                "node_id": node_inbound['id'],
            })
        return targets

    # def _sync_clients_for_target(self, central, central_session, node, node_session, target, now_ms):
    #     central_inbound = target["central_inbound"]
    #     c_clients = target["central_clients"]
    #     central_id = target["central_id"]
    #     node_id = target["node_id"]
    #     node_inbound = target.get("node_inbound")

    #     n_clients = []
    #     if node_inbound:
    #         n_clients = self._parse_inbound_clients(node_inbound)

    #     protocol = (central_inbound.get('protocol') or '').lower()

    #     n_client_map = {self._client_key(cl, protocol): cl for cl in n_clients if self._client_key(cl, protocol)}
    #     c_client_map = {self._client_key(cl, protocol): cl for cl in c_clients if self._client_key(cl, protocol)}

    #     # --- 1) If central has fresh SAFU clients: push them directly to node, skip merging
    #     if any(self._is_safu_fresh(ccl) for ccl in c_clients):
    #         for k, ccl in c_client_map.items():
    #             if not self._is_safu_fresh(ccl):
    #                 continue
    #             if k in n_client_map:
    #                 nid = self._client_id_for_api(n_client_map[k], protocol)
    #                 if nid is not None:
    #                     try:
    #                         self.api_manager.update_client(node, node_session, nid, node_id, ccl)
    #                     except Exception as _e:
    #                         logging.error(f"Failed to push SAFU from central to node for client {k}: {_e}")
    #             else:
    #                 try:
    #                     self.api_manager.add_client(node, node_session, node_id, ccl)
    #                 except Exception as _e:
    #                     logging.error(f"Failed to add SAFU client {k} to node: {_e}")

    #     else:
    #         # --- 2) If central does not have fresh SAFU: only promote active start time from node to central if needed
    #         for k, ccl in c_client_map.items():
    #             ncl = n_client_map.get(k)
    #             if not ncl:
    #                 continue

    #             central_exp = self._to_int(ccl.get('expiryTime'), 0)
    #             node_exp = self._to_int(ncl.get('expiryTime'), 0)

    #             central_started_active = central_exp > now_ms
    #             node_started_active = node_exp > now_ms

    #             should_promote = (not central_started_active) and node_started_active
    #             if should_promote:
    #                 merged = node_exp if central_exp <= 0 else min(central_exp, node_exp)
    #                 if merged != central_exp and merged > now_ms:
    #                     ccl['expiryTime'] = merged
    #                     if 'startAfterFirstUse' in ccl and ccl.get('startAfterFirstUse') is True:
    #                         ccl['startAfterFirstUse'] = False
    #                     try:
    #                         client_id = self._client_id_for_api(ccl, protocol) or self._client_id_for_api(ncl, protocol)
    #                         if client_id is None:
    #                             logging.warning(f"[SAFU-MERGE] Missing clientId for protocol={protocol} key={k} on inbound {central_id}; central update skipped.")
    #                         else:
    #                             self.api_manager.update_client(central, central_session, client_id, central_id, ccl)
    #                             logging.info(f"[SAFU-MERGE] expiryTime merged to central for client {k} (inbound {central_id}): {central_exp} -> {merged}")
    #                     except Exception as _e:
    #                         logging.error(f"Failed to update central client {k} after SAFU merge: {_e}")

    #     # --- 3) Final PUSH: central version (after above policy) to node
    #     for ccl in c_clients:
    #         k = self._client_key(ccl, protocol)
    #         if k in n_client_map:
    #             nid = self._client_id_for_api(n_client_map[k], protocol)
    #             try:
    #                 self.api_manager.update_client(node, node_session, nid, node_id, ccl)
    #             except Exception as _e:
    #                 logging.error(f"Failed to update client {k} on node: {_e}")
    #             n_client_map.pop(k, None)
    #         else:
    #             try:
    #                 self.api_manager.add_client(node, node_session, node_id, ccl)
    #             except Exception as _e:
    #                 logging.error(f"Failed to add client {k} on node: {_e}")

    #     for k, ncl in list(n_client_map.items()):
    #         n_clid = self._client_id_for_api(ncl, protocol)
    #         if n_clid is not None:
    #             try:
    #                 self.api_manager.delete_client(node, node_session, node_id, n_clid)
    #             except Exception as _e:
    #                 logging.error(f"Failed to delete extra client {k} on node: {_e}")
    def _sync_clients_for_target(self, central, central_session, node, node_session, target, now_ms):
        central_inbound = target["central_inbound"]
        c_clients = target["central_clients"]
        central_id = target["central_id"]
        node_id = target["node_id"]
        node_inbound = target.get("node_inbound")

        suffix = node.get("email_suffix", "")
        
        # Helper: Safely append suffix before pushing to node
        def _with_suffix(cl):
            if not suffix or not isinstance(cl, dict) or 'email' not in cl: return cl
            out = deepcopy(cl)
            out['email'] = f"{out['email']}{suffix}"
            return out

        n_clients = []
        if node_inbound:
            n_clients = self._parse_inbound_clients(node_inbound)
            # STRIP suffix from node data so the merge logic calculates purely
            if suffix:
                for cl in n_clients:
                    if 'email' in cl and cl['email'].endswith(suffix):
                        cl['email'] = cl['email'][:-len(suffix)]

        protocol = (central_inbound.get('protocol') or '').lower()
        n_client_map = {self._client_key(cl, protocol): cl for cl in n_clients if self._client_key(cl, protocol)}
        c_client_map = {self._client_key(cl, protocol): cl for cl in c_clients if self._client_key(cl, protocol)}

        # --- 1) SAFU Clients
        if any(self._is_safu_fresh(ccl) for ccl in c_clients):
            for k, ccl in c_client_map.items():
                if not self._is_safu_fresh(ccl):
                    continue
                ccl_push = _with_suffix(ccl)
                if k in n_client_map:
                    nid = self._client_id_for_api(ccl_push, protocol)
                    if nid is not None:
                        try:
                            self.api_manager.update_client(node, node_session, nid, node_id, ccl_push)
                        except Exception as _e:
                            logging.error(f"Failed to push SAFU from central to node for client {k}: {_e}")
                else:
                    try:
                        self.api_manager.add_client(node, node_session, node_id, ccl_push)
                    except Exception as _e:
                        logging.error(f"Failed to add SAFU client {k} to node: {_e}")
        else:
            # --- 2) Central Promote (Uses PURE email mapping, unchanged)
            for k, ccl in c_client_map.items():
                ncl = n_client_map.get(k)
                if not ncl:
                    continue
                central_exp = self._to_int(ccl.get('expiryTime'), 0)
                node_exp = self._to_int(ncl.get('expiryTime'), 0)
                central_started_active = central_exp > now_ms
                node_started_active = node_exp > now_ms

                if (not central_started_active) and node_started_active:
                    merged = node_exp if central_exp <= 0 else min(central_exp, node_exp)
                    if merged != central_exp and merged > now_ms:
                        ccl['expiryTime'] = merged
                        if 'startAfterFirstUse' in ccl and ccl.get('startAfterFirstUse') is True:
                            ccl['startAfterFirstUse'] = False
                        try:
                            client_id = self._client_id_for_api(ccl, protocol) or self._client_id_for_api(ncl, protocol)
                            if client_id is not None:
                                self.api_manager.update_client(central, central_session, client_id, central_id, ccl)
                        except Exception as _e:
                            logging.error(f"Failed to update central client {k} after SAFU merge: {_e}")

        # --- 3) Final PUSH
        for ccl in c_clients:
            k = self._client_key(ccl, protocol)
            ccl_push = _with_suffix(ccl)
            if k in n_client_map:
                nid = self._client_id_for_api(ccl_push, protocol)
                try:
                    self.api_manager.update_client(node, node_session, nid, node_id, ccl_push)
                except Exception as _e:
                    logging.error(f"Failed to update client {k} on node: {_e}")
                n_client_map.pop(k, None)
            else:
                try:
                    self.api_manager.add_client(node, node_session, node_id, ccl_push)
                except Exception as _e:
                    logging.error(f"Failed to add client {k} on node: {_e}")

        for k, ncl in list(n_client_map.items()):
            ncl_push = _with_suffix(ncl)
            n_clid = self._client_id_for_api(ncl_push, protocol)
            if n_clid is not None:
                try:
                    self.api_manager.delete_client(node, node_session, node_id, n_clid)
                except Exception as _e:
                    logging.error(f"Failed to delete extra client {k} on node: {_e}")

    def _traffic_nodes_by_email(self, central_inbounds, nodes):
        by_email = {}
        seen = {}
        for node in nodes:
            mappings = self._inbound_mappings(node)
            if mappings:
                central_ports = {mapping.get("central_port") for mapping in mappings}
                inbounds = [inbound for inbound in central_inbounds if self._inbound_port(inbound) in central_ports]
            else:
                inbounds = central_inbounds

            node_key = self._server_state_key(node)
            for inbound in inbounds:
                for email in self._extract_inbound_emails(inbound):
                    seen_for_email = seen.setdefault(email, set())
                    if node_key in seen_for_email:
                        continue
                    by_email.setdefault(email, []).append(node)
                    seen_for_email.add(node_key)
        return by_email

    # -------------------------------
    # Inbounds & Clients synchronization
    # -------------------------------
    def sync_inbounds_and_clients(self):
        central = self.config_manager.get_central_server()
        nodes = self.config_manager.get_nodes()

        try:
            central_session = self.api_manager.login(central)
            central_inbounds = self.api_manager.get_inbounds(central, central_session)
            if not central_inbounds:
                logging.error("No inbounds retrieved from central server, skipping sync")
                return
        except Exception as e:
            logging.error(f"Failed to connect to central server: {e}")
            return

        parsed_central = [(ib, self._parse_inbound_clients(ib)) for ib in central_inbounds]

        for node in nodes:
            try:
                node_session = self.api_manager.login(node)
                node_inbounds = self.api_manager.get_inbounds(node, node_session)

                if self._inbound_mappings(node):
                    sync_targets = self._sync_mapped_node_inbounds(node, node_session, parsed_central, node_inbounds)
                else:
                    sync_targets = self._sync_legacy_node_inbounds(node, node_session, parsed_central, node_inbounds)

                now_ms = self._now_ms()
                for target in sync_targets:
                    self._sync_clients_for_target(central, central_session, node, node_session, target, now_ms)

            except Exception as e:
                logging.error(f"Error syncing with node {node['url']}: {e}")

    # -------------------------------
    # Traffic synchronization (V2)
    # -------------------------------
    def _fetch_node_traffic_parallel(self, nodes_by_key, node_sessions, email):
        """Parallelize traffic reads (I/O-bound only). Writes remain serial."""
        # NEW: Query the node using the suffixed email
        currents_by_server = {}
        futures = {}
        max_workers = min(len(node_sessions), self.config_manager.net().get('max_workers', 8))
        if max_workers <= 0:
            max_workers = 1

        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            for srv_key, sess in node_sessions.items():
                node = nodes_by_key.get(srv_key)
                if not node or not sess:
                    continue
                node_email = f"{email}{node.get('email_suffix', '')}" if node.get('email_suffix') else email
                # futures[ex.submit(self.api_manager.get_client_traffic, node, sess, email)] = srv_key
                futures[ex.submit(self.api_manager.get_client_traffic, node, sess, node_email)] = srv_key

            for fut in as_completed(futures):
                srv_key = futures[fut]
                try:
                    n_up, n_down = fut.result()
                    currents_by_server[srv_key] = (n_up, n_down)
                except Exception as e:
                    logging.error(f"Traffic fetch failed for {email} on {srv_key}: {e}")
                    # مهم: روی خطا baseline لمس نشه → None برای skip در حلقه‌ی دلتا
                    currents_by_server[srv_key] = None

        return currents_by_server

    def sync_traffic(self):
        central = self.config_manager.get_central_server()
        nodes = self.config_manager.get_nodes()
        net_opts = self.config_manager.net()
        central_key = self._server_state_key(central)

        # دلخواه: سقف دلتا در هر اینتروال (بایت). اگر 0 یا منفی، غیرفعال.
        delta_cap = int(net_opts.get('delta_max_bytes_per_interval', 0) or 0)

        # Login to central server
        try:
            central_sess = self.api_manager.login(central)
        except Exception as e:
            logging.error(f"Failed to connect to central server: {e}")
            return

        # Get client list from central server
        try:
            central_inbounds = self.api_manager.get_inbounds(central, central_sess)
            if not central_inbounds:
                logging.error("No inbounds retrieved from central server, skipping traffic sync")
                return
        except Exception as e:
            logging.error(f"Failed to get inbounds from central server: {e}")
            return

        traffic_nodes_by_email = self._traffic_nodes_by_email(central_inbounds, nodes)
        client_emails = set(traffic_nodes_by_email.keys())

        # Login to nodes (optional)
        node_sessions = {}
        used_node_keys = {self._server_state_key(node) for targets in traffic_nodes_by_email.values() for node in targets}
        for node in nodes:
            node_key = self._server_state_key(node)
            if node_key not in used_node_keys:
                continue
            try:
                node_sessions[node_key] = self.api_manager.login(node)
            except Exception as e:
                logging.error(f"Failed to login node {node['url']}: {e}")

        nodes_by_key = {self._server_state_key(node): node for node in nodes}
        parallel_reads = net_opts.get('parallel_node_calls', True)

        for email in client_emails:
            try:
                email_node_keys = {self._server_state_key(node) for node in traffic_nodes_by_email.get(email, [])}
                email_node_sessions = {
                    srv_key: sess
                    for srv_key, sess in node_sessions.items()
                    if srv_key in email_node_keys
                }

                # 1) Read current traffic from all servers
                currents_by_server = {}
                c_up, c_down = self.api_manager.get_client_traffic(central, central_sess, email)
                currents_by_server[central_key] = (c_up, c_down)

                if parallel_reads and email_node_sessions:
                    currents_by_server.update(
                        self._fetch_node_traffic_parallel(nodes_by_key, email_node_sessions, email)
                    )
                else:
                    for srv_key, sess in email_node_sessions.items():
                        node = nodes_by_key.get(srv_key)
                        if not node or not sess:
                            continue
                        try:
                            node_email = f"{email}{node.get('email_suffix', '')}" if node.get('email_suffix') else email
                            # n_up, n_down = self.api_manager.get_client_traffic(node, sess, email)
                            n_up, n_down = self.api_manager.get_client_traffic(node, sess, node_email)
                            currents_by_server[srv_key] = (n_up, n_down)
                        except Exception as e:
                            logging.error(f"Traffic fetch failed for {email} on {srv_key}: {e}")
                            # مهم: روی خطا baseline لمس نشه → None برای skip در حلقه‌ی دلتا
                            currents_by_server[srv_key] = None

                # 2) Detect first time or central reset
                last_central = self.traffic_state_manager.get_last_counter(email, central_key)
                if last_central is None:
                    # First observation of this user -> start cycle at central snapshot
                    self.traffic_state_manager.reset_cycle(email, currents_by_server, central_key)
                    total_up, total_down = currents_by_server[central_key]

                    # Write total to central + nodes; سپس baseline هر سرور = total (اگر write موفق بود)
                    try:
                        self.api_manager.update_client_traffic(central, central_sess, email, total_up, total_down)
                        self.traffic_state_manager.set_last_counter(email, central_key, total_up, total_down)
                    except Exception as e:
                        logging.error(f"[INIT] Failed to write total to central for {email}: {e}")

                    for srv_key, sess in email_node_sessions.items():
                        node = nodes_by_key.get(srv_key)
                        if node and sess:
                            try:
                                # self.api_manager.update_client_traffic(node, sess, email, total_up, total_down)
                                node_email = f"{email}{node.get('email_suffix', '')}" if node.get('email_suffix') else email
                                self.api_manager.update_client_traffic(node, sess, node_email, total_up, total_down)
                                self.traffic_state_manager.set_last_counter(email, srv_key, total_up, total_down)
                            except Exception as e:
                                logging.error(f"[INIT] Failed to write total to node {srv_key} for {email}: {e}")

                    # total را در state هم بنویسیم تا پایدار باشد
                    self.traffic_state_manager.set_total(email, total_up, total_down)

                    logging.info(f"[INIT] {email}: total set to central current ({total_up},{total_down}); baselines initialized & aligned to total; node_totals cleared.")
                    continue

                last_cu, last_cd = last_central
                # IMPORTANT: consider central reset only if BOTH counters dropped (real reset)
                central_reset = (c_up < last_cu) and (c_down < last_cd)
                if central_reset:
                    # Start a new cycle (central reset) using current observations as baselines
                    self.traffic_state_manager.reset_cycle(email, currents_by_server, central_key)
                    total_up, total_down = currents_by_server[central_key]

                    # Write total to central + nodes; سپس baseline هر سرور = total (اگر write موفق بود)
                    try:
                        self.api_manager.update_client_traffic(central, central_sess, email, total_up, total_down)
                        self.traffic_state_manager.set_last_counter(email, central_key, total_up, total_down)
                    except Exception as e:
                        logging.error(f"[CENTRAL RESET] Failed to write total to central for {email}: {e}")

                    for srv_key, sess in email_node_sessions.items():
                        node = nodes_by_key.get(srv_key)
                        if node and sess:
                            try:
                                # self.api_manager.update_client_traffic(node, sess, email, total_up, total_down)
                                node_email = f"{email}{node.get('email_suffix', '')}" if node.get('email_suffix') else email
                                self.api_manager.update_client_traffic(node, sess, node_email, total_up, total_down)
                                self.traffic_state_manager.set_last_counter(email, srv_key, total_up, total_down)
                            except Exception as e:
                                logging.error(f"[CENTRAL RESET] Failed to write total to node {srv_key} for {email}: {e}")

                    # total را هم ذخیره می‌کنیم
                    self.traffic_state_manager.set_total(email, total_up, total_down)

                    logging.warning(
                        f"[CENTRAL RESET] {email}: total reset to central current ({total_up},{total_down}); baselines reinitialized & aligned; node_totals cleared."
                    )
                    continue

                # 3) If no central reset: calculate per-server deltas (Scenario 1..3)
                total_up, total_down = self.traffic_state_manager.get_total(email)
                added_up, added_down = 0, 0

                for srv_url, cur_pair in currents_by_server.items():
                    # اگر خواندن نود fail بوده، این چرخه برای آن نود را نادیده بگیر و baseline را لمس نکن
                    if cur_pair is None:
                        logging.warning(f"[SKIP NODE] {email} @ {srv_url}: traffic read failed; keeping previous baseline.")
                        continue

                    cur_up, cur_down = cur_pair
                    last = self.traffic_state_manager.get_last_counter(email, srv_url)
                    if last is None:
                        # First observation from this server: baseline = current (delta 0)
                        self.traffic_state_manager.set_last_counter(email, srv_url, cur_up, cur_down)
                        continue

                    last_up, last_down = last

                    # Real reset on this node: BOTH directions dropped -> delta=0
                    if (cur_up < last_up) and (cur_down < last_down):
                        du = 0
                        dd = 0
                        logging.warning(
                            f"[NODE COUNTER DROP] {email} @ {srv_url}: "
                            f"last=({last_up},{last_down}) -> cur=({cur_up},{cur_down}); treat as reset (delta=0)."
                        )
                    else:
                        # Safe component-wise delta (no negatives)
                        du = max(0, cur_up - last_up)
                        dd = max(0, cur_down - last_down)

                    # دلتا غیرعادی را محدود کنیم (اختیاری)
                    if delta_cap > 0:
                        if (du + dd) > delta_cap:
                            logging.warning(f"[DELTA CLAMP] {email} @ {srv_url}: (du+dd)={(du+dd)} > cap={delta_cap}; clamped to 0 for this interval.")
                            du = 0
                            dd = 0

                    # Always update per-node baseline to the current observation
                    self.traffic_state_manager.set_last_counter(email, srv_url, cur_up, cur_down)

                    # Accumulate only positive deltas
                    if du > 0 or dd > 0:
                        added_up += du
                        added_down += dd
                        self.traffic_state_manager.add_node_delta(email, srv_url, du, dd)

                # 4) Add deltas and save new total (only if changed)
                changed = False
                if added_up != 0 or added_down != 0:
                    total_up += added_up
                    total_down += added_down
                    changed = self.traffic_state_manager.set_total(email, total_up, total_down)

                # 5) Write total to central and nodes; سپس baseline سرورِ موفق = total
                if changed:
                    # Central first
                    central_written = False
                    try:
                        self.api_manager.update_client_traffic(central, central_sess, email, total_up, total_down)
                        self.traffic_state_manager.set_last_counter(email, central_key, total_up, total_down)
                        central_written = True
                    except Exception as e:
                        logging.error(f"[WRITE] Failed to write total to central for {email}: {e}")

                    # Nodes
                    for srv_key, sess in email_node_sessions.items():
                        node = nodes_by_key.get(srv_key)
                        if not node or not sess:
                            continue
                        try:
                            # self.api_manager.update_client_traffic(node, sess, email, total_up, total_down)
                            node_email = f"{email}{node.get('email_suffix', '')}" if node.get('email_suffix') else email
                            self.api_manager.update_client_traffic(node, sess, node_email, total_up, total_down)
                            # فقط اگر write موفق بود baseline را هم‌راستا کنیم
                            self.traffic_state_manager.set_last_counter(email, srv_key, total_up, total_down)
                        except Exception as e:
                            logging.error(f"[WRITE] Failed to write total to node {srv_key} for {email}: {e}")

                    logging.debug(f"[DELTA ADD] {email}: +({added_up},{added_down}) -> total=({total_up},{total_down})")

            except Exception as e:
                logging.error(f"Error syncing traffic for {email}: {e}")
