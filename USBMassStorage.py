# USBMassStorage.py
#
# Contains class definitions to implement a USB mass storage device.

from mmap import mmap

import os
import struct
import time

from facedancer.USB import *
from facedancer.USBDevice import *
from facedancer.USBConfiguration import *
from facedancer.USBInterface import *
from facedancer.USBEndpoint import *
from facedancer.USBVendor import *


def bytes_as_hex(b, delim=" "):
    return delim.join(["%02x" % x for x in b])

class USBMassStorageClass(USBClass):
    name = "USB mass storage class"

    def setup_request_handlers(self):
        self.request_handlers = {
            0xFF : self.handle_bulk_only_mass_storage_reset_request,
            0xFE : self.handle_get_max_lun_request
        }

    def handle_bulk_only_mass_storage_reset_request(self, req):
        self.interface.configuration.device.send_control_message(b'')

    def handle_get_max_lun_request(self, req):
        self.interface.configuration.device.send_control_message(b'\x00')


class USBMassStorageInterface(USBInterface):
    name = "USB mass storage interface"

    STATUS_OKAY       = 0x00
    STATUS_FAILURE    = 0x02 # TODO: Should this be 0x01?
    STATUS_INCOMPLETE = -1   # Special case status that aborts before response.

    def __init__(self, disk_image, verbose=0):
        self.disk_image = disk_image
        descriptors = { }

        self.ep_from_host = USBEndpoint(
                1,          # endpoint number
                USBEndpoint.direction_out,
                USBEndpoint.transfer_type_bulk,
                USBEndpoint.sync_type_none,
                USBEndpoint.usage_type_data,
                64,         # max packet size
                0,          # polling interval, see USB 2.0 spec Table 9-13
                self.handle_data_available    # handler function
        )
        self.ep_to_host = USBEndpoint(
                3,          # endpoint number
                USBEndpoint.direction_in,
                USBEndpoint.transfer_type_bulk,
                USBEndpoint.sync_type_none,
                USBEndpoint.usage_type_data,
                64,         # max packet size
                0,          # polling interval, see USB 2.0 spec Table 9-13
                None        # handler function
        )

        # TODO: un-hardcode string index (last arg before "verbose")
        USBInterface.__init__(
                self,
                0,          # interface number
                0,          # alternate setting
                8,          # interface class: Mass Storage
                6,          # subclass: SCSI transparent command set
                0x50,       # protocol: bulk-only (BBB) transport
                0,          # string index
                verbose,
                [ self.ep_from_host, self.ep_to_host ],
                descriptors
        )

        self.device_class = USBMassStorageClass()
        self.device_class.set_interface(self)

        self.is_write_in_progress = False
        self.write_cbw = None
        self.write_base_lba = 0
        self.write_length = 0
        self.write_data = b''

        self._initialize_scsi_commands()


    def _register_scsi_command(self, number, name, handler=None):

        if handler is None:
            handler = self.handle_unknown_command

        descriptor = {
            "number": number,
            "name": name,
            "handler": handler
        }

        self.commands[number] = descriptor


    def _initialize_scsi_commands(self):
        self.commands = {}

        self._register_scsi_command(0x00, "Test Unit Ready", self.handle_ignored_event)
        self._register_scsi_command(0x03, "Request Sense", self.handle_sense)
        self._register_scsi_command(0x12, "Inquiry", self.handle_inquiry)
        self._register_scsi_command(0x1a, "Mode Sense (6)", self.handle_mode_sense)
        self._register_scsi_command(0x5a, "Mode Sense (10)", self.handle_mode_sense)
        self._register_scsi_command(0x1e, "Prevent/Allow Removal", self.handle_ignored_event)
        self._register_scsi_command(0x23, "Get Format Capacity", self.handle_get_format_capacity)
        self._register_scsi_command(0x25, "Get Read Capacity", self.handle_get_read_capacity)
        self._register_scsi_command(0x28, "Read", self.handle_read)
        self._register_scsi_command(0x2a, "Write", self.handle_write)
        self._register_scsi_command(0x36, "Synchronize Cache", self.handle_ignored_event)


    def handle_scsi_command(self, cbw):
        """
            Handles an SCSI command.
        """

        opcode = cbw.cb[0]
        direction = cbw.flags >> 7

        # If we have a handler for this routine, handle it.
        if opcode in self.commands:

            # Extract the command's data.
            command = self.commands[opcode]
            name    = command['name']
            handler = command['handler']
            direction_name = 'IN' if direction else 'OUT'

            if self.verbose > 0:
                print("{}: handling {} ({}) [{}]".format(self.name, name.upper(), direction_name, bytes_as_hex(cbw.cb[1:])))

            # Delegate to its handler funciton.
            return handler(cbw)

        # Otherwise, run the unknown command handler.
        else:
            return self.handle_unknown_command(cbw)


    def handle_unknown_command(self, cbw):
        """
            Handles unsupported SCSI commands.
        """
        print(self.name, "received unsupported SCSI opcode 0x%x" % cbw.cb[0])

        # Generate an empty response to the relevant command.
        if cbw.data_transfer_length > 0:
            response = bytes([0] * cbw.data_transfer_length)
        else:
            response = None

        # Return failure.
        return self.STATUS_FAILURE, response


    def handle_ignored_event(self, cbw):
        """
            Handles SCSI events that we can safely ignore.
        """

        # Always return success, and no response.
        return self.STATUS_OKAY, None


    def handle_sense(self, cbw):
        """
            Handles SCSI sense requests.
        """
        response = b'\x70\x00\xFF\x00\x00\x00\x00\x0A\x00\x00\x00\x00\xFF\xFF\x00\x00\x00\x00\x00\x00\x00\x00\x00'
        return self.STATUS_OKAY, response


    def handle_inquiry(self, cbw):
        opcode, flags, page_code, allocation_length, control = struct.unpack(">BBBHB", cbw.cb[0:6])

        # Print out the details of our inquiry.
        if self.verbose > 1:
            print("-- INQUIRY ({}) flags: {} page_code: {} allocation_length: {} control: {}". \
                  format(opcode, flags, page_code, allocation_length, control))

        response = bytes([
            0x00,       # 0x00 = device present, and provides direct access to blocks
            0x00,       # 0x00 = media not removable, 0x80 = media removable
            0x05,       # 0 = no standards compliance, 3 = SPC compliant, 4 = SPC-2 compliant, 5 = SCSI compliant :)
            0x02,       # 0x02 = data responses follow the spec
            0x14,       # Additional length.
            0x00, 0x00, 0x00
        ])

        response += b'GoodFET '         # vendor
        response += b'GoodFET '         # product id
        response += b'        '         # product revision
        response += b'0.01'

        # pad up to data_transfer_length bytes
        diff = cbw.data_transfer_length - len(response)
        response += bytes([0] * diff)

        return self.STATUS_OKAY, response


    def handle_mode_sense(self, cbw):
        page = cbw.cb[2] & 0x3f

        response = b'\x07\x00\x00\x00\x00\x00\x00\x1c'
        if page != 0x3f:
            print(self.name, "unkonwn page, returning empty page")
            response = b'\x07\x00\x00\x00\x00\x00\x00\x00'

        return self.STATUS_OKAY, response


    def handle_get_format_capacity(self, cbw):
        response = bytes([
            0x00, 0x00, 0x00, 0x08,     # capacity list length
            0x00, 0x00, 0x10, 0x00,     # number of sectors (0x1000 = 10MB)
            0x10, 0x00,                 # reserved/descriptor code
            0x02, 0x00,                 # 512-byte sectors
        ])
        return self.STATUS_OKAY, response


    def handle_get_read_capacity(self, cbw):
        lastlba = self.disk_image.get_sector_count()

        response = bytes([
            (lastlba >> 24) & 0xff,
            (lastlba >> 16) & 0xff,
            (lastlba >>  8) & 0xff,
            (lastlba      ) & 0xff,
            0x00, 0x00, 0x02, 0x00,     # 512-byte blocks
        ])
        return self.STATUS_OKAY, response


    def handle_read(self, cbw):
        base_lba = cbw.cb[2] << 24 \
                 | cbw.cb[3] << 16 \
                 | cbw.cb[4] << 8 \
                 | cbw.cb[5]

        num_blocks = cbw.cb[7] << 8 \
                   | cbw.cb[8]

        if self.verbose > 0:
            print(self.name, "got SCSI Read (10), lba", base_lba, "+", num_blocks, "block(s)")

        # Note that here we send the data directly rather than putting
        # something in 'response' and letting the end of the switch send
        for block_num in range(num_blocks):
            data = self.disk_image.get_sector_data(base_lba + block_num)
            self.ep_to_host.send(data)

        return self.STATUS_OKAY, None


    def handle_write(self, cbw):
        base_lba = cbw.cb[1] << 24 \
                 | cbw.cb[2] << 16 \
                 | cbw.cb[3] <<  8 \
                 | cbw.cb[4]

        num_blocks = cbw.cb[7] << 8 \
                   | cbw.cb[8]

        if self.verbose > 0:
            print(self.name, "got SCSI Write (10), lba", base_lba, "+",
                    num_blocks, "block(s)")

        # save for later
        self.write_cbw = cbw
        self.write_base_lba = base_lba
        self.write_length = num_blocks * self.disk_image.block_size
        self.is_write_in_progress = True

        # because we need to snarf up the data from wire before we reply
        # with the CSW
        return self.STATUS_INCOMPLETE, None


    def continue_write(self, cbw):
        if self.verbose > 0:
            print(self.name, "got more", len(data), "bytes of SCSI write data")

        self.write_data += data

        if len(self.write_data) < self.write_length:
            # more yet to read, don't send the CSW
            return self.STATUS_INCOMPLETE, None

        self.disk_image.put_sector_data(self.write_base_lba, self.write_data)
        cbw = self.write_cbw

        self.is_write_in_progress = False
        self.write_data = b''

        return self.STATUS_OKAY, None


    def handle_data_available(self, data):
        if self.verbose > 0:
            print("--", len(data), "bytes of SCSI data")

        cbw = CommandBlockWrapper(data)

        if self.is_write_in_progress:
            status, response = self.continue_write(cbw)
        else:
            status, response = self.handle_scsi_command(cbw)

        # If we weren't able to complete the operation, return without
        # transmitting a response.
        if status == self.STATUS_INCOMPLETE:
            return

        # If we have a response payload to transmit, transmit it.
        if response:
            if self.verbose > 2:
                print(self.name, "responding with", len(response),
                      "bytes [{}], status={}".format(bytes_as_hex(response), status))

            self.ep_to_host.send(response, blocking=True)

        # Otherwise, respond with our status.
        csw = bytes([
            ord('U'), ord('S'), ord('B'), ord('S'),
            cbw.tag[0], cbw.tag[1], cbw.tag[2], cbw.tag[3],
            0x00, 0x00, 0x00, 0x00,
            status
        ])

        self.ep_to_host.send(csw)


