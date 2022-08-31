"""
[HLP:$$ $# $G $I $N $x=val $Nx=line $J=line $SLP $C $X $H ~ ! ? ctrl-x]
ok

Case insensitive best I can tell
"""

from uscope.motion.hal import MotionHAL, format_t, AxisExceeded

from uscope import util
import termios
import serial
import time
import os
import threading


class Timeout(Exception):
    pass


def default_port():
    return "/dev/ttyUSB0"


def trim_data_line(l):
    # print("test", l)
    assert l[0] == "["
    assert l[-1] == "]"
    return l[1:-1]


def trim_status_line(l):
    if len(l) < 2:
        raise ValueError("bad status line, len=%u" % len(l))
    # print("test", l)
    assert l[0] == "<"
    assert l[-1] == ">"
    return l[1:-1]


"""
https://www.sainsmart.com/blogs/news/grbl-v1-1-quick-reference
"""

error_i2s = {
    1: "GCode Command letter was not found",
    2: "GCode Command value invalid or missing",
    3: "Grbl '$' not recognized or supported",
    4: "Negative value for an expected positive value",
    5: "Homing fail. Homing not enabled in settings",
    6: "Min step pulse must be greater than 3usec",
    7: "EEPROM read failed. Default values used",
    8: "Grbl '$' command Only valid when Idle",
    9: "GCode commands invalid in alarm or jog state",
    10: "Soft limits require homing to be enabled",
    11: "Max characters per line exceeded. Ignored",
    12: "Grbl '$' setting exceeds the maximum step rate",
    13: "Safety door opened and door state initiated",
    14: "Build info or start-up line > EEPROM line length",
    15: "Jog target exceeds machine travel, ignored",
    16: "Jog Cmd missing '=' or has prohibited GCode",
    17: "Laser mode requires PWM output",
    20: "Unsupported or invalid GCode command",
    21: "> 1 GCode command in a modal group in block",
    22: "Feed rate has not yet been set or is undefined",
    23: "GCode command requires an integer value",
    24: "> 1 GCode command using axis words found",
    25: "Repeated GCode word found in block",
    26: "No axis words found in command block",
    27: "Line number value is invalid",
    28: "GCode Cmd missing a required value word",
    29: "G59.x WCS are not supported",
    30: "G53 only valid with G0 and G1 motion modes",
    31: "Unneeded Axis words found in block",
    32: "G2/G3 arcs need >= 1 in-plane axis word",
    33: "Motion command target is invalid",
    34: "Arc radius value is invalid",
    35: "G2/G3 arcs need >= 1 in-plane offset word",
    36: "Unused value words found in block",
    37: "G43.1 offset not assigned to tool length axis",
    38: "Tool number greater than max value",
}

alarm_i2s = {
    1: "Hard limit triggered. Position Lost",
    2: "Soft limit alarm, position kept. Unlock is Safe",
    3: "Reset while in motion. Position lost",
    4: "Probe fail. Probe not in expected initial state",
    5: "Probe fail. Probe did not contact the work",
    6: "Homing fail. The active homing cycle was reset",
    7: "Homing fail. Door opened during homing cycle",
    8: "Homing fail. Pull off failed to clear limit switch",
    9: "Homing fail. Could not find limit switch",
}

