import json5
import os
from collections import OrderedDict
from uscope.util import writej, readj
from pathlib import Path
from uscope import jsond
import shutil
import subprocess
'''
There is a config directory with two primary config files:
-microscope.j5: inherent config that doesn't really change
-imager_calibration.j5: for different modes (ex: w/ and w/o polarizer)



A few general assumptions:
-Camera is changed rarely.  Therefore only one camera per config file
-Objectives are changed reasonably often
    They cannot changed during a scan
    They can be changed in the GUI
'''
"""
defaults = {
    "out_dir": "out",
    "imager": {
        "hal": 'mock',
        "snapshot_dir": "snapshot",
        "width": 3264,
        "height": 2448,
        "scalar": 0.5,
    },
    "motion": {
        # Good for testing and makes usable to systems without CNC
        "hal": "mock",
        "startup_run": False,
        "startup_run_exit": False,
        "overwrite": False,
        "backlash": 0.0,
    }
}
"""

# microscope.j5
usj = None
usc = None
"""
Calibration broken out into separate file to allow for easier/safer frequent updates
Ideally we'd also match on S/N or something like that
"""


def find_panotools_exe(config, configk, exe_name, flatpak_name):
    exe = config.get(configk)
    if exe is not None:
        return tuple(exe)

    if 1:
        exe = shutil.which(exe_name)
        if exe is not None:
            return (exe, )

    # flatpak run --command=enfuse net.sourceforge.Hugin --help
    # bwrap: execvp align_image_stackD: No such file or directory
    # bad command => returns 1
    # good command => returns 0
    command = [
        "flatpak", "run", f"--command={flatpak_name}", "net.sourceforge.Hugin",
        "--help"
    ]
    try:
        process = subprocess.Popen(command,
                                   stderr=subprocess.PIPE,
                                   stdout=subprocess.PIPE)
        _stdout, _stderr = process.communicate()
        exit_code = process.wait()
        if exit_code == 0:
            return ("flatpak", "run", "--filesystem=host",
                    f"--command={flatpak_name}", "net.sourceforge.Hugin")
    # FIME: catch the specific exception for command not found
    except:
        pass
    return None


class SystemNotFound(Exception):
    pass


