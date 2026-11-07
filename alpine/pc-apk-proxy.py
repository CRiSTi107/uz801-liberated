#!/usr/bin/env python3
"""Tiny forward HTTP/HTTPS proxy, run on the PC so the dongle's chroot can
reach the internet (for `apk`) over the USB link while the dongle's own
cellular WAN is unusable.

Run on the PC:   python3 pc-apk-proxy.py 0.0.0.0 8899
Then in the chroot set:  http_proxy / https_proxy = http://<PC-rndis-ip>:8899

Handles absolute-URI GET/HEAD/POST (plain HTTP) and CONNECT (TLS tunnel).
No auth, bind it only to the dongle LAN. Dev helper, not production.
"""
import socket, sys, threading

def pipe(a, b):
    try:
        while True:
            data = a.recv(65536)
            if not data:
                break
            b.sendall(data)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try: s.shutdown(socket.SHUT_RDWR)
            except OSError: pass

def handle(client):
    try:
        client.settimeout(30)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = client.recv(4096)
            if not chunk:
                return
            buf += chunk
            if len(buf) > 65536:
                return
        head, _, rest = buf.partition(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        method, target, _ver = lines[0].split(b" ", 2)
        method = method.upper()

        if method == b"CONNECT":                      # TLS tunnel
            host, _, port = target.decode().partition(":")
            port = int(port or 443)
            upstream = socket.create_connection((host, port), timeout=30)
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            if rest:
                upstream.sendall(rest)
            threading.Thread(target=pipe, args=(client, upstream), daemon=True).start()
            pipe(upstream, client)
            return

        # Plain HTTP with absolute URI: GET http://host/path HTTP/1.1
        t = target.decode()
        if t.startswith("http://"):
            t = t[7:]
        host, _, path = t.partition("/")
        host, _, port = host.partition(":")
        port = int(port or 80)
        path = "/" + path
        upstream = socket.create_connection((host, port), timeout=30)
        # rebuild request with origin-form path; drop proxy-only headers
        out = [b"%s %s %s" % (method, path.encode(), _ver)]
        for ln in lines[1:]:
            low = ln.lower()
            if low.startswith(b"proxy-connection"):
                continue
            out.append(ln)
        upstream.sendall(b"\r\n".join(out) + b"\r\n\r\n" + rest)
        threading.Thread(target=pipe, args=(client, upstream), daemon=True).start()
        pipe(upstream, client)
    except Exception as e:
        try: client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n" + str(e).encode())
        except OSError: pass
    finally:
        try: client.close()
        except OSError: pass

def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "0.0.0.0"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8899
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(128)
    print(f"[proxy] listening on {host}:{port}", flush=True)
    while True:
        c, _ = srv.accept()
        threading.Thread(target=handle, args=(c,), daemon=True).start()

if __name__ == "__main__":
    main()