config_i2s = {
    0: "Step pulse, microseconds",
    1: "Step idle delay, milliseconds",
    2: "Step port invert, XYZmask*",
    3: "Direction port invert, XYZmask*",
    4: "Step enable invert, (0=Disable, 1=Invert)",
    5: "Limit pins invert, (0=N-Open. 1=N-Close)",
    6: "Probe pin invert, (0=N-Open. 1=N-Close)",
    10:
    "Status report, ‘?’ status.  0=WCS position, 1=Machine position, 2= plan/buffer and WCS position, 3=plan/buffer and Machine position",
    11: "Junction deviation, mm",
    12: "Arc tolerance, mm",
    13: "Report in inches, (0=mm. 1=Inches)**",
    20: "Soft limits, (0=Disable. 1=Enable, Homing must be enabled)",
    21: "Hard limits, (0=Disable. 1=Enable)",
    22: "Homing cycle, (0=Disable. 1=Enable)",
    23: "Homing direction invert, XYZmask* Sets which corner it homes to",
    24: "Homing feed, mm/min",
    25: "Homing seek, mm/min",
    26: "Homing debounce, milliseconds",
    27: "Homing pull-off, mm",
    30: "Max spindle speed, RPM",
    31: "Min spindle speed, RPM",
    32: "Laser mode, (0=Off, 1=On)",
    100: "Number of X steps to move 1mm",
    101: "Number of Y steps to move 1mm",
    102: "Number of Z steps to move 1mm",
    110: "X Max rate, mm/min",
    111: "Y Max rate, mm/min",
    112: "Z Max rate, mm/min",
    120: "X Acceleration, mm/sec^2",
    121: "Y Acceleration, mm/sec^2",
    122: "Z Acceleration, mm/sec^2",
    130: "X Max travel, mm Only for Homing and Soft Limits",
    131: "Y Max travel, mm Only for Homing and Soft Limits",
    132: "Z Max travel, mm Only for Homing and Soft Limits",
}


