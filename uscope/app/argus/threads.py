from uscope.planner.planner_util import get_planner
from uscope.planner.planner import PlannerStop
from uscope.benchmark import Benchmark
from uscope.motion.hal import AxisExceeded, MotionHAL, MotionCritical
from uscope.motion.plugins import get_motion_hal
from uscope.joystick import Joystick, JoystickNotFound
from uscope import cloud_stitch
from uscope import config
from PyQt5.QtCore import QThread, pyqtSignal
import traceback
import datetime
import queue
import threading
import time
from queue import Queue, Empty
import subprocess
import cv2 as cv
import numpy as np
from uscope.planner.planner_util import microscope_to_planner_config
from uscope.kinematics import Kinematics


def dbg(*args):
    if len(args) == 0:
        print()
    elif len(args) == 1:
        print('threading: %s' % (args[0], ))
    else:
        print('threading: ' + (args[0] % args[1:]))


'''
Offloads controller processing to another thread (or potentially even process)
Makes it easier to keep RT deadlines and such
However, it doesn't provide feedback completion so use with care
(other blocks until done)
TODO: should block?
'''


class MotionThreadMotion(MotionHAL):
    def __init__(self, mt):
        self.mt = mt
        MotionHAL.__init__(self, log=mt.motion.log, verbose=mt.motion.verbose)

        # Don't re-apply pipeline (scaling, etc)
        self.configure({})

    def axes(self):
        return self.mt.motion.axes()

    def home(self, axes):
        self.mt.home(block=True)

    def _move_absolute(self, pos):
        self.mt.move_absolute(pos, block=True)

    def _move_relative(self, pos):
        self.mt.move_relative(pos, block=True)

    def _pos(self):
        # return self.mt.pos_cache
        return self.mt.pos()

    def settle(self):
        # No hardware to let settle
        pass

    def ar_stop(self):
        pass


