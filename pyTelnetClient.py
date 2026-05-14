"""
pyTelnetClient — tkinter telnet client with 80x24 display
emulating VT-100 or ADM-3A escape sequences.

Run:  python pyTelnetClient.py

The escape-code semantics mirror the vZ80 console parser so the same
mode switch ("vt100" | "adm3a") behaves identically on either end.
"""

import socket
import threading
import queue
import tkinter as tk
from tkinter import ttk, font


# ---------------------------------------------------------------------------
# Terminal emulator (parser + 80x24 buffer)
# ---------------------------------------------------------------------------

class TerminalEmulator:
    COLS = 80
    ROWS = 24

    MODE_VT100 = 0
    MODE_ADM3A = 1

    PS_GROUND      = 0
    PS_ESC         = 1
    PS_CSI         = 2
    PS_ADM_CUP_ROW = 3   # saw ESC '='; next byte = row + 0x20
    PS_ADM_CUP_COL = 4   # saw row byte; next byte = col + 0x20

    def __init__(self):
        self.buf = [[' '] * self.COLS for _ in range(self.ROWS)]
        self.cx = 0
        self.cy = 0
        self.saved_cx = 0
        self.saved_cy = 0
        self.mode = self.MODE_VT100
        self.pstate = self.PS_GROUND
        self.params = []
        self.cur_param = ''
        self.adm_cup_row = 0
        self.dirty = True

    # ---- mode --------------------------------------------------------------
    def set_mode(self, mode):
        self.mode = mode

    # ---- low-level buffer ops ---------------------------------------------
    def clear(self):
        for r in range(self.ROWS):
            for c in range(self.COLS):
                self.buf[r][c] = ' '
        self.cx = 0
        self.cy = 0
        self.dirty = True

    def scroll_up(self):
        for r in range(self.ROWS - 1):
            self.buf[r] = self.buf[r + 1][:]
        self.buf[self.ROWS - 1] = [' '] * self.COLS
        self.dirty = True

    def newline(self):
        self.cx = 0
        if self.cy >= self.ROWS - 1:
            self.scroll_up()
        else:
            self.cy += 1

    def cursor_to(self, row, col):
        row = max(0, min(self.ROWS - 1, row))
        col = max(0, min(self.COLS - 1, col))
        self.cy = row
        self.cx = col

    def erase_in_line(self, mode):
        # 0 = cursor..EOL, 1 = BOL..cursor, 2 = entire line
        if mode == 0:
            for c in range(self.cx, self.COLS):
                self.buf[self.cy][c] = ' '
        elif mode == 1:
            for c in range(0, self.cx + 1):
                self.buf[self.cy][c] = ' '
        elif mode == 2:
            for c in range(self.COLS):
                self.buf[self.cy][c] = ' '
        self.dirty = True

    def erase_in_display(self, mode):
        # 0 = cursor..end, 1 = start..cursor, 2 = whole screen
        if mode == 0:
            for c in range(self.cx, self.COLS):
                self.buf[self.cy][c] = ' '
            for r in range(self.cy + 1, self.ROWS):
                for c in range(self.COLS):
                    self.buf[r][c] = ' '
        elif mode == 1:
            for r in range(0, self.cy):
                for c in range(self.COLS):
                    self.buf[r][c] = ' '
            for c in range(0, self.cx + 1):
                self.buf[self.cy][c] = ' '
        elif mode == 2:
            for r in range(self.ROWS):
                for c in range(self.COLS):
                    self.buf[r][c] = ' '
        self.dirty = True

    # ---- byte stream entrypoint -------------------------------------------
    def write(self, data):
        for b in data:
            self.putc(b)

    def putc(self, b):
        # CAN unconditionally cancels any pending escape
        if b == 0x18:
            self.pstate = self.PS_GROUND
            return

        # SUB — ADM-3A clear; VT-100 cancel
        if b == 0x1A:
            if self.mode == self.MODE_ADM3A:
                self.clear()
            self.pstate = self.PS_GROUND
            return

        # ADM-3A binary cursor address — route raw bytes before the
        # C0 filter so 0x20 (space) reaches the col handler.
        if self.pstate == self.PS_ADM_CUP_ROW:
            self.adm_cup_row = b - 0x20
            self.pstate = self.PS_ADM_CUP_COL
            return
        if self.pstate == self.PS_ADM_CUP_COL:
            col = b - 0x20
            self.cursor_to(self.adm_cup_row, col)
            self.pstate = self.PS_GROUND
            return

        # ESC always restarts escape parsing
        if b == 0x1B:
            self.pstate = self.PS_ESC
            return

        if self.pstate == self.PS_ESC:
            self.parse_esc(b)
            return
        if self.pstate == self.PS_CSI:
            self.parse_csi(b)
            return

        self.putc_ground(b)

    def putc_ground(self, b):
        # C0 controls
        if b == 0x07:                                   # BEL
            self.root_bell()
            return
        if b == 0x08:                                   # BS
            if self.cx > 0:
                self.cx -= 1
            return
        if b == 0x09:                                   # HT
            self.cx = (self.cx + 8) & ~7
            if self.cx > self.COLS - 1:
                self.cx = self.COLS - 1
            return
        if b == 0x0A:                                   # LF
            self.newline()
            return
        if b == 0x0D:                                   # CR
            self.cx = 0
            return
        if b == 0x0B:                                   # VT — ADM cursor up; VT-100 ignores
            if self.mode == self.MODE_ADM3A and self.cy > 0:
                self.cy -= 1
            return
        if b == 0x0C:                                   # FF — VT-100 clear+home; ADM cursor right
            if self.mode == self.MODE_ADM3A:
                if self.cx < self.COLS - 1:
                    self.cx += 1
            else:
                self.clear()
            return
        if b == 0x1E:                                   # RS — home (no clear)
            self.cx = 0
            self.cy = 0
            return

        # Printable
        if 0x20 <= b < 0x7F:
            self.buf[self.cy][self.cx] = chr(b)
            self.dirty = True
            if self.cx < self.COLS - 1:
                self.cx += 1
            # else: stay clamped at COLS-1, matching CP/M-friendly behavior

    # ---- escape parsing ---------------------------------------------------
    def parse_esc(self, b):
        # ADM-3A keys — always available regardless of mode
        if b == ord('='):
            self.pstate = self.PS_ADM_CUP_ROW
            return
        if b == ord('T'):
            self.erase_in_line(0)
            self.pstate = self.PS_GROUND
            return
        if b == ord('Y'):
            self.erase_in_display(0)
            self.pstate = self.PS_GROUND
            return
        if b == ord('*'):
            self.clear()
            self.pstate = self.PS_GROUND
            return

        # VT-100
        if b == ord('['):
            self.pstate = self.PS_CSI
            self.params = []
            self.cur_param = ''
            return
        if b == ord('E'):                                # NEL
            self.newline()
            self.pstate = self.PS_GROUND
            return
        if b == ord('D'):                                # IND
            if self.cy >= self.ROWS - 1:
                self.scroll_up()
            else:
                self.cy += 1
            self.pstate = self.PS_GROUND
            return
        if b == ord('M'):                                # RI (reverse index)
            if self.cy > 0:
                self.cy -= 1
            self.pstate = self.PS_GROUND
            return
        if b == ord('7'):                                # DECSC
            self.saved_cx = self.cx
            self.saved_cy = self.cy
            self.pstate = self.PS_GROUND
            return
        if b == ord('8'):                                # DECRC
            self.cx = self.saved_cx
            self.cy = self.saved_cy
            self.pstate = self.PS_GROUND
            return

        # Unknown — drop and resume
        self.pstate = self.PS_GROUND

    def parse_csi(self, b):
        if 0x30 <= b <= 0x39:                            # digit
            self.cur_param += chr(b)
            return
        if b == ord(';'):
            self.params.append(int(self.cur_param) if self.cur_param else 0)
            self.cur_param = ''
            return
        if 0x20 <= b <= 0x2F:                            # intermediate — ignore
            return
        if 0x40 <= b <= 0x7E:                            # final byte
            if self.cur_param != '':
                self.params.append(int(self.cur_param))
                self.cur_param = ''
            self.dispatch_csi(b)
            self.pstate = self.PS_GROUND
            return
        # Anything else — abandon
        self.pstate = self.PS_GROUND

    def csi_param(self, i, default_val):
        if i < len(self.params):
            v = self.params[i]
            return v if v != 0 else default_val
        return default_val

    def dispatch_csi(self, final):
        if final == ord('H') or final == ord('f'):       # CUP / HVP
            row = self.csi_param(0, 1) - 1
            col = self.csi_param(1, 1) - 1
            self.cursor_to(row, col)
        elif final == ord('J'):                          # ED
            mode = self.params[0] if self.params else 0
            self.erase_in_display(mode)
        elif final == ord('K'):                          # EL
            mode = self.params[0] if self.params else 0
            self.erase_in_line(mode)
        elif final == ord('A'):                          # CUU
            n = self.csi_param(0, 1)
            self.cy = max(0, self.cy - n)
        elif final == ord('B'):                          # CUD
            n = self.csi_param(0, 1)
            self.cy = min(self.ROWS - 1, self.cy + n)
        elif final == ord('C'):                          # CUF
            n = self.csi_param(0, 1)
            self.cx = min(self.COLS - 1, self.cx + n)
        elif final == ord('D'):                          # CUB
            n = self.csi_param(0, 1)
            self.cx = max(0, self.cx - n)
        elif final == ord('G'):                          # CHA — column abs
            col = self.csi_param(0, 1) - 1
            self.cx = max(0, min(self.COLS - 1, col))
        elif final == ord('d'):                          # VPA — row abs
            row = self.csi_param(0, 1) - 1
            self.cy = max(0, min(self.ROWS - 1, row))
        elif final == ord('s'):                          # save cursor
            self.saved_cx = self.cx
            self.saved_cy = self.cy
        elif final == ord('u'):                          # restore cursor
            self.cx = self.saved_cx
            self.cy = self.saved_cy
        # Other CSI (SGR, DSR, etc.) silently ignored.

    # ---- hooks (overridable) ----------------------------------------------
    def root_bell(self):
        pass


