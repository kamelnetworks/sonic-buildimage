#############################################################################
# Celestica
#
# Module contains an implementation of SONiC Platform Base API and
# provides the PSUs status which are available in the platform
#
#############################################################################

try:
    from sonic_platform_base.psu_base import PsuBase
    from .helper import APIHelper
    from sonic_platform.fan import Fan
except ImportError as e:
    raise ImportError(str(e) + "- required module not found")

IPMI_SENSOR_NETFN = "0x04"
IPMI_SENSOR_READ_CMD = "0x2D {}"
IPMI_SENSOR_THRESH_CMD = "0x27 {}"
IPMI_FRU_MODEL_KEY = "Product Name"
IPMI_FRU_SERIAL_KEY = "Product Serial"

MAX_PSU_POWER = 550 #Watts
psu_ipmi_id = {\
    1: {\
        "TEMP": "0x0d",\
        "VOUT": "0x0f",\
        "COUT": "0x10",\
        "POUT": "0x11",\
        "STATUS": "0x72",\
        "FRU": 3,\
    },
    2: {\
        "TEMP": "0x17",\
        "VOUT": "0x19",\
        "COUT": "0x1a",\
        "POUT": "0x1b",\
        "STATUS": "0x73",\
        "FRU": 4,\
    }
}

ANALOG_READ_OFFSET = 0
EVENT_0_7_OFFSET = 2
EVENT_8_14_OFFSET = 3

FRU_SERIAL = 4
FRU_MODEL = 1

NOT_AVAILABLE = 'N/A'


