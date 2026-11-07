#!/usr/bin/python3

########################################################################
# DellEMC
#
# Module contains implementation of IpmiSensor and IpmiFru classes that
# provide Sensor's and FRU's information respectively.
#
########################################################################

import subprocess
import re
import time

# IPMI Request Network Function Codes
NetFn_SensorEvent = 0x04
NetFn_Storage = 0x0A

# IPMI Sensor Device Commands
Cmd_GetSensorReadingFactors = 0x23
Cmd_GetSensorThreshold = 0x27
Cmd_GetSensorReading = 0x2D

# IPMI FRU Device Commands
Cmd_ReadFRUData = 0x11

# ipmitool reply cache (kamelnetworks/sonic-buildimage#163)
#
# Every sensor attribute is an `ipmitool` subprocess, one BMC round trip
# over KCS takes 0.3-0.8 s, and a reading costs two of them (Get Sensor
# Reading plus Get Sensor Reading Factors). thermalctld asks the same
# question several times per cycle (a fan's presence and speed, a thermal's
# thresholds), psud polls every 3 s, and a thermalctld cycle on the S5232F
# took over 30 s.
#
# Duplicate readings and FRU replies expire within psud's existing 3-second
# polling interval. FRU data determines airflow and must not survive a missed
# replacement indefinitely. Sensor factors/thresholds describe BMC sensors and
# retain their longer TTL. Writes invalidate only this process's cache.
CACHE_TTL_READ = 3
CACHE_TTL_STATIC = 300
CACHE_TTLS = {
    (NetFn_SensorEvent, Cmd_GetSensorReading): CACHE_TTL_READ,
    (NetFn_SensorEvent, Cmd_GetSensorReadingFactors): CACHE_TTL_STATIC,
    (NetFn_SensorEvent, Cmd_GetSensorThreshold): CACHE_TTL_STATIC,
    (NetFn_Storage, Cmd_ReadFRUData): CACHE_TTL_READ,
}
_cache = {}
_discrete_states = {}


def clear_cache():
    """Forgets every cached ipmitool reply."""
    _cache.clear()
    _discrete_states.clear()


def _observe_discrete(sensor_id, state):
    # A presence/status transition can mean a replacement. Drop all FRU
    # replies because this shared helper has no platform-specific FRU map.
    previous = _discrete_states.get(sensor_id)
    if previous is not None and previous != state:
        for key in list(_cache):
            if isinstance(key, tuple) and (key[0] == 'fru' or
                    key[:2] == (NetFn_Storage, Cmd_ReadFRUData)):
                del _cache[key]
    _discrete_states[sensor_id] = state


def _cached(key, ttl, run):
    """
    Returns the cached reply for key if it is still valid, else run()'s,
    which is cached for ttl seconds (CACHE_TTL_READ if it is empty, i.e.
    the command failed). ttl None means: not cacheable, and whatever is
    cached may describe the state before this command; ttl 0 means the
    cache is off.
    """
    if ttl is None:
        _cache.clear()
    if not ttl:
        return run()

    now = time.monotonic()
    entry = _cache.get(key)
    if entry is not None and entry[0] > now:
        return entry[1]

    result = run()
    if not result:
        ttl = min(ttl, CACHE_TTL_READ)
    _cache[key] = (now + ttl, result)
    return result


def _raw_command_key(args):
    """
    Returns the cache key for the arguments of an `ipmitool raw` command
    (its bytes, normalised to integers) and the TTL for its reply.
    """
    try:
        words = tuple(int(word, 0) for word in args.split())
    except ValueError:
        return args, None
    if len(words) < 2:
        return words, None
    return words, CACHE_TTLS.get(words[:2])


def _run_ipmitool(command):
    """
    Runs an ipmitool command line and returns its output, or '' if it
    failed.
    """
    result = ""
    try:
        proc = subprocess.Popen(command.split(), stdout=subprocess.PIPE,
                                universal_newlines=True, stderr=subprocess.STDOUT)
        stdout = proc.communicate()[0]
        proc.wait()
        if not proc.returncode:
            result = stdout.rstrip('\n')
    except EnvironmentError:
        pass

    return result


def get_ipmitool_raw_output(args):
    """
    Returns a list the elements of which are the individual bytes of
    ipmitool raw <cmd> command output.
    """
    key, ttl = _raw_command_key(args)
    result = _cached(key, ttl, lambda: _run_ipmitool("ipmitool raw {}".format(args)))

    result_bytes = list()
    for i in result.split():
        result_bytes.append(int(i, 16))

    return result_bytes

