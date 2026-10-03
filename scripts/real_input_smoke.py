"""Live check for --real-input: run the server over stdio and count real pointer moves.

A low-level mouse hook in this process counts the injected WM_MOUSEMOVE events
each Move call produces. SetCursorPos produces none that carry the injected
flag, so a teleport counts 0 and a real-input glide counts many. Only moves the
pointer, so it is safe to run with apps open.

    uv run python scripts/real_input_smoke.py
"""

import asyncio
import ctypes
import ctypes.wintypes as wt
import os
import sys
import threading
import time

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WH_MOUSE_LL, WM_MOUSEMOVE, LLMHF_INJECTED, WM_QUIT = 14, 0x0200, 0x01, 0x0012


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wt.POINT),
        ("mouseData", wt.DWORD),
        ("flags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wt.WPARAM, wt.LPARAM)
user32.CallNextHookEx.argtypes = [wt.HHOOK, ctypes.c_int, wt.WPARAM, wt.LPARAM]
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD]
user32.SetWindowsHookExW.restype = wt.HHOOK

injected_moves = 0


@HOOKPROC
def _hook(code: int, wparam: int, lparam: int) -> int:
    global injected_moves
    if code >= 0 and wparam == WM_MOUSEMOVE:
        data = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
        if data.flags & LLMHF_INJECTED:
            injected_moves += 1
    return user32.CallNextHookEx(None, code, wparam, lparam)


def _pump(ready: threading.Event, tid: list[int]) -> None:
    tid.append(kernel32.GetCurrentThreadId())
    hook = user32.SetWindowsHookExW(WH_MOUSE_LL, _hook, None, 0)
    if not hook:
        raise ctypes.WinError(ctypes.get_last_error())
    ready.set()
    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        pass
    user32.UnhookWindowsHookEx(hook)


def _cursor() -> tuple[int, int]:
    p = wt.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


async def run(real_input: bool) -> list[str]:
    global injected_moves
    args = ["run", "windows-mcp", "serve", "--tools", "Move"]
    if real_input:
        args.append("--real-input")
    transport = StdioTransport(
        command="uv", args=args, env={**os.environ, "ANONYMIZED_TELEMETRY": "false"}
    )
    failures = []
    async with Client(transport) as client:
        for target in ([400, 300], [700, 420]):
            injected_moves = 0
            t0 = time.monotonic()
            for attempt in range(6):
                try:
                    await client.call_tool("Move", {"loc": target})
                    break
                except Exception as e:
                    # The takeover guard pauses AI input for a few seconds after
                    # any physical input; wait it out.
                    if "USER_CONTROL" not in str(e) or attempt == 5:
                        raise
                    print("  physical input seen, waiting for the takeover guard...")
                    await asyncio.sleep(5)
                    injected_moves = 0
                    t0 = time.monotonic()
            elapsed = time.monotonic() - t0
            await asyncio.sleep(0.1)  # Let the hook thread drain.
            pos = _cursor()
            mode = "real-input" if real_input else "default"
            print(
                f"{mode:10} Move {target} -> {pos}, {injected_moves} injected moves, {elapsed:.2f} s"
            )
            if list(pos) != target:
                failures.append(f"{mode}: landed on {pos}, not {target}")
            if real_input and injected_moves < 15:
                failures.append(f"{mode}: only {injected_moves} injected moves")
            if not real_input and injected_moves:
                failures.append(f"{mode}: {injected_moves} injected moves, expected teleports")
    return failures


async def main() -> int:
    start = _cursor()
    ready, tid = threading.Event(), []
    threading.Thread(target=_pump, args=(ready, tid), daemon=True).start()
    ready.wait()
    try:
        failures = await run(False) + await run(True)
    finally:
        user32.SetCursorPos(*start)
        user32.PostThreadMessageW(tid[0], WM_QUIT, 0, 0)
    for f in failures:
        print("FAIL:", f)
    print("smoke failed" if failures else "smoke passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