class Psu(PsuBase):
    """Platform-specific Psu class"""

    def __init__(self, psu_index):
        PsuBase.__init__(self)
        self.index = psu_index+1
        # PSU has only one FAN
        fan = Fan(0, 0, is_psu_fan=True, psu_index=psu_index)
        self._fan_list.append(fan)
        self._api_helper = APIHelper()

    def _read_sensor(self, key):
        """
        Reads the analog byte of one of this PSU's IPMI sensors.
        Returns:
            The raw reading as an int, or None if ipmitool failed or answered
            too little.
        """
        status, output = self._api_helper.ipmi_raw(
            IPMI_SENSOR_NETFN, IPMI_SENSOR_READ_CMD.format(psu_ipmi_id[self.index][key]))
        fields = output.split()
        if not status or len(fields) <= ANALOG_READ_OFFSET:
            return None
        return int(fields[ANALOG_READ_OFFSET], 16)

    def _read_status_byte(self):
        """
        Reads the event byte of this PSU's status sensor.
        Returns:
            The event bits as an int, or None if ipmitool failed or answered
            too little.
        """
        status, output = self._api_helper.ipmi_raw(
            IPMI_SENSOR_NETFN, IPMI_SENSOR_READ_CMD.format(psu_ipmi_id[self.index]['STATUS']))
        fields = output.split()
        if not status or len(fields) <= EVENT_0_7_OFFSET:
            return None
        return int(fields[EVENT_0_7_OFFSET], 16)

    def get_voltage(self):
        """
        Retrieves current PSU voltage output
        Returns:
            A float number, the output voltage in volts,
            e.g. 12.1, or 'N/A' if the reading failed
        """
        value = self._read_sensor('VOUT')
        if value is None:
            return NOT_AVAILABLE
        # Formula: Rx10^-1
        return value / 10.0

    def get_voltage_high_threshold(self):
        return 12.6

    def get_voltage_low_threshold(self):
        return 11.4

    def get_current(self):
        """
        Retrieves present electric current supplied by PSU
        Returns:
            A float number, the electric current in amperes, e.g 15.4,
            or 'N/A' if the reading failed
        """
        value = self._read_sensor('COUT')
        if value is None:
            return NOT_AVAILABLE
        # Formula: Rx3x10^-1
        return value * 3 / 10.0

    def get_power(self):
        """
        Retrieves current energy supplied by PSU
        Returns:
            A float number, the power in watts, e.g. 302.6,
            or 'N/A' if the reading failed
        """
        value = self._read_sensor('POUT')
        if value is None:
            return NOT_AVAILABLE
        # Formula: Rx5
        return value * 5

    def get_powergood_status(self):
        """
        Retrieves the powergood status of PSU
        Returns:
            A boolean, True if PSU has stablized its output voltages and passed all
            its internal self-tests, False if not.
        """
        return self.get_status()

    def get_status_led(self):
        """
        Gets the state of the PSU status LED
        Returns:
            A string, one of the predefined STATUS_LED_COLOR_* strings above
        """
        if self.get_presence():
            if self.get_powergood_status():
                return self.STATUS_LED_COLOR_GREEN
            else:
                return "blinking green"
        else:
            return "N/A"

    def get_temperature(self):
        """
        Retrieves current temperature reading from PSU
        Returns:
            A float number of current temperature in Celsius up to nearest thousandth
            of one degree Celsius, e.g. 30.125, or 'N/A' if the reading failed
        """
        # PSU's ambient temperature sensor is considered as the PSU temperature
        value = self._read_sensor('TEMP')
        if value is None:
            return NOT_AVAILABLE
        return float(value)

    def get_maximum_supplied_power(self):
        """
        Retrieves the maximum supplied power by PSU
        Returns:
            A float number, the maximum power output in Watts.
            e.g. 1200.1
        """

        return MAX_PSU_POWER

    ##############################################################
    ###################### Device methods ########################
    ##############################################################

    def get_name(self):
        """
        Retrieves the name of the device
            Returns:
            string: The name of the device
        """
        return "PSU {}".format(self.index)

    def get_presence(self):
        """
        Retrieves the presence of the PSU
        Returns:
            bool: True if PSU is present, False if not
        """
        status_byte = self._read_status_byte()
        if status_byte is None:
            return False
        return status_byte & 0x01 == 0x01

    def get_model(self):
        """
        Retrieves the model number (or part number) of the device
        Returns:
            string: Model/part number of device
        """
        model = "Unknown"
        if self.get_presence():
            ipmi_fru_idx = psu_ipmi_id[self.index]['FRU']
            status, raw_model = self._api_helper.ipmi_fru_id(
                ipmi_fru_idx, IPMI_FRU_MODEL_KEY)

            fru_pn_list = raw_model.split(':')[-1]
            model = fru_pn_list.strip()
        else:
            model = "N/A"

        return model

    def get_serial(self):
        """
        Retrieves the serial number of the device
        Returns:
            string: Serial number of device
        """
        serial = "Unknown"
        if self.get_presence():
            ipmi_fru_idx = psu_ipmi_id[self.index]['FRU']
            status, raw_serial = self._api_helper.ipmi_fru_id(
                ipmi_fru_idx, IPMI_FRU_SERIAL_KEY)

            fru_sr_list = raw_serial.split(':')[-1]
            serial = fru_sr_list.strip()
        else:
            serial = "N/A"

        return serial

    def get_status(self):
        """
        Retrieves the operational status of the device
        Returns:
            A boolean value, True if device is operating properly, False if not
        """
        status_byte = self._read_status_byte()
        if status_byte is None:
            return False
        return status_byte & 0x08 != 0x08

    def get_position_in_parent(self):
        """
        Retrieves 1-based relative physical position in parent device. If the agent cannot determine the parent-relative position
        for some reason, or if the associated value of entPhysicalContainedIn is '0', then the value '-1' is returned
        Returns:
            integer: The 1-based relative physical position in parent device or -1 if cannot determine the position
        """
        return self.index

    def is_replaceable(self):
        """
        Indicate whether this device is replaceable.
        Returns:
            bool: True if it is replaceable.
        """
        return True

    def get_direction(self):
        """
        Return the airflow direction of PSU FAN
        Returns:
            string: 'intake' or 'exhaust'
        """
        self
