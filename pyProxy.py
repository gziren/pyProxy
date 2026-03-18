#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pure Python Proxy Server (Stable Version)
Fix: [WinError 10038] Non-socket operation error
"""

import sys
import socket
import threading
import argparse
import ipaddress
from urllib.parse import urlparse
import select
import winreg

# Configuration
BUFFER_SIZE = 8192
CONNECT_TIMEOUT = 10
RECV_TIMEOUT = 30
RETRY_COUNT = 2
SO_TIMEOUT = 15

def get_system_proxy():
    """Get Windows system proxy configuration"""
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, 
                            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        proxy_enable = winreg.QueryValueEx(key, "ProxyEnable")[0]
        if not proxy_enable:
            winreg.CloseKey(key)
            return None
        
        proxy_server = winreg.QueryValueEx(key, "ProxyServer")[0]
        winreg.CloseKey(key)
        
        if ':' in proxy_server:
            proxy_host, proxy_port = proxy_server.split(':', 1)
            return (proxy_host, int(proxy_port))
        else:
            return (proxy_server, 8080)
    except Exception as e:
        print(f"Get system proxy failed: {e}")
        return None

class ProxyServer:
    def __init__(self, host, port, allowed_network):
        self.host = host
        self.port = port
        self.allowed_network = ipaddress.ip_network(allowed_network)
        self.system_proxy = get_system_proxy()
        
        if self.system_proxy:
            print(f"System proxy detected: {self.system_proxy[0]}:{self.system_proxy[1]}")
        else:
            print(f"No system proxy detected, connect directly to target")
        
        # Socket optimization
        self.socket_opts = [
            (socket.SOL_SOCKET, socket.SO_REUSEADDR, 1),
            (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),
        ]

    def is_ip_allowed(self, client_ip):
        """Check if client IP is in whitelist"""
        try:
            ip_obj = ipaddress.ip_address(client_ip)
            return ip_obj in self.allowed_network
        except:
            return False

    def create_socket(self):
        """Create optimized socket"""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        for opt in self.socket_opts:
            try:
                s.setsockopt(*opt)
            except:
                pass
        s.settimeout(SO_TIMEOUT)
        return s

    def connect_via_proxy(self, target_host, target_port):
        """Connect to target via system proxy"""
        if self.system_proxy:
            proxy_host, proxy_port = self.system_proxy
            for i in range(RETRY_COUNT):
                try:
                    s = self.create_socket()
                    s.settimeout(CONNECT_TIMEOUT)
                    s.connect((proxy_host, proxy_port))
                    
                    connect_req = f"CONNECT {target_host}:{target_port} HTTP/1.1\r\nHost: {target_host}:{target_port}\r\n\r\n"
                    s.sendall(connect_req.encode('utf-8'))
                    
                    response = s.recv(BUFFER_SIZE)
                    if b"200" in response:
                        print(f"Connect to {target_host}:{target_port} via system proxy success (retry {i})")
                        return s
                    else:
                        s.close()
                        print(f"System proxy return non-200 response: {response[:100]}")
                except Exception as e:
                    print(f"Connect to {target_host}:{target_port} via system proxy failed (retry {i+1}/{RETRY_COUNT}): {e}")
                    if i == RETRY_COUNT - 1:
                        return None
                    continue
        # No system proxy, connect directly
        else:
            for i in range(RETRY_COUNT):
                try:
                    addr_info = socket.getaddrinfo(target_host, target_port, socket.AF_INET, socket.SOCK_STREAM)
                    if not addr_info:
                        raise Exception("DNS resolve failed")
                    target_addr = addr_info[0][4]
                    
                    s = self.create_socket()
                    s.settimeout(CONNECT_TIMEOUT)
                    s.connect(target_addr)
                    print(f"Connect to {target_host}:{target_port} directly success (retry {i})")
                    return s
                except Exception as e:
                    print(f"Connect to {target_host}:{target_port} directly failed (retry {i+1}/{RETRY_COUNT}): {e}")
                    if i == RETRY_COUNT - 1:
                        return None
        return None

    def forward_data(self, src, dst):
        """Forward data (fix 10038 error: avoid duplicate close)"""
        try:
            while True:
                r, _, _ = select.select([src], [], [], RECV_TIMEOUT)
                if not r:
                    break
                data = src.recv(BUFFER_SIZE)
                if not data:
                    break
                dst.sendall(data)
        except Exception as e:
            # Only print critical errors, avoid 10038 noise
            if "10038" not in str(e):
                print(f"Data forward error: {e}")
        # Remove duplicate close to fix 10038

    def handle_https(self, client_socket, first_line):
        """Handle HTTPS request"""
        try:
            target = first_line.split(' ')[1]
            if ':' not in target:
                target += ':443'
            target_host, target_port = target.split(':')
            target_port = int(target_port)

            target_socket = self.connect_via_proxy(target_host, target_port)
            if not target_socket:
                client_socket.send(b"HTTP/1.1 504 Gateway Timeout\r\n\r\nTarget connection timeout")
                client_socket.close()
                return

            client_socket.send(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            client_socket.settimeout(RECV_TIMEOUT)

            # Start forward (daemon thread, no duplicate close)
            t1 = threading.Thread(target=self.forward_data, args=(client_socket, target_socket))
            t2 = threading.Thread(target=self.forward_data, args=(target_socket, client_socket))
            t1.daemon = True
            t2.daemon = True
            t1.start()
            t2.start()

        except Exception as e:
            print(f"HTTPS handle failed: {e}")
            try:
                client_socket.send(b"HTTP/1.1 500 Internal Server Error\r\n\r\n")
            except:
                pass
            client_socket.close()

    def handle_http(self, client_socket, request):
        """Handle HTTP request"""
        try:
            request_str = request.decode('utf-8', errors='ignore')
            request_lines = request_str.split('\n')
            if not request_lines:
                client_socket.close()
                return

            first_line = request_lines[0].strip()
            if not first_line:
                client_socket.close()
                return
                
            parts = first_line.split(' ', 2)
            if len(parts) < 3:
                client_socket.send(b"HTTP/1.1 400 Bad Request\r\n\r\nInvalid request")
                client_socket.close()
                return
                
            method, url, _ = parts

            if not url.startswith('http'):
                host = None
                for line in request_lines:
                    if line.lower().startswith('host:'):
                        host = line.split(':', 1)[1].strip()
                        break
                if host:
                    url = f"http://{host}{url}"
                else:
                    client_socket.send(b"HTTP/1.1 400 Bad Request\r\n\r\nMissing Host header")
                    client_socket.close()
                    return

            parsed_url = urlparse(url)
            target_host = parsed_url.hostname
            target_port = parsed_url.port or 80

            target_socket = self.connect_via_proxy(target_host, target_port)
            if not target_socket:
                client_socket.send(b"HTTP/1.1 504 Gateway Timeout\r\n\r\nHTTP connection timeout")
                client_socket.close()
                return

            target_socket.sendall(request)
            self.forward_data(target_socket, client_socket)

        except Exception as e:
            print(f"HTTP handle failed: {e}")
            try:
                client_socket.send(b"HTTP/1.1 500 Internal Server Error\r\n\r\n")
            except:
                pass
            client_socket.close()

    def handle_client(self, client_socket, client_addr):
        """Handle client connection"""
        client_ip = client_addr[0]
        print(f"Received connection request: {client_ip}:{client_addr[1]}")

        if not self.is_ip_allowed(client_ip):
            print(f"Access denied: {client_ip} not in allowed network {self.allowed_network}")
            try:
                client_socket.send(b"HTTP/1.1 403 Forbidden\r\n\r\nIP not allowed")
            except:
                pass
            client_socket.close()
            return

        try:
            client_socket.settimeout(RECV_TIMEOUT)
            request = client_socket.recv(BUFFER_SIZE)
            if not request:
                client_socket.close()
                return

            request_str = request.decode('utf-8', errors='ignore')
            if request_str.startswith('CONNECT'):
                self.handle_https(client_socket, request_str)
            else:
                self.handle_http(client_socket, request)

        except Exception as e:
            print(f"Client handle failed {client_ip}:{client_addr[1]}: {e}")
            try:
                client_socket.send(b"HTTP/1.1 500 Internal Server Error\r\n\r\n")
            except:
                pass
            client_socket.close()

    def start(self):
        """Start proxy server"""
        self.server_socket = self.create_socket()
        try:
            self.server_socket.bind((self.host, self.port))
            self.server_socket.listen(10)
            print(f"\nProxy server started successfully")
            print(f"Bound to: {self.host}:{self.port}")
            print(f"Allowed network: {self.allowed_network}")
            print(f"Timeout config: connect {CONNECT_TIMEOUT}s | receive {RECV_TIMEOUT}s | retry {RETRY_COUNT} times")
            print(f"System proxy: {self.system_proxy if self.system_proxy else 'Not enabled'}")
            print(f"========================================")
            print(f"Press Ctrl+C to stop server")

            while True:
                try:
                    client_socket, client_addr = self.server_socket.accept()
                    t = threading.Thread(target=self.handle_client, args=(client_socket, client_addr))
                    t.daemon = True
                    t.start()
                except KeyboardInterrupt:
                    print("\nStop signal received, shutting down server...")
                    break
                except Exception as e:
                    print(f"Accept connection failed: {e}")
                    continue

        except socket.error as e:
            print(f"\nProxy server start failed")
            print(f"Error reason: {e}")
            print(f"Check items: 1. IP {self.host} is local NIC IP 2. Port {self.port} is not occupied")
            sys.exit(1)
        finally:
            if self.server_socket:
                try:
                    self.server_socket.close()
                except:
                    pass
            print("Proxy server stopped")

def main():
    parser = argparse.ArgumentParser(description='Pure Python Proxy Server')
    parser.add_argument('--host', default='0.0.0.0', help='Bind to specified NIC IP')
    parser.add_argument('--port', type=int, default=8080, help='Proxy server port')
    parser.add_argument('--allowed', default='10.251.175.0/24', help='Allowed IP network')
    args = parser.parse_args()

    proxy = ProxyServer(args.host, args.port, args.allowed)
    proxy.start()

if __name__ == '__main__':
    main()
