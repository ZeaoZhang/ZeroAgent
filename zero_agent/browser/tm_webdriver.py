import json, threading, time, uuid, queue, socket, requests, traceback
from typing import Any
from simple_websocket_server import WebSocketServer, WebSocket
import bottle
from bottle import request

class Session:
    def __init__(self, session_id, info, client=None):
        self.id = session_id
        self.info = info
        self.connect_at = time.time()
        self.disconnect_at = None
        self.type = info.get('type', 'ws')
        self.ws_client = client if self.type in ('ws', 'ext_ws') else None
        self.http_queue = client if self.type == 'http' else None
    @property
    def url(self): return self.info.get('url', '')
    def is_active(self):
        if self.type == 'http' and time.time() - self.connect_at > 60: self.mark_disconnected()
        return self.disconnect_at is None
    def reconnect(self, client, info):
        self.info = info
        self.type = info.get('type', 'ws')
        if self.type in ('ws', 'ext_ws'):
            self.ws_client = client
            self.http_queue = None
        elif self.type == 'http':
            self.http_queue = client
        self.connect_at = time.time()
        self.disconnect_at = None
    def mark_disconnected(self):
        if self.is_active(): print(f"Tab disconnected: {self.url} (Session: {self.id})")
        self.disconnect_at = time.time()


