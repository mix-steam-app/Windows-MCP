"""Real-input mode (--real-input) moves the pointer with real mouse-move events.

SetCursorPos teleports the pointer without emitting movement input, so apps
that read raw input or track hover (games above all) never see it arrive. With
real-input mode on, every pointer move is an absolute mouse_event move and a
click glides to its target and settles before the button goes down. With it
off, behavior is unchanged.
"""

from unittest.mock import Mock

import pytest

from windows_mcp.desktop import service
from windows_mcp.desktop.service import Desktop
from windows_mcp.uia import core
from windows_mcp.uia.enums import KeyboardEventFlag, Keys, MouseEventFlag

SCREEN = (0, 0, 1920, 1080)
MOVE = MouseEventFlag.Move | MouseEventFlag.Absolute | MouseEventFlag.VirtualDesk


class Pointer:
    """A fake pointer: records every event and follows absolute moves."""

    def __init__(self, x: int, y: int) -> None:
        self.x, self.y = x, y
        self.events: list[tuple[str, int, int]] = []

    def mouse_event(self, flags: int, dx: int, dy: int, data: int, extra: int) -> None:
        left, top, width, height = SCREEN
        x = left + round(dx * (width - 1) / 65535)
        y = top + round(dy * (height - 1) / 65535)
        button = flags & ~MOVE
        if flags & MouseEventFlag.Move:
            self.x, self.y = x, y
            if not button:
                self.events.append(("move", x, y))
        if button == MouseEventFlag.LeftDown:
            self.events.append(("down", self.x, self.y))
        elif button == MouseEventFlag.LeftUp:
            self.events.append(("up", self.x, self.y))

    def set_cursor_pos(self, x: int, y: int) -> bool:
        self.x, self.y = x, y
        self.events.append(("teleport", x, y))
        return True

    def moves(self) -> list[tuple[str, int, int]]:
        return [e for e in self.events if e[0] == "move"]


@pytest.fixture
def pointer(monkeypatch: pytest.MonkeyPatch) -> Pointer:
    p = Pointer(100, 100)
    monkeypatch.setattr(core, "mouse_event", p.mouse_event)
    monkeypatch.setattr(core, "SetCursorPos", p.set_cursor_pos)
    monkeypatch.setattr(core, "GetCursorPos", lambda: (p.x, p.y))
    # The desktop service reads the cursor through the package namespace.
    monkeypatch.setattr(service.uia, "GetCursorPos", lambda: (p.x, p.y))
    monkeypatch.setattr(core, "GetVirtualScreenRect", lambda: SCREEN)
    monkeypatch.setattr(core.time, "sleep", lambda seconds: None)
    return p


@pytest.fixture
def real_input():
    core.SetRealInput(True)
    yield
    core.SetRealInput(False)


def test_off_by_default_click_teleports(pointer: Pointer) -> None:
    assert not core.RealInputEnabled()
    core.Click(500, 300)
    assert pointer.events[0] == ("teleport", 500, 300)
    assert pointer.moves() == []
    assert [e[0] for e in pointer.events[1:]] == ["down", "up"]


def test_click_glides_and_settles_before_the_press(pointer: Pointer, real_input) -> None:
    core.Click(500, 300)
    kinds = [e[0] for e in pointer.events]
    assert "teleport" not in kinds
    down = kinds.index("down")
    moves = pointer.events[:down]
    assert all(e[0] == "move" for e in moves)
    assert len(moves) >= core.REAL_INPUT_GLIDE_STEPS
    # The glide heads toward the target, then wiggles off and back onto it.
    assert moves[core.REAL_INPUT_GLIDE_STEPS - 1][1:] == (500, 300)
    assert moves[-2][1:] != (500, 300)
    assert moves[-1][1:] == (500, 300)
    assert pointer.events[down] == ("down", 500, 300)
    assert pointer.events[-1] == ("up", 500, 300)


def test_second_click_in_place_does_not_move(pointer: Pointer, real_input) -> None:
    pointer.x, pointer.y = 500, 300
    core.Click(500, 300)
    assert [e[0] for e in pointer.events] == ["down", "up"]