class MotionThread(QThread):
    log_msg = pyqtSignal(str)

    def __init__(self, usc):
        QThread.__init__(self)
        self.usc = usc
        self.verbose = False
        self.queue = queue.Queue()
        self.motion = None
        self.running = threading.Event()
        self.idle = threading.Event()
        self.idle.set()
        self.normal_running = threading.Event()
        self.normal_running.set()
        self.lock = threading.Event()
        # Let main gui get the last position from a different thread
        # It can request updates
        self.pos_cache = None
        self._stop = False
        self._estop = False
        # XXX: add config directive
        self.allow_motion_reboot = False

        # Seed state / refuse to start without motion
        self.init_motion()

    def init_motion(self):
        self.motion = get_motion_hal(usc=self.usc, log=self.log)

    def log(self, msg):
        self.log_msg.emit(msg)

    def setRunning(self, running):
        if running:
            self.normal_running.set()
        else:
            self.normal_running.clear()

    def wait_idle(self):
        while True:
            time.sleep(0.15)
            if self.idle.is_set():
                break

    def command(self, command, *args, block=False):
        command_done = None
        if block:
            ready = threading.Event()
            ret = []

            def command_done(command, args, ret_e):
                ret.append(ret_e)
                ready.set()

        self.queue.put((command, args, command_done))
        if block:
            ready.wait()
            ret = ret[0]
            if type(ret) is Exception:
                raise Exception("oopsie: %s" % (ret, ))
            return ret

    def pos(self):
        # XXX: this caused crashes but I'm not sure why
        # Just offload to the thread to avoid this special case
        if 0:
            self.lock.set()
            ret = self.motion.pos()
            self.lock.clear()
            return ret
        else:
            return self.command("pos", block=True)

    def mdi(self, cmd):
        self.command("mdi", cmd)

    def jog(self, pos):
        self.command("jog", pos)

    def stop(self):
        # self.command("stop")
        self._stop = True

    def estop(self):
        # self.command("estop")
        self._estop = True

    def home(self, block=False):
        self.command("home", block=block)

    def backlash_disable(self, block=False):
        self.command("backlash_disable", block=block)

    def backlash_enable(self, block=False):
        self.command("backlash_enable", block=block)

    def move_absolute(self, pos, block=False):
        self.command("move_absolute", pos, block=block)

    def move_relative(self, pos, block=False):
        self.command("move_relative", pos, block=block)

    def set_jog_rate(self, rate):
        self.command("set_jog_rate", rate)

    def update_pos_cache(self):
        self.command("update_pos_cache")

    def qsize(self):
        return self.queue.qsize()

    def queue_clear(self):
        while True:
            try:
                self.queue.get(block=False)
            except queue.Empty:
                break

    def get_planner_motion(self):
        return MotionThreadMotion(self)

    def shutdown(self):
        self.running.clear()

    def run(self):
        self.verbose and print("Motion thread started: %s" %
                               (threading.get_ident(), ))
        self.running.set()
        self.idle.clear()
        self.motion.on()

        def motion_status(status):
            # print("register_status_cb: via motion-status: %s" % (status,))
            self.pos_cache = status["pos"]

        self.motion.register_status_cb(motion_status)

        try:
            while self.running.is_set():
                self.lock.set()

                if not self.motion:
                    if not self.allow_motion_reboot:
                        self.log("Fatal error: motion controller is dead")
                        break
                    else:
                        # See if its back...
                        try:
                            self.init_motion()
                            self.motion.on()
                        except Exception as e:
                            self.log(
                                "Failed to reboot motion controller :( %s" %
                                (str(e), ))
                            time.sleep(3)
                        continue

                if self._estop:
                    self.motion.estop()
                    self.queue_clear()
                    self._estop = False
                    continue

                if self._stop:
                    self.motion.stop()
                    self.queue_clear()
                    self._stop = False
                    continue

                if not self.normal_running.isSet():
                    self.normal_running.wait(0.1)
                    continue
                try:
                    self.lock.clear()
                    (command, args, command_done) = self.queue.get(True, 0.1)
                except queue.Empty:
                    self.idle.set()
                    continue
                finally:
                    self.lock.set()

                self.idle.clear()

                def default(*args):
                    raise Exception("Bad command %s" % (command, ))

                def move_absolute(pos):
                    try:
                        self.motion.move_absolute(pos)
                    except AxisExceeded as e:
                        self.log(str(e))
                    return self.motion.pos()

                def move_relative(pos):
                    try:
                        self.motion.move_relative(pos)
                    except AxisExceeded as e:
                        self.log(str(e))
                    return self.motion.pos()

                def update_pos_cache():
                    pos = self.motion.pos()
                    self.pos_cache = pos
                    # print("register_status_cb: via update_pos_cache: %s" % (pos,))

                self.verbose and print("")
                self.verbose and print(
                    "process @ %s" % datetime.datetime.utcnow().isoformat())
                #print 'cnc thread: dispatch %s' % command
                # Maybe I should just always emit the pos
                f = {
                    'update_pos_cache': update_pos_cache,
                    'move_absolute': move_absolute,
                    'move_relative': move_relative,
                    'jog': self.motion.jog,
                    'pos': self.motion.pos,
                    'set_jog_rate': self.motion.set_jog_rate,
                    'home': self.motion.home,
                    'backlash_disable': self.motion.backlash_disable,
                    'backlash_enable': self.motion.backlash_enable,
                    # 'stop': self.motion.stop,
                    # 'estop': self.motion.estop,
                    'unestop': self.motion.unestop,
                    'mdi': self.motion.command,
                }.get(command, default)
                try:
                    ret = f(*args)
                # Depending on the motion controller this may be a bad idea
                # Only some of them retain the old coordinate system / may need re-home
                except MotionCritical as e:
                    print("")
                    print("ERROR: motion controller crashed w/ critical error")
                    print(traceback.format_exc())
                    self.motion.close()
                    self.motion = None
                    if command_done:
                        command_done(command, args, e)
                    continue
                except Exception as e:
                    print("")
                    print("WARNING: motion thread crashed")
                    print(traceback.format_exc())
                    if command_done:
                        command_done(command, args, e)
                    continue

                if command_done:
                    command_done(command, args, ret)

        finally:
            if self.motion:
                self.motion.stop()
                # self.motion.ar_stop()


