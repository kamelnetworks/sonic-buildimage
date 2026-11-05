#!/usr/bin/env python

import os
import struct
import subprocess
import time
from mmap import *
from sonic_py_common.device_info import get_platform_and_hwsku

SCALE = 16
BIN_BITS = 8
EMPTY_STRING = ""
HOST_CHK_CMD = "docker > /dev/null 2>&1"

# ipmitool reply cache (kamelnetworks/sonic-buildimage#163)
#
# Every fan, PSU and thermal attribute on this platform is an `ipmitool`
# subprocess, and one BMC round trip over KCS takes 0.3-0.8 s on the
# Seastone 2. thermalctld asks the same question several times per cycle
# (a fan's presence three times, its speed four times, a thermal's
# thresholds twice, the fan FRU every minute) and psud polls every 3 s, so
# the BMC was saturated and a thermalctld cycle took 45-50 s.
#
# Read-only ipmitool commands are therefore answered from this per-process
# cache: sensor readings, presence, CPLD registers and LEDs for a few
# seconds, FRU records and sensor thresholds (constant while a unit is
# inserted) for longer. Writes are never cached and drop the cache, so a
# read after a write sees the new state. A failed command is cached only
# for the short time, so it is retried soon.
CACHE_TTL_READ = 10
CACHE_TTL_STATIC = 300
# (lower-cased command prefix, TTL)
CACHE_TTLS = (
    ("ipmitool raw 0x04 0x2d ", CACHE_TTL_READ),         # Get Sensor Reading
    ("ipmitool raw 0x04 0x27 ", CACHE_TTL_STATIC),       # Get Sensor Threshold
    ("ipmitool raw 0x3a 0x03 0x03 ", CACHE_TTL_READ),    # fan / PSU presence
    ("ipmitool raw 0x3a 0x0c 0x00 0x02 ", CACHE_TTL_READ),  # CPLD register read
    ("ipmitool raw 0x3a 0x0b ", CACHE_TTL_READ),         # fan LED
    ("ipmitool fru list ", CACHE_TTL_STATIC),
    ("ipmitool fru print ", CACHE_TTL_STATIC),
)
_cache = {}


def clear_cache():
    """Forgets every cached ipmitool reply."""
    _cache.clear()


def _cache_key_and_ttl(cmd):
    """
    Returns the cache key for a command line and how long its reply may be
    reused: 0 for a command that is not cached, None for an ipmitool command
    that is not known to be read-only.
    """
    key = ' '.join(cmd.split()).lower()
    for prefix, ttl in CACHE_TTLS:
        if key.startswith(prefix):
            return key, ttl
    return key, None if key.startswith('ipmitool ') else 0


def run_ipmitool(cmd):
    """
    Runs an ipmitool command line through the cache.
    Returns:
        (exit status, output) as subprocess.getstatusoutput does.
    """
    key, ttl = _cache_key_and_ttl(cmd)
    if ttl is None:
        # A write (LED, threshold, power) or an unknown ipmitool command:
        # whatever is cached may describe the state before it.
        _cache.clear()
    if not ttl:
        return subprocess.getstatusoutput(cmd)

    now = time.monotonic()
    entry = _cache.get(key)
    if entry is not None and entry[0] > now:
        return entry[1]

    ret, data = subprocess.getstatusoutput(cmd)
    if ret != 0:
        ttl = min(ttl, CACHE_TTL_READ)
    _cache[key] = (now + ttl, (ret, data))
    return ret, data


