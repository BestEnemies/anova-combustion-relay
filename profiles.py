"""Cook profiles for the Combustion Engine (MicroPython).

A profile is a list of stages. Each stage sets the Engine set point, then
moves on to the next stage when ANY of its triggers fire:

    after_min  - minutes since the Engine took the stage's set point
    core_at    - the probe's virtual core temperature reaches this (C)

A stage with no triggers holds forever (use it as the final stage). If the
last stage has a trigger, the run finishes when it fires and the Engine keeps
that set point.

    {"name": "Brisket", "stages": [
        {"setpoint": 110, "core_at": 68},
        {"setpoint": 125, "after_min": 120, "core_at": 93},
        {"setpoint": 75}]}

The active run is saved to run.json with wall-clock (NTP) times, so a reboot
resumes where it left off. Set points are never fought: if the Engine's set
point is changed elsewhere (app, knob), the runner leaves it alone until the
next stage starts.
"""

import json
import time

import protocol

PROFILES_PATH = "profiles.json"
RUN_PATH = "run.json"
CLOCK_VALID = 750000000      # seconds since 2000 (~2023-10): NTP has synced
RETRY_S = 60                 # retry a failed set point change this often
MAX_STAGES = 12
_TOL = 0.15

# Hot-and-fast pork shoulder without losing the smoke: smoke uptake (and the
# smoke ring) happens while the meat is below ~60 C, so start low, then go hot
# once it has taken its smoke. Time limits are backstops in case the probe
# drops out. Wrap (butcher paper) when stage 3 starts (core 71 C).
EXAMPLE = {"name": "Quicker Pulled Pork", "stages": [
    {"setpoint": 121, "core_at": 60, "after_min": 180},   # 250F: take smoke
    {"setpoint": 149, "core_at": 71, "after_min": 150},   # 300F: set bark
    {"setpoint": 149, "core_at": 95, "after_min": 300},   # wrapped: to 203F
    {"setpoint": 77},                                     # 170F: rest/hold
]}


def _num(v, lo, hi, what):
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise ValueError("%s must be a number" % what)
    if not lo <= v <= hi:
        raise ValueError("%s must be %g..%g" % (what, lo, hi))
    return v


def validate(p):
    """Return a clean copy of profile p, or raise ValueError."""
    if not isinstance(p, dict):
        raise ValueError("profile must be an object")
    name = str(p.get("name", "")).strip()[:40]
    if not name:
        raise ValueError("profile needs a name")
    stages = p.get("stages")
    if not isinstance(stages, list) or not stages:
        raise ValueError("profile needs at least one stage")
    if len(stages) > MAX_STAGES:
        raise ValueError("at most %d stages" % MAX_STAGES)
    out = []
    for i, s in enumerate(stages):
        what = "stage %d" % (i + 1)
        if not isinstance(s, dict):
            raise ValueError(what + " must be an object")
        st = {"setpoint": round(_num(s.get("setpoint"), protocol.ENGINE_SETPOINT_MIN_C,
                                     protocol.ENGINE_SETPOINT_MAX_C,
                                     what + " set point"), 1)}
        if s.get("after_min") not in (None, ""):
            st["after_min"] = round(_num(s["after_min"], 0.1, 10080,
                                         what + " time (minutes)"), 1)
        if s.get("core_at") not in (None, ""):
            st["core_at"] = round(_num(s["core_at"], 0, 300,
                                       what + " core temperature"), 1)
        out.append(st)
    return {"name": name, "stages": out}


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    import os
    try:
        os.remove(path)
    except OSError:
        pass
    os.rename(tmp, path)


class ProfileStore:
    def __init__(self, path=PROFILES_PATH):
        self.path = path
        data = _load(path, None)
        if data is None:
            self.profiles = [EXAMPLE]
            self._persist()
        else:
            self.profiles = [p for p in data.get("profiles", [])
                             if isinstance(p, dict)]

    def _persist(self):
        _save(self.path, {"profiles": self.profiles})

    def get(self, name):
        for p in self.profiles:
            if p.get("name") == name:
                return p
        return None

    def put(self, profile, old_name=None):
        p = validate(profile)
        names = (p["name"], old_name) if old_name else (p["name"],)
        idx = None
        for i, q in enumerate(self.profiles):
            if q.get("name") in names:
                idx = i
                break
        if idx is None:
            self.profiles.append(p)
        else:
            self.profiles[idx] = p
            # Renamed onto another existing profile: drop the duplicate.
            self.profiles = [q for i, q in enumerate(self.profiles)
                             if i == idx or q.get("name") != p["name"]]
        self._persist()
        return p

    def delete(self, name):
        n = len(self.profiles)
        self.profiles = [p for p in self.profiles if p.get("name") != name]
        if len(self.profiles) != n:
            self._persist()
            return True
        return False


def clock_ok():
    return time.time() > CLOCK_VALID


