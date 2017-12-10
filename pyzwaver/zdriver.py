#!/usr/bin/python3
# Copyright 2016 Robert Muth <robert@muth.org>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; version 3
# of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA  02111-1307, USA.

"""
zdriver.py contains the code interacting directly with serial device
"""

import logging
import serial
import threading
import time
import collections

from pyzwaver import zwave
from pyzwaver import zmessage


# TODO: why the heck to we need this silly delay?
SEND_DELAY_LARGE = 0.05
SEND_DELAY_SMALL = 0.01


def MakeSerialDevice(port="/dev/ttyUSB0"):
    dev = serial.Serial(
        port=port,
        baudrate=115200,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        bytesize=serial.EIGHTBITS,
        # blocking
        timeout=5)
    # dev.open()
    return dev


class History(object):

    def __init__(self):
        self._raw_history = []
        self._history = []

    def LogSent(self, ts, m, comment):
        self._raw_history.append((ts, True, m, comment))
        logging.warning("sent: %s", zmessage.PrettifyRawMessage(m))

    def LogReceived(self, ts, m, comment):
        logging.warning("recv: %s", zmessage.PrettifyRawMessage(m))
        self._raw_history.append((ts, False, m, comment))

    def Append(self, m):
        self._history.append(m)

    def __str__(self):
        cutoff = 0
        with_can = 0
        total_can = 0
        by_node_cnt = collections.Counter()
        by_node_can = collections.Counter()
        by_node_dur = collections.Counter()
        by_state = collections.Counter()
        sum_duration = 0
        mm = self._history
        if cutoff > 0:
            mm = mm[-cutoff:]
        count = len(mm)
        for m in mm:
            if m.can > 0:
                with_can += 1
                total_can += m.can
                by_node_can[m.node] += 1
            by_state[m.state] += 1
            by_node_cnt[m.node] += 1
            if m.end:
                duration = int(1000.0 * (m.end - m.start))
                by_node_dur[m.node] += duration
                sum_duration += duration
        out = []
        out.append("processed: %d  with-can: %d (total can: %d) avg-time: %dms" %
                   (count, with_can, total_can, sum_duration // count))
        out.append("by state: %s" % by_state)

        s = ["by node:"]
        for n in sorted(by_node_cnt.keys()):
            s.append("  %d: %d (%d) %dms" % (n, by_node_cnt[n], by_node_can[n],
                       by_node_dur[n] // by_node_cnt[n]))
        out.append("".join(s))
        return "\n".join(out)


class Driver(object):
    """TBD"""

    def __init__(self, serialDevice, message_queue):
        self._device = serialDevice
        self.history = History()

        self._device_idle = True
        self._terminate = False   # True if we want to shut things down
        self._mq = message_queue
        # Make sure we flush old stuff
        self._ClearDevice()
        self._ClearDevice()

        self._tx_thread = threading.Thread(target=self._DriverSendingThread,
                                           name="DriverSend")
        self._tx_thread.start()
        self._rx_thread = threading.Thread(target=self._DriverReceivingThread,
                                           name="DriverReceive")
        self._rx_thread.start()
        self._last = None
        self._inflight = None

    def __str__(self):
        out = [str(self._mq),
               str(self.history)]
        return "\n".join(out)

    def GetInFlightMessage(self):
        return self._inflight

    def Terminate(self):
        self._terminate = True
        self._mq.EnqueueMessage(zmessage.Message(None, zmessage.LowestPriority(), lambda _: None, None))
        logging.info("Driver terminated")

    def SendRaw(self, payload, comment):
        time.sleep(SEND_DELAY_LARGE)
        #if len(payload) >= 5:
        #    if self._last == payload[4]:
        #        time.sleep(SEND_DELAY_LARGE)
        #    self._last = payload[4]

        # logging.info("sending: %s", zmessage.PrettifyRawMessage(payload))
        self.history.LogSent(time.time(), payload, comment)
        self._device.write(payload)
        self._device.flush()

    # Without delay
    def SendControl(self, payload):
        assert len(payload) == 1
        # logging.info("sending: %s", zmessage.PrettifyRawMessage(payload))
        self.history.LogSent(time.time(), payload, "")
        self._device.write(payload)
        self._device.flush()

    def _DriverSendingThread(self):
        """
        Forwards message from _mq to device
        """
        logging.warning("_DriverSendingThread started")
        lock = threading.Lock()
        while not self._terminate:
            self._inflight = self._mq.DequeueMessage(lock)
            if self._inflight is None:
                continue
            self.history.Append(self._inflight)
            self.SendRaw(self._inflight.payload, "")
            lock.acquire()
            self._inflight = None
            lock.release()

        logging.warning("_DriverSendingThread terminated")

    def _ClearDevice(self):
        self._device.write(zmessage.RAW_MESSAGE_NAK)
        self._device.write(zmessage.RAW_MESSAGE_NAK)
        self._device.write(zmessage.RAW_MESSAGE_NAK)
        self._device.flush()
        self._device.flushInput()
        self._device.flushOutput()


    def _ProcessReceivedMessage(self, m):
        # logging.debug("rx buffer: %s", buf)
        inflight = self._inflight
        if m[0] == zwave.NAK:
            return False, ""
        elif m[0] == zwave.CAN:
            if inflight is None:
                logging.error("nothing to re-send after CAN")
                return False, "stray"
            logging.error("re-sending message after CAN ==== %s",
                          zmessage.PrettifyRawMessage(inflight.payload))
            inflight.can += 1
            # TODO: maybe add max
            # if self._inflight.can > 3:
            self.SendRaw(inflight.payload, "re-try")
            return False, ""

        elif m[0] == zwave.ACK:
            if inflight is None:
                logging.error("nothing to re-send after ACK")
                return False, "stray"
            return False, self._inflight.MaybeComplete(m)
        elif m[0] == zwave.SOF:
            if zmessage.Checksum(m) != zwave.SOF:
                # maybe send a CAN?
                logging.error("bad checksum")
                return False, "bad"
            if m[2] == zwave.RESPONSE:
                if inflight is None:
                    logging.error("nothing to re-send after RESPONSE")
                    return True, "stray"
                return True, self._inflight.MaybeComplete(m)
            elif m[2] == zwave.REQUEST:
                if (m[3] == zwave.API_ZW_APPLICATION_UPDATE or
                    m[3] == zwave.API_APPLICATION_COMMAND_HANDLER):
                    self._mq.PutIncommingRawMessage(m)
                    return True, ""
                else:
                    if inflight is None:
                        logging.error("nothing to re-send after REQUEST")
                        return True, "stray"
                    return True, self._inflight.MaybeComplete(m)
            else:
                logging.error("message is neither request nor response")
                return False, "bad"
        else:
            logging.error("received unknown start byte: %s", m[0])
            return False, "bad"

    def _DriverReceivingThread(self):
        logging.warning("_DriverReceivingThread started")
        buf = b""
        while not self._terminate:
            r = self._device.read(1)
            if not r:
                #logging.warning("received empty message/timeout")
                continue
            buf += r
            m = buf[0:1]
            if m[0] == zwave.SOF:
                m = zmessage.ExtracRawMessage(buf)
                if not m:
                    continue
            buf = buf[len(m):]
            ts = time.time()
            must_ack, comment = self._ProcessReceivedMessage(m)
            self.history.LogReceived(ts, m, comment)
            if must_ack:
                self.SendControl(zmessage.RAW_MESSAGE_ACK)


        logging.warning("_DriverReceivingThread terminated")
