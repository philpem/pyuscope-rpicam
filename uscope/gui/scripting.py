from uscope.gui.widgets import ArgusTab
from uscope.gui.input_widget import InputWidget
from uscope.motion import motion_util
from uscope.microscope import StopEvent, MicroscopeStop
from uscope.util import readj, writej, time_str_1dec

from PyQt5 import Qt
from PyQt5.QtGui import *
from PyQt5.QtCore import *
from PyQt5.QtWidgets import *
import importlib.util
import sys
import os
import ctypes
import threading
import time
import traceback


class TestFailed(Exception):
    pass


class TestAborted(Exception):
    pass


class TestKilled(SystemExit):
    pass


class QHLine(QFrame):
    def __init__(self):
        super(QHLine, self).__init__()
        self.setFrameShape(QFrame.HLine)
        self.setFrameShadow(QFrame.Sunken)


class QVLine(QFrame):
    def __init__(self):
        super(QVLine, self).__init__()
        self.setFrameShape(QFrame.VLine)
        self.setFrameShadow(QFrame.Sunken)


# class ArgusScriptingPlugin(threading.Thread):
# needed to do signals
class ArgusScriptingPlugin(QThread):
    log_msg = pyqtSignal(str)
    done = pyqtSignal()

    def __init__(self, ac):
        super().__init__()
        self._ac = ac
        self._input = None
        self.se = None
        # Graceful shutdown request
        self._running = threading.Event()
        self.reset()
        self.tstart = None
        self.tend = None

    def reset(self):
        self._succeeded = None
        self.result_message = None
        self.new_defaults = {}
        self._running.set()

    def log(self, s):
        """
        Log a message to the script window
        (not the main window)
        """
        self.log_msg.emit(s)

    def shutdown(self):
        """
        Request graceful termination
        """
        self._running.clear()

    def get_input(self):
        """
        Return a dictionary containing applicable fields
        """
        return self._input

    def set_input_default(self, k, value):
        """
        Allows a script to have modes that setup various parameters
        NOTE: this used to be by label, now its by key
        """
        self.new_defaults[k] = value

    def fail(self, message):
        """
        Thread should call this to abort a run
        Indicates the operation failed
        """
        self._succeeded = False
        self.result_message = message
        raise TestFailed(message)

    def check_running(self):
        """
        This thread should periodically check for graceful shutdown
        """
        self.se.poll()
        if not self._running.is_set():
            raise TestAborted()

    def succeeded(self):
        return bool(self._succeeded)

    def run(self, input_=None, button_value=None, top_level=True):
        self.tstart = time.time()
        if button_value is not None:
            self._input = {"button": {"value": button_value}}
        if input_ is not None:
            self._input = input_
        self.ident = threading.current_thread().ident
        try:
            with StopEvent(self._ac.microscope) as self.se:
                self.run_test()
                self.wrap_cleanup("Done. Running cleanup")
            self._succeeded = True
        except TestFailed:
            self.wrap_cleanup("Failed. Running cleanup")
            self._succeeded = False
            self.result_message = "Failed"
        # Test stopped but not microscope
        except TestAborted:
            self.wrap_cleanup("Aborted. Running cleanup")
            self._succeeded = False
            self.result_message = "Aborted"
        # Full microscope stop
        # Closer to estop
        # Don't clean up
        except MicroscopeStop:
            self._succeeded = False
            self.result_message = "Aborted"
        # Test unstable and force killed
        # Unstable, don't attempt cleanup
        except TestKilled:
            self._succeeded = False
            self.result_message = "killed"
        # Generic test crash
        # Try to cleanup if possible
        except Exception as e:
            self.wrap_cleanup("Exception. Running cleanup")
            self._succeeded = False
            self.result_message = f"Exception: {e}"
            print("")
            print("Script generated unhandled exception")
            traceback.print_exc()
        # file exceptions can cause this
        # XXX: actually I think this was camera disconnect
        except OSError as e:
            self.wrap_cleanup("OSError. Running cleanup")
            self._succeeded = False
            self.result_message = f"Exception (OSError): {e}"
            print("")
            print("Script generated unhandled exception")
            traceback.print_exc()
        finally:
            self._running.clear()
            if top_level:
                self.done.emit()

    def wrap_cleanup(self, msg):
        self.tend = time.time()
        try:
            self._running.set()
            self.log(msg)
            self.log("Completed after %s" %
                     time_str_1dec(self.tend - self.tstart))
            try:
                self.cleanup()
            except Exception as _e:
                self.log("Script generated unhandled exception in cleanup")
                print("Script generated unhandled exception in cleanup")
                traceback.print_exc()
        finally:
            self._running.clear()

    def cleanup(self):
        pass

    """
    Main API
    """

    def run_scan(self, scanj):
        assert 0, "fixme"

    def snap_image(self, filename=None):
        assert 0, "fixme"

    def autofocus(self):
        """
        Autofocus at the current location
        """
        self._ac.image_processing_thread.auto_focus(
            objective_config=self._ac.objective_config(), block=True)

    def pos(self):
        """
        Get current stage position
        Returns a dictionary like:
        {"x": 12.345, "y": 2.356, "z": 4.5}
        """
        self.check_running()
        return self._ac.motion_thread.pos_cache

    def move_absolute(self, pos, block=True):
        """
        Set current position
        Pos can include one or more positions like:
        {"z": 4.5}
        {"x": 12.345, "y": 2.356, "z": 4.5}
        """
        self.check_running()

        # TODO: find a way to get exceptions to bubble up here
        self._ac.motion.check_valid_position(pos)
        self._ac.motion_thread.move_absolute(pos, block=block)
        self.check_running()

    def move_relative(self, pos, block=True):
        self.check_running()
        # TODO: find a way to get exceptions to bubble up here
        self._ac.motion_thread.move_relative(pos, block=block)
        self.check_running()

    def position_format(self, axes):
        """
        Convert a dictionary of axis positions to a string
        Ex: {"x" 1, "y": 2} => "X1 Y2"
        """
        return self._ac.usc.motion.format_positions(axes)

    def position_parse(self, s):
        """
        Convert a axis position string to a dictionary of positions
        Ex: "X1 Y2" => {"x" 1, "y": 2}
        """
        return motion_util.parse_move(s)

    def sleep(self, t):
        """
        Sleep for given number of seconds, watching for abort requests
        """

        delta = 0.1
        tstart = time.time()
        while True:
            dt = time.time() - tstart
            remain = t - dt
            if remain < 0:
                break
            self.check_running()
            time.sleep(min(delta, remain))
        self.check_running()

    def image(self, wait_imaging_ok=True, raw=False):
        """
        Request and return a snapshot as PIL image

        FIXME: this is an unprocessed image
        Should be returning like snapshot
        """
        if wait_imaging_ok:
            self.wait_imaging_ok()
        imager = self.imager()
        if raw:
            images = imager.get()
            assert len(images) == 1
            return images["0"]
        else:
            return imager.get_processed()

    def wait_imaging_ok(self):
        """
        Wait for camera / stage to settle
        After this a picture can be snapped with acceptable quality
        """

        self._ac.microscope.kinematics.wait_imaging_ok()

    def image_save_extension(self):
        """
        Return currently selected filename postfix such as .jpg or .tif
        """
        return self._ac.microscope.image_save_extension()

    def message_box_yes_cancel(self, title, message):
        # quick hack: run as subprocess?
        assert 0, "FIXME: not thread safe"
        ret = QMessageBox.question(None, title, message,
                                   QMessageBox.Yes | QMessageBox.Cancel,
                                   QMessageBox.Cancel)
        return ret == QMessageBox.Yes

    def get_objectives_config(self):
        """
        Returns the entire objective DB structure
        """
        return self._ac.microscope.objectives.get_full_config()

    def get_objective_config(self):
        """
        Sample entry:

        {
            "magnification": 5,
            "model": "5X",
            "na": 0.1,
            # The auto-generated name on the dropdown menu
            "name": "5X",
            "tsettle_motion": 0.0,
            "um_per_pixel": 1.0,
            "vendor": "Mock",
            "x_view": 0.8,
            "y_view": 0.75
        }
        """
        return self.get_objectives_config()[self.get_active_objective()]

    def get_active_objective(self):
        """
        Returns the name of the active objective
        """
        return self._ac.microscope.get_active_objective()

    def set_active_objective(self, objective):
        """
        Check if name is in cache
        """
        self._ac.microscope.set_active_objective(objective)

    def microscope_model(self):
        """
        Config file name
        Will always return something
        """
        return self._ac.microscope.model()

    def microscope_serial(self):
        """
        From GRBL
        May not be present and return None
        """
        return self._ac.microscope.serial()

    """
    Advanced API
    Try to use the higher level functions first if possible
    """

    def run_plugin(self, plugin, input_=None, button_value=None):
        p = plugin.Plugin(ac=self._ac)
        """
        WARNING: this is very rough and doesn't work well

        TODO:
        -Better Input defaults
        -Imports don't get fully cleaned?
        -Cleanup / stop doesn't work correctly
        """
        p.log = self.log
        p.check_running = self.check_running
        p.run(input_=input_, button_value=button_value, top_level=False)

    def run_planner(self, pconfig):
        assert 0, "FIXME"

    def motion(self):
        """
        Get a (thread safe) motion object
        Access to the more powerful but less stable stage API
        """
        return self._ac.microscope.motion_ts()

    def imager(self):
        """
        Get a (thread safe) imager object
        Access to the more powerful but less stable camera API
        """
        # Planner uses this directly / is already thread safe
        return self._ac.microscope.imager_ts()

    def kinematics(self):
        """
        Get a (thread safe) kinematics object
        Access to the more powerful but less stable system synchronization API
        """
        return self._ac.microscope.kinematics_ts()

    def backlash_disable(self, block=True):
        """
        Disable backlash compensation
        """
        self._ac.motion_thread.backlash_disable(block=block)

    def backlash_enable(self, block=True):
        """
        Enable backlash compensation
        """
        self._ac.motion_thread.backlash_enable(block=block)

    def set_um_per_pixel_raw_1x(self, val):
        """
        Set calibration info
        """
        self._ac.mainTab.objective_widget.setUmPerPixelRaw1x.emit(val)

    """
    Plugin defined functions
    """

    def run_test(self):
        """
        The script entry point
        The most important user function
        """
        pass

    def input_config(self):
        """
        Return a dictionary to configure InputWidget
        """
        return {}

    def show_run_button(self):
        """
        Instrument might want to disable if they have alternate buttons
        """
        return True