class IpmiSensor(object):

    # Sensor Threshold types and their respective bit masks
    THRESHOLD_BIT_MASK = {
        "LowerNonCritical"    : 0,
        "LowerCritical"       : 1,
        "LowerNonRecoverable" : 2,
        "UpperNonCritical"    : 3,
        "UpperCritical"       : 4,
        "UpperNonRecoverable" : 5
    }

    def __init__(self, sensor_id, is_discrete=False):
        self.id = sensor_id
        self.is_discrete = is_discrete

    def _get_converted_sensor_reading(self, raw_value):
        """
        Returns a 2 element tuple(bool, int) in which first element
        provides the validity of the reading and the second element is
        the converted sensor reading
        """
        # Get Sensor Reading Factors
        cmd_args = "{} {} {} {}".format(NetFn_SensorEvent,
                                        Cmd_GetSensorReadingFactors,
                                        self.id, raw_value)
        factors = get_ipmitool_raw_output(cmd_args)

        if len(factors) != 7:
            return False, 0

        # Compute Twos complement
        def get_twos_complement(val, bits):
            if val & (1 << (bits - 1)):
                val = val - (1 << bits)
            return val

        # Calculate actual sensor value from the raw sensor value
        # using the sensor reading factors.
        M = get_twos_complement(((factors[2] & 0xC0) << 8) | factors[1], 10)
        B = get_twos_complement(((factors[4] & 0xC0) << 8) | factors[3], 10)
        R_exp = get_twos_complement((factors[6] & 0xF0) >> 4, 4)
        B_exp = get_twos_complement(factors[6] & 0x0F, 4)

        if R_exp < 0:
            converted_reading = ((M * raw_value) + (B * 10**B_exp)) / 10**(-R_exp)
        else:
            converted_reading = ((M * raw_value) + (B * 10**B_exp)) * 10**R_exp

        return True, converted_reading

    def get_reading(self):
        """
        For Threshold sensors, returns the sensor reading.
        For Discrete sensors, returns the state value.

        Returns:
            A tuple (bool, int) where the first element provides the
            validity of the reading and the second element provides the
            sensor reading/state value.
        """
        # Get Sensor Reading
        cmd_args = "{} {} {}".format(NetFn_SensorEvent, Cmd_GetSensorReading,
                                     self.id)
        output = get_ipmitool_raw_output(cmd_args)
        if self.is_discrete:
            _observe_discrete(self.id, tuple(output))
        if len(output) != 4:
            return False, 0

        # Check reading/state unavailable
        if output[1] & 0x20:
            return False, 0

        if self.is_discrete:
            state = ((output[3] & 0x7F) << 8) | output[2]
            return True, state
        else:
            return self._get_converted_sensor_reading(output[0])

    def get_threshold(self, threshold_type):
        """
        Returns the sensor's threshold value for a given threshold type.

        Args:
            threshold_type (str) - one of the below mentioned
                                   threshold type strings

                "LowerNonCritical"
                "LowerCritical"
                "LowerNonRecoverable"
                "UpperNonCritical"
                "UpperCritical"
                "UpperNonRecoverable"
        Returns:
            A tuple (bool, int) where the first element provides the
            validity of that threshold and second element provides the
            threshold value.
        """
        # Thresholds are not valid for discrete sensors
        if self.is_discrete:
            raise TypeError("Threshold is not applicable for Discrete Sensor")

        if threshold_type not in list(self.THRESHOLD_BIT_MASK.keys()):
            raise ValueError("Invalid threshold type {} provided. Valid types "
                             "are {}".format(threshold_type,
                                             list(self.THRESHOLD_BIT_MASK.keys())))

        bit_mask = self.THRESHOLD_BIT_MASK[threshold_type]

        # Get Sensor Threshold
        cmd_args = "{} {} {}".format(NetFn_SensorEvent, Cmd_GetSensorThreshold,
                                     self.id)
        thresholds = get_ipmitool_raw_output(cmd_args)
        if len(thresholds) != 7:
            return False, 0

        valid_thresholds = thresholds.pop(0)
        # Check whether particular threshold is readable
        if valid_thresholds & (1 << bit_mask):
            return self._get_converted_sensor_reading(thresholds[bit_mask])
        else:
            return False, 0

class IpmiFru(object):

    def __init__(self, fru_id):
        self.id = fru_id

    def _get_ipmitool_fru_print(self):
        command = "ipmitool fru print {}".format(self.id)
        return _cached(('fru', int(self.id)),
                       CACHE_TTLS.get((NetFn_Storage, Cmd_ReadFRUData), 0),
                       lambda: _run_ipmitool(command))

    def _get_from_fru(self, info):
        """
        Returns a string containing the info from FRU
        """
        fru_output = self._get_ipmitool_fru_print()
        if not fru_output:
            return "NA"

        info_req = re.search(r"%s\s*:(.*)" % info, fru_output)
        if not info_req:
            return "NA"

        return info_req.group(1).strip()

    def get_board_serial(self):
        """
        Returns a string containing the Serial Number of the device.
        """
        return self._get_from_fru('Board Serial')

    def get_board_part_number(self):
        """
        Returns a string containing the Part Number of the device.
        """
        return self._get_from_fru('Board Part Number')

    def get_board_mfr_id(self):
        """
        Returns a string containing the manufacturer id of the FRU.
        """
        return self._get_from_fru('Board Mfg')

    def get_board_product(self):
        """
        Returns a string containing the manufacturer id of the FRU.
        """
        return self._get_from_fru('Board Product')

    def get_fru_data(self, offset, count=1):
        """
        Reads and returns the FRU data at the provided offset.

        Args:
            offset (int) - FRU offset to read
            count (int) - Number of bytes to read [optional, default = 1]
        Returns:
            A tuple (bool, list(int)) where the first element provides
            the validity of the data read and the second element is a
            list, the elements of which are the individual bytes of the
            FRU data read.
        """
        result_bytes = list()

        offset_LSB = offset & 0xFF
        offset_MSB = (offset >> 8) & 0xFF
        args = "{} {} {} {} {} {}".format(NetFn_Storage, Cmd_ReadFRUData,
                                          self.id, offset_LSB, offset_MSB,
                                          count)
        key, ttl = _raw_command_key(args)
        result = _cached(key, ttl, lambda: _run_ipmitool("ipmitool raw {}".format(args)))

        if not result:
            return False, result_bytes

        for i in result.split():
            result_bytes.append(int(i, 16))

        read_count = result_bytes.pop(0)
        if read_count != count:
            return False, result_bytes
        else:
            return True, result_bytes