"""
Sends events to the imaging and movement threads

rconfig: misc parmeters including complex objects
plannerj: planner configuration JSON. Written to disk
"""


class PlannerThread(QThread):
    plannerDone = pyqtSignal(dict)
    log_msg = pyqtSignal(str)

    def __init__(self, parent, planner_args, progress_cb):
        QThread.__init__(self, parent)
        self.planner_args = planner_args
        self.planner = None
        self.progress_cb = progress_cb

    def log(self, msg=""):
        #print 'emitting log %s' % msg
        #self.log_buff += str(msg) + '\n'
        self.log_msg.emit(str(msg))

    def setRunning(self, running):
        planner = self.planner
        if planner:
            planner.setRunning(running)

    def is_paused(self):
        if self.planner:
            return self.planner.is_paused()
        return False

    def pause(self):
        if self.planner:
            self.planner.pause()

    def unpause(self):
        if self.planner:
            self.planner.unpause()

    def shutdown(self):
        if self.planner:
            self.planner.stop()

    def run(self):
        ret = {
            "result": None,
        }
        try:
            self.log('Initializing planner!')
            # print("Planner thread started: %s" % (threading.get_ident(), ))

            self.planner = get_planner(log=self.log, **self.planner_args)
            self.planner.register_progress_callback(self.progress_cb)
            self.log('Running planner')
            b = Benchmark()
            self.log()
            self.log()
            self.log()
            self.log()
            ret["meta"] = self.planner.run()
            ret["result"] = "ok"
            b.stop()
            self.log('Planner done!  Took : %s' % str(b))
        except PlannerStop as e:
            ret["result"] = "stopped"
        except Exception as e:
            self.log('WARNING: planner thread crashed: %s' % str(e))
            traceback.print_exc()
            ret["result"] = "exception"
            ret["exception"] = e
            #raise
        finally:
            self.plannerDone.emit(ret)


class StitcherThread(QThread):
    # stitcherDone = pyqtSignal()
    log_msg = pyqtSignal(str)

    def __init__(self, parent=None):
        QThread.__init__(self, parent)
        self.queue = Queue()
        self.running = threading.Event()
        self.running.set()

    def log(self, msg):
        self.log_msg.emit(msg)

    def shutdown(self):
        self.running.clear()

    def cli_stitch_add(self, directory, command):
        j = {
            "type": "cli",
            "directory": directory,
            "command": command,
        }
        self.queue.put(j)

    # Offload uploads etc to thread since they might take a while
    def cloud_stitch_add(
        self,
        directory,
        access_key,
        secret_key,
        id_key,
        notification_email,
    ):

        j = {
            "type": "CloudStitch",
            "directory": directory,
            "access_key": access_key,
            "secret_key": secret_key,
            "id_key": id_key,
            "notification_email": notification_email,
        }
        self.queue.put(j)

    def run(self):
        while self.running:
            try:
                j = self.queue.get(block=True, timeout=0.1)
            except Empty:
                continue
            try:
                if j["type"] == "CloudStitch":
                    cloud_stitch.upload_dir(
                        directory=j["directory"],
                        id_key=j["id_key"],
                        access_key=j["access_key"],
                        secret_key=j["secret_key"],
                        notification_email=j["notification_email"],
                        log=self.log,
                        running=self.running)
                elif j["type"] == "cli":
                    self.log(
                        f"Stitch CLI: kicking off {j['command']} {j['directory']}"
                    )
                    # Hacky but good enough for now
                    # Check terminal for process output
                    print("")
                    print("")
                    print("")
                    print(
                        f"Stitch CLI: kicking off {j['command']} {j['directory']}"
                    )
                    subprocess.check_call([j['command'], j['directory']])
                    self.log(f"Stitch CLI: finished job")
                    print(f"Stitch CLI: finished job")
                else:
                    assert 0, j

            except Exception as e:
                self.log('WARNING: stitcher thread crashed: %s' % str(e))
                traceback.print_exc()
            finally:
                # self.stitcherDone.emit()
                pass


