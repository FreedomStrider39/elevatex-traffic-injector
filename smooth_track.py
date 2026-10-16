#!/usr/bin/env python3
"""
smooth_track.py - delayed, interpolated playback of sparse position samples.

Why: dead-reckoning forward from the last sample means every new sample is a
*correction* (a visible leap). Instead we render a fixed time in the PAST
(about one feed interval + a small margin), so the playhead is always between
two samples we already have and just moves along a curve between them.
Nothing ever needs correcting, so nothing leaps.

Per flight:
    track = Track(expected_interval=12.0)          # your poll interval
    track.push(Sample(t=created_at, lat=.., lon=.., alt=.., hdg=.., gs=.., vs=..),
               arrival=time.monotonic())           # whenever a NEW sample comes in
    ...every tick:
    pose = track.step(time.monotonic())            # Pose, or None if no data yet
"""
from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional

# Along-track timing: the speed played at the END of a segment is the reported
# ground speed, held within this band around the speed needed to cover the
# distance to that sample. Tighter = steadier taxi; looser = follows the
# reported ground speed more closely (sharper take-off acceleration).
SPEED_TRUST_LO = 0.75
SPEED_TRUST_HI = 2.0


@dataclass
class Sample:
    t: float      # feed timestamp (createdAt); seconds or milliseconds
    lat: float
    lon: float
    alt: float    # ft
    hdg: float    # deg
    gs: float     # kt
    vs: float = 0.0  # fpm
    v_play: float = -1.0  # kt: speed at which playback LEFT this sample (set by _interp)


@dataclass
class Pose:
    lat: float
    lon: float
    alt: float
    hdg: float
    gs: float
    vs: float
    hdg_rate: float = 0.0  # deg/s of the segment being played (for bank)
    next_alt: Optional[float] = None  # altitude of the sample being played TOWARD (ft)
    next_gs: Optional[float] = None   # its ground speed (kt)
    prev_gs: Optional[float] = None   # ground speed of the sample just passed (kt)
    # next_*/prev_gs let the landing flare tell a runway sample from an airborne one


def _wrap180(d: float) -> float:
    return (d + 540.0) % 360.0 - 180.0


def _norm_lon(lon: float) -> float:
    return (lon + 540.0) % 360.0 - 180.0


def _to_seconds(t: float) -> float:
    return t / 1000.0 if t > 1e11 else t  # tolerate ms timestamps


def _dist_nm(lat1, lon1, lat2, lon2) -> float:
    cos0 = math.cos(math.radians((lat1 + lat2) / 2))
    return 60.0 * math.hypot(lat2 - lat1, _wrap180(lon2 - lon1) * cos0)


def _pose_of(s: Sample) -> Pose:
    return Pose(s.lat, s.lon, s.alt, s.hdg % 360.0, s.gs, s.vs)


def _project(s: Sample, dt: float) -> Pose:
    """Straight-line projection of a sample forward (dt>0) or backward (dt<0)."""
    d_nm = s.gs * dt / 3600.0
    h = math.radians(s.hdg)
    cos0 = max(math.cos(math.radians(s.lat)), 0.01)
    return Pose(
        lat=s.lat + d_nm * math.cos(h) / 60.0,
        lon=_norm_lon(s.lon + d_nm * math.sin(h) / (60.0 * cos0)),
        alt=s.alt + max(-2500.0, min(2500.0, s.vs * dt / 60.0)),
        hdg=s.hdg % 360.0, gs=s.gs, vs=s.vs, hdg_rate=0.0,
    )


def _hermite(p0: float, p1: float, m0: float, m1: float, u: float):
    """Monotone-limited cubic Hermite. m0/m1 are slopes in value-per-unit-u.
    Limiting each slope to [0, 3x the chord] (in the chord's direction) means
    the curve can never overshoot. Returns (value, d value / d u)."""
    d = p1 - p0
    if abs(d) < 1.0:
        m0 = m1 = 0.0
    else:
        sg = 1.0 if d > 0 else -1.0
        lim = 3.0 * abs(d)
        m0 = min(max(m0 * sg, 0.0), lim) * sg
        m1 = min(max(m1 * sg, 0.0), lim) * sg
    u2 = u * u
    u3 = u2 * u
    val = ((2 * u3 - 3 * u2 + 1) * p0 + (u3 - 2 * u2 + u) * m0
           + (-2 * u3 + 3 * u2) * p1 + (u3 - u2) * m1)
    dv = ((6 * u2 - 6 * u) * p0 + (3 * u2 - 4 * u + 1) * m0
          + (6 * u - 6 * u2) * p1 + (3 * u2 - 2 * u) * m1)
    return val, dv