class USCImager:
    """
    Rough pipeline for typical Touptek camera:
    -Set esize + w/h to configure sensor size
    -Optionally set crop to reduce the incoming image size
    -scalar to reduce output width/height
    """

    valid_keys = {"source", "width", "height", "crop"}

    def __init__(self, j=None, microscope=None):
        """
        j: usj["imager"]
        """
        self.j = j
        self.microscope = microscope
        #if not "width" in j or not "height" in j:
        #    raise ValueError("width/height required")
        self.cache_constants()

    def cache_constants(self):
        # More of a hard real time constant
        # Something is wrong if this is exceeded
        self._snapshot_timeout = self.microscope.bc.timeout_scalar_scale(3.0)
        # If processing gets expensive or backs up this could get high
        # Maybe this should be set to a high value
        # However planner etc relies on this running relatively quickly
        self._processing_timeout = self.microscope.bc.timeout_scalar_scale(5.0)

    def snapshot_timeout(self):
        return self._snapshot_timeout

    def processing_timeout(self):
        return self._processing_timeout

    def source(self):
        return self.j.get("source", "auto")

    def native_wh(self):
        """
        The largest possible sensor resolution
        Following are not applied yet: crop, scaling
        """
        valw = self.j.get("native_width", self.j.get("width"))
        valh = self.j.get("native_height", self.j.get("height"))
        if valw is None or valh is None:
            raise Exception(
                "can't compute um_per_pixel_raw_1x: not specified and missing native_width/height"
            )
        return int(valw), int(valh)

    def raw_wh(self):
        """
        The selected sensor size before any processing
        Following are not applied yet: crop, scaling
        """
        w = int(self.j['width'])
        h = int(self.j['height'])
        return w, h

    def cropped_wh(self):
        """
        The intended w/h after expected crop (if any) is applied
        Usually we use the full sensor but sometimes its cropped
        Scaling, if any, is not yet applied
        (ex: if too large a sensor is used)
        """
        w = int(self.j['width'])
        h = int(self.j['height'])

        crop = self.crop_tblr()
        if crop:
            w = w - crop["left"] - crop["right"]
            h = h - crop["top"] - crop["bottom"]

        if w <= 0 or h <= 0:
            raise ValueError("Bad cropped w/h")
        return w, h

    def crop_tblr(self):
        """
        Crop properties
        Intended for gstreamer "videocrop"
        top/bottom/left/right

        Returns either None or a dict with 4 keys
        """
        assert not "crop" in self.j, "Obsolete crop in config. Please update to crop_fractions"
        # Explicit config by pixels
        ret = {
            "top": 0,
            "bottom": 0,
            "left": 0,
            "right": 0,
        }
        tmp = self.j.get("crop_pixels", {})
        if tmp:
            for k in list(tmp.keys()):
                if k not in ("top", "bottom", "left", "right"):
                    raise ValueError("Unexpected key" % (k, ))
                ret[k] = int(tmp.get(k, 0))
            return ret
        # Convert config based on fraction of sensor size
        tmp = self.j.get("crop_fractions", {})
        if tmp:
            w, h = self.raw_wh()
            for k in list(ret.keys()):
                if k in ("top", "bottom"):
                    ret[k] = int(tmp.get(k, 0.0) * h)
                elif k in ("left", "right"):
                    ret[k] = int(tmp.get(k, 0.0) * w)
                else:
                    raise ValueError("Unexpected key" % (k, ))
            return ret
        return None

    def final_wh(self):
        """
        Final expected width and height in pixels
        Should be the same for snapshots and scans
        """
        crop = self.crop_tblr() or {}
        width, height = self.raw_wh()
        width -= crop.get("left", 0) + crop.get("right", 0)
        height -= crop.get("top", 0) + crop.get("bottom", 0)
        width *= self.scalar()
        height *= self.scalar()
        width = int(width)
        height = int(height)
        return width, height

    def source_properties(self):
        """
        A dict of configuration specific parameters to apply to the imager
        Usually these are gstreamer properties
        ex: use this to set hflip
        """
        return self.j.get("source_properties", {})

    def source_properties_mod(self):
        """
        A way to change ranges based on application specific environments
        ex: can limit exposure range to something you like better

        "source_properties_mod": {
            //In us. Can go up to 15 sec which is impractical for typical usage
            "expotime": {
                "max": 200000
            },
        },
        """
        return self.j.get("source_properties_mod", {})

    def scalar(self):
        """
        Scale image by given factor
        Ex: a 640x480 image (after crop) with scalar 0.5 will be output 320x240
        A return value of None is equivalent to 1.0
        """
        return float(self.j.get("scalar", 1.0))

    def save_extension(self):
        """
        Used by PIL to automagically save files

        Used by:
        -Planner output
        -Argus snapshot
        """
        return self.j.get("save_extension", ".jpg")

    def save_quality(self):
        """
        When .jpg output, determines the saved compression level

        Used by:
        -Planner output
        -Argus snapshot
        """
        return self.j.get("save_quality", 95)

    def ff_cal_fn(self):
        return os.path.join(self.microscope.usc.get_microscope_data_dir(),
                            "imager_calibration_ff.tif")

    def has_ff_cal(self):
        return os.path.exists(self.ff_cal_fn())

    def videoflip_method(self):
        return self.j.get("videoflip_method", None)

    def cal_fn_data(self):
        return os.path.join(self.microscope.usc.get_microscope_data_dir(),
                            "imager_calibration.j5")

    def cal_fn_microscope(self):
        return os.path.join(self.microscope.usc.get_config_dir(),
                            "imager_calibration.j5")

    def cal_load(self, load_data_dir=True):
        def load_config(fn):
            try:
                if not fn:
                    return {}
                if not os.path.exists(fn):
                    return {}
                configj = readj(fn)
                configs = configj["configs"]
                config = configs["default"]
                #if source and config["source"] != source:
                #    raise ValueError("Source mismatches in config file")
                if "disp_properties" not in config:
                    raise ValueError("Old config format")
                return config["disp_properties"]
            except Exception as e:
                print("WARNING: Failed to load cal: %s" % (e, ))
                return {}

        # configs/ls-hvy-1/imager_calibration.j5
        microscopej = load_config(self.cal_fn_microscope())
        if not load_data_dir:
            return microscopej
        # Take defaults from dataj, the user directory
        # data/microscopes/ls-hvy-1/imager_calibration.j5
        dataj = load_config(self.cal_fn_data())
        for k, v in dataj.items():
            microscopej[k] = v
        return microscopej

    def cal_save_to_data(self, source, disp_properties, mkdir=False):
        if mkdir and not os.path.exists(self.microscope.bc.get_data_dir()):
            os.mkdir(self.microscope.bc.get_data_dir())
        jout = {
            "configs": {
                "default": {
                    "source": source,
                    "disp_properties": disp_properties
                }
            }
        }
        writej(self.cal_fn_data(), jout)

    def native_pixel_pitch_um(self):
        """
        "pixel size" at max camera resolution
        The number you find in the datasheet
        Assumes square pixels
        Only used for checking calibration
        """
        return self.j.get("native_pixel_pitch_um")

    def hardware_resolution_scalar(self):
        """
        Scalar going from native pixel resolution to selected resolution
        Ex: native 1000 wide, but selected 500 wide
        Returns 0.5
        """
        native_w_pix, _native_h_pix = self.native_wh()
        this_w_pix, _this_h_pix = self.raw_wh()
        return this_w_pix / native_w_pix