class ImageProcessingThread(QThread):
    log_msg = pyqtSignal(str)

    def __init__(self, motion_thread, ac, parent=None):
        QThread.__init__(self, parent)
        self.queue = Queue()
        self.running = threading.Event()
        self.running.set()
        self.motion_thread = motion_thread
        self.ac = ac
        self.imager = self.ac.imager
        self.kinematics = None

        self.kinematics = Kinematics(
            microscope=self.ac.microscope,
            log=self.log,
        )
        self.kinematics.configure()

    def log(self, msg):
        self.log_msg.emit(msg)

    def shutdown(self):
        self.running.clear()

    def auto_focus(self):
        j = {
            "type": "auto_focus",
        }
        self.queue.put(j)

    def move_absolute(self, pos):
        self.motion_thread.move_absolute(pos, block=True)
        self.kinematics.wait_imaging_ok()

    def pos(self, pos):
        return self.motion_thread.pos()

    def do_auto_focus(self):
        def get_score(image, blur=9):
            filtered = cv.medianBlur(image, blur)
            laplacian = cv.Laplacian(filtered, cv.CV_64F)
            return laplacian.var()

        def image_pil2cv(im):
            return np.array(im)[:, :, ::-1].copy()

        step_pm = 2
        start_pos = self.pos()
        im_pil = self.imager.get()["0"]
        im_cv = image_pil2cv(im_pil)
        score = get_score(im_cv)
        self.log("Current score: %0.3f" % score)

    def run(self):
        while self.running:
            try:
                j = self.queue.get(block=True, timeout=0.1)
            except Empty:
                continue
            try:
                if j["type"] == "auto_focus":
                    self.do_auto_focus()
                else:
                    assert 0, j

            except Exception as e:
                self.log('WARNING: stitcher thread crashed: %s' % str(e))
                traceback.print_exc()
            finally:
                # self.stitcherDone.emit()
                pass


class JoystickThread(QThread):
    joystickDone = pyqtSignal()
    log_msg = pyqtSignal(str)

    def __init__(self, ac, parent=None):
        QThread.__init__(self, parent)
        self.joystick = None
        self.ac = ac
        self.parent = parent
        self.queue = Queue()
        self.running = threading.Event()
        self.running.set()
        try:
            self.joystick = Joystick(ac=self.ac)
        except JoystickNotFound:
            raise JoystickNotFound()
        self._state_btn = None

    def post_ui_init(self):
        self._state_btn = self.parent.mainTab.motion_widget.joystick_listener
        self.enable()
        self.activate()

    def log_info(self):
        self.log("Joystick")
        self.log(f"  Name: {self.joystick.joystick.name}")
        self.log(f"  Axes: {self.joystick.joystick.numaxes}")
        self.log(f"  Trackballs: {self.joystick.joystick.numballs}")
        self.log(f"  Hats: {self.joystick.joystick.numhats}")
        self.log(f"  Buttons: {self.joystick.joystick.numbuttons}")

    def disable(self):
        # This deactivates and disables joystick
        # actions, and user cannot re-enable.
        self._state_btn.setEnabled(False)

    def enable(self):
        # This enables activation of joystick
        # actions by the user.
        self._state_btn.setEnabled(True)

    def activate(self):
        # This activates joystick actions, and
        # user can deactivate.
        if not self._state_btn.isChecked():
            self._state_btn.toggle()

    def deactivate(self):
        # This deactivates joystick actions but
        # user can re-activate.
        if self._state_btn.isChecked():
            self._state_btn.toggle()

    def log(self, msg):
        self.log_msg.emit(msg)

    def shutdown(self):
        self.running.clear()

    def run(self):
        while self.running:
            try:
                time.sleep(self.parent.bc.joystick.scan_secs())
                # It is important to check that the button is both enabled and
                # active before performing actions. This allows us to preserve
                # state by disabling and enabling the button only during scans.
                if self._state_btn.isEnabled() and self._state_btn.isChecked():
                    #self.joystick.debug_dump()
                    self.joystick.execute()
            except Exception as e:
                self.log('WARNING: joystick thread crashed: %s' % str(e))
                traceback.print_exc()
            finally:
                self.joystickDone.emit()
