"""USB lifecycle and wire-level regression tests; no printer required."""

import asyncio
import errno
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import usb.core
from PIL import Image

from fichero import (
    PrinterClient,
    PrinterError,
    PrinterNotFound,
    PrinterTimeout,
    USBClient,
    connect,
    discover_usb,
)
from fichero.cli import do_print, main


@pytest.fixture
def hardware():
    endpoints = [SimpleNamespace(bmAttributes=2, bEndpointAddress=a) for a in (1, 129)]

    class Interface(list):
        bInterfaceClass = 7
        bInterfaceSubClass = 1
        bInterfaceProtocol = 2
        bAlternateSetting = 0
        bInterfaceNumber = 0

    device = MagicMock()
    device.serial_number = "example"
    device.bus, device.address = 1, 2
    device.get_active_configuration.return_value = [Interface(endpoints)]
    device.is_kernel_driver_active.return_value = True
    device.write.side_effect = lambda ep, data, timeout: len(data)
    util = MagicMock()
    core = MagicMock(USBError=usb.core.USBError, NoBackendError=usb.core.NoBackendError)
    core.find.return_value = [device]
    with patch("fichero.usb._modules", return_value=(core, util)):
        yield device, core, util


@pytest.mark.asyncio
async def test_discovery(hardware):
    device, core, util = hardware
    assert await discover_usb() == [{"serial": "example", "bus": 1, "address": 2}]
    core.find.assert_called_once_with(find_all=True, idVendor=0x09C5, idProduct=0x0200)
    util.claim_interface.assert_not_called()
    util.dispose_resources.assert_called_once_with(device)


@pytest.mark.asyncio
@pytest.mark.parametrize("devices,message", [(0, "No matching"), (2, "Multiple")])
async def test_selection(hardware, devices, message):
    device, core, util = hardware
    core.find.return_value = [device] * devices
    with pytest.raises(PrinterError, match=message):
        async with USBClient():
            pass
    util.claim_interface.assert_not_called()


@pytest.mark.asyncio
async def test_serial_selector(hardware):
    with pytest.raises(PrinterNotFound):
        async with USBClient(serial="other"):
            pass
    async with USBClient(serial="example"):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "number,message", [(errno.EACCES, "permission denied"), (errno.EBUSY, "busy")]
)
async def test_claim_failure_restores_driver(hardware, number, message):
    device, _, util = hardware
    util.claim_interface.side_effect = usb.core.USBError("failure", errno=number)
    with pytest.raises(PrinterError, match=message):
        async with USBClient():
            pass
    device.detach_kernel_driver.assert_called_once_with(0)
    device.attach_kernel_driver.assert_called_once_with(0)
    util.dispose_resources.assert_called_once_with(device)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,exception",
    [
        (0, PrinterError),
        (usb.core.USBTimeoutError("timeout"), PrinterTimeout),
        (usb.core.USBError("disconnected"), PrinterError),
    ],
)
async def test_write_failure_cleanup(hardware, failure, exception):
    device, _, util = hardware
    device.write.side_effect = failure if isinstance(failure, Exception) else None
    device.write.return_value = failure
    with pytest.raises(exception):
        async with USBClient() as client:
            await client.write_gatt_char("", b"abc")
    util.release_interface.assert_called_once_with(device, 0)
    device.attach_kernel_driver.assert_called_once_with(0)
    util.dispose_resources.assert_called_once_with(device)


@pytest.mark.asyncio
async def test_no_kernel_driver(hardware):
    device, _, util = hardware
    device.is_kernel_driver_active.side_effect = NotImplementedError
    async with USBClient():
        pass
    device.detach_kernel_driver.assert_not_called()
    device.attach_kernel_driver.assert_not_called()
    util.release_interface.assert_called_once()


@pytest.mark.asyncio
async def test_bad_endpoints(hardware):
    device, _, util = hardware
    device.get_active_configuration.return_value = []
    with pytest.raises(PrinterError, match="interface"):
        async with USBClient():
            pass
    util.dispose_resources.assert_called_once_with(device)


@pytest.mark.asyncio
async def test_missing_backend(hardware):
    _, core, _ = hardware
    core.find.side_effect = usb.core.NoBackendError("No backend")
    with pytest.raises(PrinterError, match="install libusb"):
        await discover_usb()


@pytest.mark.asyncio
async def test_missing_pyusb():
    with (
        patch.dict("sys.modules", {"usb.core": None}),
        pytest.raises(PrinterError, match="extra usb"),
    ):
        await discover_usb()


@pytest.mark.asyncio
async def test_print_wire_sequence(hardware, capsys):
    device, _, _ = hardware

    # Literal all-black raster makes orientation/packing independent of mocks.
    async def no_delay(_):
        pass

    with patch("fichero.cli.asyncio.sleep", no_delay):
        async with connect(usb=True) as pc:
            await do_print(pc, Image.new("1", (96, 30), 0), density=2, copies=2)
            with pytest.raises(PrinterError, match="not supported"):
                await pc.get_model()
    raster = bytes.fromhex("1d 76 30 00 0c 00 1e 00") + b"\xff" * 360
    copy = [
        bytes.fromhex("10 ff 84 00"),
        b"\0" * 12,
        bytes.fromhex("10 ff fe 01"),
        raster[:256],
        raster[256:],
        bytes.fromhex("1d 0c"),
        bytes.fromhex("10 ff fe 45"),
    ]
    assert [call.args[1] for call in device.write.call_args_list] == [
        bytes.fromhex("10 ff 10 00 02")
    ] + copy * 2
    assert all(
        call.args[0] == 1 and call.kwargs == {"timeout": 5000}
        for call in device.write.call_args_list
    )
    device.read.assert_not_called()
    assert "WARNING" not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_cancelled_open_cleans_after_worker(hardware):
    device, _, util = hardware
    started, finish = threading.Event(), threading.Event()

    def claim(*_):
        started.set()
        assert finish.wait(3)

    util.claim_interface.side_effect = claim

    async def run():
        async with USBClient():
            pass

    task = asyncio.create_task(run())
    await asyncio.to_thread(started.wait, 3)
    task.cancel()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    util.release_interface.assert_called_once_with(device, 0)
    device.attach_kernel_driver.assert_called_once()