class USCMotion:
    def __init__(self, j=None, microscope=None):
        """
        j: usj["motion"]
        """
        self.j = j
        self.microscope = microscope
        # See set_axes() for more fine grained control
        # Usually this is a reasonable approximation
        # Iterate (list, dict, etc) to reserve for future metadata if needed
        self.axes_meta = OrderedDict([("x", {}), ("y", {}), ("z", {})])
        # hmm pconfig tries to overlay on USCMotion
        # not a good idea?
        # assert "hal" in self.j

    def format_position(self, axis, position, digit_spaces=True):
        """
        This was intended to be a simple way to display high precision numbers
        but is a bit of a mess

        Goals:
        -Allow displaying high precision measurements in an easy to to read way
        -Display rounded values to avoid precision floor() issues making inaccurate display
        """
        if self.j.get("z_format6") and axis == "z" or self.j.get(
                "xyz_format6"):
            if position >= 0:
                sign = "+"
            else:
                sign = "-"
            if digit_spaces:
                digit_space = " "
            else:
                digit_space = ""
            position = abs(position)
            whole = int(position)
            position3f = (position - whole) * 1000
            position3 = int(position3f)
            position6 = int(round((position3f - position3) * 1000))
            # Fixes when rounds up
            if position6 >= 1000:
                position6 -= 1000
                position3 += 1
                if position3 >= 1000:
                    position3 -= 1000
                    whole += 1
            return "%c%u.%03u%s%03u" % (sign, whole, position3, digit_space,
                                        position6)
        else:
            return "%0.3f" % position

    def format_positions(self, position):
        ret = ""
        for axis, this_pos in sorted(position.items()):
            if ret:
                ret += " "
            ret += "%c%s" % (axis.upper(),
                             self.format_position(
                                 axis, this_pos, digit_spaces=False))
        return ret

    def validate_axes_dict(self, axes):
        # FIXME
        pass

    def set_axes_meta(self, axes_meta):
        self.axes_meta = axes_meta

    def hal(self):
        """
        Which movement engine to use
        Sample values:
        grbl: use GRBL controller
        mock: use an emulatd controller

        Note: there is also a family of LinuxCNC (machinekit) HALs
        However they are not currently maintained / supported
        """
        ret = self.j["hal"]
        if ret not in ("mock", "grbl-ser", "lcnc-rpc", "lcnc-arpc", "lcnc-py"):
            raise ValueError("Invalid hal: %s" % (ret, ))
        return ret

    def raw_scalars(self):
        """
        WARNING: this is without system specific tweaks applied

        Scale each user command by given amount when driven to physical system
        Return a dictionary, one key per axis, of the possible axes
        Or None if should not be scaled

        Ex GRBL controller with:
        "scalars": {
            "x": 4.0,
            "y": 4.0,
            "z": 20.0,
        GUI move x relative 2 => move GRBL x by 8
        GUI move z relative 3 => move GRBL x by 60
        """
        ret = self.j.get("scalars", {})
        self.validate_axes_dict(ret)
        return ret

    def backlash(self):
        """
        Return a dictionary, one key per known axis, of the possible axes
        Backlash ("slop") defines the amount of motion needed in one axis to engage motion
        if previously going the other direction
        """
        default_backlash = 0.0
        backlash = self.j.get("backlash", {})
        ret = {}
        if backlash is None:
            pass
        elif type(backlash) in (float, int):
            default_backlash = float(backlash)
        elif type(backlash) in (dict, OrderedDict):
            for axis, v in backlash.items():
                ret[axis] = float(v)
        else:
            raise Exception("Invalid backlash: %s" % (backlash, ))

        # If axes are known validate and add defaults
        if self.axes_meta:
            # If axes were registered, set defaults
            for k in self.axes_meta:
                if not k in ret:
                    ret[k] = default_backlash

        self.validate_axes_dict(ret)
        return ret

    def backlash_compensation(self):
        """
        +1: move negative along axis then positive to final position
        0 => none
        -1: move positive along axis then negative to final position
        """

        backlashes = self.backlash()

        default_comp = None
        backlash = self.j.get("backlash_compensation", {})
        ret = {}
        if backlash is None:
            pass
        elif type(backlash) is int:
            default_comp = backlash
            assert default_comp in (-1, +1), default_comp
        elif type(backlash) in (dict, OrderedDict):
            for axis, v in backlash.items():
                ret[axis] = int(v)
        else:
            raise Exception("Invalid backlash compensation: %s" % (backlash, ))

        # If axes are known validate and add defaults
        if self.axes_meta:
            # If axes were registered, set defaults
            for k in self.axes_meta:
                if not k in ret:
                    # If there is backlash, assign default compensation
                    if backlashes.get(k):
                        ret[k] = default_comp if default_comp is not None else -1
                    else:
                        ret[k] = 0
                assert ret[k] in (-1, 0, +1), ret

        return ret

    def origin(self):
        """
        Where the coordinate system starts from
        Primarily used by planner and related

        CNC industry standard coordinate system is lower left
        However, image typical coordinate system is upper left
        There are also other advantages for some fixturing to have upper left
        As such support a few choices here
        """
        ret = self.j.get("origin", "ll")
        if ret not in ("ll", "ul"):
            raise ValueError("Invalid coordinate origin: %s" % (ret, ))
        return ret

    def soft_limits(self):
        """
        Do not allow travel beyond given values
        Return a dictionary, one key per axis, of the possible axes

        Useful if your system doesn't easily support soft or hard limits
        """
        raw = self.j.get("soft_limits", None)
        if raw is None:
            return None

        ret = {}
        for axis in self.axes_meta:
            axmin = raw.get(axis + "min")
            axmax = raw.get(axis + "max")
            if axmin is not None or axmax is not None:
                axmin = axmin if axmin else 0.0
                axmax = axmax if axmax else 0.0
                ret[axis] = (axmin, axmax)
        self.validate_axes_dict(ret)
        return ret

    def use_wcs_offsets(self):
        return bool(self.j.get("use_wcs_offsets", 0))

    def limit_switches(self):
        """
        Used to be extra careful to avoid homing systems without limit switches
        """

        v = self.j.get("limit_switches")
        if v is None:
            return None
        else:
            return bool(v)

    def axes(self):
        """
        Some systems don't use all axes
        Ex: system might just be XY with Z N/C
        """
        return set(self.j.get("axes", "xyz"))

    def damper(self):
        """
        Turn down acceleration and velocity
        Hack to slow down a system that's too aggressive
        Typically used when samples aren't fixtured down well enough
        Otherwise you can jog slower but scan G0 will be too aggressive

        WARNING: assumes you are configuring velocity / acceleration at startup
        Otherwise will "compound" each time you start up
        """
        ret = self.j.get("damper", None)
        if ret is not None:
            assert 0 < ret <= 1.0
        return ret