def test_settle_wiggles_inward_at_the_screen_edge(pointer: Pointer, real_input) -> None:
    core.SettlePointer(1919, 1079)
    assert pointer.moves() == [("move", 1917, 1078), ("move", 1919, 1079)]


def test_settle_is_a_no_op_when_off(pointer: Pointer) -> None:
    core.SettlePointer(10, 10)
    assert pointer.events == []


def test_move_to_duration_uses_move_events(pointer: Pointer, real_input) -> None:
    core.MoveToDuration(400, 200, duration=0.1, waitTime=0)
    kinds = {e[0] for e in pointer.events}
    assert kinds == {"move"}
    assert pointer.moves()[-1][1:] == (400, 200)


def _desktop(monkeypatch: pytest.MonkeyPatch) -> Desktop:
    monkeypatch.setattr(service, "sleep", lambda seconds: None)
    desktop = Desktop.__new__(Desktop)
    desktop.desktop_state = None
    return desktop


def test_desktop_move_glides_and_settles(
    monkeypatch: pytest.MonkeyPatch, pointer: Pointer, real_input
) -> None:
    _desktop(monkeypatch).move((110, 105))
    moves = pointer.moves()
    # A short hop still takes a full glide, so the app sees motion.
    assert len(moves) >= core.REAL_INPUT_GLIDE_STEPS
    assert "teleport" not in {e[0] for e in pointer.events}
    assert moves[-1][1:] == (110, 105)


def test_desktop_drag_moves_with_events(
    monkeypatch: pytest.MonkeyPatch, pointer: Pointer, real_input
) -> None:
    _desktop(monkeypatch).drag((300, 300), from_loc=(100, 100))
    kinds = [e[0] for e in pointer.events]
    assert "teleport" not in kinds
    down, up = kinds.index("down"), kinds.index("up")
    assert down < up
    assert len([k for k in kinds[down:up] if k == "move"]) >= 2 * core.REAL_INPUT_GLIDE_STEPS
    assert pointer.events[up] == ("up", 300, 300)


@pytest.mark.parametrize(("shortcut", "vk"), [("b", Keys.VK_B), ("5", Keys.VK_5)])
def test_shortcut_taps_a_lone_key_as_a_virtual_key(
    monkeypatch: pytest.MonkeyPatch, shortcut: str, vk: int, real_input
) -> None:
    desktop = _desktop(monkeypatch)
    sendkeys, tapkey = Mock(), Mock()
    monkeypatch.setattr(service.uia, "SendKeys", sendkeys)
    monkeypatch.setattr(service.uia, "TapKey", tapkey)
    desktop.shortcut(shortcut)
    tapkey.assert_called_once_with(vk)
    sendkeys.assert_not_called()


@pytest.mark.parametrize(
    ("shortcut", "on", "sent"),
    [("b", False, "b"), ("ctrl+b", True, "{ctrl}b"), ("enter", True, "{enter}")],
)
def test_shortcut_otherwise_goes_through_sendkeys(
    monkeypatch: pytest.MonkeyPatch, shortcut: str, on: bool, sent: str
) -> None:
    desktop = _desktop(monkeypatch)
    sendkeys, tapkey = Mock(), Mock()
    monkeypatch.setattr(service.uia, "SendKeys", sendkeys)
    monkeypatch.setattr(service.uia, "TapKey", tapkey)
    core.SetRealInput(on)
    try:
        desktop.shortcut(shortcut)
    finally:
        core.SetRealInput(False)
    assert sendkeys.call_args.args[0] == sent
    tapkey.assert_not_called()


def test_tap_key_sends_virtual_key_and_scan_code(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[int, int, int]] = []
    monkeypatch.setattr(
        core, "keybd_event", lambda vk, scan, flags, extra: sent.append((vk, scan, flags))
    )
    monkeypatch.setattr(core.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(core, "_VKtoSC", lambda vk: 0x30)
    core.TapKey(Keys.VK_B)
    assert sent == [
        (Keys.VK_B, 0x30, KeyboardEventFlag.KeyDown),
        (Keys.VK_B, 0x30, KeyboardEventFlag.KeyUp),
    ]