class TMWebDriver:  
    def __init__(self, host: str = '127.0.0.1', port: int = 18765):  
        self.host, self.port = host, port
        self._init_state()
        self.is_remote = socket.socket().connect_ex((host, port+1)) == 0
        if not self.is_remote:  
            self.start_ws_server()  
            self.start_http_server()
        else:
            self.remote = f'http://{self.host}:{self.port+1}/link'

    def _init_state(self) -> None:
        """Reset per-connection state. Kept separate so tests can build a driver."""
        self.sessions, self.results, self.acks = {}, {}, {}
        self.default_session_id = None
        self.latest_session_id = None
        self.ext_client = None
        self._cdp_event_buffer: list[dict[str, Any]] = []
        self._event_lock = threading.Lock()
        self._event_limit = 2000
        self._send_lock = threading.Lock()

    def start_http_server(self):
        self.app = app = bottle.Bottle()

        @app.route('/api/longpoll', method=['GET', 'POST'])
        def long_poll():
            data = request.json
            session_id = data.get('sessionId')  
            session_info = {'url': data.get('url'), 'title': data.get('title', ''), 'type': 'http'}  
            if session_id not in self.sessions: 
                session = Session(session_id, session_info, queue.Queue())
                print(f"Browser http connected: {session.url} (Session: {session_id})")  
                self.sessions[session_id] = session
            session = self.sessions[session_id]
            if session.disconnect_at is not None and session.type != 'http': session.reconnect(queue.Queue(), session_info)
            session.disconnect_at = None
            if session.type == 'http': msgQ = session.http_queue
            else: return json.dumps({"id": "", "ret": "use ws"})
            session.connect_at = start_time = time.time()
            while time.time() - start_time < 5:
                try:
                    msg = msgQ.get(timeout=0.2)
                    try: self.acks[json.loads(msg).get('id','')] = True
                    except Exception: traceback.print_exc()
                    return msg
                except queue.Empty: continue
            return json.dumps({"id": "", "ret": "next long-poll"})

        @app.route('/api/result', method=['GET','POST'])
        def result():
            data = request.json
            if data.get('type') == 'result':  
                self.results[data.get('id')] = {'success': True, 'data': data.get('result'), 'newTabs': data.get('newTabs', [])}  
            elif data.get('type') == 'error':  
                self.results[data.get('id')] = {'success': False, 'data': data.get('error'), 'newTabs': data.get('newTabs', [])}  
            return 'ok'

        @app.route('/link', method=['GET','POST'])
        def link():
            data = request.json
            if data.get('cmd') == 'get_all_sessions': return json.dumps({'r': self.get_all_sessions()}, ensure_ascii=False)  
            if data.get('cmd') == 'find_session': 
                url_pattern = data.get('url_pattern', '')
                return json.dumps({'r': self.find_session(url_pattern)}, ensure_ascii=False)
            if data.get('cmd') == 'execute_js':
                session_id = data.get('sessionId')
                code = data.get('code')
                timeout = float(data.get('timeout', 10.0))
                try:
                    result = self.execute_js(code, timeout=timeout, session_id=session_id)
                    print('[remote result]', (str(code)[:50] + ' RESULT:' +str(result)[:50]).replace('\n', ' '))
                    return json.dumps({'r': result}, ensure_ascii=False)
                except Exception as e:
                    return json.dumps({'r': {'error': str(e)}}, ensure_ascii=False)
            if data.get('cmd') == 'ext_command':
                try:
                    result = self.send_ext_command(
                        data.get('command') or {},
                        tab_id=data.get('tabId'),
                        timeout=float(data.get('timeout', 15.0)),
                    )
                    return json.dumps({'r': result}, ensure_ascii=False)
                except Exception as e:
                    return json.dumps({'r': {'error': str(e)}}, ensure_ascii=False)
            if data.get('cmd') == 'get_cdp_events':
                return json.dumps({'r': self.get_cdp_events(
                    tab_id=data.get('tabId'),
                    clear=bool(data.get('clear')),
                    methods=data.get('methods'),
                    limit=int(data.get('limit', 200)),
                )}, ensure_ascii=False)
            if data.get('cmd') == 'clear_cdp_events':
                self.clear_cdp_events(tab_id=data.get('tabId'))
                return json.dumps({'r': True}, ensure_ascii=False)
            return 'ok'
        def run():
            from wsgiref.simple_server import make_server, WSGIServer, WSGIRequestHandler
            from socketserver import ThreadingMixIn
            class _T(ThreadingMixIn, WSGIServer): pass
            class _H(WSGIRequestHandler):
                def log_request(self, *a): pass
            make_server(self.host, self.port+1, app, server_class=_T, handler_class=_H).serve_forever()
        http_thread = threading.Thread(target=run, daemon=True)
        http_thread.start()  

    def clean_sessions(self):
        sids = list(self.sessions.keys())
        for sid in sids:
            session = self.sessions[sid]
            if not session.is_active() and time.time() - session.disconnect_at > 600:
                del self.sessions[sid]
    
    def start_ws_server(self) -> None:  
        driver = self  
        class JSExecutor(WebSocket):  
            def handle(self) -> None:  
                try:  
                    data = json.loads(self.data)  
                    if data.get('type') == 'ready':  
                        session_id = data.get('sessionId')  
                        session_info = {'url': data.get('url'), 'title': data.get('title', ''),
                            'connected_at': time.time(), 'type': 'ws'}  
                        driver._register_client(session_id, self, session_info)  
                    elif data.get('type') in ['ext_ready', 'tabs_update']:
                        driver.ext_client = self
                        tabs = data.get('tabs', [])
                        current_tab_ids = {str(tab['id']) for tab in tabs}
                        print(f"Received tabs update: {current_tab_ids}")
                        for sid in list(driver.sessions.keys()):
                            sess = driver.sessions[sid]
                            if sess.type == 'ext_ws' and sid not in current_tab_ids:
                                sess.mark_disconnected()
                        for tab in tabs:
                            session_id = str(tab['id'])
                            session_info = {**tab, 'url': tab.get('url'), 'title': tab.get('title', ''),
                                'connected_at': time.time(), 'type': 'ext_ws'}
                            sess = driver.sessions.get(session_id)
                            if sess and sess.is_active(): sess.info = session_info
                            else: driver._register_client(session_id, self, session_info)
                    elif data.get('type') == 'newTabs':
                        for tab in data.get('tabs', []):
                            session_id = str(tab.get('id'))
                            if not session_id or session_id == 'None':
                                continue
                            session_info = {**tab, 'url': tab.get('url'), 'title': tab.get('title', ''),
                                'connected_at': time.time(), 'type': 'ext_ws'}
                            sess = driver.sessions.get(session_id)
                            if sess and sess.is_active(): sess.info = session_info
                            else: driver._register_client(session_id, self, session_info)
                    elif data.get('type') == 'cdp_event':
                        driver._record_cdp_event(
                            data.get('tabId'),
                            data.get('method'),
                            data.get('params'),
                            data.get('ts'),
                        )
                    elif data.get('type') == 'ack': driver.acks[data.get('id','')] = True
                    elif data.get('type') == 'result':  
                        driver.results[data.get('id')] = {'success': True, 'data': data.get('result'), 'newTabs': data.get('newTabs', [])}  
                    elif data.get('type') == 'error':  
                        driver.results[data.get('id')] = {'success': False, 'data': data.get('error'), 'newTabs': data.get('newTabs', [])}  
                except Exception as e:  
                    print(f"Error handling message: {e}")  
                    if hasattr(self, 'data'): print(self.data)  
            def connected(self): (f"New connection from {self.address}")  
            def handle_close(self): 
                print(f"WS Connection closed: {self.address}")
                if driver.ext_client is self:
                    driver.ext_client = None
                driver._unregister_client(self)  
        
        self.server = WebSocketServer(self.host, self.port, JSExecutor)  
        server_thread = threading.Thread(target=self.server.serve_forever)  
        server_thread.daemon = True  
        server_thread.start()  
        print(f"WebSocket server running on ws://{self.host}:{self.port}")  
    
    def _register_client(self, session_id: str, client: WebSocket, session_info) -> None:  
        is_new_session = session_id not in self.sessions

        if is_new_session:
            session = Session(session_id, session_info, client)
            self.sessions[session_id] = session            
            print(f"New tab connected: {session.url} (Session: {session_id})")  
        else:
            session = self.sessions[session_id]
            session.reconnect(client, session_info)
            print(f"Tab reconnected: {session.url} (Session: {session_id})")  

        self.latest_session_id = session_id
        if self.default_session_id is None: self.default_session_id = session_id 
    
    def _unregister_client(self, client: WebSocket) -> None:  
        for session in self.sessions.values():
            if session.ws_client == client: session.mark_disconnected()
    
    def execute_js(self, code, timeout=15, session_id=None) -> Any:  
        if session_id is None: session_id = self.default_session_id  
        if self.is_remote:
            print('remote_execute_js')
            response = self._remote_cmd({"cmd": "execute_js", "sessionId": session_id, 
                                         "code": code, "timeout": str(timeout)},
                                        timeout=max(30.0, timeout + 5)).get('r', {})
            if response.get('error'): raise Exception(response['error'])
            return response
 
        session = self.sessions.get(session_id)
        if not session or not session.is_active(): 
            time.sleep(3)
            session = self.sessions.get(session_id)
            if not session or not session.is_active(): 
                alive_sessions = [s for s in self.sessions.values() if s.is_active()]
                if alive_sessions:
                    session = alive_sessions[0]  
                    print(f"会话 {session_id} 未连接，自动切换到最新活动会话: {session.id}")
                    session_id = self.default_session_id = session.id
                if not session or not session.is_active(): 
                    raise ValueError(f"会话ID {session_id} 未连接")  

        tp = session.type
        if tp not in ('ws', 'http', 'ext_ws'):
            raise ValueError(f"Unsupported session type: {tp}")
        exec_id = str(uuid.uuid4())  
        payload_dict = {'id': exec_id, 'code': code}
        if tp == 'ext_ws': payload_dict['tabId'] = int(session.id)
        payload = json.dumps(payload_dict)

        with self._send_lock:
            if tp in ['ws', 'ext_ws']: session.ws_client.send_message(payload)
            elif tp == 'http': session.http_queue.put(payload)
        return self._await_result(exec_id, tp, timeout, session)

    def _await_result(self, exec_id: str, transport: str, timeout: float,
                      session: Session | None = None,
                      *, raise_on_timeout: bool = False) -> dict[str, Any]:
        """Wait for one WS/HTTP result, preserving the legacy timeout semantics."""
        start_time = time.time()  
        self.clean_sessions() 
        hasjump = acked = False

        while exec_id not in self.results:  
            time.sleep(0.2)  
            if session is None and transport == 'ext_ws' and self.ext_client is None:
                raise ConnectionError("浏览器扩展连接已断开")
            if not acked and exec_id in self.acks:
                acked = True; start_time = time.time()
            if session is not None and transport in ['ws', 'ext_ws']:
                if not session.is_active(): hasjump = True
                if hasjump and session.is_active():
                    return {'result': f"Session {session.id} reloaded.", "closed":1}
            if time.time() - start_time > timeout:  
                self.acks.pop(exec_id, None)
                if raise_on_timeout:
                    raise TimeoutError(f"No response data in {timeout}s")
                if transport in ['ws', 'ext_ws']:
                    if hasjump: return {'result': f"Session {session.id} reloaded and new page is loading...", 'closed':1}
                    if acked: return {"result": f"No response data in {timeout}s (ACK received, script may still be running)"}
                    return {"result": f"No response data in {timeout}s (no ACK, script may not have been delivered)"}
                if acked: return {"result": f"Session {session.id} no response in {timeout}s (delivered but no result)"}
                return {"result": f"Session {session.id} no response in {timeout}s (script not polled)"}
        
        result = self.results.pop(exec_id)  
        if exec_id in self.acks: self.acks.pop(exec_id)
        if not result['success']: raise Exception(result['data'])  
        rr = {'data': result['data']}
        newtabs = result.get('newTabs', []); [x.pop('ts', None) for x in newtabs]
        if newtabs: rr['newTabs'] = newtabs
        return rr

    @staticmethod
    def _int_tab_id(tab_id: Any) -> int:
        try:
            return int(tab_id)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"无效的标签页 ID: {tab_id!r}") from exc

    @staticmethod
    def _ext_data(result: Any) -> Any:
        return result.get('data') if isinstance(result, dict) else result

    def send_ext_command(self, command: dict[str, Any], tab_id: Any = None,
                         timeout: float = 15) -> dict[str, Any]:
        """Send a command to the extension over the connection-level WS.

        Unlike execute_js this does not require a live tab session, so tabs can
        still be listed, created, or closed when the last scriptable tab is gone.
        """
        if self.is_remote:
            response = self._remote_cmd({
                "cmd": "ext_command",
                "command": command,
                "tabId": tab_id,
                "timeout": str(timeout),
            }, timeout=max(30.0, timeout + 5)).get('r', {})
            if isinstance(response, dict) and response.get('error'):
                raise Exception(response['error'])
            return response

        client = self.ext_client
        if client is None:
            for session in self.sessions.values():
                if session.type == 'ext_ws' and session.is_active():
                    client = session.ws_client
                    self.ext_client = client
                    break
        if client is None:
            raise ConnectionError("浏览器扩展未连接（确认扩展已加载并连接到 18765 端口）")

        exec_id = str(uuid.uuid4())
        payload_dict = {'id': exec_id, 'code': json.dumps(command, ensure_ascii=False)}
        if tab_id is not None:
            payload_dict['tabId'] = self._int_tab_id(tab_id)
        try:
            with self._send_lock:
                client.send_message(json.dumps(payload_dict, ensure_ascii=False))
        except Exception as exc:
            raise ConnectionError(f"发送扩展命令失败: {exc}") from exc
        return self._await_result(exec_id, 'ext_ws', timeout, None, raise_on_timeout=True)

    def list_tabs(self, include_all: bool = False) -> list[dict[str, Any]]:
        command = {'cmd': 'tabs', 'method': 'list'}
        if include_all: command['includeAll'] = True
        data = self._ext_data(self.send_ext_command(command))
        return data if isinstance(data, list) else []

    def close_tab(self, tab_id: Any) -> Any:
        numeric = self._int_tab_id(tab_id)
        data = self._ext_data(self.send_ext_command({
            'cmd': 'tabs', 'method': 'close', 'tabId': numeric,
        }, tab_id=numeric))
        return data if data is not None else True

    def activate_tab(self, tab_id: Any) -> bool:
        numeric = self._int_tab_id(tab_id)
        self.send_ext_command(
            {'cmd': 'tabs', 'method': 'switch', 'tabId': numeric}, tab_id=numeric,
        )
        self.default_session_id = str(numeric)
        return True

    def navigate_tab(self, tab_id: Any, url: str) -> Any:
        numeric = self._int_tab_id(tab_id)
        return self._ext_data(self.send_ext_command({
            'cmd': 'tabs', 'method': 'navigate', 'tabId': numeric, 'url': url,
        }, tab_id=numeric))

    def create_tab(self, url: str, active: bool = False, wait: float = 5.0) -> Any:
        data = self._ext_data(self.send_ext_command({
            'cmd': 'tabs', 'method': 'create', 'url': url,
            'active': bool(active), 'waitMs': int(max(0.0, wait) * 1000),
        }, timeout=max(15.0, wait + 5)))
        tab_id = data.get('id') if isinstance(data, dict) else None
        if tab_id is None:
            return data
        deadline = time.time() + max(0.0, wait)
        while time.time() < deadline:
            session = next((s for s in self.get_all_sessions() if str(s.get('id')) == str(tab_id)), None)
            if session:
                merged = {**data, **session}
                if active or self.default_session_id is None:
                    self.default_session_id = str(tab_id)
                return merged
            time.sleep(0.2)
        return data

    def upload(self, tab_id: Any, selector: str, files: list[str],
               index: int = 0, timeout: float = 15) -> dict[str, Any]:
        """Attach local files to a page's file input through CDP.

        The evaluate and setFileInputFiles calls run in one extension batch so
        the RemoteObject id stays valid across the detached one-shot debugger.
        """
        numeric = self._int_tab_id(tab_id)
        selector_json = json.dumps(str(selector), ensure_ascii=False)
        index = max(0, int(index or 0))
        paths = [str(item) for item in files or [] if str(item).strip()]
        if not paths:
            raise ValueError("files 不能为空")
        expression = (
            f"document.querySelectorAll({selector_json})[{index}]"
        )
        summary_expression = f"""(() => {{
          const el = document.querySelectorAll({selector_json})[{index}];
          if (!el) return {{ok: false, error: 'selector not found'}};
          if (el.tagName !== 'INPUT' || String(el.type).toLowerCase() !== 'file') {{
            return {{ok: false, error: 'target is not <input type=file>', tag: el.tagName, type: el.type}};
          }}
          return {{ok: true, multiple: !!el.multiple, accept: el.accept || '',
                   files: Array.from(el.files || []).map(f => ({{name: f.name, size: f.size, type: f.type}}))}};
        }})()"""
        payload = self.send_ext_command({
            'cmd': 'batch',
            'tabId': numeric,
            'commands': [
                {
                    'cmd': 'cdp',
                    'method': 'Runtime.evaluate',
                    'params': {
                        'expression': expression,
                        'objectGroup': 'zeroagent-upload',
                        'returnByValue': False,
                    },
                },
                {
                    'cmd': 'cdp',
                    'method': 'DOM.setFileInputFiles',
                    'params': {
                        'files': paths,
                        'objectId': '$0.result.objectId',
                    },
                },
                {
                    'cmd': 'cdp',
                    'method': 'Runtime.evaluate',
                    'params': {
                        'expression': summary_expression,
                        'returnByValue': True,
                    },
                },
            ],
        }, tab_id=numeric, timeout=timeout)
        data = self._ext_data(payload)
        results = data.get('results') if isinstance(data, dict) else data
        if not isinstance(results, list) or len(results) < 2:
            raise RuntimeError(f"上传命令未执行: {data!r}")
        first, second = results[0], results[1]
        if isinstance(first, dict) and first.get('exceptionDetails'):
            details = first.get('exceptionDetails') or {}
            raise RuntimeError(details.get('text') or '无法定位 file input')
        if isinstance(second, dict) and second.get('error'):
            raise RuntimeError(second.get('error'))
        if isinstance(second, dict) and second.get('ok') is False:
            raise RuntimeError(second.get('error') or 'setFileInputFiles failed')
        summary: Any = None
        if len(results) >= 3 and isinstance(results[2], dict):
            result_obj = results[2].get('result')
            if isinstance(result_obj, dict):
                summary = result_obj.get('value')
        return {'files': paths, 'summary': summary}

    def download(self, method: str = 'list', tab_id: Any = None,
                 timeout: float = 20, **kwargs: Any) -> Any:
        """Drive the extension's chrome.downloads route."""
        command: dict[str, Any] = {'cmd': 'download', 'method': method}
        for key, value in kwargs.items():
            if value is not None:
                command[key] = value
        if 'timeout_ms' in command:
            command['timeoutMs'] = command.pop('timeout_ms')
        return self._ext_data(self.send_ext_command(
            command, tab_id=tab_id, timeout=timeout,
        ))

    def cdp(self, method: str, params: dict[str, Any] | None = None,
            tab_id: Any = None, persistent: bool = False, timeout: float = 15) -> Any:
        tab_id = tab_id if tab_id is not None else self.default_session_id
        if tab_id in (None, ''):
            raise ValueError("没有可用的标签页")
        numeric = self._int_tab_id(tab_id)
        command = {'cmd': 'cdp', 'method': method, 'params': params or {}, 'tabId': numeric}
        if persistent: command['persistent'] = True
        return self._ext_data(self.send_ext_command(command, tab_id=numeric, timeout=timeout))

    def cdp_attach(self, tab_id: Any = None, timeout: float = 15) -> Any:
        return self.cdp('attach', {}, tab_id=tab_id, timeout=timeout)

    def cdp_detach(self, tab_id: Any = None, timeout: float = 15) -> Any:
        return self.cdp('detach', {}, tab_id=tab_id, timeout=timeout)

    def intercept(self, tab_id: Any, rules: list[dict[str, Any]] | None = None,
                  action: str = "apply", timeout: float = 15) -> Any:
        numeric = self._int_tab_id(tab_id)
        command = {'cmd': 'intercept', 'action': action, 'tabId': numeric}
        if rules is not None:
            command['rules'] = rules
        return self._ext_data(self.send_ext_command(command, tab_id=numeric, timeout=timeout))

    def clear_intercept(self, tab_id: Any, timeout: float = 15) -> Any:
        return self.intercept(tab_id, action='clear', timeout=timeout)

    def list_intercepts(self, tab_id: Any, timeout: float = 15) -> list[dict[str, Any]]:
        data = self.intercept(tab_id, action='list', timeout=timeout)
        return data if isinstance(data, list) else []

    def cdp_events(self, tab_id: Any = None, clear: bool = False,
                   methods: list[str] | None = None, limit: int = 200) -> list[dict[str, Any]]:
        command = {'cmd': 'cdpEvents', 'clear': bool(clear), 'limit': int(limit)}
        if methods: command['methods'] = list(methods)
        data = self._ext_data(self.send_ext_command(command, tab_id=tab_id))
        return data if isinstance(data, list) else []

    def _record_cdp_event(self, tab_id: Any, method: str | None,
                          params: Any, ts: Any = None) -> None:
        if tab_id is None or not method:
            return
        event = {
            'tabId': tab_id,
            'method': method,
            'params': params or {},
            'ts': ts if ts is not None else time.time(),
        }
        with self._event_lock:
            self._cdp_event_buffer.append(event)
            if len(self._cdp_event_buffer) > self._event_limit:
                del self._cdp_event_buffer[:len(self._cdp_event_buffer) - self._event_limit]

    def get_cdp_events(self, tab_id: Any = None, clear: bool = False,
                       methods: list[str] | None = None,
                       limit: int = 200) -> list[dict[str, Any]]:
        if self.is_remote:
            return self._remote_cmd({
                'cmd': 'get_cdp_events', 'tabId': tab_id, 'clear': bool(clear),
                'methods': methods, 'limit': int(limit),
            }).get('r', [])
        with self._event_lock:
            events = list(self._cdp_event_buffer)
            if clear: self._cdp_event_buffer.clear()
        if tab_id is not None:
            events = [e for e in events if str(e.get('tabId')) == str(tab_id)]
        if methods:
            allowed = set(methods)
            events = [e for e in events if e.get('method') in allowed]
        if limit and limit > 0:
            events = events[-int(limit):]
        return events

    def clear_cdp_events(self, tab_id: Any = None) -> None:
        if self.is_remote:
            self._remote_cmd({'cmd': 'clear_cdp_events', 'tabId': tab_id})
            return
        with self._event_lock:
            if tab_id is None:
                self._cdp_event_buffer.clear()
            else:
                self._cdp_event_buffer = [
                    e for e in self._cdp_event_buffer if str(e.get('tabId')) != str(tab_id)
                ]
    
    def _remote_cmd(self, cmd, timeout=30):
        try: return requests.post(self.remote, headers={"Content-Type": "application/json"}, json=cmd, timeout=timeout).json()
        except (ConnectionError, requests.exceptions.ConnectionError):
            raise ConnectionError("TMWebDriver master未运行，看tmwebdriver_sop启动master")

    def get_all_sessions(self):  
        if self.is_remote:
            return self._remote_cmd({"cmd": "get_all_sessions"}).get('r', [])
        return [{'id': session.id, **session.info} for session in self.sessions.values()
                if session.is_active()]  

    def get_session_dict(self):
        return {session['id']: session['url'] for session in self.get_all_sessions()}
        
    def find_session(self, url_pattern: str):
        if url_pattern == '': 
            session = self.sessions.get(self.latest_session_id)
            return [(session.id, session.info)] if session else []
        matching_sessions = []  
        for session in self.sessions.values():
            if not session.is_active(): continue
            if 'url' in session.info and url_pattern in session.info['url']:  
                matching_sessions.append((session.id, session.info))  
        return matching_sessions

    def set_session(self, url_pattern: str) -> bool:  
        if self.is_remote:
            matched = self._remote_cmd({"cmd": "find_session", "url_pattern": url_pattern}).get('r', [])
        else:
            matched = self.find_session(url_pattern)
        if not matched: return print(f"警告: 未找到URL包含 '{url_pattern}' 的会话")  
        if len(matched) > 1: print(f"警告: 找到多个URL包含 '{url_pattern}' 的会话，选择第一个")  
        self.default_session_id, info = matched[0]
        print(f"成功设置默认会话: {self.default_session_id}: {info['url']}")  
        return self.default_session_id  
    
    def jump(self, url, timeout=10): self.execute_js(f"window.location.href='{url}'", timeout=timeout)
    
if __name__ == "__main__":
    driver = TMWebDriver(host='127.0.0.1', port=18765)