class USCPlanner:
    def __init__(self, j={}, microscope=None):
        """
        j: usj["planner"]
        """
        self.j = j
        self.microscope = microscope

    def overlap(self):
        """
        ideal faction of image that overlaps to each adjacent image
        Default: 0.3 => overlap adjacent image by 30% on each side (40% unique)
        """
        return float(self.j.get("overlap", 0.3))

    def border(self):
        """
        Automatically add this many mm to the edges of a panorama
        """
        return float(self.j.get("border", 0.0))


class USCKinematics:
    def __init__(self, j={}, microscope=None):
        """
        j: usj["kinematics"]
        """
        self.j = j
        self.microscope = microscope

    """
    Full motion delay: NA / tsettle_motion_na1() + tsettle_motion()

    Ex:
    tsettle_motion = 0.1
    tsettle_motion_na1 = 0.8
    NA = 0.50
    min delay: 0.50 / 0.8 + 0.1 = 0.725 sec
    """

    def tsettle_autofocus(self):
        return float(self.j.get("tsettle_autofocus", 0.1))

    def tsettle_motion_base(self):
        """
        How much *minimum* time to wait after moving to take an image
        This is a base constant added to the NA scalar below
        A factor of vibration + time to clear a frame
        """
        # Set a semi-reasonable default
        return float(self.j.get("tsettle_motion_base", 0.25))

    def tsettle_motion_na1(self):
        """
        How much delay added to tsettle_motion for a 1.0 numerical aperture objective
        Ex: a 0.50 NA objective will wait half as long
        """
        # Set a semi-reasonable default
        return float(self.j.get("tsettle_motion_na1", 0.5))

    def tsettle_motion_max(self):
        NA_MAX = 1.4
        return self.tsettle_motion_base() + self.tsettle_motion_na1() * NA_MAX

    def tsettle_hdr(self):
        """
        How much time to wait after moving to take an image
        A factor of vibration + time to clear a frame
        """
        # Set a semi-reasonable default
        return float(self.j.get("tsettle_hdr", 0.2))

    def frame_sync(self):
        # Recommended for real imagers
        return bool(self.j.get("frame_sync", True))


class USCOptics:
    def __init__(self, j=None, microscope=None):
        self.j = j
        self.microscope = microscope

    def image_wh_raw_1x_mm(self):
        """
        1x "objective", not 1x magnification on sensor
        Relay, barlow, etc lens may significantly alter this from actual sensor size
        No cropping applied
        """
        return self.j.get("image_width_1x_mm"), self.j.get(
            "image_height_1x_mm")

    def um_per_pixel_raw_1x(self):
        """
        1x "objective", not 1x magnification on sensor at selected resolution
        raw => non-scaled image at selected resolution
        Relay, barlow, etc lens may significantly alter this from actual pixel size
        """
        # Directly specified?
        ret = self.j.get("um_per_pixel_raw_1x", None)
        if ret is not None:
            return ret
        # Fallback to calculating based on resolution
        this_w_pix, _this_h_pix = self.microscope.usc.imager.raw_wh()
        w_mm, _h_mm = self.image_wh_raw_1x_mm()
        w_um = w_mm * 1000
        return w_um / this_w_pix

    def diffusion(self):
        """
        "diffusion": {
            "red": 2.0,
            "green": 2.0,
            "blue": 4.0,
        }
        """
        return self.j.get("optics", None)


class USCImageProcessingPipeline:
    def __init__(self, j={}, microscope=None):
        self.j = j
        self.microscope = microscope

    def pipeline_first(self):
        return self.j.get("pipeline_first", [])

    def snapshot_correction(self):
        """
        Get image processing pipeline configuration
        "ipp": [
            {"plugin": "correct-sharp1"},
            {"plugin": "correct-vm1v1", "config": {"kernel_width": 3}},
        ],
        """
        return self.j.get("snapshot_correction", [])

    # plugin specific options
    def get_plugin(self, name):
        return self.j.get("plugins", {}).get(name, {})


