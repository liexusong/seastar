#!/usr/bin/env python3
from contextlib import contextmanager
import socket
import struct
import random
import argparse
import time
import unittest

server_addr = None
call = None

@contextmanager
def tcp_connection():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect(server_addr)
    def call(msg):
        s.send(msg.encode())
        return s.recv(16*1024)
    yield call
    s.close()

def recv_all(s):
    m = b''
    while True:
        data = s.recv(1024)
        if not data:
            break
        m += data
    return m

def tcp_call(msg):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect(server_addr)
    s.send(msg.encode())
    s.shutdown(socket.SHUT_WR)
    data = recv_all(s)
    s.close()
    return data

def udp_call(msg):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    this_req_id = random.randint(-32768, 32767)

    datagram = struct.pack(">hhhh", this_req_id, 0, 1, 0) + msg.encode()
    sock.sendto(datagram, server_addr)

    messages = {}
    n_determined = None
    while True:
        data, addr = sock.recvfrom(1500)
        req_id, seq, n, res = struct.unpack_from(">hhhh", data)
        content = data[8:]

        if n_determined and n_determined != n:
            raise Exception('Inconsitent number of total messages, %d and %d' % (n_determined, n))
        n_determined = n

        if req_id != this_req_id:
            raise Exception('Invalid request id: ' + req_id + ', expected ' + this_req_id)

        if seq in messages:
            raise Exception('Duplicate message for seq=' + seq)

        messages[seq] = content
        if len(messages) == n:
            break

    msg = b''
    for k, v in sorted(messages.items(), key=lambda e: e[0]):
        msg += v

    sock.close()
    return msg

class TcpSpecificTests(unittest.TestCase):
    def test_recovers_from_errors_in_the_stream(self):
        with tcp_connection() as conn:
            self.assertEqual(conn('get\r\n'), b'ERROR\r\n')
            self.assertEqual(conn('get key\r\n'), b'END\r\n')

    def test_incomplete_command_results_in_error(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect(server_addr)
        s.send(b'get')
        s.shutdown(socket.SHUT_WR)
        self.assertEqual(recv_all(s), b'ERROR\r\n')
        s.close()

    def test_stream_closed_results_in_error(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect(server_addr)
        s.shutdown(socket.SHUT_WR)
        self.assertEqual(recv_all(s), b'')
        s.close()

class TestCommands(unittest.TestCase):
    def call_set(self, key, value, flags=0, expiry=0):
        self.assertEqual(call('set %s %d %d %d\r\n%s\r\n' % (key, flags, expiry, len(value), value)), b'STORED\r\n')

    def call_delete(self, key):
        self.assertEqual(call('delete %s\r\n' % key), b'DELETED\r\n')

    def test_basic_commands(self):
        self.assertEqual(call('get key\r\n'), b'END\r\n')
        self.assertEqual(call('set key 0 0 5\r\nhello\r\n'), b'STORED\r\n')
        self.assertEqual(call('get key\r\n'), b'VALUE key 0 5\r\nhello\r\nEND\r\n')
        self.assertEqual(call('delete key\r\n'), b'DELETED\r\n')
        self.assertEqual(call('delete key\r\n'), b'NOT_FOUND\r\n')
        self.assertEqual(call('get key\r\n'), b'END\r\n')

    def test_error_handling(self):
        self.assertEqual(call('get\r\n'), b'ERROR\r\n')

    def test_expiry(self):
        self.assertEqual(call('set key 0 1 5\r\nhello\r\n'), b'STORED\r\n')
        self.assertEqual(call('get key\r\n'), b'VALUE key 0 5\r\nhello\r\nEND\r\n')
        time.sleep(1)
        self.assertEqual(call('get key\r\n'), b'END\r\n')

    def test_expiry_at_epoch_time(self):
        expiry = int(time.time()) + 1
        self.assertEqual(call('set key 0 %d 5\r\nhello\r\n' % expiry), b'STORED\r\n')
        self.assertEqual(call('get key\r\n'), b'VALUE key 0 5\r\nhello\r\nEND\r\n')
        time.sleep(2)
        self.assertEqual(call('get key\r\n'), b'END\r\n')

    def test_mutliple_keys_in_get(self):
        self.assertEqual(call('set key1 0 0 2\r\nv1\r\n'), b'STORED\r\n')
        self.assertEqual(call('set key 0 0 2\r\nv2\r\n'), b'STORED\r\n')
        self.assertEqual(call('get key1 key\r\n'), b'VALUE key1 0 2\r\nv1\r\nVALUE key 0 2\r\nv2\r\nEND\r\n')
        self.call_delete("key")
        self.call_delete("key1")

    def test_response_spanning_many_datagrams(self):
        key1_data = '1' * 1000
        key2_data = '2' * 1000
        key3_data = '3' * 1000
        self.call_set('key1', key1_data)
        self.call_set('key2', key2_data)
        self.call_set('key3', key3_data)
        self.assertEqual(call('get key1 key2 key3\r\n').decode(),
            'VALUE key1 0 %d\r\n%s\r\n' \
            'VALUE key2 0 %d\r\n%s\r\n' \
            'VALUE key3 0 %d\r\n%s\r\n' \
            'END\r\n' % (len(key1_data), key1_data, len(key2_data), key2_data, len(key3_data), key3_data))
        self.call_delete('key1')
        self.call_delete('key2')
        self.call_delete('key3')

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="memcache protocol tests")
    parser.add_argument('--server', '-s', action="store", help="server adddress in <host>:<port> format", default="localhost:11211")
    parser.add_argument('--udp', '-U', action="store_true", help="Use UDP protocol")
    args = parser.parse_args()

    host, port = args.server.split(':')
    server_addr = (host, int(port))

    call = udp_call if args.udp else tcp_call

    runner = unittest.TextTestRunner()
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTest(loader.loadTestsFromTestCase(TestCommands))
    if not args.udp:
        suite.addTest(loader.loadTestsFromTestCase(TcpSpecificTests))
    runner.run(suite)