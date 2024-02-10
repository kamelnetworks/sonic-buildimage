#!/usr/bin/env python

#############################################################################
# Celestica Silverstone-X
#
# Module contains an implementation of SONiC Platform Base API and
# provides the platform information
#
#############################################################################

try:
    import os
    import time
    import subprocess
    from sonic_platform_base.sonic_xcvr.sfp_optoe_base import SfpOptoeBase
except ImportError as e:
    raise ImportError(str(e) + "- required module not found")

QSFP_INFO_OFFSET = 128
SFP_INFO_OFFSET = 0
QSFP_DD_PAGE0 = 0

SFP_I2C_START = 12

SFP_TYPE_LIST = [
    '0x3' # SFP/SFP+/SFP28 and later
]
QSFP_TYPE_LIST = [
    '0xc', # QSFP
    '0xd', # QSFP+ or later
    '0x11'  # QSFP28 or later
]
QSFP_DD_TYPE_LIST = [
    '0x18' #QSFP-DD Type
]


class Sfp(SfpOptoeBase):

    QSFP_PRS_PATH = "/sys/class/SFF/QSFP{0}/qsfp_modprs"
    SFP_PRS_PATH = "/sys/devices/platform/fpga-xcvr/SFP{0}/sfp_modabs"
    LP_PATH = "/sys/class/SFF/QSFP{0}/qsfp_lpmode"
    OSFP_PRS_PATH = "/sys/class/SFF/QSFP{0}/qsfp_modprs"
    RESET_PATH = "/sys/class/SFF/QSFP{0}/qsfp_reset"
    SFP_PRS_PATH = "/sys/devices/platform/fpga-xcvr/SFP{0}/sfp_modabs"

    def __init__(self, index):
        SfpOptoeBase.__init__(self)
        self.index = index
        self.port_type, self.port_name = self.__get_port_info()
        self.sfp_type = self.port_type
        self.eeprom_path = '/sys/bus/i2c/devices/i2c-{0}/{0}-0050/eeprom'.format(SFP_I2C_START + index)
        self._initialize_media()

    def __get_port_info(self):
        if self.index >= 0 and self.index <= 31:
            return 'QSFP', 'QSFP' + str(self.index + 1)
        elif self.index >= 32 and self.index <= 33:
            return 'SFP', 'SFP' + str(self.index - 31)
        return 'Unknown', 'Unknown'

    def get_eeprom_path(self):
        return self.eeprom_path

    def get_name(self):
        return "SFP/SFP+/SFP28" if self.index < 25 else "QSFP28 or later"

    def _initialize_media(self):
        """
        Initialize the media type and eeprom driver for SFP
        """
        if self.get_presence():
            self._probe_media_type()
            self._reinit_sfp_driver()

    def get_position_in_parent(self):
        """
        Retrieves 1-based relative physical position in parent device.
        Returns:
            integer: The 1-based relative physical position in parent
            device or -1 if cannot determine the position
        """
        return self.index

    def is_replaceable(self):
        """
        Indicate whether this device  is replaceable.
        Returns:
            bool: True if it is replaceable.
        """
        return True

    def get_presence(self):
        """
        Retrieves the presence of the PSU
        Returns:
            bool: True if PSU is present, False if not
        """
        if self.index >= 0 and self.index <= 31:
            port_present_path = self.QSFP_PRS_PATH.format(self.index + 1)
        elif self.index >= 32 and self.index <= 33:
            port_present_path = self.SFP_PRS_PATH.format(self.index - 31)
        else:
            print("Out of SFP range {} - {}".format(PORT_START, PORT_END))

        with open(port_present_path, 'r', errors='replace') as fd:
            presence_status_raw = fd.read().strip()
        if not presence_status_raw:
            return False

        # ModPrsL is active low
        if presence_status_raw == '0':
            return True

        return False

    def get_reset_status(self):
        """
        Retrieves the reset status of SFP
        Returns:
            A Boolean, True if reset enabled, False if disabled
        """
        port_reset_path = self.RESET_PATH.format(self.index + 1)
        with open(port_reset_path, 'r') as f:
            reset_status_raw = f.read().strip()
        if not reset_status_raw:
            return False

        return reset_status_raw == '0'

    def get_lpmode(self):
        """
        Retrieves the lpmode (low power mode) status of this SFP
        Returns:
            A Boolean, True if lpmode is enabled, False if disabled
        """
        try:
            port_lpmode_path = self.LP_PATH.format(self.index + 1)
            with open(port_lpmode_path, 'r') as f:
                content = f.read().strip()
        except IOError as e:
            print("Error: unable to open file: %s" % str(e))
            return False

        # LPMode is active high
        if content == '0':
            return False

        return True

    def reset(self):
        """
        Reset SFP and return all user module settings to their default srate.
        Returns:
            A boolean, True if successful, False if not
        """
        # Check for invalid port_num
        try:
            port_reset_path = self.RESET_PATH.format(self.index + 1)
            reg_file = open(port_reset_path, "r+")
        except IOError as e:
            print("Error: unable to open file: %s" % str(e))
            return False

        # Convert our register value back to a hex string and write back
        reg_file.seek(0)
        reg_file.write(hex(0))
        reg_file.close()

        # Sleep 1 second to allow it to settle
        time.sleep(1)

        # Flip the bit back high and write back to the register to take port out of reset
        try:
            reg_file = open(port_reset_path, "w")
        except IOError as e:
            print("Error: unable to open file: %s" % str(e))
            return False

        reg_file.seek(0)
        reg_file.write(hex(1))
        reg_file.close()

        return True

    def set_lpmode(self, lpmode):
        """
        Sets the lpmode (low power mode) of SFP
        Args:
            lpmode: A Boolean, True to enable lpmode, False to disable it
            Note  : lpmode can be overridden by set_power_override
        Returns:
            A boolean, True if lpmode is set successfully, False if not
        """
        try:
            port_lpmode_path = self.LP_PATH.format(self.index + 1)
            reg_file = open(port_lpmode_path, "r+")
        except IOError as e:
            print("Error: unable to open file: %s" % str(e))
            return False

        # LPMode is active high; set or clear the bit accordingly
        content = 1 if lpmode else 0

        reg_file.seek(0)
        reg_file.write(str(content))
        reg_file.close()

        return True

    def get_status(self):
        """
        Retrieves the operational status of the device
        Returns:
            A boolean value, True if device is operating properly, False if not
        """
        return self.get_presence() and not self.get_reset_status()

    def _probe_media_type(self):
        """
        Reads optic eeprom byte to determine media type inserted
        """
        eeprom_raw = []
        eeprom_raw = self._xcvr_api_factory._get_id()
        if eeprom_raw is not None:
            eeprom_raw = hex(eeprom_raw)
            if eeprom_raw in SFP_TYPE_LIST:
                self.sfp_type = 'SFP'
            elif eeprom_raw in QSFP_TYPE_LIST:
                self.sfp_type = 'QSFP'
            elif eeprom_raw in QSFP_DD_TYPE_LIST:
                self.sfp_type = 'QSFP_DD'
            else:
                #Set native port type if EEPROM type is not recognized/readable
                self.sfp_type = self.port_type
        else:
            self.sfp_type = self.port_type

        return self.sfp_type

    def _reinit_sfp_driver(self):
        """
        Changes the driver based on media type detected
        """
        del_sfp_path = "/sys/class/i2c-adapter/i2c-{0}/delete_device".format(self.index + SFP_I2C_START)
        new_sfp_path = "/sys/class/i2c-adapter/i2c-{0}/new_device".format(self.index + SFP_I2C_START)
        driver_path = "/sys/class/i2c-adapter/i2c-{0}/{0}-0050/name".format(self.index + SFP_I2C_START)
        delete_device = "echo 0x50 >" + del_sfp_path

        if not os.path.isfile(driver_path):
            print(driver_path, "does not exist")
            return False

        try:
            with os.fdopen(os.open(driver_path, os.O_RDONLY)) as fd:
                driver_name = fd.read()
                driver_name = driver_name.rstrip('\r\n')
                driver_name = driver_name.lstrip(" ")

            #Avoid re-initialization of the QSFP/SFP optic on QSFP/SFP port.
            if self.sfp_type == 'SFP' and driver_name != 'optoe2':
                subprocess.Popen(delete_device, shell=True, stdout=subprocess.PIPE)
                time.sleep(0.2)
                new_device = "echo optoe2 0x50 >" + new_sfp_path
                subprocess.Popen(new_device, shell=True, stdout=subprocess.PIPE)
                time.sleep(2)
            elif self.sfp_type == 'QSFP' and driver_name != 'optoe1':
                subprocess.Popen(delete_device, shell=True, stdout=subprocess.PIPE)
                time.sleep(0.2)
                new_device = "echo optoe1 0x50 >" + new_sfp_path
                subprocess.Popen(new_device, shell=True, stdout=subprocess.PIPE)
                time.sleep(2)
            elif self.sfp_type == 'QSFP_DD' and driver_name != 'optoe3':
                subprocess.Popen(delete_device, shell=True, stdout=subprocess.PIPE)
                time.sleep(0.2)
                new_device = "echo optoe3 0x50 >" + new_sfp_path
                subprocess.Popen(new_device, shell=True, stdout=subprocess.PIPE)
                time.sleep(2)

        except IOError as e:
            print("Error: Unable to open file: %s" % str(e))
            return False

    def get_error_description(self):
        """
        Retrives the error descriptions of the SFP module
        Returns:
            String that represents the current error descriptions of vendor specific errors
            In case there are multiple errors, they should be joined by '|',
            like: "Bad EEPROM|Unsupported cable"
        """
        if not self.get_presence():
            return self.SFP_STATUS_UNPLUGGED
        else:
            if not os.path.isfile(self.eeprom_path):
                return "EEPROM driver is not attached"

            if self.sfp_type == 'SFP':
                offset = SFP_INFO_OFFSET
            elif self.sfp_type == 'QSFP':
                offset = QSFP_INFO_OFFSET
            elif self.sfp_type == 'QSFP_DD':
                offset = QSFP_DD_PAGE0

            try:
                with open(self.eeprom_path, mode="rb", buffering=0) as eeprom:
                    eeprom.seek(offset)
                    eeprom.read(1)
            except OSError as e:
                return "EEPROM read failed ({})".format(e.strerror)

        return self.SFP_STATUS_OK