def _near_field(alt: float, elevs) -> bool:
    return any(e > -100.0 and -30.0 <= alt - e <= 25.0 for e in elevs)


def _alt_profile(a: Sample, b: Sample, t: float, seg: float, elevs):
    """Altitude and vertical speed (fpm) t seconds into the segment a -> b.

    General case: monotone Hermite using the samples' vertical speeds, so the
    climb/descent rate is continuous (no step at each sample).
    With 12 s between samples the TIMING of touchdown and lift-off would be
    smeared over the whole segment (a float of 12 s, or lifting off at 100 kt),
    so when the field elevations are known:
      landing  - keep descending at the approach rate until the surface is
                 reached (a.alt-b.alt)/vs seconds in, then sit on it
                 (the next sample must be near the field and slower);
      take-off - stay on the runway until ~85% of the climb-out speed, then climb.
    """
    if elevs:
        if (a.gs >= 60.0 and a.vs <= -250.0 and a.alt - b.alt > 5.0
                and b.gs <= a.gs - LAND_GS_DROP_KT and _near_field(b.alt, elevs)):
            T = min(max((a.alt - b.alt) / (-a.vs / 60.0), 0.5), seg)
            if t >= T:
                return b.alt, 0.0
            # constant approach sink; the AttitudeModel flare shapes the last metres
            return max(a.alt + a.vs / 60.0 * t, b.alt), a.vs
        if (_near_field(a.alt, elevs) and b.alt - a.alt > 100.0
                and b.gs - a.gs > 15.0 and b.gs >= 60.0):
            frac = min(max((0.85 * b.gs - a.gs) / (b.gs - a.gs), 0.0), 0.85)
            tl = seg * frac
            if t <= tl:
                return a.alt, 0.0
            T = seg - tl
            val, dv = _hermite(a.alt, b.alt, 0.0, b.vs / 60.0 * T, (t - tl) / T)
            return val, dv / T * 60.0
    val, dv = _hermite(a.alt, b.alt, a.vs / 60.0 * seg, b.vs / 60.0 * seg, t / seg)
    return val, dv / seg * 60.0