class ObjectiveDB:
    def __init__(self, fn=None, strict=None):
        if fn is None:
            fn = os.path.join(get_configs_dir(), "objectives.j5")
        with open(fn) as f:
            self.j = json5.load(f, object_pairs_hook=OrderedDict)
        # Index by (vendor, model)
        self.db = OrderedDict()
        for entry in self.j["objectives"]:
            assert entry["vendor"]
            assert entry["model"]
            # na, magnification is highly encouraged but not required?
            k = (entry["vendor"].upper(), entry["model"].upper())
            self.db[k] = entry
        # hack in case needed to bypass short term
        if strict is None:
            strict = os.getenv("PYUSCOPE_STRICT_OBJECTIVEDB", "Y") == "Y"
        self.strict = strict

    def get(self, vendor, model):
        return self.db[(vendor.upper(), model.upper())]

    def set_defaults_list(self, objectivejs):
        for objectivej in objectivejs:
            self.set_default(objectivej)

    def set_default(self, objectivej):
        """
        if vendor/model is found in db fill in default values from db
        """

        # experimental shorthand
        # vendor, model is required to match
        # other fields can be specified to make readable
        # however they are optional and just checked for consistency
        if "db_find" not in objectivej:
            return
        """
        "db_find": "vendor: Mitutoyo, model: 46-145, magnification: 20, na: 0.28",
        """
        fields = {}
        for entry in objectivej["db_find"].split(","):
            try:
                parts = entry.split(":")
                if len(parts) != 2:
                    raise Exception(
                        f"Fields must have key:value pairs: {entry}")
                k, v = parts
                k = k.strip()
                v = v.strip()
                if k == "magnification":
                    v = int(v)
                if k == "na":
                    v = float(v)
                fields[k] = v
            except:
                print(f"Failed to parse field: {entry}")
                raise
        # Required
        vendor = fields["vendor"]
        model = fields["model"]
        db_entry = self.db.get((vendor.upper(), model.upper()))
        if not db_entry:
            raise ValueError(f"Objective {vendor} {model} not found in db")
        # Validate consistency on optional keys
        for k, v in fields.items():
            db_has = db_entry[k]
            if db_has != v:
                raise ValueError(
                    f"db_find {vendor} {model}: config has {v} but db has {db_has}"
                )
        for k, v in db_entry.items():
            # Anything user has already set
            if k not in objectivej:
                objectivej[k] = v


"""
Microscope usj config parser
"""


class USC:
    default_microscope_name = None

    @staticmethod
    def has_default_microscope_name():
        return bool(USC.default_microscope.name)

    def __init__(self, usj=None, microscope=None, config_dir=None):
        # Crude microscope object defining name + serial number
        # No other fields are expected to be initialized
        assert microscope is not None, "Microscope is required"
        # Used for data dir configuration (ex: serial number)
        self.microscope = microscope
        self.bc = get_bc()
        if usj is None:
            usj = self.get_usj(config_dir=config_dir)
        self.usj = usj
        self.init_dirs()

        self.imager = USCImager(self.usj.get("imager"),
                                microscope=self.microscope)
        self.motion = USCMotion(self.usj.get("motion"),
                                microscope=self.microscope)
        self.planner = USCPlanner(self.usj.get("planner", {}),
                                  microscope=self.microscope)
        self.kinematics = USCKinematics(self.usj.get("kinematics", {}),
                                        microscope=self.microscope)
        self.optics = USCOptics(self.usj.get("optics", {}),
                                microscope=self.microscope)
        self.ipp = USCImageProcessingPipeline(self.usj.get("ipp", {}),
                                              microscope=self.microscope)
        self.apps = {}

    def get_usj(self, config_dir=None):
        if config_dir is None:
            config_dir = os.path.join(get_configs_dir(), self.microscope.name)
        self._config_dir = config_dir

        # XXX: it would be possible to do an out of tree microscope config now using dconfig
        # might be useful for testing?

        # Check if user has patches
        dconfig = None
        bc_system = self.bc.get_system(self.microscope)
        if bc_system:
            dconfig = bc_system.get("dconfig", None)

        fn = os.path.join(config_dir, "microscope.j5")
        if not os.path.exists(fn):
            fn = os.path.join(config_dir, "microscope.json")
        if not os.path.exists(fn):
            if dconfig is not None:
                print(
                    f"WARNING: failed to find microscope {self.microscope.name} but found dconfig. Out of tree configuration?"
                )
                usj = {}
            else:
                raise Exception("couldn't find microscope.j5 in %s" %
                                config_dir)
        else:
            with open(fn) as f:
                usj = json5.load(f, object_pairs_hook=OrderedDict)

        if dconfig is not None:
            jsond.apply_update(usj, dconfig)

        return usj

    def get_microscope_data_dir(self, mkdir=True):
        return self._microscope_data_dir

    """
    def has_microscope_name(self):
        return bool(self.microscope) and bool(self.microscope.name) 

    def set_microscope_name(self, name):
        self.microscope.name = name
    """

    def get_config_dir(self):
        return self._config_dir

    def get_microscope_dataname(self):
        return self._microscope_dataname

    def init_dirs(self, mkdir=True):
        def get_microscope_dataname():
            # Add serial if possible
            # Otherwise calibration files for one unit may conflict with another
            serial = self.microscope.serial()
            if serial is not None:
                return f"{self.microscope.name}_sn-{serial}"
            else:
                return f"{self.microscope.name}"

        self._microscope_dataname = get_microscope_dataname()
        self._microscope_data_dir = os.path.join(self.bc.get_microscopes_dir(),
                                                 self._microscope_dataname)
        if not os.path.exists(self._microscope_data_dir):
            os.mkdir(self._microscope_data_dir)

    def app_register(self, name, cls):
        """
        Register app name with class cls
        """
        j = self.usj.get("apps", {}).get(name, {})
        self.apps[name] = cls(j=j, microscope=self.microscope)

    def app(self, name):
        return self.apps[name]

    def find_system(self, microscope=None):
        """
        Look for system specific configuration by matching camera S/N
        In the future we might use other info
        Expect file to have a default entry with null key or might consie
        """
        if microscope:
            self.microscope = microscope
        if microscope and microscope.imager:
            camera_sn = microscope.imager.get_sn()
        else:
            camera_sn = None
        # Provide at least a very basic baseline
        default_system = {
            "objectives_db": [
                "vendor: Mock, model: 5X",
                "vendor: Mock, model: 10X",
                "vendor: Mock, model: 20X",
            ],
        }
        for system in self.usj.get("systems", []):
            if system["camera_sn"] == camera_sn:
                return system
            if not system["camera_sn"]:
                default_system = system
        return default_system
        """
        raise SystemNotFound(
            f"failed to either match system or find default for camera S/N {camera_sn}"
        )
        """

    def get_uncalibrated_objectives(self, microscope=None):
        """
        Get baseline objective configuration without system specific scaling applied
        """
        system = self.find_system(microscope)
        # Shorthand notation?

        ret = []
        # XXX: order between these?
        # Generally if you have both the DB are the normal and objectives is the custom
        # So put the custom at the end
        if "objectives_db" in system:
            for entry in system["objectives_db"]:
                ret.append({"db_find": entry})
        if "objectives" in system:
            for objective in system["objectives"]:
                ret.append(objective)
        if len(ret) == 0:
            raise ValueError(
                "Found system but missing objective configuration")
        return ret

    def get_motion_scalars(self, microscope):
        """
        Get scalars after applying system level tweaks
        Ex: model trim w/ a higher ratio gearbox
        """
        scalars = dict(self.motion.raw_scalars())
        system = self.find_system(microscope)
        scalars_scalar = system.get("motion", {}).get("scalars_scalar", {})
        for k, v in scalars_scalar.items():
            scalars[k] = scalars[k] * v
        return scalars