class ScriptingTab(ArgusTab):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stitcher_thread = None
        self.last_cs_upload = None
        self.filename = None

        fn = os.path.join(self.ac.microscope.usc.bc.get_data_dir(),
                          "script_log.txt")
        existed = os.path.exists(fn)
        self.log_all_fd = open(fn, "w+")
        if existed:
            self.log_all_fd.write("\n\n\n")
            self.log_all_fd.flush()

        self.plugin = None
        self.running = False
        self.active_objective = None
        self.ac.objectiveChanged.connect(self.active_objective_updated)

    def _initUI(self):
        layout = QGridLayout()
        row = 0

        self.script_dirs = {'uscope': './uscope/script'}
        for name, directory in self.ac.microscope.bc.script_dirs().items():
            self.script_dirs[name] = directory

        if len(self.script_dirs) > 1:
            self.select_pb1 = QPushButton("Select script (uscope)")
        else:
            self.select_pb1 = QPushButton("Select script")

        self.select_pbs = {}
        for name in self.script_dirs.keys():
            pb = QPushButton(f"Select script ({name})")
            self.select_pbs[name] = pb

            def connect(name):
                def select_script_clicked():
                    self.browse_for_script(name)

                pb.clicked.connect(select_script_clicked)

            connect(name)
            layout.addWidget(pb, row, 0)
            row += 1

        # self.test_name_cb = QComboBox()

        layout.addWidget(QHLine(), row, 0)
        row += 1

        self.fn_le = QLineEdit("No file selected")
        layout.addWidget(self.fn_le, row, 0)
        row += 1
        self.fn_le.setReadOnly(True)

        self.run_pb = QPushButton("Run")
        self.run_pb.setEnabled(False)
        self.run_pb.clicked.connect(self.run_pb_clicked)
        layout.addWidget(self.run_pb, row, 0)
        row += 1

        layout.addWidget(QHLine(), row, 0)
        row += 1

        # Less commonly used functions below

        self.reload_pb = QPushButton("Reload")
        self.reload_pb.clicked.connect(self.reload_pb_clicked)
        self.reload_pb.setEnabled(False)
        layout.addWidget(self.reload_pb, row, 0)
        row += 1

        # Could we combine these into one button?
        # Require the user to attempt a graceful stop first

        self.stop_pb = QPushButton("Stop gracefully")
        self.stop_pb.setEnabled(False)
        self.stop_pb.clicked.connect(self.stop_pb_clicked)
        layout.addWidget(self.stop_pb, row, 0)
        row += 1

        self.kill_pb = QPushButton("Kill")
        self.kill_pb.setEnabled(False)
        self.kill_pb.clicked.connect(self.kill_pb_clicked)
        layout.addWidget(self.kill_pb, row, 0)
        row += 1

        def load_save_layout():
            layout = QHBoxLayout()

            self.load_config_pb = QPushButton("Load config")
            self.load_config_pb.setEnabled(False)
            self.load_config_pb.clicked.connect(self.load_config_pb_clicked)
            layout.addWidget(self.load_config_pb)

            self.save_config_pb = QPushButton("Save config")
            self.save_config_pb.setEnabled(False)
            self.save_config_pb.clicked.connect(self.save_config_pb_clicked)
            layout.addWidget(self.save_config_pb)

            return layout

        layout.addLayout(load_save_layout(), row, 0)
        row += 1

        self.input = InputWidget(clicked=self.inputWidgetClicked)
        layout.addWidget(self.input, row, 0)
        row += 1

        self.status_le = QLineEdit("Status: idle")
        layout.addWidget(self.status_le, row, 0)
        row += 1
        self.status_le.setReadOnly(True)

        # TODO: save button
        # Should always log to filesystem?
        self.log_widget = QTextEdit()
        self.log_widget.setReadOnly(True)
        layout.addWidget(self.log_widget, row, 0)
        row += 1

        self.setLayout(layout)

        # Most users don't need this
        # TODO: make this a menu item
        self.enable_advanced_scripting(self.ac.microscope.usc.bc.dev_mode())

    def enable_advanced_scripting(self, enabled):
        self.reload_pb.setVisible(enabled)
        self.kill_pb.setVisible(enabled)

    def browse_for_script(self, name):
        directory = self.script_dirs[name]
        filename = QFileDialog.getOpenFileName(None, "Select script",
                                               directory, "Script (*.py)")
        if not filename:
            return
        filename = str(filename[0])
        self.select_script(filename)

    def select_script(self, filename):
        if not filename:
            self.log_local("No file selected")
            return
        if not os.path.exists(filename):
            self.log_local("File does not exist")
            return

        self.unload_script()
        try:
            self.fn_le.setText(filename)

            spec = importlib.util.spec_from_file_location(
                "pyuscope_plugin", filename)
            plugin_module = importlib.util.module_from_spec(spec)
            sys.modules["pyuscope_plugin"] = plugin_module
            spec.loader.exec_module(plugin_module)
            # Entry point: construct the ArgusScriptingPlugin class named Plugin
            self.plugin = plugin_module.Plugin(ac=self.ac)

            self.input.configure(self.plugin.input_config())

            self.plugin.log_msg.connect(self.log_local)
            self.plugin.done.connect(self.plugin_done)

            self.status_le.setText("Status: idle")
            self.run_pb.setEnabled(True)
            self.save_config_pb.setEnabled(True)
            self.load_config_pb.setEnabled(True)
            self.run_pb.setVisible(self.plugin.show_run_button())

            # self.test_name_cb.clear()
            # for now just support one function
            # self.test_name_cb.addItem("run")
            # self.pconfig_sources[self.pconfig_source_cb.currentIndex()]
            self.filename = filename
            self.log_local(f"Script selected: {filename}")
        except Exception as e:
            self.unload_script()
            self.log_local(f"Plugin failed to load: {e}")
            print("")
            print("Script generated unhandled exception")
            traceback.print_exc()
            return

    def unload_script(self):
        self.plugin = None
        self.fn_le.setText("")
        self.status_le.setText("Status: idle")
        self.input.configure({})
        self.log_widget.clear()
        self.run_pb.setEnabled(False)

    def set_filename(self, filename):
        self.fn_le.setText(filename)
        if filename:
            self.select_script(filename)

    def reload_pb_clicked(self):
        self.select_script(self.filename)

    def run_pb_clicked(self, _checked=None, input_val=None):
        if self.running:
            self.log_local("Can't run while already running")
            return
        # This can happen if plugin fails to load
        if self.plugin is None:
            self.log_local("Can't run without plugin")
            return

        if input_val is None:
            input_val = self.input.getValues()
        self.plugin._input = input_val
        for pb in self.select_pbs.values():
            pb.setEnabled(False)
        self.reload_pb.setEnabled(False)
        self.stop_pb.setEnabled(True)
        self.kill_pb.setEnabled(True)
        self.save_config_pb.setEnabled(False)
        self.save_config_pb.setEnabled(False)
        self.log_local("Plugin loading")
        self.plugin.reset()
        self.plugin.start()
        # pool = QThreadPool.globalInstance()
        # pool.start(self.plugin)
        self.status_le.setText("Status: running")
        self.running = True

    # An alternate way to launch using custom buttons
    def inputWidgetClicked(self, j):
        input_val = self.input.getValues()
        input_val["button"] = j
        self.run_pb_clicked(input_val=input_val)

    def stop_pb_clicked(self):
        if not self.running:
            self.log_local("Plugin isn't running")
            return
        self.plugin.shutdown()

    def kill_pb_clicked(self):
        thread_id = self.plugin.ident
        if not self.running or not thread_id:
            self.log_local("Plugin isn't running")
            return

        self.log_local(f"Killing thread {thread_id}")
        res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
            ctypes.c_long(thread_id), ctypes.py_object(TestKilled))
        if res > 1:
            ctypes.pythonapi.PyThreadState_SetAsyncExc(
                ctypes.c_long(thread_id), 0)
            self.log_local("Exception raise failure")

    def default_config_file_name(self):
        # /home/mcmaster/script/my_script.py => my_script.json
        return os.path.basename(str(
            self.fn_le.text())).split(".")[0] + ".script.json"

    def load_config_pb_clicked(self):
        directory = self.ac.bc.script_data_dir()
        directory = os.path.join(directory, self.default_config_file_name())
        filename = QFileDialog.getOpenFileName(None,
                                               "Select input script config",
                                               directory,
                                               "Script config (*.json *.j5)")
        if not filename:
            return
        filename = str(filename[0])
        if not filename:
            return
        try:
            j = readj(filename)
            self.input.setValues(j)
        except Exception as e:
            self.log_local(f"Failed to load script config: {type(e)}: {e}")
            traceback.print_exc()

    def set_config(self, j):
        try:
            self.input.setValues(j)
        except Exception as e:
            self.log_local(f"Failed to load script config: {type(e)}: {e}")
            traceback.print_exc()

    def save_config_pb_clicked(self):
        directory = self.ac.bc.script_data_dir()
        directory = os.path.join(directory, self.default_config_file_name())
        filename = QFileDialog.getSaveFileName(None,
                                               "Select output script config",
                                               directory,
                                               "Script config (*.json *.j5)")
        if not filename:
            return
        filename = str(filename[0])

        j = self.input.getValues()
        writej(filename, j)

    def plugin_done(self):
        if self.plugin.succeeded():
            status = "Status: finished ok"
            try:
                self.input.setValues(self.plugin.new_defaults)
            except KeyError as e:
                self.log_local(f"Failed to update defaults: bad label: {e}")
            except Exception as e:
                self.log_local(f"Failed to update defaults: {type(e)}: {e}")
        else:
            status = "Status: failed :("
        self.status_le.setText(status)
        self.stop_pb.setEnabled(False)
        self.kill_pb.setEnabled(False)
        self.reload_pb.setEnabled(True)
        self.save_config_pb.setEnabled(True)
        self.save_config_pb.setEnabled(True)
        if self.plugin.succeeded():
            self.log_local("Plugin completed ok")
        else:
            self.log_local("Plugin completed w/ issue")
            self.log_local(self.plugin.result_message)
        for pb in self.select_pbs.values():
            pb.setEnabled(True)
        self.running = False

    def _post_ui_init(self):
        pass

    def _shutdown_request(self):
        if self.plugin:
            self.plugin.shutdown()

    def log_local(self, s='', newline=True):
        s = str(s)
        # print("LOG: %s" % s)
        if newline:
            s += '\n'

        c = self.log_widget.textCursor()
        c.clearSelection()
        c.movePosition(QTextCursor.End)
        c.insertText(s)
        self.log_widget.setTextCursor(c)

        self.log_all_fd.write(s)
        self.log_all_fd.flush()
        """
        if self.log_plugin_fd is not None:
            self.log_plugin_fd.write(s)
            self.log_plugin_fd.flush()
        """

    def active_objective_updated(self, data):
        """
        Cache the active objective
        """
        self.active_objective = data

    def _cache_save(self, cachej):
        j = {}
        j["filename"] = str(self.fn_le.text())
        j["config"] = self.input.getValues()
        cachej["scripting"] = j

    def _cache_load(self, cachej):
        j = cachej.get("scripting", {})
        self.set_filename(j.get("filename", ""))
        config = j.get("config", None)
        if config is not None:
            self.set_config(config)
