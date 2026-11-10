#!/usr/bin/env python

########################################################################
# DellEMC S5232F
#
# Module contains an implementation of SONiC Platform Base API and
# provides the Thermals' information which are available in the platform
#
########################################################################


try:
    from sonic_platform_base.thermal_base import ThermalBase
    from sonic_platform.ipmihelper import IpmiSensor
except ImportError as e:
    raise ImportError(str(e) + "- required module not found")


class Thermal(ThermalBase):
    """DellEMC Platform-specific Thermal class"""

    # [ Sensor-Name, Sensor-ID ]
    SENSOR_MAPPING = [
        ['CPU On-board', 0xe],
        ['ASIC On-board', 0x2],
        ['System Front Left', 0x3],
        ['System Front Middle', 0x1],
        ['System Front Right', 0x4],
        ['Inlet Airflow Sensor', 0x5],
        ['PSU1 Airflow Sensor', 0x7],
        ['PSU2 Airflow Sensor', 0x8]
    ]

    # Thresholds, in Celsius, for sensors whose BMC sensor data record
    # carries none: { Sensor-Name: (high, high critical) }. The S5232F BMC
    # (Celestica, firmware 0.55) reports no thresholds at all for the
    # 'ASIC On-board' sensor (NPU_Near_temp, 0x2), so get_high_threshold()
    # fell through to the base class, thermalctld stored 'N/A' and
    # system-health stayed Not OK with 'Invalid ASIC On-board temperature
    # data ... threshold=N/A'. The values are the ones Dell's BMC reports for
    # the same sensor on the sister S5248F-ON: 78 C upper non-critical and
    # 80 C upper critical (85 C upper non-recoverable). A threshold the BMC
    # does report always takes precedence over this table. The thresholds
    # can't be repaired on the BMC instead: its SDR marks none of them
    # settable for this sensor, and Set Sensor Threshold is refused with
    # 0xCD (command illegal for specified sensor). A failed BMC read is not
    # replaced by these defaults, see _get_upper_threshold().
    DEFAULT_THRESHOLDS = {
        'ASIC On-board': (78.0, 80.0),
    }

    def __init__(self, thermal_index=0):
        ThermalBase.__init__(self)
        self.index = thermal_index + 1
        self.sensor = IpmiSensor(self.SENSOR_MAPPING[self.index - 1][1])

    def _get_upper_threshold(self, threshold_type, position):
        """
        Returns the BMC's threshold of the given type, else this sensor's
        platform default if the BMC has no such threshold, as a float.
        position is 0 for the high threshold and 1 for the high critical
        threshold in DEFAULT_THRESHOLDS.

        Returns None if the threshold could not be read from the BMC
        (ipmitool failed, short reply): thermalctld then stores 'N/A' and
        system-health reports the ASIC as Not OK, a failed read never
        passes as the default. Raises NotImplementedError if the BMC has
        no such threshold and there is no default.
        """
        is_valid, threshold = self.sensor.get_threshold(threshold_type)
        if is_valid:
            return float(threshold)

        if self.sensor.is_threshold_readable(threshold_type) is not False:
            return None

        defaults = self.DEFAULT_THRESHOLDS.get(self.get_name())
        if defaults is None:
            raise NotImplementedError

        return float(defaults[position])

    def get_name(self):
        """
        Retrieves the name of the thermal

        Returns:
            string: The name of the thermal
        """
        return self.SENSOR_MAPPING[self.index - 1][0]

    def get_presence(self):
        """
        Retrieves the presence of the thermal

        Returns:
            bool: True if thermal is present, False if not
        """
        return True

    def get_model(self):
        """
        Retrieves the model number (or part number) of the Thermal

        Returns:
            string: Model/part number of Thermal
        """
        return 'NA'

    def get_serial(self):
        """
        Retrieves the serial number of the Thermal

        Returns:
            string: Serial number of Thermal
        """
        return 'NA'

    def get_status(self):
        """
        Retrieves the operational status of the thermal

        Returns:
            A boolean value, True if thermal is operating properly,
            False if not
        """
        return True

    def get_temperature(self):
        """
        Retrieves current temperature reading from thermal

        Returns:
            A float number of current temperature in Celsius up to
            nearest thousandth of one degree Celsius, e.g. 30.125
        """
        is_valid, temperature = self.sensor.get_reading()
        if not is_valid:
            # A failed BMC read is not a temperature: None makes
            # thermalctld store 'N/A', which system-health reports as
            # Not OK, instead of 0 C, which it reported as fine.
            return None

        return float(temperature)

    def get_high_threshold(self):
        """
        Retrieves the high threshold temperature of thermal

        Returns:
            A float number, the high threshold temperature of thermal in
            Celsius up to nearest thousandth of one degree Celsius,
            e.g. 30.125
        """
        return self._get_upper_threshold("UpperNonCritical", 0)

    def get_low_threshold(self):
        """
        Retrieves the low threshold temperature of thermal

        Returns:
            A float number, the low threshold temperature of thermal in
            Celsius up to nearest thousandth of one degree Celsius,
            e.g. 30.125
        """
        is_valid, low_threshold = self.sensor.get_threshold("LowerNonRecoverable")
        if not is_valid:
            low_threshold = 0

        return float(low_threshold)

    def get_high_critical_threshold(self):
        """
        Retrieves the high critical threshold temperature of thermal
        Returns:
            A float number, the high critical threshold temperature of
            thermal in Celsius up to nearest thousandth of one degree
            Celsius, e.g. 30.125
        """
        return self._get_upper_threshold("UpperCritical", 1)

    def set_high_threshold(self, temperature):
        """
        Sets the high threshold temperature of thermal

        Args :
            temperature: A float number up to nearest thousandth of one
            degree Celsius, e.g. 30.125
        Returns:
            A boolean, True if threshold is set successfully, False if
            not
        """
        # Thermal threshold values are pre-defined based on HW.
        return False

    def set_low_threshold(self, temperature):
        """
        Sets the low threshold temperature of thermal

        Args :
            temperature: A float number up to nearest thousandth of one
            degree Celsius, e.g. 30.125
        Returns:
            A boolean, True if threshold is set successfully, False if
            not
        """
        # Thermal threshold values are pre-defined based on HW.
        return False