def validate_usj(usj):
    """
    Load all config parameters and ensure they appear to be valid

    strict
        True:
            Error on any keys not matching a valid directive
            If possible, error on duplicate keys
        False
            Allow keys like "!motion" to effectively comment a section out
    XXX: is there a generic JSON schema system?
    """
    # Good approximation for now
    axes = "xyz"
    usc = USC(usj=usj)

    # Imager
    usc.imager.source()
    usc.imager.raw_wh()
    usc.imager.cropped_wh()
    usc.imager.crop_tblr()
    usc.imager.source_properties()
    usc.imager.source_properties_mod()
    usc.imager.scalar()
    usc.imager.save_extension()
    usc.imager.save_quality()

    # Motion
    usc.motion.set_axes_meta(axes)
    # In case a plugin is registered validate here?
    usc.motion.hal()
    usc.motion.scalars()
    usc.motion.backlash()
    usc.motion.backlash_compensation()
    usc.motion.origin()
    usc.motion.soft_limits()

    # Planner
    usc.planner.step()
    usc.planner.border()
    usc.planner.tsettle_motion()
    usc.planner.tsettle_hdr()


def set_usj(j):
    global usj
    usj = j


def get_configs_dir():
    # Assume for now its next to package
    return os.path.realpath(
        os.path.dirname(os.path.realpath(__file__)) + "/../configs")


def get_usc(usj=None, config_dir=None, microscope=None):
    global usc

    if usc is None:
        usc = USC(usj=usj, config_dir=config_dir, microscope=microscope)
    return usc


"""
Planner configuration
"""


class PCImager:
    def __init__(self, j=None):
        self.j = j

        # self.save_extension = USCImager.save_extension
        # self.save_quality = USCImager.save_quality

    def save_extension(self, *args, **kwargs):
        return USCImager.save_extension(self, *args, **kwargs)

    def save_quality(self, *args, **kwargs):
        return USCImager.save_quality(self, *args, **kwargs)


class PCMotion:
    def __init__(self, j=None):
        self.j = j
        self.axes_meta = OrderedDict([("x", {}), ("y", {}), ("z", {})])

        # self.backlash = USCMotion.backlash
        # self.backlash_compensation = USCMotion.backlash_compensation
        # self.set_axes_meta = USCMotion.set_axes_meta

    def backlash(self, *args, **kwargs):
        return USCMotion.backlash(self, *args, **kwargs)

    def backlash_compensation(self, *args, **kwargs):
        return USCMotion.backlash_compensation(self, *args, **kwargs)

    def set_axes_meta(self, *args, **kwargs):
        return USCMotion.set_axes_meta(self, *args, **kwargs)

    def validate_axes_dict(self, *args, **kwargs):
        return USCMotion.validate_axes_dict(self, *args, **kwargs)


class PCKinematics:
    def __init__(self, j=None):
        self.j = j

    def tsettle_motion(self):
        return self.j.get("tsettle_motion", 0.0)

    def tsettle_hdr(self):
        return self.j.get("tsettle_hdr", 0.0)


"""
Planner configuration
"""