class DiskImage:
    def __init__(self, filename, block_size):
        self.filename = filename
        self.block_size = block_size

        statinfo = os.stat(self.filename)
        self.size = statinfo.st_size

        self.file = open(self.filename, 'r+b')
        self.image = mmap(self.file.fileno(), 0)

    def close(self):
        self.image.flush()
        self.image.close()

    def get_sector_count(self):
        return int(self.size / self.block_size) - 1

    def get_sector_data(self, address):
        block_start = address * self.block_size
        block_end   = (address + 1) * self.block_size   # slices are NON-inclusive

        return self.image[block_start:block_end]

    def put_sector_data(self, address, data):
        block_start = address * self.block_size
        block_end   = (address + 1) * self.block_size   # slices are NON-inclusive

        self.image[block_start:block_end] = data[:self.block_size]
        self.image.flush()


class CommandBlockWrapper:
    def __init__(self, bytestring):
        self.signature              = bytestring[0:4]
        self.tag                    = bytestring[4:8]
        self.data_transfer_length   = bytestring[8] \
                                    | bytestring[9] << 8 \
                                    | bytestring[10] << 16 \
                                    | bytestring[11] << 24
        self.flags                  = int(bytestring[12])
        self.lun                    = int(bytestring[13] & 0x0f)
        self.cb_length              = int(bytestring[14] & 0x1f)
        #self.cb                     = bytestring[15:15+self.cb_length]
        self.cb                     = bytestring[15:]

    def __str__(self):
        s  = "sig: " + bytes_as_hex(self.signature) + "\n"
        s += "tag: " + bytes_as_hex(self.tag) + "\n"
        s += "data transfer len: " + str(self.data_transfer_length) + "\n"
        s += "flags: " + str(self.flags) + "\n"
        s += "lun: " + str(self.lun) + "\n"
        s += "command block len: " + str(self.cb_length) + "\n"
        s += "command block: " + bytes_as_hex(self.cb) + "\n"

        return s


class USBMassStorageDevice(USBDevice):
    name = "USB mass storage device"

    def __init__(self, maxusb_app, disk_image_filename, verbose=0):
        self.disk_image = DiskImage(disk_image_filename, 512)

        interface = USBMassStorageInterface(self.disk_image, verbose=verbose)

        config = USBConfiguration(
                1,                                          # index
                "Maxim umass config",                       # string desc
                [ interface ]                               # interfaces
        )

        USBDevice.__init__(
                self,
                maxusb_app,
                0,                      # device class
                0,                      # device subclass
                0,                      # protocol release number
                64,                     # max packet size for endpoint 0
                0x8107,                 # vendor id: Sandisk
                0x5051,                 # product id: SDCZ2 Cruzer Mini Flash Drive (thin)
                0x0003,                 # device revision
                "Maxim",                # manufacturer string
                "MAX3420E Enum Code",   # product string
                "S/N3420E",             # serial number string
                [ config ],
                verbose=verbose
        )


    def disconnect(self):
        self.disk_image.close()
        USBDevice.disconnect(self)