class GRBLSer:
    def __init__(
        self,
        port=None,
        # All commands I've seen so far complete responses in < 10 ms
        # so this should be plenty of margin for now
        ser_timeout=0.15,
        flush=True,
        verbose=None):
        if port is None:
            port = default_port()
        self.verbose = verbose if verbose is not None else bool(
            int(os.getenv("GRBLSER_VERBOSE", "0")))
        # For debugging concurrency issue
        self.poison_threads = False
        self.last_thread = None

        self.verbose and print("opening %s in thread %s" %
                               (port, threading.get_ident()))

        # workaround for pyserial toggling flow control lines on open
        # https://github.com/pyserial/pyserial/issues/124
        f = open(port)
        attrs = termios.tcgetattr(f)
        """
       HUPCL  Lower modem control lines after last process closes the
              device (hang up).

       TCSAFLUSH
              the change occurs after all output written to the object
              referred by fd has been transmitted, and all input that
              has been received but not read will be discarded before
        """
        attrs[2] = attrs[2] & ~termios.HUPCL
        termios.tcsetattr(f, termios.TCSAFLUSH, attrs)
        f.close()

        # Now carefuly do an open
        self.serial = serial.Serial(
            port,
            baudrate=115200,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            rtscts=False,
            dsrdtr=False,
            xonxoff=False,
            timeout=ser_timeout,
            # Blocking writes
            writeTimeout=None)

        if flush:
            # Try to abort an in progress command
            # ^X will also work but resets whole controller
            # ^C does not work
            # WARNING: ! will also freeze most commands but not ?
            # they will buffer into unfrozen
            self.flush()

    def flush(self):
        """
        Wait to see if there is anything in progress
        """
        self.serial.flushInput()
        self.serial.flushOutput()
        timeout = self.serial.timeout
        try:
            self.serial.timeout = 0.1
            while True:
                c = self.serial.read()
                if not c:
                    return
        finally:
            self.serial.timeout = timeout

    def tx(self, out, nl=True):
        self.verbose and print("tx '%s'" % (out, ))

        if self.poison_threads:
            if self.last_thread:
                assert self.last_thread == threading.get_ident(), (
                    self.last_thread, threading.get_ident())
            else:
                self.last_thread = threading.get_ident()
            print("grbl thread: %s" % threading.get_ident())

        if nl:
            out = out + '\r'
        out = out.encode('ascii')
        # util.hexdump(out)
        self.serial.write(out)
        self.serial.flush()

    def txb(self, out):
        # self.verbose and print("tx '%s'" % (out, ))
        # util.hexdump(out)
        self.serial.write(out)
        self.serial.flush()

    def readline(self):
        tstart = time.time()
        b = self.serial.readline()
        if self.verbose:
            tend = time.time()
            print("rx %u bytes in %0.3f sec" % (len(b), tend - tstart))
            from uscope import util
            util.hexdump(b)
        return b.decode("ascii").strip()

    def txrxs(self, out, nl=True, trim_data=True):
        """
        Send a command and return array of lines before ok line
        """
        self.tx(out, nl=nl)
        ret = []
        while True:
            l = self.readline().strip()
            self.verbose and print("rx '%s'" % (l, ))
            if not l:
                raise Timeout()
            elif l == "ok":
                return ret
            elif l.find("error") == 0:
                raise Exception(l)
            else:
                if trim_data:
                    ret.append(trim_data_line(l))
                else:
                    ret.append(l)

    def txrx0(self, out, nl=True):
        """
        Send a command and expect nothing back before ok
        """
        ret = self.txrxs(out, nl=nl)
        assert len(ret) == 0

    def txrx(self, out, nl=True):
        """
        Send a command and expect one line back before ok
        """
        ret = self.txrxs(out, nl=nl)
        assert len(ret) == 1
        return ret[0]

    def help(self):
        """
        [HLP:$$ $# $G $I $N $x=val $Nx=line $J=line $SLP $C $X $H ~ ! ? ctrl-x]
        """
        return self.txrx("$")

    def reset(self):
        """
        ^X
        Grbl 1.1f ['$' for help]
        """
        self.tx("\x18", nl=False)
        # Leave recovery to higher level logic
        """
        l = self.readline().strip()
        assert l == ""
        l = self.readline().strip()
        assert "Grbl" in l, l
        """

    def tilda(self):
        """Cycle Start/Resume from Feed Hold, Door or Program pause"""
        self.tx("~", nl=False)

    def exclamation(self):
        """Feed Hold – Stop all motion"""
        self.tx("!", nl=False)

    def dollar(self):
        """
        $$
        $0=10
        $1=25
        $2=0
        $3=2
        $4=0
        $5=0
        $6=0
        $10=1
        $11=0.010
        $12=0.002
        $13=0
        $20=0
        $21=0
        $22=0
        $23=0
        $24=25.000
        $25=500.000
        $26=250
        $27=1.000
        $30=1000
        $31=0
        $32=0
        $100=800.000
        $101=800.000
        $102=800.000
        $110=1000.000
        $111=1000.000
        $112=600.000
        $120=30.000
        $121=30.000
        $122=30.000
        $130=200.000
        $131=200.000
        $132=200.000
        """
        return self.txrxs("$$", trim_data=False)

    def hash(self):
        """
        [G54:0.000,0.000,0.000]
        [G55:0.000,0.000,0.000]
        [G56:0.000,0.000,0.000]
        [G57:0.000,0.000,0.000]
        [G58:0.000,0.000,0.000]
        [G59:0.000,0.000,0.000]
        [G28:0.000,0.000,0.000]
        [G30:0.000,0.000,0.000]
        [G92:0.000,0.000,0.000]
        [TLO:0.000]
        [PRB:0.000,0.000,0.000:0]
        ok
        """
        return self.txrxs("$#")

    def question(self):
        """
        <Idle|MPos:0.000,0.000,0.000|FS:0,0|WCO:0.000,0.000,0.000>
        <Idle|MPos:0.000,0.000,0.000|FS:0,0|Ov:100,100,100>
        <Idle|MPos:0.000,0.000,0.000|FS:0,0>
        """
        self.tx("?", nl=False)
        l = self.readline()
        self.verbose and print("rx '%s'" % (l, ))
        return trim_status_line(l)

    def c(self):
        """
        $C
        """

    def g(self):
        """
        $G
        [GC:G0 G54 G17 G21 G90 G94 M5 M9 T0 F0 S0]
        """
        return self.txrx("$G")

    def h(self):
        """
        """

    def i(self):
        """
        [VER:1.1f.20170801:]
        [OPT:V,15,128]
        ok
        """
        return self.txrxs("$I")

    def info(self):
        ver, opt = self.i()
        return ver, opt

    def j(self, command):
        """
        $J=G90 X0.0 Y0.0 F1
        """
        self.txrx0("$J=" + command)

    def n(self):
        """
        $N
        $N0=
        $N1=
        """
        return self.txrxs("$N")

    def cancel_jog(self):
        # From Yusuf
        self.txb(b"\x85")


