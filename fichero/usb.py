"""Optional PyUSB transport for the D11s (09c5:0200).

Only raster printing is verified; no proprietary queries/notifications are used.
Blocking libusb operations run in worker threads, serialized through cancellation.
"""

import asyncio
import sys
import warnings

from fichero.printer import PrinterError, PrinterNotFound, PrinterTimeout

USB_VID = 0x09C5
USB_PID = 0x0200


def _modules():
    try:
        import usb.core
        import usb.util
    except ImportError as exc:
        raise PrinterError(
            "USB requires PyUSB: install with uv sync --extra usb"
        ) from exc
    return usb.core, usb.util


def _error(exc):
    if getattr(exc, "errno", None) == 13:
        return PrinterError(
            "USB permission denied; configure the D11s udev rule (see README)"
        )
    if getattr(exc, "errno", None) == 16:
        return PrinterError(
            "USB interface busy; close other clients and check CUPS/usblp contention"
        )
    if getattr(exc, "errno", None) == 110 or type(exc).__name__ == "USBTimeoutError":
        return PrinterTimeout(
            "USB transfer timed out; print outcome is unknown (do not retry blindly)"
        )
    if type(exc).__name__ == "NoBackendError":
        return PrinterError("USB backend unavailable; install libusb (see README)")
    return PrinterError(f"USB operation failed: {exc}")


async def _worker(func, *args):
    # A cancelled to_thread call keeps running. Wait for it through any number
    # of cancellations so cleanup never releases the handle mid-transfer, then
    # re-raise the cancellation in preference to any error the worker produced.
    task = asyncio.ensure_future(asyncio.to_thread(func, *args))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            pass  # surfaced below through task.result()
    if cancelled:
        if not task.cancelled():
            task.exception()  # mark retrieved; cancellation takes precedence
        raise asyncio.CancelledError
    return task.result()


def _serial(device):
    try:
        return device.serial_number
    except ValueError as exc:
        raise PrinterError(
            "Cannot read USB serial descriptor; check udev permissions "
            "and device connection (see README)"
        ) from exc


def _devices(core):
    try:
        return list(core.find(find_all=True, idVendor=USB_VID, idProduct=USB_PID))
    except (core.USBError, core.NoBackendError) as exc:
        raise _error(exc) from exc


async def discover_usb() -> list[dict]:
    """Return serial/bus/address records without claiming printer interfaces."""

    def scan():
        core, util = _modules()
        devices = _devices(core)
        try:
            return [
                {"serial": _serial(d), "bus": d.bus, "address": d.address}
                for d in devices
            ]
        except (core.USBError, core.NoBackendError) as exc:
            raise _error(exc) from exc
        finally:
            for device in devices:
                util.dispose_resources(device)

    return await _worker(scan)


class USBClient:
    """Async transport adapter; successful writes do not imply a printed label."""

    is_usb = True

    def __init__(self, serial: str | None = None, timeout: int = 5000):
        if timeout <= 0:
            raise ValueError("USB timeout must be positive (milliseconds)")
        self.serial = serial
        self.timeout = timeout
        self._device = None
        self._interface = None
        self._endpoint = None
        self._detached = False
        self._claimed = False
        self._lock = asyncio.Lock()

    def _open(self):
        core, util = _modules()
        devices = _devices(core)
        try:
            matches = [
                d for d in devices if self.serial is None or _serial(d) == self.serial
            ]
            if not matches:
                raise PrinterNotFound("No matching D11s USB printer (09c5:0200)")
            if len(matches) > 1:
                raise PrinterError(
                    "Multiple D11s USB printers; select one with --serial"
                )
            self._device = device = matches[0]
            # Do not reset an already configured device (which can disrupt CUPS).
            cfg = device.get_active_configuration()
            candidates = []
            for interface in cfg:
                if (
                    interface.bInterfaceClass,
                    interface.bInterfaceSubClass,
                    interface.bInterfaceProtocol,
                    interface.bAlternateSetting,
                ) != (7, 1, 2, 0):
                    continue
                endpoints = [ep for ep in interface if ep.bmAttributes & 3 == 2]
                outputs = [ep for ep in endpoints if not ep.bEndpointAddress & 0x80]
                inputs = [ep for ep in endpoints if ep.bEndpointAddress & 0x80]
                if len(outputs) == 1 and inputs:
                    candidates.append(
                        (interface.bInterfaceNumber, outputs[0].bEndpointAddress)
                    )
            if len(candidates) != 1:
                raise PrinterError(
                    "Expected one bidirectional USB Printer Class interface with bulk endpoints"
                )
            self._interface, self._endpoint = candidates[0]
            try:
                active = device.is_kernel_driver_active(self._interface)
            except NotImplementedError:
                active = False
            if active:
                device.detach_kernel_driver(self._interface)
                self._detached = True
            util.claim_interface(device, self._interface)
            self._claimed = True
        except (core.USBError, core.NoBackendError) as exc:
            raise _error(exc) from exc
        finally:
            for device in devices:
                if device is not self._device:
                    util.dispose_resources(device)

    def _close(self):
        if self._device is None:
            return
        _, util = _modules()
        device, self._device = self._device, None
        errors = []
        try:
            if self._claimed:
                try:
                    util.release_interface(device, self._interface)
                except Exception as exc:
                    errors.append(exc)
            if self._detached:
                try:
                    device.attach_kernel_driver(self._interface)
                except Exception as exc:
                    errors.append(exc)
        finally:
            self._claimed = self._detached = False
            try:
                util.dispose_resources(device)
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise _error(errors[0])

    async def __aenter__(self):
        try:
            await _worker(self._open)
        except BaseException:
            await self.__aexit__(*sys.exc_info())
            raise
        return self

    async def __aexit__(self, exc_type, exc, tb):
        async with self._lock:
            try:
                await _worker(self._close)
            except PrinterError as cleanup_error:
                if exc_type is None:
                    raise
                warnings.warn(f"USB cleanup failed: {cleanup_error}", RuntimeWarning)

    async def start_notify(self, _uuid, callback):
        """No USB notification protocol has been verified."""

    async def write_gatt_char(self, _uuid, data, response=False):
        def write():
            if self._device is None:
                raise PrinterError("USB transport is not connected")
            core, _ = _modules()
            try:
                written = self._device.write(self._endpoint, data, timeout=self.timeout)
            except (core.USBError, core.NoBackendError) as exc:
                raise _error(exc) from exc
            if written != len(data):
                raise PrinterError(
                    f"Incomplete USB write ({written}/{len(data)} bytes); print outcome unknown"
                )

        async with self._lock:
            await _worker(write)