class PC:
    def __init__(self, j=None):
        self.j = j
        self.imager = PCImager(self.j.get("imager"))
        self.motion = PCMotion(self.j.get("motion", {}))
        self.kinematics = PCKinematics(self.j.get("kinematics", {}))
        self.apps = {}

    def exclude(self):
        return self.j.get('exclude', [])

    def end_at(self):
        return self.j.get("end_at", "start")

    def contour(self):
        return self.j["points-xy2p"]["contour"]

    def ideal_overlap(self, axis=None):
        # FIXME: axis option
        return self.j.get("overlap", 0.3)

    def border(self):
        """
        How much to add onto each side of the XY scan
        Convenience parameter to give a systematic fudge factor
        """
        return float(self.j.get("border", 0.0))

    def image_raw_wh_hint(self):
        return self.j.get("imager", {}).get("raw_wh_hint", None)

    def image_final_wh_hint(self):
        return self.j.get("imager", {}).get("final_wh_hint", None)

    def image_crop_tblr_hint(self):
        """
        Only used for loggin
        """
        return self.j.get("imager", {}).get("crop_tblr_hint", {})

    def image_scalar_hint(self):
        """
        Multiplier to go from Imager image size to output image size
        Only used for logging: the Imager itself is responsible for actual scaling
        """
        return float(self.j.get("imager", {}).get("scalar_hint", 1.0))

    def motion_origin(self):
        ret = self.j.get("motion", {}).get("origin", "ll")
        assert ret in ("ll", "ul"), "Invalid coordinate origin"
        return ret

    def x_view(self):
        return float(self.j["imager"]["x_view"])


def validate_pconfig(pj, strict=False):
    pass


class GUI:
    assets_dir = os.path.join(os.getcwd(), 'uscope', 'gui', 'assets')
    stylesheet_file = os.path.join(assets_dir, 'main.qss')
    icon_files = {}
    icon_files['gamepad'] = os.path.join(
        assets_dir, 'videogame_asset_FILL0_wght700_GRAD0_opsz48.png')
    icon_files['jog'] = os.path.join(
        assets_dir, 'directions_run_FILL1_wght700_GRAD0_opsz48.png')
    icon_files['NE'] = os.path.join(
        assets_dir, 'north_east_FILL1_wght700_GRAD0_opsz48.png')
    icon_files['SW'] = os.path.join(
        assets_dir, 'south_west_FILL1_wght700_GRAD0_opsz48.png')
    icon_files['NW'] = os.path.join(
        assets_dir, 'north_west_FILL0_wght700_GRAD0_opsz48.png')
    icon_files['SE'] = os.path.join(
        assets_dir, 'south_east_FILL0_wght700_GRAD0_opsz48.png')
    icon_files['camera'] = os.path.join(
        assets_dir, 'photo_camera_FILL1_wght400_GRAD0_opsz48.png')
    icon_files['go'] = os.path.join(
        assets_dir, 'smart_display_FILL1_wght400_GRAD0_opsz48.png')
    icon_files['stop'] = os.path.join(
        assets_dir, 'stop_circle_FILL1_wght400_GRAD0_opsz48.png')
    icon_files['logo'] = os.path.join(assets_dir, 'logo.png')