def _interp(a: Sample, b: Sample, u: float, seg: float, elevs=()) -> Pose:
    """Cubic Hermite path in a local NM frame (tangents from heading), played
    back with a separate continuous speed profile along it (see below)."""
    cos0 = max(math.cos(math.radians((a.lat + b.lat) / 2)), 0.01)
    x1 = _wrap180(b.lon - a.lon) * 60.0 * cos0
    y1 = (b.lat - a.lat) * 60.0
    chord = math.hypot(x1, y1)

    def tangent(s: Sample):
        # Direction from the sample's heading, length = the chord, so the SHAPE
        # of the path never depends on how fast the aircraft happens to be going.
        h = math.radians(s.hdg)
        dx, dy = math.sin(h), math.cos(h)
        if chord > 0.005 and dx * x1 + dy * y1 < 0.0:
            dx, dy = -dx, -dy                         # moving backwards (pushback)
        return dx * chord, dy * chord

    m0x, m0y = tangent(a)
    m1x, m1y = tangent(b)

    # TIMING along that path is a separate monotone speed profile. We model
    # speed s(tau) as a *guarded* cubic Hermite in tau from the previous
    # sample's ground speed to this sample's ground speed, with zero end
    # slopes (ease-in/ease-out). Because s(tau)=va+(vb-va)(3tau^2-2tau^3) has
    # s'(tau)=6tau(1-tau) >= 0, it is monotone and never overshoots or
    # reverses -- this is exactly PSXT's "guarded Hermite speed interpolation"
    # for take-off/landing rolls. Integrating s to get the distance fraction
    # gives w(u)=[va*u + (vb-va)(u^3 - u^4/2)] / mean, which accelerates and
    # decays gently instead of the old S-shaped distance profile that crawled
    # then suddenly rushed on take-off. va is the speed the previous segment
    # ended at (same reported gs at the shared sample), so speed is continuous
    # across segments.
    avg = chord / seg                                  # NM/s needed on average
    if chord < 0.002:
        w = u                                          # parked: nothing to time
    else:
        va = min(max(a.gs / 3600.0, SPEED_TRUST_LO * avg), SPEED_TRUST_HI * avg)
        vb = min(max(b.gs / 3600.0, SPEED_TRUST_LO * avg), SPEED_TRUST_HI * avg)
        denom = (va + vb) * 0.5                        # mean speed (= integral of s over 0..1)
        if denom <= 1e-9:
            w = u
        else:
            w = (va * u + (vb - va) * (u * u * u - 0.5 * u * u * u * u)) / denom
        w = min(max(w, 0.0), 1.0)
    w2 = w * w
    w3 = w2 * w
    h10 = w3 - 2 * w2 + w
    h01 = -2 * w3 + 3 * w2
    h11 = w3 - w2
    x = h10 * m0x + h01 * x1 + h11 * m1x
    y = h10 * m0y + h01 * y1 + h11 * m1y

    alt, vs_out = _alt_profile(a, b, u * seg, seg, elevs)

    dh = _wrap180(b.hdg - a.hdg)
    return Pose(
        lat=a.lat + y / 60.0,
        lon=_norm_lon(a.lon + x / (60.0 * cos0)),
        alt=alt,
        hdg=(a.hdg + dh * u) % 360.0,
        gs=a.gs + (b.gs - a.gs) * u,
        vs=vs_out,
        hdg_rate=dh / seg,
        next_alt=b.alt,
        next_gs=b.gs,
        prev_gs=a.gs,
    )