class APIHelper():

    def __init__(self):
        (self.platform, self.hwsku) = get_platform_and_hwsku()

    def get_register_value(self, getreg_path, register):
        cmd = "echo {1} > {0}; cat {0}".format(getreg_path, register)
        ret, data = subprocess.getstatusoutput(cmd)
        if ret != 0:
            return None
        else:
            return data

    def hex_to_bin(self, ini_string):
        return bin(int(ini_string, SCALE)).zfill(BIN_BITS)

    def is_host(self):
        rv, _ = subprocess.getstatusoutput(HOST_CHK_CMD)
        return (rv == 0)

    def pci_get_value(self, resource, offset):
        status = True
        result = ""
        try:
            fd = os.open(resource, os.O_RDWR)
            mm = mmap(fd, 0)
            mm.seek(int(offset))
            read_data_stream = mm.read(4)
            result = struct.unpack('I', read_data_stream)
        except Exception:
            status = False
        return status, result

    def run_command(self, cmd):
        status = True
        result = ""
        ret, data = run_ipmitool(cmd)
        if ret != 0:
            status = False
        else:
            result = data

        return status, result

    def read_txt_file(self, file_path):
        try:
            with open(file_path, 'r') as fd:
                data = fd.read()
                return data.strip()
        except IOError:
            pass
        return None

    def read_one_line_file(self, file_path):
        try:
            with open(file_path, 'r') as fd:
                data = fd.readline()
                return data.strip()
        except IOError:
            pass
        return None

    def search_file_by_contain(self, directory, search_str, file_start):
        for dirpath, dirnames, files in os.walk(directory):
            for name in files:
                file_path = os.path.join(dirpath, name)
                if name.startswith(file_start) and search_str in self.read_txt_file(file_path):
                    return dirpath
        return None

    def write_file(self, file_path, data):
        try:
            with open(file_path, 'w') as fd:
                fd.write(str(data))
                return True
        except Exception:
            pass
        return False

    def ipmi_raw(self, netfn, cmd):
        status = True
        result = ""
        cmd = "ipmitool raw {} {}".format(str(netfn), str(cmd))
        ret, data = run_ipmitool(cmd)
        if ret != 0:
            status = False
        else:
            result = data

        return status, result

    def ipmi_fru_id(self, id, key=None):
        status = True
        result = ""
        cmd = "ipmitool fru print {}".format(str(
            id)) if not key else "ipmitool fru print {0} | grep '{1}' ".format(str(id), str(key))

        ret, data = run_ipmitool(cmd)
        if ret != 0:
            status = False
        else:
            result = data

        return status, result

    def ipmi_set_ss_thres(self, id, threshold_key, value):
        status = True
        result = ""
        cmd = "ipmitool sensor thresh '{}' {} {}".format(
            str(id), str(threshold_key), str(value))
        ret, data = run_ipmitool(cmd)
        if ret != 0:
            status = False
        else:
            result = data

        return status, result

    def fru_decode_product_serial(self, data):

        if data and data[4] != 00:
            start_product_info = ord(data[4]) * 8
            start_format_version = start_product_info
            start_product_info = start_format_version + 1
            start_product_Lang_code = start_product_info + 1
            start_product_Manu_name = start_product_Lang_code + 1
            start_product_Manu_name_length = ord(data[start_product_Manu_name]) & 0x0F
            start_product_name =  start_product_Manu_name + start_product_Manu_name_length + 1
            start_product_name_length = ord(data[start_product_name]) & 0x0F
            start_product_module_number = start_product_name + start_product_name_length +1
            start_product_module_number_length = ord(data[start_product_module_number]) & 0x0F
            start_product_version = start_product_module_number + start_product_module_number_length +1
            start_product_version_length = ord(data[start_product_version]) & 0x0F
            start_product_serial_number = start_product_version + start_product_version_length +1
            start_product_serial_number_length = ord(data[start_product_serial_number]) & 0x1F
            return data[start_product_serial_number+1:start_product_serial_number+start_product_serial_number_length+1]
        return "N/A"

    def fru_decode_product_model(self, data):
        if data and data[4] != 00:
            start_product_info = ord(data[4]) * 8
            start_format_version = start_product_info
            start_product_info = start_format_version + 1
            start_product_lang_code = start_product_info + 1
            start_product_manu_name = start_product_lang_code + 1
            start_product_manu_name_length = ord(data[start_product_manu_name]) & 0x1F
            start_product_name = start_product_manu_name + start_product_manu_name_length + 1
            start_product_name_length = ord(data[start_product_name]) & 0x1F
            start_product_module_number = start_product_name + start_product_name_length + 1
            start_product_module_number_length = ord(data[start_product_module_number]) & 0x1F
            return data[start_product_module_number + 1: start_product_module_number +start_product_module_number_length + 1]
        return "N/A"
 
    def fru_decode_product_name(self, data):

        if data and data[4] != 00:
            start_product_info = ord(data[4]) * 8
            start_format_version = start_product_info
            start_product_info = start_format_version + 1
            start_product_Lang_code = start_product_info + 1
            start_product_Manu_name = start_product_Lang_code + 1
            start_product_Manu_name_length = ord(data[start_product_Manu_name]) & 0x0F
            start_product_name =  start_product_Manu_name + start_product_Manu_name_length + 1
            start_product_name_length = ord(data[start_product_name]) & 0x0F
            return data[start_product_name+1: start_product_name+start_product_name_length+1]

        return "N/A"

    def read_eeprom_sysfs(self,sys_path,sysfs_file):
        sysfs_path = os.path.join(sys_path, sysfs_file)
        try:
            with open(sysfs_path, mode='rb', buffering=0) as fd:
                data = fd.read(256)
                return data
        except Exception:
            pass
        return None