@pytest.mark.parametrize(
    "arguments",
    [
        ["--usb", "info"],
        ["--usb", "status"],
        ["--usb", "set", "density", "2"],
        ["--usb", "--classic", "text", "hello"],
        ["--serial", "x", "info"],
        ["--usb", "text", "hello", "--copies", "0"],
    ],
)
def test_cli_rejects_unsupported(arguments):
    with (
        patch.dict("os.environ", {}, clear=True),
        patch("sys.argv", ["fichero"] + arguments),
        pytest.raises(SystemExit) as exc,
    ):
        main()
    assert exc.value.code == 2


@pytest.mark.asyncio
async def test_descriptor_permission_diagnostic(hardware):
    device, _, util = hardware
    from unittest.mock import PropertyMock

    type(device).serial_number = PropertyMock(side_effect=ValueError("no langid"))
    with pytest.raises(PrinterError, match="udev permissions"):
        await discover_usb()
    util.dispose_resources.assert_called_once_with(device)


@pytest.mark.asyncio
async def test_release_failure_still_restores_and_disposes(hardware):
    device, _, util = hardware
    util.release_interface.side_effect = usb.core.USBError("release failed")
    with pytest.raises(PrinterError, match="release failed"):
        async with USBClient():
            pass
    device.attach_kernel_driver.assert_called_once_with(0)
    util.dispose_resources.assert_called_once_with(device)


@pytest.mark.asyncio
async def test_cancelled_write_finishes_before_release(hardware):
    device, _, util = hardware
    started, finish = threading.Event(), threading.Event()
    events = []

    def write(ep, data, timeout):
        started.set()
        assert finish.wait(3)
        events.append("write completed")
        return len(data)

    device.write.side_effect = write
    util.release_interface.side_effect = lambda *_: events.append("released")

    async def run():
        async with USBClient() as client:
            await client.write_gatt_char("", b"hello")

    task = asyncio.create_task(run())
    assert await asyncio.to_thread(started.wait, 3)
    # Reaching here while write is blocked proves the event loop is responsive.
    task.cancel()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events == ["write completed", "released"]


@pytest.mark.asyncio
async def test_cancelled_write_failure_still_raises_cancelled(hardware):
    device, _, util = hardware
    started, finish = threading.Event(), threading.Event()

    def write(ep, data, timeout):
        started.set()
        assert finish.wait(3)
        raise usb.core.USBError("disconnected")

    device.write.side_effect = write

    async def run():
        async with USBClient() as client:
            await client.write_gatt_char("", b"hello")

    task = asyncio.create_task(run())
    assert await asyncio.to_thread(started.wait, 3)
    task.cancel()
    finish.set()
    # The worker's PrinterError must not replace the cancellation.
    with pytest.raises(asyncio.CancelledError):
        await task
    util.release_interface.assert_called_once_with(device, 0)


@pytest.mark.asyncio
async def test_repeated_cancel_waits_for_write_before_release(hardware):
    device, _, util = hardware
    started, finish = threading.Event(), threading.Event()
    events = []

    def write(ep, data, timeout):
        started.set()
        assert finish.wait(3)
        events.append("write completed")
        return len(data)

    device.write.side_effect = write
    util.release_interface.side_effect = lambda *_: events.append("released")

    async def run():
        async with USBClient() as client:
            await client.write_gatt_char("", b"hello")

    task = asyncio.create_task(run())
    assert await asyncio.to_thread(started.wait, 3)
    task.cancel()
    await asyncio.sleep(0.05)  # deliver the first cancellation
    task.cancel()
    asyncio.get_running_loop().call_later(0.2, finish.set)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events == ["write completed", "released"]


@pytest.mark.asyncio
@pytest.mark.parametrize("classic,size", [(False, 200), (True, 16384)])
async def test_bluetooth_chunk_sizes_and_reply_semantics(classic, size):
    from unittest.mock import AsyncMock

    client = SimpleNamespace(is_classic=classic, write_gatt_char=AsyncMock())
    pc = PrinterClient(client)
    await pc.send_chunked(b"x" * (size + 1))
    assert [len(c.args[1]) for c in client.write_gatt_char.call_args_list] == [size, 1]

    async def reply(*args, **kwargs):
        pc._on_notify(None, bytearray(b"OK"))

    client.write_gatt_char.side_effect = reply
    assert await pc.set_density(2) is True
    assert await pc.set_paper_type(0) is True
    assert await pc.stop_print() is True


def test_cli_usb_print_dispatch():
    from unittest.mock import AsyncMock

    with (
        patch.dict("os.environ", {}, clear=True),
        patch(
            "sys.argv",
            ["fichero", "--usb", "--serial", "example", "text", "Hello USB!"],
        ),
        patch("fichero.cli.cmd_text", new_callable=AsyncMock) as command,
    ):
        main()
    args = command.call_args.args[0]
    assert args.usb and args.serial == "example" and args.text == ["Hello USB!"]
