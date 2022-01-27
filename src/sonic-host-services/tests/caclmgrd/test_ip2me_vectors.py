from unittest.mock import call

"""
    caclmgrd ip2me block test vector
"""
CACLMGRD_IP2ME_TEST_VECTOR = [
    [
        "Only MGMT interface, no block",
        {
            "config_db": {
                "LOOPBACK_INTERFACE": {},
                "VLAN_INTERFACE": {},
                "PORTCHANNEL_INTERFACE": {},
                "INTERFACE": {},
                "DEVICE_METADATA": {
                    "localhost": {
                    }
                },
            },
            "return": [
            ],
        },
    ],
    [
        "One interface of each type, /32",
        {
            "config_db": {
                "LOOPBACK_INTERFACE": {
                    "Loopback0|10.10.10.10/32": {},
                },
                "VLAN_INTERFACE": {
                    "Vlan110|10.10.11.10/32": {},
                },
                "PORTCHANNEL_INTERFACE": {
                    "PortChannel0001|10.10.12.10/32": {},
                },
                "INTERFACE": {
                    "Ethernet0|10.10.13.10/32": {}
                },
                "DEVICE_METADATA": {
                    "localhost": {
                    }
                },
            },
            "return": [
                "iptables -A INPUT -d 10.10.10.10 -j DROP -m comment --comment 'Block IP2ME on interface Loopback0'",
                "iptables -A INPUT -d 10.10.11.10 -j DROP -m comment --comment 'Block IP2ME on interface Vlan110'",
                "iptables -A INPUT -d 10.10.12.10 -j DROP -m comment --comment 'Block IP2ME on interface PortChannel0001'",
                "iptables -A INPUT -d 10.10.13.10 -j DROP -m comment --comment 'Block IP2ME on interface Ethernet0'"
            ],
        },
    ]
]