# ---------------------------------------------------------------------------
# Telnet client (background-thread socket)
# ---------------------------------------------------------------------------

class TelnetClient:
    IAC  = 0xFF
    DONT = 0xFE
    DO   = 0xFD
    WONT = 0xFC
    WILL = 0xFB
    SB   = 0xFA
    SE   = 0xF0

    OPT_BINARY = 0
    OPT_ECHO   = 1
    OPT_SGA    = 3

    def __init__(self, rx_callback, status_callback):
        self.sock = None
        self.rx_callback = rx_callback
        self.status_callback = status_callback
        self.rx_thread = None
        self.running = False

    def connect(self, host, port):
        if self.running:
            return False
        try:
            self.sock = socket.create_connection((host, port), timeout=10)
            self.sock.settimeout(None)
            self.running = True
            self.rx_thread = threading.Thread(target=self._rx_loop, daemon=True)
            self.rx_thread.start()
            self.status_callback(f"Connected to {host}:{port}")
            return True
        except Exception as e:
            self.status_callback(f"Connect failed: {e}")
            self.sock = None
            return False

    def disconnect(self):
        was_running = self.running
        self.running = False
        if self.sock is not None:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
        if was_running:
            self.status_callback("Disconnected")

    def send(self, data):
        if self.sock is None or not self.running:
            return
        if isinstance(data, str):
            data = data.encode('latin-1', errors='replace')
        try:
            self.sock.sendall(data)
        except Exception as e:
            self.status_callback(f"Send error: {e}")
            self.disconnect()

    def _rx_loop(self):
        iac_state = None
        while self.running and self.sock is not None:
            try:
                data = self.sock.recv(4096)
            except Exception as e:
                if self.running:
                    self.status_callback(f"Recv error: {e}")
                break
            if not data:
                break

            out = bytearray()
            for b in data:
                if iac_state is None:
                    if b == self.IAC:
                        iac_state = 'cmd'
                    else:
                        out.append(b)
                elif iac_state == 'cmd':
                    if b == self.IAC:                    # escaped 0xFF data
                        out.append(self.IAC)
                        iac_state = None
                    elif b in (self.DO, self.DONT, self.WILL, self.WONT):
                        iac_state = ('opt', b)
                    elif b == self.SB:
                        iac_state = 'sb'
                    else:
                        iac_state = None
                elif isinstance(iac_state, tuple) and iac_state[0] == 'opt':
                    cmd = iac_state[1]
                    self._respond_iac(cmd, b)
                    iac_state = None
                elif iac_state == 'sb':
                    if b == self.IAC:
                        iac_state = 'sb_iac'
                elif iac_state == 'sb_iac':
                    if b == self.SE:
                        iac_state = None
                    else:
                        iac_state = 'sb'

            if out:
                self.rx_callback(bytes(out))

        self.running = False
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
        self.status_callback("Disconnected")

    def _respond_iac(self, cmd, opt):
        if self.sock is None:
            return
        try:
            if cmd == self.DO:
                resp_will = opt in (self.OPT_SGA, self.OPT_BINARY)
                resp = bytes([self.IAC,
                              self.WILL if resp_will else self.WONT,
                              opt])
                self.sock.sendall(resp)
            elif cmd == self.WILL:
                resp_do = opt in (self.OPT_SGA, self.OPT_ECHO, self.OPT_BINARY)
                resp = bytes([self.IAC,
                              self.DO if resp_do else self.DONT,
                              opt])
                self.sock.sendall(resp)
            # DONT/WONT — no response required
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Tkinter UI
# ---------------------------------------------------------------------------