class Track:
    """Plays samples back at a fixed delay behind the feed clock."""

    def __init__(self, expected_interval: float = 12.0, margin: float = 3.0,
                 teleport_kt: float = 1500.0, max_gap_s: float = 180.0,
                 max_hold_s: float = 90.0, max_samples: int = 12):
        self.expected_interval = expected_interval
        self.margin = margin          # tolerance for late/jittery arrivals (s)
        self.teleport_kt = teleport_kt
        self.max_gap_s = max_gap_s
        self.max_hold_s = max_hold_s
        self.max_samples = max_samples

        self.q: Deque[Sample] = deque()
        self.cands: Deque[float] = deque(maxlen=8)   # feed_time - local_time per arrival
        self.interval: Optional[float] = None
        self.off: Optional[float] = None             # playhead = now + off
        self.last_now: Optional[float] = None
        self.out: Optional[Pose] = None
        self.extrap = False
        self.corr = (0.0, 0.0, 0.0)                  # decaying (lat, lon, alt) correction
        self.elevs = ()                              # (origin_ft, dest_ft): enables touchdown/lift-off timing
        self._lock = threading.Lock()

    # ---- input -------------------------------------------------------------
    def push(self, s: Sample, arrival: Optional[float] = None) -> None:
        arrival = time.monotonic() if arrival is None else arrival
        s.t = _to_seconds(s.t)
        with self._lock:
            if self.q:
                last = self.q[-1]
                gap = s.t - last.t
                if gap <= 0.0:
                    return  # duplicate / out of order
                implied_kt = _dist_nm(last.lat, last.lon, s.lat, s.lon) / max(gap, 0.1) * 3600.0
                if gap > self.max_gap_s or implied_kt > self.teleport_kt:
                    # genuine jump: snap instead of gliding across the map
                    self.q.clear()
                    self.out = _pose_of(s)
                    self.corr = (0.0, 0.0, 0.0)
                    self.extrap = False
                    self.interval = None
                    self.cands.clear()
                    self.off = None
                else:
                    g = min(max(gap, 0.5), 60.0)
                    self.interval = g if self.interval is None else 0.7 * self.interval + 0.3 * g
            self.q.append(s)
            while len(self.q) > self.max_samples:
                self.q.popleft()
            self.cands.append(s.t - arrival)
            if self.out is None:
                self.out = _pose_of(s)

    # ---- output ------------------------------------------------------------
    def _delay(self) -> float:
        return (self.interval if self.interval is not None else self.expected_interval) + self.margin

    def step(self, now: float) -> Optional[Pose]:
        with self._lock:
            q = self.q
            if not q:
                return self.out
            dt = 0.0 if self.last_now is None else min(max(now - self.last_now, 0.0), 1.0)
            self.last_now = now

            # Playhead offset: follow the best (lowest-latency) arrival, but slew
            # slowly (<=5%) so a latency change never shows up as a speed jump.
            target = max(self.cands) - self._delay()
            if self.off is None:
                self.off = target
            else:
                lim = 0.05 * dt
                self.off += max(-lim, min(lim, target - self.off))
            P = now + self.off

            while len(q) > 2 and q[1].t <= P:
                q.popleft()

            if len(q) == 1:
                dtp = max(-60.0, min(P - q[0].t, self.max_hold_s))
                raw = _pose_of(q[0]) if q[0].gs < GROUND_HOLD_KT else _project(q[0], dtp)
                extrap_now = P > q[0].t
            elif P >= q[-1].t:                       # starved: run on from newest sample
                if q[-1].gs < GROUND_HOLD_KT:
                    # Ground/taxi with no fresh data: hold the last known spot
                    # rather than dead-reckon off the taxiway (PSXT does this).
                    raw = _pose_of(q[-1])
                else:
                    raw = _project(q[-1], min(P - q[-1].t, self.max_hold_s))
                extrap_now = True
            elif P <= q[0].t:                        # startup: just before first sample
                raw = _project(q[0], max(P - q[0].t, -60.0))
                extrap_now = False
            else:
                a, b = q[0], q[1]
                seg = max(b.t - a.t, 0.05)
                raw = _interp(a, b, (P - a.t) / seg, seg, self.elevs)
                extrap_now = False

            # Leaving a starved/extrapolated stretch: absorb the mismatch and fade it out.
            if self.extrap and not extrap_now and self.out is not None:
                self.corr = (self.out.lat - raw.lat,
                             _wrap180(self.out.lon - raw.lon),
                             self.out.alt - raw.alt)
            self.extrap = extrap_now
            k = math.exp(-dt / 4.0)
            self.corr = (self.corr[0] * k, self.corr[1] * k, self.corr[2] * k)
            raw.lat += self.corr[0]
            raw.lon = _norm_lon(raw.lon + self.corr[1])
            raw.alt += self.corr[2]
            self.out = raw
            return raw


# ---- attitude / vertical profile ---------------------------------------------
GROUND_HOLD_KT = 60.0        # below this speed a gap means "hold position", not dead-reckon
GROUND_AGL_FT = 40.0         # below this (gs >= 50) the aircraft is on the runway
ROTATE_GS_KT = 110.0         # earliest speed at which the nose may rotate
ROTATE_PITCH_DEG = 8.0       # pitch reached on the runway before lift-off
FLARE_TD_SINK_FPS = 2.8      # sink rate at touchdown (~170 fpm)
FLARE_PITCH_BONUS_DEG = 1.5  # extra nose-up added over the flare itself
DEROTATE_RATE_DPS = 1.8      # nose lowering rate after touchdown
LAND_GS_DROP_KT = 3.0        # next sample this much slower than the approach => it is on the runway


def ground_ref(pose: Pose, origin_elev_ft: float, dest_elev_ft: float) -> Optional[float]:
    """Elevation of the field the aircraft is vertically CLOSEST above.

    (Taking the lowest height over both fields was wrong for a sea-level
    departure toward a high airport: it measured height above the destination.)
    """
    best = None
    for e in (origin_elev_ft, dest_elev_ft):
        if e > -100.0 and pose.alt - e >= -50.0 and (best is None or e > best):
            best = e
    return best