class ProfileRunner:
    """Drives the Engine through a profile's stages. Call service() ~1 Hz."""

    def __init__(self, engine, log=print, probe_pref="", path=RUN_PATH):
        self.engine = engine
        self.log = log
        self.probe_pref = (probe_pref or "").strip().upper()
        self.path = path
        self.run = _load(path, None)
        if self.run and self.run.get("state") != "running":
            self.run = None
        # Per-boot command tracking (not persisted).
        self._cmd_at = None       # ticks the current stage's command started
        self._retry_at = None     # time.time() to retry after a failure
        self._note = ""
        self._last_core = None
        if self.run:
            self.log("[profile] resuming '%s' at stage %d"
                     % (self.run["profile"]["name"], self.run["stage"] + 1))

    # -- control ---------------------------------------------------------------

    def start(self, profile, probe=None):
        if not clock_ok():
            return "clock not synced yet (needs WiFi/NTP) - try again shortly"
        p = validate(profile)
        now = int(time.time())
        self.run = {"profile": p, "stage": 0, "started": now,
                    "stage_started": now, "probe": (probe or "").upper() or None,
                    "applied": -1, "override": False, "state": "running"}
        self._reset_cmd()
        self._persist()
        self.log("[profile] started '%s' (%d stages)" % (p["name"], len(p["stages"])))
        return None

    def stop(self):
        if self.run:
            self.log("[profile] stopped '%s'" % self.run["profile"]["name"])
        self.run = None
        self._reset_cmd()
        try:
            import os
            os.remove(self.path)
        except OSError:
            pass

    def skip(self):
        if self.run:
            self._advance("skipped from the web UI")

    def _reset_cmd(self):
        self._cmd_at = None
        self._retry_at = None
        self._note = ""

    def _persist(self):
        try:
            _save(self.path, self.run)
        except Exception as exc:
            self.log("[profile] could not save run: %s" % exc)

    # -- probe -----------------------------------------------------------------

    def _probe(self):
        """(serial4, core_c) for the run's probe; picks one if not set."""
        readings = self.engine.probe_readings()
        want = self.run.get("probe") or self.probe_pref
        if want:
            for s, r in readings.items():
                if protocol.probe_serial_str(s) == want:
                    return s, (r["core_c"] if r["age_ms"] < 60000 else None)
            return None, None
        best = None
        for s, r in readings.items():
            if r["core_c"] is not None and r["age_ms"] < 60000:
                if best is None or r["age_ms"] < best[1]["age_ms"]:
                    best = (s, r)
        if best is None:
            return None, None
        self.run["probe"] = protocol.probe_serial_str(best[0])
        self._persist()
        self.log("[profile] using probe %s for core temperature" % self.run["probe"])
        return best[0], best[1]["core_c"]

    # -- stages ----------------------------------------------------------------

    def _advance(self, why):
        run = self.run
        stages = run["profile"]["stages"]
        i = run["stage"]
        if i + 1 >= len(stages):
            self.log("[profile] '%s' finished (%s)" % (run["profile"]["name"], why))
            self.stop()
            return
        run["stage"] = i + 1
        run["stage_started"] = int(time.time())
        run["override"] = False
        self._reset_cmd()
        self._persist()
        self.log("[profile] stage %d -> %d (%s): set point %.1f C"
                 % (i + 1, i + 2, why, stages[i + 1]["setpoint"]))

    def service(self):
        run = self.run
        if not run:
            return
        if not clock_ok():
            self._note = "waiting for the clock (NTP)"
            return
        stage = run["profile"]["stages"][run["stage"]]
        sp = stage["setpoint"]
        now = int(time.time())
        eng = self.engine
        # 1. Get the Engine to this stage's set point (once per stage).
        if run["applied"] != run["stage"]:
            if self._cmd_at is None:
                if self._retry_at is None or now >= self._retry_at:
                    err = eng.request_setpoint(sp, "profile stage %d" % (run["stage"] + 1))
                    if err:
                        self._note = err
                        self._retry_at = now + RETRY_S
                    else:
                        self._cmd_at = time.ticks_ms()
                        self._note = "setting Engine to %.1f C" % sp
            else:
                res = eng.result_for(self._cmd_at)
                if res is not None:
                    self._cmd_at = None
                    if res.startswith("ok") or res.startswith("overridden"):
                        run["applied"] = run["stage"]
                        run["override"] = res.startswith("overridden")
                        # The stage's timer runs from when the Engine took the
                        # set point, so a slow/unreachable Engine can't eat it.
                        run["stage_started"] = now
                        self._note = "" if not run["override"] else res
                        self._persist()
                    else:
                        self._note = res + " - retrying in %ds" % RETRY_S
                        self._retry_at = now + RETRY_S
        elif not run["override"]:
            # 2. Don't fight a manual change: just note it.
            cur = eng.state.get("setpoint_c")
            if cur is not None and eng.cmd is None and abs(cur - sp) >= _TOL:
                run["override"] = True
                self._persist()
                self._note = "set point changed to %.1f C outside the profile" % cur
                self.log("[profile] Engine set point changed to %.1f C elsewhere - "
                         "leaving it until the next stage" % cur)
        # 3. Triggers.
        serial, core = self._probe()
        self._last_core = core
        elapsed_min = (now - run["stage_started"]) / 60.0
        if ("after_min" in stage and run["applied"] == run["stage"]
                and elapsed_min >= stage["after_min"]):
            self._advance("%g min elapsed" % stage["after_min"])
        elif "core_at" in stage and core is not None and core >= stage["core_at"]:
            self._advance("core reached %.1f C" % core)

    # -- status for the UI -------------------------------------------------------

    def status(self):
        run = self.run
        if not run:
            return {"running": False}
        stages = run["profile"]["stages"]
        i = run["stage"]
        synced = clock_ok()
        return {
            "running": True,
            "name": run["profile"]["name"],
            "stage": i + 1,
            "stages": stages,
            "setpoint": stages[i]["setpoint"],
            "stage_min": ((int(time.time()) - run["stage_started"]) / 60.0
                          if synced else None),
            "total_min": ((int(time.time()) - run["started"]) / 60.0
                          if synced else None),
            "probe": run.get("probe"),
            "core_c": self._last_core,
            "applied": run["applied"] == i,
            "override": run["override"],
            "note": self._note,
        }