class TerminalApp:
    REFRESH_MS = 33  # ~30 fps

    def __init__(self, root):
        self.root = root
        self.root.title("pyTelnetClient — VT-100 / ADM-3A")

        self.term = TerminalEmulator()
        self.term.root_bell = self._bell
        self.rx_queue = queue.Queue()
        self.telnet = TelnetClient(self._on_rx_bytes, self._on_status)

        self._build_ui()
        self._schedule_refresh()
        self._cursor_blink_on = True
        self._schedule_blink()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---- UI construction --------------------------------------------------
    def _build_ui(self):
        top = ttk.Frame(self.root, padding=4)
        top.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(top, text="Host:").pack(side=tk.LEFT)
        self.host_var = tk.StringVar(value="127.0.0.1")
        ttk.Entry(top, textvariable=self.host_var, width=20)\
            .pack(side=tk.LEFT, padx=2)

        ttk.Label(top, text="Port:").pack(side=tk.LEFT)
        self.port_var = tk.StringVar(value="23")
        ttk.Entry(top, textvariable=self.port_var, width=6)\
            .pack(side=tk.LEFT, padx=2)

        ttk.Label(top, text="Mode:").pack(side=tk.LEFT, padx=(8, 0))
        self.mode_var = tk.StringVar(value="VT-100")
        mode_combo = ttk.Combobox(top, textvariable=self.mode_var,
                                  values=["VT-100", "ADM-3A"],
                                  width=8, state="readonly")
        mode_combo.pack(side=tk.LEFT, padx=2)
        mode_combo.bind("<<ComboboxSelected>>", self._on_mode_change)

        self.connect_btn = ttk.Button(top, text="Connect",
                                      command=self._on_connect)
        self.connect_btn.pack(side=tk.LEFT, padx=4)

        ttk.Button(top, text="Clear", command=self._on_clear)\
            .pack(side=tk.LEFT, padx=2)

        # Pick a monospace font that exists
        available = set(font.families())
        for fam in ("Consolas", "Courier New", "Courier", "DejaVu Sans Mono",
                    "Monaco", "TkFixedFont"):
            if fam in available or fam == "TkFixedFont":
                self.mono = font.Font(family=fam, size=12)
                break
        else:
            self.mono = font.Font(family="Courier", size=12)

        self.cell_w = self.mono.measure("M")
        self.cell_h = self.mono.metrics("linespace")

        cw_total = self.cell_w * TerminalEmulator.COLS
        ch_total = self.cell_h * TerminalEmulator.ROWS

        self.fg = "#E6E6E6"
        self.bg = "#000000"
        self.cursor_color = "#33FF33"

        self.canvas = tk.Canvas(self.root, width=cw_total, height=ch_total,
                                bg=self.bg, highlightthickness=0,
                                takefocus=True)
        self.canvas.pack(padx=4, pady=4)

        # Pre-build text items per cell — updating items is far cheaper
        # than redrawing the whole canvas each frame.
        self.cell_items = [[None] * TerminalEmulator.COLS
                           for _ in range(TerminalEmulator.ROWS)]
        for r in range(TerminalEmulator.ROWS):
            for c in range(TerminalEmulator.COLS):
                x = c * self.cell_w + self.cell_w // 2
                y = r * self.cell_h + self.cell_h // 2
                self.cell_items[r][c] = self.canvas.create_text(
                    x, y, text=' ', font=self.mono, fill=self.fg,
                    anchor='center')

        self.cursor_item = self.canvas.create_rectangle(
            0, 0, self.cell_w, self.cell_h,
            outline=self.cursor_color, width=1)

        # Status bar
        self.status_var = tk.StringVar(value="Not connected")
        ttk.Label(self.root, textvariable=self.status_var, anchor=tk.W,
                  relief=tk.SUNKEN).pack(side=tk.BOTTOM, fill=tk.X)

        # Keyboard input
        self.canvas.bind("<Key>", self._on_key)
        self.canvas.bind("<Button-1>", lambda e: self.canvas.focus_set())
        self.canvas.focus_set()

    # ---- callbacks --------------------------------------------------------
    def _bell(self):
        try:
            self.root.bell()
        except Exception:
            pass

    def _on_mode_change(self, event=None):
        if self.mode_var.get() == "ADM-3A":
            self.term.set_mode(TerminalEmulator.MODE_ADM3A)
        else:
            self.term.set_mode(TerminalEmulator.MODE_VT100)

    def _on_connect(self):
        if self.telnet.running:
            self.telnet.disconnect()
            self.connect_btn.config(text="Connect")
            return
        try:
            port = int(self.port_var.get())
        except ValueError:
            self.status_var.set("Invalid port")
            return
        self._on_mode_change()
        host = self.host_var.get().strip()
        if not host:
            self.status_var.set("Enter a host")
            return
        if self.telnet.connect(host, port):
            self.connect_btn.config(text="Disconnect")
            self.canvas.focus_set()

    def _on_clear(self):
        self.term.clear()

    def _on_rx_bytes(self, data):
        # Called from telnet rx thread — push to queue, drained on UI thread
        self.rx_queue.put(data)

    def _on_status(self, text):
        # Marshal to UI thread
        def apply():
            self.status_var.set(text)
            if not self.telnet.running:
                self.connect_btn.config(text="Connect")
        self.root.after(0, apply)

    def _on_close(self):
        self.telnet.disconnect()
        self.root.destroy()

    # ---- refresh loop -----------------------------------------------------
    def _schedule_refresh(self):
        # Drain bytes received from the telnet thread
        drained = False
        try:
            while True:
                data = self.rx_queue.get_nowait()
                self.term.write(data)
                drained = True
        except queue.Empty:
            pass

        if self.term.dirty or drained:
            self._redraw()
            self.term.dirty = False

        self._update_cursor()
        self.root.after(self.REFRESH_MS, self._schedule_refresh)

    def _redraw(self):
        cfg = self.canvas.itemconfig
        items = self.cell_items
        buf = self.term.buf
        for r in range(TerminalEmulator.ROWS):
            row = buf[r]
            row_items = items[r]
            for c in range(TerminalEmulator.COLS):
                cfg(row_items[c], text=row[c])

    def _update_cursor(self):
        x = self.term.cx * self.cell_w
        y = self.term.cy * self.cell_h
        self.canvas.coords(self.cursor_item,
                           x, y, x + self.cell_w, y + self.cell_h)

    def _schedule_blink(self):
        self._cursor_blink_on = not self._cursor_blink_on
        color = self.cursor_color if self._cursor_blink_on else ""
        self.canvas.itemconfig(self.cursor_item, outline=color)
        self.root.after(500, self._schedule_blink)

    # ---- keyboard ---------------------------------------------------------
    def _on_key(self, event):
        if not self.telnet.running:
            return

        ks = event.keysym
        ch = event.char

        if self.term.mode == TerminalEmulator.MODE_ADM3A:
            key_map = {
                'Left':      b'\x08',
                'Down':      b'\x0A',
                'Up':        b'\x0B',
                'Right':     b'\x0C',
                'Home':      b'\x1E',
                'BackSpace': b'\x08',
                'Delete':    b'\x7F',
                'Return':    b'\x0D',
                'Tab':       b'\x09',
                'Escape':    b'\x1B',
            }
        else:
            key_map = {
                'Left':      b'\x1B[D',
                'Down':      b'\x1B[B',
                'Up':        b'\x1B[A',
                'Right':     b'\x1B[C',
                'Home':      b'\x1B[H',
                'End':       b'\x1B[F',
                'Prior':     b'\x1B[5~',   # PgUp
                'Next':      b'\x1B[6~',   # PgDn
                'BackSpace': b'\x7F',
                'Delete':    b'\x1B[3~',
                'Return':    b'\x0D',
                'Tab':       b'\x09',
                'Escape':    b'\x1B',
                'F1':        b'\x1BOP',
                'F2':        b'\x1BOQ',
                'F3':        b'\x1BOR',
                'F4':        b'\x1BOS',
            }

        if ks in key_map:
            self.telnet.send(key_map[ks])
            return

        if ch:
            try:
                self.telnet.send(ch.encode('latin-1'))
            except UnicodeEncodeError:
                pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    root = tk.Tk()
    TerminalApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
