from uscope.gui.scripting import InstrumentScriptPlugin


class Plugin(InstrumentScriptPlugin):
    def functions(self):
        return {"shout": {"n": {"type": int}}}

    def shout(self, n=1):
        for _i in range(n):
            self.log("yipeeee!")
