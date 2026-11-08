#!/usr/bin/env python3
"""Packet test helper; check_bridge_nd_guard_netns.sh creates its isolated bridge."""
import sys
import socket
import struct

source = bytes.fromhex('020000000001')
dest = bytes.fromhex('020000000002')
send = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))
send.bind(('send0', 0))
receive = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))
receive.bind(('recv0', 0))
receive.settimeout(0.15)

for tagged in [False, True]:
    for extension in [False, True]:
        for kind in [128, 133, 134, 135, 136, 137]:
            marker = f'NDGUARD:{tagged}:{extension}:{kind}'.encode()
            payload = struct.pack('!BBHI', kind, 0, 0, 0) + marker
            pseudo = (socket.inet_pton(socket.AF_INET6, 'fe80::1') +
                      socket.inet_pton(socket.AF_INET6, 'fe80::2') +
                      struct.pack('!I3xB', len(payload), 58))
            checksum_input = pseudo + payload
            if len(checksum_input) % 2:
                checksum_input += b'\0'
            total = sum(struct.unpack('!' + 'H' * (len(checksum_input) // 2), checksum_input))
            while total >> 16:
                total = (total & 0xffff) + (total >> 16)
            payload = payload[:2] + struct.pack('!H', (~total) & 0xffff) + payload[4:]
            if extension:
                # Hop-by-Hop header: final next-header ICMPv6, six Pad1 options.
                payload = bytes([58, 0]) + bytes(6) + payload
            ipv6 = struct.pack(
                '!IHBB16s16s', 6 << 28, len(payload),
                0 if extension else 58, 255,
                socket.inet_pton(socket.AF_INET6, 'fe80::1'),
                socket.inet_pton(socket.AF_INET6, 'fe80::2'),
            )
            ethertype = bytes.fromhex('8100039886dd' if tagged else '86dd')
            send.send(dest + source + ethertype + ipv6 + payload)
            seen = False
            while True:
                try:
                    packet = receive.recv(2048)
                except TimeoutError:
                    break
                if marker in packet:
                    seen = True
            assert seen == ('--unguarded' in sys.argv or kind == 128), (tagged, extension, kind, seen)
print('PASS: ' + ('unguarded ND and echo reach receiver' if '--unguarded' in sys.argv else 'ND suppressed and echo preserved') + ' for untagged/tagged IPv6, with/without Hop-by-Hop headers')