def agl_ft(pose: Pose, origin_elev_ft: float, dest_elev_ft: float) -> Optional[float]:
    ref = ground_ref(pose, origin_elev_ft, dest_elev_ft)
    return None if ref is None else pose.alt - ref


def _fpa(vs_fpm: float, gs_kt: float) -> float:
    """Flight-path angle (deg) from vertical speed and ground speed."""
    return math.degrees(math.atan2(vs_fpm / 60.0, max(gs_kt, 1.0) * 1.68781))


def _aoa(gs_kt: float) -> float:
    """Rough angle of attack: ~5 deg slow/dirty, ~2 deg fast/clean."""
    slow = 1.0 - min(max((gs_kt - 130.0) / 100.0, 0.0), 1.0)
    return 2.0 + 3.0 * slow


def _slew(cur: float, tgt: float, dt: float, tau: float, up: float, dn: float) -> float:
    """First-order ease toward tgt, limited to up/dn degrees per second."""
    step = (tgt - cur) * (1.0 - math.exp(-dt / tau))
    return cur + max(-dn * dt, min(up * dt, step))


class AttitudeModel:
    """Per-aircraft pitch / bank / altitude shaping (natural signs: +pitch up,
    +bank right; the injector negates them for MSFS).

    pitch  = flight-path angle + angle of attack, eased and rate-limited so
             climb, level-off and descent changes are gradual;
             rotation before lift-off; a proper landing flare; nose lowering
             after touchdown.
    flare  = exponential sink-rate decay toward the runway surface, started
             at ~3 s above it, ending at ~170 fpm at touchdown. The runway
             height is read from the NEXT sample when that sample is on the
             ground (so it is the feed's own ground level, not a guess).
    """

    def __init__(self) -> None:
        self.pitch: Optional[float] = None
        self.rot = False          # rotating / just lifted off
        self.flare = False
        self.landed = False       # touched down, still rolling out
        self.shape_alt = False    # flare is also shaping the altitude
        self.h = 0.0              # shaped height above the surface (ft)
        self.hf = 0.0             # height at which the flare started
        self.tau = 1.0
        self.hb = 0.0
        self.ref = 0.0            # surface altitude used by the flare (ft MSL)
        self.off = 0.0            # decaying altitude offset (ft)

    def update(self, pose: Pose, elev_ft: Optional[float], dt: float):
        """-> (pitch_deg, bank_deg, alt_ft)"""
        dt = min(max(dt, 0.0), 1.0)
        gs = pose.gs
        agl = None if elev_ft is None else pose.alt - elev_ft
        alt_out = pose.alt

        if gs < 50.0:                        # parked / taxi: reset sequences
            self.flare = self.landed = self.rot = False
            self.off = 0.0

        fresh = False
        # ---- landing flare: trigger -------------------------------------
        # The feed has no on-ground flag and its altitude is only good to a
        # few tens of feet, so the runway height is taken from the NEXT sample
        # only when that sample clearly IS on the runway: low, and noticeably
        # slower than the approach (braking). Then the flare shapes both
        # pitch and altitude. Otherwise only a short, late, pitch-only flare
        # is used, close to the field elevation.
        if (not self.flare and not self.landed and elev_ft is not None
                and 60.0 <= gs <= 200.0 and pose.vs < -200.0):
            floor = None
            if (pose.next_alt is not None and pose.next_gs is not None
                    and pose.prev_gs is not None):
                na = pose.next_alt - elev_ft
                if (-30.0 <= na <= 25.0 and pose.next_gs <= 200.0
                        and pose.next_gs <= pose.prev_gs - LAND_GS_DROP_KT):
                    floor = pose.next_alt
            v0 = min(max(-pose.vs / 60.0, 4.0), 20.0)            # ft/s
            if floor is not None:
                ref, hmax = floor, min(max(3.0 * v0, 15.0), 40.0)
            else:
                ref, hmax = elev_ft, 12.0
            h = pose.alt - ref
            if -5.0 <= h <= hmax:
                td = min(FLARE_TD_SINK_FPS, 0.6 * v0)
                self.hf = max(h, 0.5)
                self.tau = self.hf / (v0 - td)    # sink starts at v0, ends at td
                self.hb = td * self.tau
                self.h = self.hf
                self.ref = ref
                self.shape_alt = floor is not None
                self.flare = True
                fresh = True

        # ---- landing flare: run -----------------------------------------
        if self.flare:
            if not fresh:
                self.h = (self.h + self.hb) * math.exp(-dt / self.tau) - self.hb
            if pose.vs > 300.0 or pose.alt - self.ref > 2.5 * self.hf + 10.0:
                # go-around: hand the altitude back to the track smoothly
                if self.shape_alt:
                    self.off = self.ref + max(self.h, 0.0) - pose.alt
                self.flare = False
            elif self.h <= 0.0:                                   # touchdown
                self.h = 0.0
                self.flare = False
                self.landed = True
                self.off = (self.ref - pose.alt) if self.shape_alt else 0.0
            elif self.shape_alt:
                alt_out = self.ref + self.h
                self.off = 0.0
        if not self.flare:
            self.off *= math.exp(-dt / (1.0 if self.landed else 2.5))
            alt_out = pose.alt + self.off

        if self.landed and ((agl is not None and agl > 150.0) or pose.vs > 300.0):
            self.landed = False                                   # touch-and-go

        # ---- pitch target -----------------------------------------------
        up, dn, tau_p = 1.5, 1.5, 0.6
        if self.flare:
            vs_s = -(self.h + self.hb) / self.tau * 60.0          # shaped sink (fpm)
            prog = 1.0 - min(self.h / self.hf, 1.0)
            target = _fpa(vs_s, gs) + _aoa(gs) + FLARE_PITCH_BONUS_DEG * prog
            up, dn = 1.2, 1.0
        elif self.landed:
            target, dn = 0.0, DEROTATE_RATE_DPS
        elif gs < 50.0:
            target = 0.0
        elif agl is not None and agl < GROUND_AGL_FT and pose.vs > -250.0:   # runway roll
            ahead = (pose.next_alt is not None and pose.next_alt - pose.alt > 150.0)
            # rotate at ~78% of the climb-out speed (type-agnostic VR), or as
            # soon as the track starts to leave the ground
            vr = 0.78 * pose.next_gs if pose.next_gs else ROTATE_GS_KT
            if ahead and (gs >= vr or pose.vs > 100.0 or agl > 6.0):
                self.rot = True
            target = ROTATE_PITCH_DEG if self.rot else 0.0
            up = 3.0
        else:
            target = max(-8.0, min(15.0, _fpa(pose.vs, gs) + _aoa(gs)))
            if self.rot:
                up = 3.0
                if agl is not None and agl > 200.0:
                    self.rot = False

        if self.pitch is None:
            self.pitch = target
        else:
            self.pitch = _slew(self.pitch, target, dt, tau_p, up, dn)

        bank = max(-30.0, min(30.0, pose.hdg_rate * (7.0 + gs / 150.0)))
        if agl is not None and agl < 100.0 and gs < 200.0:
            bank = max(-8.0, min(8.0, bank))                      # wings level near the ground
        return self.pitch, bank, alt_out


def attitude(pose: Pose, agl: Optional[float] = None):
    """Stateless fallback (no rate limiting / flare): (pitch_deg, bank_deg)."""
    gs = pose.gs
    if gs < 50.0 or (agl is not None and agl < GROUND_AGL_FT):
        pitch = 0.0
    else:
        pitch = max(-8.0, min(15.0, _fpa(pose.vs, gs) + _aoa(gs)))
    bank = max(-30.0, min(30.0, pose.hdg_rate * (7.0 + gs / 150.0)))
    return pitch, bank


def is_on_ground(pose: Pose, origin_elev_ft: float, dest_elev_ft: float) -> bool:
    known = [pose.alt - e for e in (origin_elev_ft, dest_elev_ft) if e > -100.0]
    min_agl = min(known) if known else pose.alt
    return pose.gs < 50.0 and abs(pose.vs) < 400.0 and min_agl < 400.0
