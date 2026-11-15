import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import time
import threading
import json
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
from forge.server import ForgeHTTPRequestHandler


class ForgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    # 50 concurrent clones open 50+ simultaneous sockets; the default
    # listen backlog (5) drops SYNs and costs seconds of retransmits.
    request_queue_size = 128


def main() -> None:
    env_url = os.environ.get("MAKEFUN_ENV_URL", "").rstrip("/")
    if not env_url:
        sys.stderr.write("[entry] MAKEFUN_ENV_URL is not set: there is no env to register with\n")
        sys.exit(1)

    server = ForgeServer(("0.0.0.0", 0), ForgeHTTPRequestHandler)
    port = server.server_port
    sys.stdout.write(f"[entry] listening on port {port}\n")
    sys.stdout.flush()
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    # Register port with env
    register_url = f"{env_url}/register"
    req_data = json.dumps({"port": port}).encode("utf-8")
    
    registered = False
    attempt = 0
    while not registered:
        try:
            req = urllib.request.Request(
                register_url,
                data=req_data,
                headers={"content-type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status in (200, 409):
                    registered = True
                    break
        except urllib.error.HTTPError as e:
            if e.code in (400, 405):
                sys.stderr.write(f"[entry] register answered HTTP {e.code}: {e.read().decode()}\n")
                sys.exit(1)
        except Exception:
            pass
        
        sleep_time = min(2.0, 0.25 * (2 ** min(attempt, 8)))
        time.sleep(sleep_time)
        attempt += 1

    sys.stdout.write("[entry] registered; waiting for the env to say done\n")
    sys.stdout.flush()

    # Poll done loop
    done_url = f"{env_url}/done"
    attempt = 0
    while True:
        try:
            req = urllib.request.Request(done_url, method="GET")
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status == 200:
                    body = json.loads(resp.read().decode("utf-8"))
                    if body.get("done") is True:
                        break
        except Exception:
            pass
        
        sleep_time = min(2.0, 0.25 * (2 ** min(attempt, 8)))
        time.sleep(sleep_time)
        attempt += 1

    server.server_close()
    sys.stdout.write("[entry] done\n")
    sys.stdout.flush()

if __name__ == "__main__":
    try:
        main()
        sys.exit(0)
    except Exception as err:
        sys.stderr.write(f"[entry] {err}\n")
        sys.exit(1)