class BaseConfig:
    def __init__(self, j=None):
        self.j = j
        self.objective_db = ObjectiveDB()
        # self.joystick = JoystickConfig(jbc=self.j.get("joystick", {}))

        # self._enblend_cli = None
        self._enfuse_cli = None
        self._align_image_stack_cli = None

        self.init_dirs()
        self.cache_constants()

    def init_dirs(self):
        self._data_dir = os.getenv("PYUSCOPE_DATA_DIR", "data")
        if not os.path.exists(self._data_dir):
            os.mkdir(self._data_dir)

        self._scan_dir = os.path.join(self.get_data_dir(), "scan")
        if not os.path.exists(self._scan_dir):
            os.mkdir(self._scan_dir)

        self._snapshot_dir = os.path.join(self.get_data_dir(), "snapshot")
        if not os.path.exists(self._snapshot_dir):
            os.mkdir(self._snapshot_dir)

        self._microscopes_dir = os.path.join(self.get_data_dir(),
                                             "microscopes")
        if not os.path.exists(self._microscopes_dir):
            os.mkdir(self._microscopes_dir)

        self._batch_data_dir = os.path.join(self.get_data_dir(), "batch")
        if not os.path.exists(self._batch_data_dir):
            os.mkdir(self._batch_data_dir)

        self._script_data_dir = os.path.join(self.get_data_dir(), "script")
        if not os.path.exists(self._script_data_dir):
            os.mkdir(self._script_data_dir)

    def cache_constants(self):
        raw = self.j.get("timeout_scalar", "1.0")
        if raw is None:
            print("WARNING: timeouts are disabled. Software may lock up")
            self._timeout_scalar = None
        else:
            self._timeout_scalar = float(raw)
            if self._timeout_scalar <= 0:
                raise ValueError(
                    f"Invalid timouet scalar {self._timeout_scalar}")
            if self._timeout_scalar < 1:
                print(
                    "WARNING: timeout scalar is below recommended value. Software may crash without cause"
                )

    def get_data_dir(self):
        return self._data_dir

    def get_scan_dir(self):
        return self._scan_dir

    def get_snapshot_dir(self):
        return self._snapshot_dir

    def get_microscopes_dir(self):
        return self._microscopes_dir

    def batch_data_dir(self):
        """
        Directory holding saved batch scans
        Note: this doesn't include the "working" state saved in the GUI
        """
        return self._batch_data_dir

    def script_data_dir(self):
        """
        Directory holding saved script parameters
        """
        return self._script_data_dir

    def labsmore_stitch_use_xyfstitch(self):
        """
        xyfstitch is the newer higher fidelity stitch engine
        It does more aggressive analysis to eliminate stitch errors
        and uses a very different algorithm to stitch vs stock
        """
        return bool(self.j.get("labsmore_stitch", {}).get("use_xyfstitch"))

    def labsmore_stitch_save_cloudshare(self):
        """
        If this setting is true, then tell the sticher to copy
        the generated zoomable files to the served cloudshare bucket.
        """
        return bool(self.j.get("labsmore_stitch", {}).get("cloudshare"))

    def labsmore_stitch_aws_access_key(self):
        return self.j.get("labsmore_stitch", {}).get("aws_access_key")

    def labsmore_stitch_aws_secret_key(self):
        return self.j.get("labsmore_stitch", {}).get("aws_secret_key")

    def labsmore_stitch_aws_id_key(self):
        return self.j.get("labsmore_stitch", {}).get("aws_id_key")

    def labsmore_stitch_notification_email(self):
        return self.j.get("labsmore_stitch", {}).get("notification_email")

    def labsmore_stitch_plausible(self):
        return self.labsmore_stitch_aws_access_key(
        ) and self.labsmore_stitch_aws_secret_key(
        ) and self.labsmore_stitch_aws_id_key(
        ) and self.labsmore_stitch_notification_email()

    def argus_stitch_cli(self):
        """
        Call given program with the scan output directory as the argument
        """
        return self.j.get("argus_stitch_cli", None)

    def argus_cs_auto_path(self):
        """
        Override with a custom stitching program
        """
        return self.j.get("argus_cs_auto", "./utils/cs_auto.py")

    def dev_mode(self):
        """
        Display unsightly extra information
        """
        return self.j.get("dev_mode", False)

    def script_dirs(self):
        """
        The path to the secondary script dir
        Allows quick access to pyuscope-kitchen, pyuscope-rhodium, your plugins
        """
        return self.j.get("script_dirs", {})

    def get_system(self, microscope):
        systems = self.j.get("systems", [])
        for system in systems:
            if system["microscope"] == microscope.name:
                return system
        return None

    def get_joystick(self, guid):
        systems = self.j.get("joysticks", [])
        for system in systems:
            if system["guid"] == guid:
                return system
        return None

    def check_panotools(self):
        """
        Check / configure all panotools paths
        Return True if they are configured correctly
        """
        ret = True
        # ret = ret and bool(self.enblend_cli())
        ret = ret and bool(self.enfuse_cli())
        ret = ret and bool(self.align_image_stack_cli())
        return ret

    '''
    def enblend_cli(self):
        if self._enblend_cli:
            return self._enblend_cli
        self._enblend_cli = find_panotools_exe(self.j.get("panotools",
                                                          {}), "enblend_cli",
                                               "enblend", "enblend")
        return self._enblend_cli
    '''

    def enfuse_cli(self):
        """
        flatpak run --command=enfuse net.sourceforge.Hugin --help
        """
        if self._enfuse_cli:
            return self._enfuse_cli
        self._enfuse_cli = find_panotools_exe(self.j.get("panotools", {}),
                                              "enfuse_cli", "enfuse", "enfuse")
        return self._enfuse_cli

    def align_image_stack_cli(self):
        if self._align_image_stack_cli:
            return self._align_image_stack_cli
        self._align_image_stack_cli = find_panotools_exe(
            self.j.get("panotools", {}), "align_image_stack_cli",
            "align_image_stack", "align_image_stack")
        return self._align_image_stack_cli

    def timeout_scalar(self):
        """
        Sigh
        https://github.com/Labsmore/pyuscope/issues/400
        System is overheating / underpowered and sometimes failing real time requirements
        """
        return self._timeout_scalar

    def timeout_scalar_scale(self, val):
        if self._timeout_scalar:
            return self._timeout_scalar * val
        # Timeout disabled
        else:
            return None

    def check_threads(self):
        return os.getenv("PYUSCOPE_CHECK_THREADS",
                         "N") == "Y" or self.dev_mode()

    def stress_test(self):
        """
        Random sleeps, consume extra CPU, extra RAM
        """
        return bool(self.j.get("stress_test", False))

    def profile(self):
        """
        Record memory / CPU utilization
        """
        return bool(self.j.get("profile", False))

    def qr_regex(self):
        return self.j.get("qr_regex", None)


def get_bcj():
    try:
        with open(os.path.join(Path.home(), ".pyuscope")) as f:
            j = json5.load(f, object_pairs_hook=OrderedDict)
        return j
    except FileNotFoundError:
        return {}


bc = None


def get_bc(j=None):
    global bc

    if bc is None:
        if j is None:
            j = get_bcj()
        bc = BaseConfig(j=j)
    return bc