class MockGRBLSer(GRBLSer):
    def __init__(
        self,
        port="/dev/ttyUSB0",
        # some boards take more than 1 second to reset
        ser_timeout=3.0,
        flush=True,
        verbose=None):
        self.verbose = verbose if verbose is not None else bool(
            int(os.getenv("GRBLSER_VERBOSE", "0")))
        self.verbose and print("MOCK: opening", port)
        self.serial = None

    def txb(self, out):
        self.verbose and print("MOCK: txb", out)

    def txrx0(self, out, nl=True):
        self.verbose and print("MOCK: txrx0", out)

    def qstatus(self):
        return {
            "status": "Idle",
            "MPos": {
                "x": 0.0,
                "y": 0.0,
                "z": 0.0
            },
            "FS": 0.0,
        }

    def question(self):
        return "Idle|MPos:0.000,0.000,0.000|FS:0,0"


class GRBL:
    def __init__(self,
                 port=None,
                 flush=True,
                 probe=True,
                 reset=False,
                 gs=None,
                 scalar=None,
                 verbose=None):
        """
        port: serial port file name
        gs: supply your own serial port object
        flush: try to clear old serial port communications before initializing
        probe: check communications at init to make sure controlelr is working
        reset: do a full reset at initialization. You will loose position and it will take a while
        verbose: yell stuff to the screen
        """

        self.verbose = verbose if verbose is not None else bool(
            int(os.getenv("GRBL_VERBOSE", "0")))
        if gs is None:
            if port == "mock":
                gs = MockGRBLSer(verbose=verbose)
            else:
                gs = GRBLSer(port=port, verbose=verbose)
        self.gs = gs
        if flush:
            pass
        if probe:
            self.reset_probe()
        # WARNING: probe issue makes this unsafe to use first
        # CPU must be safely brought out of reset
        if reset:
            self.reset()

    def __del__(self):
        self.stop()

    def stop(self):
        self.cancel_jog()

    def reset(self):
        self.gs.reset()
        self.reset_recover()

    def reset_probe(self):
        """
        Try to establish communications as a reset may be occurring
        Workaround for poorly understood issue (see below)
        Takes about 1.5 seconds typically to get response

        Weird connection startup issue
        Connecting serial port may reset the device (???)
        Workaround: probe will try to establish connection
        If can't get response, try to get the sync message back

        rx 2 bytes in 1.379 sec
        00000000  0D 0A

        typical resposne is < 0.05 sec
        """
        tbegin = time.time()

        self.gs.tx("?", nl=False)
        l = self.gs.readline()
        # print("got", len(l), l)
        # Line shoudl be quite long and have markers
        if len(l) > 4 and "<" in l and ">" in l:
            # Connection ok, no need to
            return

        print("grbl WARNING: failed to respond, attempting reset recovery")
        self.reset_recover()

        # Run full status command to be sure
        self.qstatus()
        # grbl: recovered after 1.388 sec
        print("grbl: recovered after %0.3f sec" % (time.time() - tbegin, ))

    def general_recover(self):
        """
        Recover after communication issue when not in reset
        """
        tries = 3
        for tryi in range(tries):
            try:
                self.verbose and print("WARNING: recovering")
                # might have been put into hold
                self.gs.tilda()
                # Clear any commands in progress
                self.gs.flush()
                # Try a simple command
                self.qstatus()
            except Exception:
                if tryi == tries - 2:
                    raise
                continue

    def reset_recover(self):
        # Prompt should come in about 1.5 seconds from start
        tbegin = time.time()
        while time.time() - tbegin < 2.0:
            l = self.gs.readline()
            if len(l):
                break
        else:
            raise Exception("Timed out reestablishing communications")

        # Initial response should be newline
        # Wait until grbl reset prompt
        tstart = time.time()
        while time.time() - tstart < 1.0:
            # Grbl 1.1f ['$' for help]
            if "Grbl" in l:
                break
            # Normal timeout here?
            # Should be moving quickly now
            l = self.gs.readline()

    def qstatus(self):
        """
        Idle|MPos:8.000,0.000,0.000|FS:0,0|WCO:0.000,0.000,0.000
        Idle|MPos:8.000,0.000,0.000|FS:0,0|Ov:100,100,100
        Idle|MPos:8.000,0.000,0.000|FS:0,0

        noisy example:
        rx '<Idle|MPos:-72.425,-25.634,0.000FS:0,0>'
        """
        tries = 3
        for i in range(tries):
            try:
                raw = self.gs.question()
                parts = raw.split("|")
                # FIXME: extra
                ij, mpos, fs = parts[0:3]
                mpos = (float(x) for x in mpos.split(":")[1].split(","))
                mpos = dict([(k, v) for k, v in zip("xyz", mpos)])
                return {
                    # Idle, Jog
                    "status": ij,
                    "MPos": mpos,
                    "FS": fs,
                }
            except Exception:
                self.verbose and print("WARNING: bad qstatus")
                if i == tries - 1:
                    raise
                self.general_recover()
        assert 0

    def mpos(self):
        """Return current absolute position"""
        return self.qstatus()["MPos"]

    def move_absolute(self, moves, f, blocking=True):
        tries = 3
        for i in range(tries):
            try:
                # implies G1
                ax_str = ''.join(
                    [' %c%0.3f' % (k.upper(), v) for k, v in moves.items()])
                self.gs.j("G90 %s F%u" % (ax_str, f))
                if blocking:
                    self.wait_idle()
            except Exception:
                self.verbose and print("WARNING: bad absolute move")
                if i == tries - 1:
                    raise
                self.general_recover()

    def move_relative(self, moves, f, blocking=True):
        # implies G1
        ax_str = ''.join(
            [' %c%0.3f' % (k.upper(), v) for k, v in moves.items()])
        self.gs.j("G91 %s F%u" % (ax_str, f))
        if blocking:
            self.wait_idle()

    def wait_idle(self):
        while self.qstatus()["status"] != "Idle":
            time.sleep(0.1)

    def jog(self, scalars, rate):
        try:
            for axis, scalar in scalars.items():
                cmd = "G91 %s%0.3f F%u" % (axis, scalar, rate)
                self.verbose and print("JOG:", cmd)
                self.gs.j(cmd)
                if 0 and self.verbose:
                    mpos = self.qstatus()["MPos"]
                    print("jog: X%0.3f Y%0.3f Z%0.3F" %
                          (mpos["x"], mpos["y"], mpos["z"]))
        except Timeout:
            # Better to under jog than retry and over jog
            self.verbose and print("WARNING: dropping jog")
            self.general_recover()

    def cancel_jog(self):
        tries = 3
        timeout = 0.5
        for i in range(tries):
            try:
                tstart = time.time()
                while True:
                    if time.time() - tstart > timeout:
                        raise Timeout("Failed to cancel jog")
                    self.gs.cancel_jog()
                    if self.qstatus()["status"] == "Idle":
                        return
                    self.verbose and print("cancel: not idle yet")
            except Exception:
                self.verbose and print("WARNING: bad cancel jog")
                if i == tries - 1:
                    raise
                self.general_recover()


class GrblHal(MotionHAL):
    def __init__(self, verbose=None, **kwargs):
        self.feedrate = None
        self.grbl = GRBL(verbose=verbose)
        MotionHAL.__init__(self, verbose=verbose, **kwargs)

    def axes(self):
        return {'x', 'y', 'z'}

    def command(self, cmd):
        return "\n".join(self.grbl.gs.txrxs(cmd))

    def _pos(self):
        return self.grbl.qstatus()["MPos"]

    def _move_absolute(self, pos, tries=3):
        self.grbl.move_absolute(pos, f=1000)

    def _move_relative(self, pos):
        self.grbl.move_relative(pos, f=1000)

    def _jog(self, scalars):
        self.grbl.jog(scalars, self.jog_rate)

    def stop(self):
        self.grbl.stop()


def get_grbl(port=None, gs=None, reset=False, verbose=False):
    return GRBL(port=port, gs=gs, reset=reset, verbose=verbose)