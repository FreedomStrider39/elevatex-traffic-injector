#!/usr/bin/env python3
"""
ElevateX Live Traffic Injector for Microsoft Flight Simulator (MSFS)
Injects live traffic from the ElevateX network feed into MSFS using FSLTL AI aircraft models.

Usage:
    python elevatex_live_traffic.py [--dry-run] [--radius 50] [--max-aircraft 40] [--community PATH]
"""

import argparse
import ctypes
from ctypes import POINTER, c_double, pointer, sizeof, cast
import dataclasses
import glob
import json
import math
import os
import re
import sys
import threading
import time
import urllib.request
from typing import Dict, List, Optional, Tuple, Set

from smooth_track import (Track, Sample, AttitudeModel, ground_ref,
                          attitude, is_on_ground, agl_ft)

# --- Dependency Check ---
try:
    import SimConnect
    from SimConnect import AircraftRequests, SimConnect as PySimConnect
    from SimConnect.Attributes import SIMCONNECT_DATA_INITPOSITION
    from SimConnect.Enum import (
        SIMCONNECT_DATATYPE,
        SIMCONNECT_EXCEPTION,
        SIMCONNECT_EVENT_FLAG,
        SIMCONNECT_GROUP_PRIORITY_HIGHEST,
        SIMCONNECT_PERIOD,
        SIMCONNECT_RECV_ID,
        SIMCONNECT_RECV_ASSIGNED_OBJECT_ID,
        SIMCONNECT_RECV_SIMOBJECT_DATA,
        SIMCONNECT_UNUSED,
    )

    SIMCONNECT_AVAILABLE = True
except ImportError:
    SIMCONNECT_AVAILABLE = False


# ==============================================================================
# DATA MODELS
# ==============================================================================

@dataclasses.dataclass
class ElevateXPosition:
    latitude: float
    longitude: float
    altitude_ft: float
    ground_speed_kt: float
    heading_deg: float
    vertical_speed_fpm: float
    xpdr: int
    created_at: float  # Unix timestamp from feed


@dataclasses.dataclass
class TrafficFlight:
    id: str
    callsign: str
    flight_number: str
    aircraft_icao: str
    registration: str
    airline_icao: str
    airline_name: str
    origin_icao: str
    origin_lat: float
    origin_lon: float
    dest_icao: str
    dest_lat: float
    dest_lon: float
    dep_gate: str
    arr_gate: str
    simulator: str
    completed: bool
    cancelled: bool
    position: ElevateXPosition
    prev_position: Optional[ElevateXPosition] = None
    sample_time: float = 0.0  # Monotonic time when fetched
    prev_sample_time: float = 0.0
    origin_elev_ft: float = 0.0
    dest_elev_ft: float = 0.0
    user_id: str = ""       # ElevateX user id of the pilot flying this flight
    user_name: str = ""     # ElevateX username of the pilot
    vs_fpm: float = 0.0     # smoothed, derived vertical speed (feed's own is 0)
    track: Track = dataclasses.field(
        default_factory=lambda: Track(expected_interval=FEED_POLL_INTERVAL_S))


# ==============================================================================
# ELEVATEX FEED CLIENT
# ==============================================================================

# Core timing: feed poll is FIXED (not user-adjustable) so tracking stays
# identical on every PC. Position writes run at ~30 Hz with dt-based
# smoothing, so slower machines stay smooth instead of jittery.
FEED_POLL_INTERVAL_S = 12.0
TRACK_INTERVAL_S = 0.033  # ~30 Hz target; smoothing is dt-based (frame-rate independent)

# Max heading change per second we allow when easing the nose toward the
# (noisy) feed heading. Kills left-right jitter during taxi without making
# turns look sluggish. PSXT/LiveTraffic smooth heading the same way.
HDG_RATE_LIMIT_DPS = 12.0

# MSFS body axes for VELOCITY BODY X/Y/Z: +X forward, +Y up, +Z right.
# Velocity is measured from the per-tick position delta and rotated into this
# frame. Flip to -1.0 if a sim build ever treats +Z as left (the lateral part
# is small, but the sign must match or it doubles the sideways error).
BODY_Z_RIGHT_SIGN = 1.0

# Nearest-N selection tuning (stable, no boundary flicker).
# Margins scale with the user's radius so a small radius stays strict
# while the default 40 NM keeps full anti-pop protection.
RADIUS_GRACE_NM = 5.0   # cap: injected aircraft linger this far past radius
SELECT_HYST_NM = 2.0    # cap: injected aircraft score this much closer

# If MSFS hasn't assigned an ObjectID within this many seconds of the spawn
# request, drop the pending entry so the next sync cycle retries it. Prevents
# phantom aircraft that occupy a slot forever without ever existing in sim.
SPAWN_TIMEOUT_S = 20.0

ELEVATEX_FEED_URL = "https://api.elevatex.app/live/elvtx-traffic.json"

# The ElevateX feed is ENTIRELY metric: position altitude and
# airport elevations are reported in meters (verified: EDDF=111,
# KJFK=4, EGLL=25, KLAX=38, EHAM=-3, KABQ=1632 - all meters).
# MSFS works in feet, so convert on ingest.
METERS_TO_FEET = 3.28084

# `groundSpeed` is metric too: METRES PER SECOND (not knots). Verified
# against the feed itself: sampling twice and dividing the great-circle
# distance moved by the sample interval reproduces groundSpeed exactly,
# in m/s, for every flight tested. Sanity: cruise median is ~240 m/s
# (= 466 kt) and the fastest jet-stream flight ~300 m/s (= 583 kt);
# reading those as knots would mean airliners cruising at 240 kt.
# The old code did exactly that, so every aircraft flew at 51 % of its
# real speed - a 150 kt take-off roll rendered as ~77 kt.
MPS_TO_KNOTS = 1.943844


class ElevateXFeed:
    """Client for fetching and parsing the ElevateX live traffic feed."""

    def __init__(self, feed_url: str = ELEVATEX_FEED_URL):
        self.feed_url = feed_url
        self.flights: Dict[str, TrafficFlight] = {}
        self.last_fetch_time: float = 0.0
        self.lock = threading.Lock()

    def snapshot(self) -> List[TrafficFlight]:
        """Thread-safe copy of current flights for the tracking loop."""
        with self.lock:
            return list(self.flights.values())

    def fetch(self) -> Dict[str, TrafficFlight]:
        """Fetch latest live traffic from ElevateX CDN/API."""
        req = urllib.request.Request(
            self.feed_url,
            headers={"User-Agent": "ElevateXLiveTrafficInjector/1.0"}
        )
        now_mono = time.monotonic()
        
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            print(f"[Feed] Error fetching ElevateX feed: {e}", file=sys.stderr)
            return self.flights

        active_ids: Set[str] = set()

        for item in data:
            flight_id = item.get("id")
            if not flight_id:
                continue

            pos_raw = item.get("position")
            if not pos_raw:
                continue

            # Parse position. Prefer altitudeCalibrated (QNH-corrected
            # pressure altitude) over raw altitude - they can differ by
            # hundreds of feet and the calibrated value is what the
            # aircraft is actually flying. BOTH are in METERS.
            alt_raw = pos_raw.get("altitudeCalibrated")
            if alt_raw is None:
                alt_raw = pos_raw.get("altitude", 0.0)
            pos = ElevateXPosition(
                latitude=float(pos_raw.get("latitude", 0.0)),
                longitude=float(pos_raw.get("longitude", 0.0)),
                altitude_ft=float(alt_raw or 0.0) * METERS_TO_FEET,
                ground_speed_kt=float(pos_raw.get("groundSpeed") or 0.0) * MPS_TO_KNOTS,
                heading_deg=float(pos_raw.get("heading", 0.0)),
                vertical_speed_fpm=float(pos_raw.get("verticalSpeed", 0.0)),
                xpdr=int(pos_raw.get("xpdr", 2000)),
                created_at=float(pos_raw.get("createdAt", 0.0))
            )

            # Extract aircraft info
            ac_info = item.get("aircraft") or {}
            ac_icao = item.get("aircraftIcao") or ac_info.get("typeCode") or ""
            ac_reg = item.get("aircraftRegistration") or ac_info.get("registration") or ""
            
            airline_info = item.get("operatedBy") or ac_info.get("airline") or {}
            airline_icao = airline_info.get("icao") or ""
            airline_name = airline_info.get("name") or ""

            origin_info = item.get("origin") or {}
            dest_info = item.get("destination") or {}

            # ElevateX pilot identity (used to detect the user's OWN flight)
            user_info = item.get("user") or {}
            flight_user_id = str(item.get("userId") or user_info.get("userId") or user_info.get("id") or "")
            flight_user_name = str(user_info.get("username") or "")

            # Construct / update flight record (thread-safe: poller thread
            # mutates while the tracking loop reads via snapshot()).
            with self.lock:
                if flight_id in self.flights:
                    flight = self.flights[flight_id]
                    # If position timestamp changed, record prev position
                    if pos.created_at != flight.position.created_at:
                        flight.prev_position = flight.position
                        flight.prev_sample_time = flight.sample_time
                        flight.position = pos
                        flight.sample_time = now_mono

                        # Derive vertical speed from the altitude delta
                        # between samples (the feed's verticalSpeed is
                        # always 0). Smooth it across samples so GPS
                        # jitter doesn't make the nose pitch around.
                        dt = pos.created_at - flight.prev_position.created_at
                        if dt > 0.0:
                            derived = ((pos.altitude_ft
                                        - flight.prev_position.altitude_ft)
                                       / dt * 60.0)
                            derived = max(-6000.0, min(6000.0, derived))
                            if abs(derived) < 100.0:
                                derived = 0.0  # noise floor
                            # Fast attack, slow release: tracks real
                            # climbs/descents, ignores one-off spikes
                            flight.vs_fpm = (0.65 * derived
                                             + 0.35 * flight.vs_fpm)
                        # Feed the delayed-interpolation track (NEW sample)
                        flight.track.push(
                            Sample(pos.created_at, pos.latitude,
                                   pos.longitude, pos.altitude_ft,
                                   pos.heading_deg, pos.ground_speed_kt,
                                   flight.vs_fpm),
                            arrival=now_mono)
                    # Keep pilot identity fresh (userId can appear after start)
                    if flight_user_id and not flight.user_id:
                        flight.user_id = flight_user_id
                    if flight_user_name and not flight.user_name:
                        flight.user_name = flight_user_name
                else:
                    flight = TrafficFlight(
                        id=flight_id,
                        callsign=item.get("callsign") or f"FLT{flight_id[:4]}",
                        flight_number=item.get("flightNumber") or "",
                        aircraft_icao=ac_icao.upper(),
                        registration=ac_reg.upper(),
                        airline_icao=airline_icao.upper(),
                        airline_name=airline_name,
                        origin_icao=(origin_info.get("icao") or "").upper(),
                        origin_lat=float(origin_info.get("latitude", 0.0)),
                        origin_lon=float(origin_info.get("longitude", 0.0)),
                        origin_elev_ft=float(origin_info.get("elevation", 0.0) or 0.0) * METERS_TO_FEET,
                        dest_icao=(dest_info.get("icao") or "").upper(),
                        dest_lat=float(dest_info.get("latitude", 0.0)),
                        dest_lon=float(dest_info.get("longitude", 0.0)),
                        dest_elev_ft=float(dest_info.get("elevation", 0.0) or 0.0) * METERS_TO_FEET,
                        dep_gate=item.get("plannedDepGate") or item.get("actualDepGate") or "",
                        arr_gate=item.get("plannedArrGate") or item.get("actualArrGate") or "",
                        simulator=item.get("simulator") or "MSFS",
                        completed=bool(item.get("completed", False)),
                        cancelled=bool(item.get("cancelled", False)),
                        position=pos,
                        sample_time=now_mono,
                        user_id=flight_user_id,
                        user_name=flight_user_name,
                    )
                    self.flights[flight_id] = flight
                    # First sample for the delayed-interpolation track
                    flight.track.push(
                        Sample(pos.created_at, pos.latitude,
                               pos.longitude, pos.altitude_ft,
                               pos.heading_deg, pos.ground_speed_kt,
                               flight.vs_fpm),
                        arrival=now_mono)

                if not flight.completed and not flight.cancelled:
                    active_ids.add(flight_id)

        # Remove stale or ended flights (AFTER the whole feed is parsed)
        with self.lock:
            stale = [fid for fid in self.flights if fid not in active_ids]
            for fid in stale:
                del self.flights[fid]

            self.last_fetch_time = now_mono
            return dict(self.flights)


# ==============================================================================
# DEAD RECKONING & POSITION INTERPOLATION
# ==============================================================================

def haversine_distance_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance in Nautical Miles between two lat/lon coordinates."""
    r_nm = 3440.065  # Earth radius in NM
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return r_nm * c


def dead_reckon_position(flight: TrafficFlight, current_time: float) -> Tuple[float, float, float, float, float, bool]:
    """
    Project position forward using ground speed, heading, and vertical speed.
    Returns: (lat, lon, alt_ft, heading_deg, ground_speed_kt, is_on_ground)
    """
    pos = flight.position
    elapsed = max(0.0, current_time - flight.sample_time)

    # Cap extrapolation to max 120 seconds to prevent runaways
    elapsed = min(elapsed, 120.0)

    # Vertical speed: derive it from the altitude delta between
    # feed samples (the feed's own verticalSpeed field is 0).
    vs_fpm = estimate_vertical_speed_fpm(flight)

    # On-ground check using height above the departure/destination field
    # (NOT raw MSL altitude, which breaks at high-elevation airports).
    agl_estimates = [pos.altitude_ft - flight.origin_elev_ft,
                     pos.altitude_ft - flight.dest_elev_ft]
    # Only trust field elevations that look real (> -100 ft sanity floor)
    known = [a for a, e in
             ((agl_estimates[0], flight.origin_elev_ft),
              (agl_estimates[1], flight.dest_elev_ft)) if e > -100.0]
    min_agl = min(known) if known else pos.altitude_ft
    is_on_ground = (pos.ground_speed_kt < 50.0
                    and abs(vs_fpm) < 400.0
                    and min_agl < 400.0)

    if is_on_ground or elapsed < 0.1:
        return (pos.latitude, pos.longitude, pos.altitude_ft, pos.heading_deg, pos.ground_speed_kt, is_on_ground)

    # Project position
    # Distance in NM = knots * hours
    dist_nm = pos.ground_speed_kt * (elapsed / 3600.0)
    
    # Spherical dead reckoning
    lat1 = math.radians(pos.latitude)
    lon1 = math.radians(pos.longitude)
    brng = math.radians(pos.heading_deg)
    d_r = dist_nm / 3440.065  # angular distance in radians

    lat2 = math.asin(math.sin(lat1) * math.cos(d_r) +
                     math.cos(lat1) * math.sin(d_r) * math.cos(brng))
    lon2 = lon1 + math.atan2(math.sin(brng) * math.sin(d_r) * math.cos(lat1),
                             math.cos(d_r) - math.sin(lat1) * math.sin(lat2))

    proj_lat = math.degrees(lat2)
    proj_lon = math.degrees(lon2)

    # Altitude adjustment using vertical speed (capped so a stale climb rate
    # can't fling the aircraft thousands of feet between samples).
    proj_alt = pos.altitude_ft + (vs_fpm * (elapsed / 60.0))
    proj_alt = max(0.0, min(proj_alt, pos.altitude_ft + 2500.0))
    proj_alt = max(proj_alt, pos.altitude_ft - 2500.0)

    return (proj_lat, proj_lon, proj_alt, pos.heading_deg, pos.ground_speed_kt, is_on_ground)


def _smooth_factor(dt: float, tau: float) -> float:
    """Frame-rate-independent exponential smoothing factor.

    tau = time constant in seconds. Same feel at 10 Hz or 60 Hz, on any PC.
    """
    dt = max(0.0, min(dt, 1.0))
    return 1.0 - math.exp(-dt / max(tau, 1e-3))


def _lerp_angle_deg(a: float, b: float, k: float) -> float:
    """Shortest-path interpolation between two headings in degrees."""
    d = ((b - a + 540.0) % 360.0) - 180.0
    return (a + d * k) % 360.0


def estimate_vertical_speed_fpm(flight: TrafficFlight) -> float:
    """Smoothed vertical speed (fpm), derived in the feed parser
    from the altitude delta between samples (the feed's own
    verticalSpeed field is always 0).
    """
    return flight.vs_fpm


def compute_attitude(flight: TrafficFlight) -> Tuple[float, float]:
    """Estimate pitch (deg) and bank (deg) so injected AI aircraft
    nose over on climbs/descents and bank into turns like real traffic
    (the same technique vPilot / xPilot / IVAO injectors use).

    Pitch  <- smoothed vertical speed. Climbs are steeper than
              descents (an airliner climbs at ~10° but descends
              at only ~2-3°), so climb/descent get different scaling.
    Bank   <- heading rate between feed samples, scaled by speed
              (a standard-rate turn needs more bank at high speed).
    """
    pos = flight.position

    # ── Pitch: separate climb / descent scaling ──
    vs_fpm = estimate_vertical_speed_fpm(flight)
    if pos.ground_speed_kt < 50.0 or abs(vs_fpm) < 100.0:
        pitch = 0.0  # parked, taxiing, or level cruise
    elif vs_fpm > 0.0:
        # Climb: ~250 fpm per degree (2500 fpm -> 10°)
        pitch = min(10.0, vs_fpm / 250.0)
    else:
        # Descent: shallower, ~400 fpm per degree
        # (-1500 fpm -> -3.75°, typical idle-descent attitude)
        pitch = max(-5.0, vs_fpm / 400.0)

    # ── Bank: heading rate, scaled by airspeed ──
    bank = 0.0
    prev = flight.prev_position
    if prev is not None:
        dt = pos.created_at - prev.created_at
        if dt > 0.0:
            hdg_diff = ((pos.heading_deg - prev.heading_deg + 180.0)
                        % 360.0) - 180.0
            rate = hdg_diff / dt  # degrees per second
            # Standard-rate turn (3°/s) ≈ 25° bank at 250 kt;
            # faster aircraft bank more for the same rate.
            factor = 7.0 + pos.ground_speed_kt / 150.0
            bank = max(-30.0, min(30.0, rate * factor))

    return pitch, bank


# ==============================================================================
# FSLTL & AI MODEL CATALOG SCANNER
# ==============================================================================

# Feed ICAO type code -> FSLTL catalog keys (atc_model codes found in FSLTL aircraft.cfg)
MODEL_ALIASES = {
    # Airbus narrowbody
    "A19N": ["A319", "A320"],
    "A20N": ["A320", "A21N", "A319"],
    "A21N": ["A321", "A320"],
    "A318": ["A319", "A320"],
    "A319": ["A319", "A320"],
    "A320": ["A320", "A20N"],
    "A321": ["A321", "A320"],
    # Airbus A220 family (atc_model BCS1/BCS3)
    "A220": ["A220"],
    "A221": ["A220"],
    "A223": ["A220"],
    # Airbus widebody
    "A332": ["A330"],
    "A333": ["A330"],
    "A338": ["A330"],
    "A339": ["A330"],
    "A342": ["A343"],
    "A343": ["A343"],
    "A345": ["A346"],
    "A346": ["A346"],
    "A358": ["A350"],
    "A359": ["A350"],
    "A35K": ["A350"],
    "A388": ["A380"],
    # Boeing 737 (FSLTL NG pack atc_model "737"; classic "B733"/"B734")
    "B37M": ["737"],
    "B38M": ["737"],
    "B39M": ["737"],
    "B735": ["B733", "737"],
    "B736": ["B733", "737"],
    "B737": ["737", "B733"],
    "B738": ["737", "B733"],
    "B739": ["737"],
    "B733": ["B733"],
    "B734": ["B734"],
    # Boeing widebody
    "B772": ["777"],
    "B773": ["777"],
    "B77L": ["777"],
    "B77W": ["777"],
    "B788": ["TT:ATCCOM.AC_MODEL B788.0.TEXT", "787"],
    "B789": ["787"],
    "B78X": ["787"],
    "B744": ["747"],
    "B748": ["747"],
    "B742": ["747"],
    "B743": ["747"],
    "B74I": ["747"],
    "B762": ["767"],
    "B763": ["767"],
    "B764": ["767"],
    # Boeing 757 has no FSLTL model; use the closest narrowbody so the flight
    # still appears instead of vanishing from the sky.
    "B752": ["A321", "737"],
    "B753": ["A321", "737"],
    # MD-80/90 and DC-9 family: no FSLTL model either.
    "MD81": ["A320", "737"],
    "MD82": ["A320", "737"],
    "MD83": ["A320", "737"],
    "MD87": ["A320", "737"],
    "MD88": ["A320", "737"],
    "MD90": ["A320", "737"],
    "DC93": ["A320", "737"],
    "DC94": ["A320", "737"],
    "DC95": ["A320", "737"],
    "B712": ["E190", "CRJ7"],
    "A30B": ["TT:ATCCOM.AC_MODEL_A300.0.TEXT"],
    "A306": ["TT:ATCCOM.AC_MODEL_A300.0.TEXT"],
    "A310": ["TT:ATCCOM.AC_MODEL_A300.0.TEXT"],
    # Regional jets / turboprops
    "E170": ["E170"],
    "E175": ["E170"],
    "E190": ["E190"],
    "E195": ["E190"],
    "CRJ2": ["CRJ7"],
    "CRJ7": ["CRJ7"],
    "CRJ9": ["CRJ7"],
    "CRJX": ["CRJ7"],
    "AT42": ["ATR42"],
    "AT43": ["ATR42"],
    "AT45": ["ATR42"],
    "AT46": ["ATR42"],
    "AT72": ["ATR76", "ATR75"],
    "AT75": ["ATR75", "ATR76"],
    "AT76": ["ATR76"],
    "DH8A": ["DASH 8"],
    "DH8B": ["DASH 8"],
    "DH8C": ["DASH 8"],
    "DH8D": ["DASH 8"],
    "SF34": ["SAAB 340"],
    # Extra feed types seen live
    "BCS1": ["A220"],
    "BCS3": ["A220"],
    "MD11": ["MD-11"],
    "MD1F": ["MD-11"],
    "E75L": ["E170"],
    "E75S": ["E170"],
    "C700": ["TT:ATCCOM.AC_MODEL_C25C.0.TEXT"],
    "C750": ["TT:ATCCOM.AC_MODEL_C25C.0.TEXT"],
    "C25A": ["TT:ATCCOM.AC_MODEL_C25C.0.TEXT"],
    "C25B": ["TT:ATCCOM.AC_MODEL_C25C.0.TEXT"],
    "C25C": ["TT:ATCCOM.AC_MODEL_C25C.0.TEXT"],
    "CL60": ["TT:ATCCOM.AC_MODEL_C25C.0.TEXT"],
    "LJ25": ["LEARJET"],
    "LJ35": ["LEARJET"],
    "LJ45": ["LEARJET"],
    "LJ55": ["LEARJET"],
    "LJ60": ["LEARJET"],
    "LJ75": ["LEARJET"],
    "C208": ["TT:ATCCOM.AC_MODEL C208.0.TEXT"],
    "B350": ["TT:ATCCOM.AC_MODEL B350.0.TEXT"],
    "BE20": ["TT:ATCCOM.AC_MODEL B350.0.TEXT"],
    "M600": ["TT:ATCCOM.AC_MODEL_TBM9.0.TEXT"],
    "PA32": ["CHEROKEE"],
    "PA28": ["CHEROKEE"],
    "PA44": ["CHEROKEE"],
    "DA40": ["TT:ATCCOM.AC_MODEL_DA40.0.TEXT"],
    "DA62": ["TT:ATCCOM.AC_MODEL_DA62.0.TEXT"],
    "DR40": ["TT:ATCCOM.AC_MODEL_DR40.0.TEXT"],
    "TBM7": ["TT:ATCCOM.AC_MODEL_TBM9.0.TEXT"],
    "TBM8": ["TT:ATCCOM.AC_MODEL_TBM9.0.TEXT"],
    "TBM9": ["TT:ATCCOM.AC_MODEL_TBM9.0.TEXT"],
    "PC12": ["TT:ATCCOM.AC_MODEL_TBM9.0.TEXT"],
    "PC24": ["TT:ATCCOM.AC_MODEL_C25C.0.TEXT"],
    "SF50": ["TT:ATCCOM.AC_MODEL_C25C.0.TEXT"],
}


# Feed airline ICAO code -> FSLTL livery airline code (only when visually identical brand)
AIRLINE_CODE_ALIASES = {
    "EJU": "EZY",  # easyJet Europe -> easyJet UK paint (identical livery)
}

# Known airliner families that may fall back to a close relative if the exact
# type isn't installed. GA / bizjet / regional types are NOT here on purpose -
# we never guess those as an A320.
AirlinerFallback = {
    "A318": ["A319", "A320"],
    "A319": ["A320"],
    "A320": ["A321"],
    "A321": ["A320"],
    "A20N": ["A320"],
    "A21N": ["A321"],
    "A19N": ["A319"],
    "A332": ["A330"],
    "A333": ["A330"],
    "A338": ["A330"],
    "A339": ["A330"],
    "A342": ["A343"],
    "A345": ["A346"],
    "A358": ["A350"],
    "A359": ["A350"],
    "A35K": ["A350"],
    "B772": ["777"],
    "B773": ["777"],
    "B77L": ["777"],
    "B77W": ["777"],
    "B788": ["787"],
    "B789": ["787"],
    "B78X": ["787"],
    "B735": ["737"],
    "B736": ["737"],
    "B737": ["B738"],
    "B738": ["B737"],
    "B739": ["737"],
    "B742": ["747"],
    "B743": ["747"],
    "B744": ["747"],
    "B748": ["747"],
    "B762": ["767"],
    "B763": ["767"],
    "E170": ["E190"],
    "E175": ["E190"],
    "E190": ["E170"],
    "E195": ["E190"],
    "E75L": ["E170"],
    "CRJ2": ["CRJ7"],
    "CRJ9": ["CRJ7"],
    "CRJX": ["CRJ7"],
}

# ---------------------------------------------------------------------------
# Closest-model fallback.
#
# The FSLTL base does not contain every aircraft type, and some feeds use
# types the base has never modelled (757, MD-80, ...). Rather than drop those
# flights, pick the geometrically closest type that IS installed: similar
# wingspan, length, seat count and class, preferring the same manufacturer.
# Tuple: (wingspan m, length m, class, seats, engines, manufacturer)
# ---------------------------------------------------------------------------
AIRCRAFT_SPECS = {
    # Installed family keys
    "737": (35.8, 39.5, "narrow", 189, 2, "boeing"),
    "B733": (28.9, 33.4, "narrow", 130, 2, "boeing"),
    "B734": (28.9, 36.5, "narrow", 150, 2, "boeing"),
    "747": (64.4, 70.6, "wide", 410, 4, "boeing"),
    "767": (47.6, 54.9, "wide", 269, 2, "boeing"),
    "777": (60.9, 73.9, "wide", 396, 2, "boeing"),
    "787": (60.1, 62.8, "wide", 290, 2, "boeing"),
    "A220": (35.1, 38.7, "narrow", 141, 2, "airbus"),
    "A319": (35.8, 33.8, "narrow", 140, 2, "airbus"),
    "A320": (35.8, 37.6, "narrow", 180, 2, "airbus"),
    "A321": (35.8, 44.5, "narrow", 220, 2, "airbus"),
    "A330": (60.3, 58.8, "wide", 277, 2, "airbus"),
    "A343": (60.3, 63.7, "wide", 295, 4, "airbus"),
    "A346": (63.4, 75.4, "wide", 380, 4, "airbus"),
    "A350": (64.8, 66.8, "wide", 350, 2, "airbus"),
    "A380": (79.8, 72.7, "wide", 555, 4, "airbus"),
    "MD-11": (51.7, 61.6, "wide", 293, 3, "mcdonnell"),
    "SU95": (27.8, 29.9, "regional", 98, 2, "sukhoi"),
    "E170": (26.0, 29.9, "regional", 76, 2, "embraer"),
    "E190": (28.7, 36.2, "regional", 100, 2, "embraer"),
    "CRJ7": (23.2, 32.5, "regional", 78, 2, "bombardier"),
    "ATR42": (24.6, 22.7, "turboprop", 48, 2, "atr"),
    "ATR75": (27.1, 27.2, "turboprop", 72, 2, "atr"),
    "ATR76": (27.1, 27.2, "turboprop", 72, 2, "atr"),
    "DASH 8": (27.4, 32.8, "turboprop", 70, 2, "dehavilland"),
    "SAAB 340": (21.4, 19.7, "turboprop", 34, 2, "saab"),
    "CHEROKEE": (10.9, 7.5, "ga", 4, 1, "piper"),
    "LEARJET": (13.7, 14.8, "bizjet", 8, 2, "learjet"),
    "VL3": (7.9, 6.2, "light", 2, 1, "vl3"),
    # Feed types
    "A318": (34.1, 31.4, "narrow", 120, 2, "airbus"),
    "A19N": (35.8, 33.8, "narrow", 140, 2, "airbus"),
    "A20N": (35.8, 37.6, "narrow", 180, 2, "airbus"),
    "A21N": (35.8, 44.5, "narrow", 220, 2, "airbus"),
    "A332": (60.3, 58.8, "wide", 277, 2, "airbus"),
    "A333": (60.3, 63.7, "wide", 295, 2, "airbus"),
    "A338": (64.0, 63.7, "wide", 300, 2, "airbus"),
    "A339": (64.0, 63.7, "wide", 300, 2, "airbus"),
    "A342": (60.3, 59.4, "wide", 260, 4, "airbus"),
    "A345": (63.4, 67.9, "wide", 320, 4, "airbus"),
    "A358": (64.8, 60.5, "wide", 310, 2, "airbus"),
    "A359": (64.8, 66.8, "wide", 350, 2, "airbus"),
    "A35K": (64.8, 73.8, "wide", 410, 2, "airbus"),
    "A388": (79.8, 72.7, "wide", 555, 4, "airbus"),
    "B37M": (35.9, 35.6, "narrow", 170, 2, "boeing"),
    "B38M": (35.9, 39.5, "narrow", 189, 2, "boeing"),
    "B39M": (35.9, 42.1, "narrow", 220, 2, "boeing"),
    "B736": (34.3, 31.2, "narrow", 110, 2, "boeing"),
    "B737": (34.3, 33.4, "narrow", 130, 2, "boeing"),
    "B738": (35.8, 39.5, "narrow", 189, 2, "boeing"),
    "B739": (35.8, 42.1, "narrow", 220, 2, "boeing"),
    "B741": (59.6, 70.7, "wide", 370, 4, "boeing"),
    "B742": (59.6, 70.7, "wide", 370, 4, "boeing"),
    "B743": (59.6, 70.7, "wide", 400, 4, "boeing"),
    "B744": (64.4, 70.6, "wide", 410, 4, "boeing"),
    "B748": (68.5, 76.3, "wide", 467, 4, "boeing"),
    "B752": (38.0, 47.3, "narrow", 200, 2, "boeing"),
    "B753": (38.0, 54.5, "narrow", 243, 2, "boeing"),
    "B762": (47.6, 48.5, "wide", 220, 2, "boeing"),
    "B763": (47.6, 54.9, "wide", 269, 2, "boeing"),
    "B764": (51.9, 61.4, "wide", 304, 2, "boeing"),
    "B772": (60.9, 63.7, "wide", 312, 2, "boeing"),
    "B773": (60.9, 73.9, "wide", 396, 2, "boeing"),
    "B77L": (64.8, 63.7, "wide", 317, 2, "boeing"),
    "B77W": (64.8, 73.9, "wide", 396, 2, "boeing"),
    "B788": (60.1, 56.7, "wide", 250, 2, "boeing"),
    "B789": (60.1, 62.8, "wide", 290, 2, "boeing"),
    "B78X": (60.1, 68.3, "wide", 330, 2, "boeing"),
    "MD81": (32.9, 45.1, "narrow", 155, 2, "mcdonnell"),
    "MD82": (32.9, 45.1, "narrow", 155, 2, "mcdonnell"),
    "MD83": (32.9, 45.1, "narrow", 165, 2, "mcdonnell"),
    "MD87": (32.9, 45.1, "narrow", 130, 2, "mcdonnell"),
    "MD88": (32.9, 45.1, "narrow", 155, 2, "mcdonnell"),
    "MD90": (32.9, 46.5, "narrow", 172, 2, "mcdonnell"),
    "DC93": (28.4, 36.6, "narrow", 115, 2, "mcdonnell"),
    "DC94": (32.9, 37.8, "narrow", 125, 2, "mcdonnell"),
    "DC95": (32.9, 40.7, "narrow", 130, 2, "mcdonnell"),
    "B712": (28.5, 37.8, "regional", 100, 2, "boeing"),
    "CRJ2": (21.2, 26.8, "regional", 50, 2, "bombardier"),
    "CRJ9": (24.9, 36.4, "regional", 90, 2, "bombardier"),
    "CRJX": (26.2, 39.1, "regional", 104, 2, "bombardier"),
    "E175": (26.0, 31.7, "regional", 88, 2, "embraer"),
    "E195": (28.7, 38.6, "regional", 118, 2, "embraer"),
    "E75L": (26.0, 31.7, "regional", 88, 2, "embraer"),
    "E75S": (26.0, 31.7, "regional", 88, 2, "embraer"),
    "AT42": (24.6, 22.7, "turboprop", 48, 2, "atr"),
    "AT43": (24.6, 22.7, "turboprop", 48, 2, "atr"),
    "AT45": (24.6, 22.7, "turboprop", 48, 2, "atr"),
    "AT46": (24.6, 22.7, "turboprop", 48, 2, "atr"),
    "AT72": (27.1, 27.2, "turboprop", 72, 2, "atr"),
    "AT75": (27.1, 27.2, "turboprop", 72, 2, "atr"),
    "DH8A": (25.9, 22.3, "turboprop", 39, 2, "dehavilland"),
    "DH8B": (25.9, 22.3, "turboprop", 39, 2, "dehavilland"),
    "DH8C": (27.4, 25.7, "turboprop", 50, 2, "dehavilland"),
    "DH8D": (27.4, 32.8, "turboprop", 70, 2, "dehavilland"),
    "SF34": (21.4, 19.7, "turboprop", 34, 2, "saab"),
    "BE20": (16.6, 13.4, "turboprop", 9, 2, "beechcraft"),
    "B350": (17.7, 14.4, "turboprop", 11, 2, "beechcraft"),
    "PC12": (16.3, 14.4, "turboprop", 6, 1, "pilatus"),
    "C208": (15.9, 11.7, "ga", 12, 1, "cessna"),
    "C172": (11.0, 8.3, "ga", 4, 1, "cessna"),
    "C25A": (15.7, 14.4, "bizjet", 8, 2, "cessna"),
    "C25B": (15.7, 14.4, "bizjet", 8, 2, "cessna"),
    "C25C": (15.7, 14.4, "bizjet", 8, 2, "cessna"),
    "C56X": (17.2, 15.9, "bizjet", 9, 2, "cessna"),
    "C700": (19.3, 17.8, "bizjet", 10, 2, "cessna"),
    "C750": (19.3, 17.8, "bizjet", 10, 2, "cessna"),
    "LJ25": (13.7, 14.8, "bizjet", 8, 2, "learjet"),
    "LJ35": (13.7, 14.8, "bizjet", 8, 2, "learjet"),
    "LJ45": (13.7, 14.8, "bizjet", 8, 2, "learjet"),
    "LJ55": (13.7, 14.8, "bizjet", 8, 2, "learjet"),
    "LJ60": (13.7, 14.8, "bizjet", 8, 2, "learjet"),
    "LJ75": (13.7, 14.8, "bizjet", 8, 2, "learjet"),
    "CL60": (19.6, 20.9, "bizjet", 12, 2, "bombardier"),
    "TBM7": (12.8, 10.7, "turboprop", 6, 1, "daher"),
    "TBM8": (12.8, 10.7, "turboprop", 6, 1, "daher"),
    "TBM9": (12.8, 10.7, "turboprop", 6, 1, "daher"),
    "PA28": (10.9, 7.5, "ga", 4, 1, "piper"),
    "PA32": (12.2, 8.6, "ga", 6, 1, "piper"),
    "PA44": (11.8, 8.4, "ga", 4, 2, "piper"),
    "DA40": (12.0, 8.0, "ga", 4, 1, "diamond"),
    "DA62": (14.5, 9.2, "ga", 7, 2, "diamond"),
    "DR40": (8.7, 7.0, "ga", 4, 1, "robin"),
    "SF50": (11.7, 9.4, "bizjet", 7, 1, "cirrus"),
    "PC24": (17.0, 16.8, "bizjet", 10, 2, "pilatus"),
    "A30B": (44.8, 54.0, "wide", 266, 2, "airbus"),
    "A306": (44.8, 54.0, "wide", 266, 2, "airbus"),
    "A310": (43.9, 46.7, "wide", 220, 2, "airbus"),
    "BE36": (10.2, 8.4, "ga", 4, 1, "beechcraft"),
    "SR22": (11.7, 7.9, "ga", 4, 1, "cirrus"),
    "SR20": (11.7, 7.9, "ga", 4, 1, "cirrus"),
}

_TT_SPEC_RE = re.compile(r"AC_MODEL[ _]([A-Z0-9 .-]+?)\.0", re.I)


def _spec_code(code: str) -> str:
    """Normalize a catalog key / ICAO code; unwrap MSFS 'TT:ATCCOM...' keys."""
    text = (code or "").strip()
    if text.upper().startswith("TT:ATCCOM"):
        m = _TT_SPEC_RE.search(text)
        if m:
            return m.group(1).strip().upper()
    return text.upper()


def _spec_for(code: str):
    """(span, length, class, seats, engines, maker) for a type, or None."""
    c = _spec_code(code)
    spec = AIRCRAFT_SPECS.get(c)
    if spec:
        return spec
    # Family inference for types that are not in the table (new variants etc.)
    if c.startswith(("B73", "B37", "B38", "B39")):
        return AIRCRAFT_SPECS["737"]
    if c.startswith("B74"):
        return AIRCRAFT_SPECS["747"]
    if c.startswith("B75"):
        return AIRCRAFT_SPECS["B752"]
    if c.startswith("B76"):
        return AIRCRAFT_SPECS["767"]
    if c.startswith("B77"):
        return AIRCRAFT_SPECS["777"]
    if c.startswith("B78"):
        return AIRCRAFT_SPECS["787"]
    if c.startswith("A31"):
        return AIRCRAFT_SPECS["A319"]
    if c.startswith("A32"):
        return AIRCRAFT_SPECS["A320"]
    if c.startswith("A33"):
        return AIRCRAFT_SPECS["A330"]
    if c.startswith("A34"):
        return AIRCRAFT_SPECS["A343"]
    if c.startswith("A35"):
        return AIRCRAFT_SPECS["A350"]
    if c.startswith("A38"):
        return AIRCRAFT_SPECS["A380"]
    if c.startswith(("E17", "E19", "E7")):
        return AIRCRAFT_SPECS["E190"]
    if c.startswith("CRJ"):
        return AIRCRAFT_SPECS["CRJ7"]
    if c.startswith(("RJ", "B46", "F70", "F10", "F50", "E13", "E14", "ERJ")):
        return AIRCRAFT_SPECS["CRJ7"]
    if c.startswith(("IL7", "IL8", "IL9", "C17", "C5", "KC1", "AN1", "AN2",
                     "A40")):
        return AIRCRAFT_SPECS["767"]
    if c.startswith(("C13", "CN35", "C30")):
        return AIRCRAFT_SPECS["ATR76"]
    if c.startswith(("GLF", "GLEX", "G55", "G65", "FA7", "FA8", "F900",
                     "E55", "E50", "HDJT", "C68", "C56")):
        return AIRCRAFT_SPECS["C25C"]
    if c.startswith(("MD8", "MD9", "DC9")):
        return AIRCRAFT_SPECS["MD88"]
    if c.startswith("AT"):
        return AIRCRAFT_SPECS["ATR76"]
    if c.startswith("DH8"):
        return AIRCRAFT_SPECS["DASH 8"]
    return None


def _type_distance(a, b) -> float:
    """How far apart two aircraft specs are (0 = identical)."""
    def lr(x, y):
        return math.log(max(x, 1.0) / max(y, 1.0)) / 0.22

    d = lr(a[0], b[0]) ** 2 + lr(a[1], b[1]) ** 2
    d += ((a[3] - b[3]) / 90.0) ** 2
    if a[2] != b[2]:
        pair = {a[2], b[2]}
        if pair <= {"wide", "narrow"}:
            d += 1.6
        elif pair <= {"narrow", "regional"}:
            d += 0.7
        elif pair <= {"regional", "turboprop"}:
            d += 0.7
        elif pair <= {"bizjet", "ga", "light"}:
            d += 0.8
        else:
            d += 2.5
    if a[4] != b[4]:
        d += 0.6
    if a[5] != b[5]:
        d += 0.25
    return d


def nearest_available_type(icao: str, models_by_icao):
    """Closest type that is actually installed, or None."""
    target = _spec_for(icao)
    if target is None:
        return None
    best, best_d = None, None
    for key, liveries in models_by_icao.items():
        if not liveries:
            continue
        spec = _spec_for(key)
        if spec is None:
            continue
        d = _type_distance(target, spec)
        if best_d is None or d < best_d:
            best, best_d = key, d
    return best

# FSLTL livery titles: FSLTL_<TYPE>_<AIRLINE-CODE>_<Name>-STUB
_FSLTL_TITLE_RE = re.compile(r"^FSLTL_[A-Z0-9]+_([A-Z0-9]{3})_")


def _livery_airline_code(title: str) -> str:
    """Extract the 3-letter airline code from an FSLTL livery title, if any.

    Handles both formats seen in the wild:
      - underscore: "FSLTL_A320_EZY_..." (base + some packs)
      - spaces:     "FSLTL A20N BAW British Airways" (real livery folders)
    """
    up = title.upper()
    m = _FSLTL_TITLE_RE.match(up)
    if m:
        return m.group(1)
    parts = up.split()
    if (len(parts) >= 3 and parts[0] == "FSLTL"
            and re.fullmatch(r"[A-Z0-9]{2,4}", parts[1])
            and re.fullmatch(r"[A-Z0-9]{3}", parts[2])):
        return parts[2]
    return ""


def _norm_reg(reg: str) -> str:
    """Normalize a tail number for comparison.

    N123AB == N-123AB == N 123AB. Only exact (normalized) tail
    matches ever exclude an aircraft, so neighbours can never be eaten.
    """
    return re.sub(r"[^A-Z0-9]", "", (reg or "").strip().upper())


class ModelCatalog:
    """Catalog of installed AI models, scanning FSLTL / Community folders."""

    def __init__(self, custom_community_path: Optional[str] = None):
        self.models_by_icao: Dict[str, List[Tuple[str, str, str]]] = {}  # icao -> [(title, atc_airline, icao_airline)]
        self.overrides: Dict[str, str] = {}  # feed ICAO -> "type:<family>" or "title:<exact>"
        self.title_index: Dict[str, str] = {}  # title -> ICAO family key
        self.scanned_packages: List[str] = []
        self.fsltl_found: bool = False
        self.custom_community_path = custom_community_path
        self.checked_paths: List[str] = []
        self.used_community_path: str = ""
        self.scan()

    def _get_candidate_community_paths(self) -> List[str]:
        candidates = []
        if self.custom_community_path:
            candidates.append(self.custom_community_path)

        # Check FBW installer config
        fbw_cfg = os.path.expandvars(r"%APPDATA%\FlyByWire Installer\config.json")
        if os.path.exists(fbw_cfg):
            try:
                with open(fbw_cfg, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                    sims = cfg.get("mainSettings", {}).get("simulator", {})
                    for sim_key in ["msfs2020", "msfs2024"]:
                        cpath = sims.get(sim_key, {}).get("communityPath")
                        if cpath and os.path.exists(cpath):
                            candidates.append(cpath)
            except Exception:
                pass

        appdata = os.getenv("APPDATA", "")
        localappdata = os.getenv("LOCALAPPDATA", "")
        progfiles_x86 = os.getenv("PROGRAMFILES(X86)", r"C:\Program Files (x86)")

        # UserCfg.opt holds the real InstalledPackagesPath (catches every
        # custom install location for both sims and both store flavours).
        usercfg_files = [
            os.path.join(appdata, r"Microsoft Flight Simulator\UserCfg.opt"),          # Steam 2020
            os.path.join(appdata, r"Microsoft Flight Simulator 2024\UserCfg.opt"),      # Steam 2024
            os.path.join(localappdata, r"Packages\Microsoft.FlightSimulator_8wekyb3d8bbwe\LocalCache\UserCfg.opt"),  # MS Store 2020
            os.path.join(localappdata, r"Packages\Microsoft.Limitless_8wekyb3d8bbwe\LocalCache\UserCfg.opt"),        # MS Store 2024
        ]
        for ucfg in usercfg_files:
            try:
                if os.path.isfile(ucfg):
                    with open(ucfg, "r", encoding="utf-8", errors="ignore") as f:
                        for line in f:
                            s = line.strip()
                            if s.startswith("InstalledPackagesPath"):
                                parts = s.split(None, 1)
                                if len(parts) == 2:
                                    base = parts[1].strip().strip('"')
                                    cpath = os.path.join(base, "Community")
                                    if os.path.isdir(cpath) and cpath not in candidates:
                                        candidates.append(cpath)
                                break
            except Exception:
                pass

        # Standard MSFS Community path patterns
        standard_paths = [
            os.path.join(appdata, r"Microsoft Flight Simulator\Packages\Community"),
            os.path.join(appdata, r"Microsoft Flight Simulator 2024\Packages\Community"),
            os.path.join(localappdata, r"Packages\Microsoft.FlightSimulator_8wekyb3d8bbwe\LocalCache\Packages\Community"),
            os.path.join(localappdata, r"Packages\Microsoft.Limitless_8wekyb3d8bbwe\LocalCache\Packages\Community"),
            # Steam default library
            os.path.join(progfiles_x86, r"Steam\steamapps\common\MicrosoftFlightSimulator\Packages\Community"),
            os.path.join(progfiles_x86, r"Steam\steamapps\common\MicrosoftFlightSimulator2024\Packages\Community"),
        ]
        # Extra Steam libraries / standalone installs across common drives
        for drive in ["C", "D", "E", "F", "G"]:
            standard_paths += [
                rf"{drive}:\SteamLibrary\steamapps\common\MicrosoftFlightSimulator\Packages\Community",
                rf"{drive}:\SteamLibrary\steamapps\common\MicrosoftFlightSimulator2024\Packages\Community",
                rf"{drive}:\Steam\steamapps\common\MicrosoftFlightSimulator\Packages\Community",
                rf"{drive}:\MSFS\Community",
                rf"{drive}:\MSFS2024\Community",
                rf"{drive}:\MicrosoftFlightSimulator\Packages\Community",
            ]
        for p in standard_paths:
            if p and os.path.exists(p) and p not in candidates:
                candidates.append(p)

        # MS Store installs: %LOCALAPPDATA%\Packages\<family>\LocalCache\Packages\Community
        for p in glob.glob(os.path.join(localappdata, "Packages", "*", "LocalCache", "Packages", "Community")):
            if os.path.isdir(p) and p not in candidates:
                candidates.append(p)

        # Remember what we checked (dashboard shows it on failure)
        self.checked_paths = list(candidates)
        return candidates

    def scan(self):
        paths = self._get_candidate_community_paths()
        print(f"[Catalog] Scanning Community paths for AI models...")

        packages: List[Tuple[str, str]] = []  # (community_path, package_name)
        for cpath in paths:
            if not os.path.exists(cpath):
                continue
            print(f"[Catalog] Checking Community path: {cpath}")
            try:
                for pkg_name in sorted(os.listdir(cpath)):
                    sim_airplanes = os.path.join(cpath, pkg_name, "SimObjects", "Airplanes")
                    if os.path.isdir(sim_airplanes):
                        packages.append((cpath, pkg_name))
            except Exception as e:
                print(f"[Catalog] Error scanning {cpath}: {e}", file=sys.stderr)

        # Prefer FSLTL packages (that is what we inject with); if absent, warn.
        fsltl_pkgs = [(c, p) for c, p in packages if "fsltl" in p.lower()]
        if fsltl_pkgs:
            print(f"[Catalog] FSLTL packages found: {', '.join(p for _, p in fsltl_pkgs)}")
            self.fsltl_found = True
            scan_list = fsltl_pkgs
            self.used_community_path = fsltl_pkgs[0][0]
            print(f"[Catalog] Using Community folder: {self.used_community_path}")
        else:
            print("[Catalog] WARNING: FSLTL not found! Install it via the FlyByWire Installer")
            print("[Catalog]          (Installer -> FSLTL -> FSLTL Traffic Base Models -> Install).")
            if not paths:
                print("[Catalog]          No Community folder found at all. Checked:")
                for probe in [
                    os.path.expandvars(r"%APPDATA%\Microsoft Flight Simulator\Packages\Community"),
                    os.path.expandvars(r"%APPDATA%\Microsoft Flight Simulator 2024\Packages\Community"),
                    os.path.expandvars(r"%LOCALAPPDATA%\Packages\Microsoft.FlightSimulator_8wekyb3d8bbwe\LocalCache\Packages\Community"),
                    os.path.expandvars(r"%LOCALAPPDATA%\Packages\Microsoft.Limitless_8wekyb3d8bbwe\LocalCache\Packages\Community"),
                ]:
                    print(f"[Catalog]            - {probe}")
                print("[Catalog]          If MSFS is on another drive, pass --community <path>.")
            self.fsltl_found = False
            # Dry-run insight only: scan any other installed aircraft packages
            scan_list = packages

        for cpath, pkg_name in scan_list:
            self.scanned_packages.append(pkg_name)
            self._scan_airplanes_dir(os.path.join(cpath, pkg_name, "SimObjects", "Airplanes"))

        total_titles = sum(len(v) for v in self.models_by_icao.values())
        print(f"[Catalog] Found {total_titles} AI model liveries across {len(self.models_by_icao)} ICAO types.")

    def _scan_airplanes_dir(self, airplanes_dir: str):
        for item in os.listdir(airplanes_dir):
            ac_dir = os.path.join(airplanes_dir, item)
            cfg_file = os.path.join(ac_dir, "aircraft.cfg")
            if os.path.isfile(cfg_file):
                self._parse_aircraft_cfg(cfg_file)

    def _resolve_base_atc_model(self, cfg_content: str, ac_dir: str) -> str:
        """For [VARIATION] livery folders, resolve base_container and inherit its atc_model."""
        if not hasattr(self, "_base_model_cache"):
            self._base_model_cache = {}
        base_m = re.search(r"base_container\s*=\s*[\"']?([^\"'\r\n;]+)", cfg_content, re.IGNORECASE)
        if not base_m:
            return ""
        rel = base_m.group(1).strip().strip('"').strip("'")
        base_dir = os.path.normpath(os.path.join(ac_dir, rel))
        base_cfg = os.path.join(base_dir, "aircraft.cfg")
        if base_cfg in self._base_model_cache:
            return self._base_model_cache[base_cfg]
        result = ""
        try:
            with open(base_cfg, "r", encoding="utf-8", errors="ignore") as f:
                base_content = f.read()
            m = re.search(r"atc_model\s*=\s*[\"']?([^\"'\r\n;]+)", base_content, re.IGNORECASE)
            if m:
                result = m.group(1).strip().upper()
        except Exception:
            pass
        self._base_model_cache[base_cfg] = result
        return result

    def _parse_aircraft_cfg(self, cfg_file: str):
        try:
            # Read line by line with fallback encodings
            content = ""
            for enc in ["utf-8", "latin-1", "cp1252"]:
                try:
                    with open(cfg_file, "r", encoding=enc) as f:
                        content = f.read()
                    break
                except UnicodeDecodeError:
                    continue

            # Parse INI sections manually (configparser struggles with duplicate key comments)
            fltsim_sections = re.findall(r"\[FLTSIM\.\d+\]([\s\S]*?)(?=\[|\Z)", content, re.IGNORECASE)
            
            # Default atc_model for base container
            base_model_match = re.search(r"atc_model\s*=\s*[\"']?([^\"'\r\n;]+)", content, re.IGNORECASE)
            base_atc_model = base_model_match.group(1).strip().upper() if base_model_match else ""
            # If no atc_model in this cfg, it may be a [VARIATION] livery inheriting from base_container
            if not base_atc_model:
                base_atc_model = self._resolve_base_atc_model(content, os.path.dirname(cfg_file))

            for sec in fltsim_sections:
                title_m = re.search(r"title\s*=\s*[\"']?([^\"'\r\n;]+)", sec, re.IGNORECASE)
                if not title_m:
                    continue
                title = title_m.group(1).strip()

                model_m = re.search(r"atc_model\s*=\s*[\"']?([^\"'\r\n;]+)", sec, re.IGNORECASE)
                atc_model = model_m.group(1).strip().upper() if model_m else base_atc_model

                airline_m = re.search(r"atc_airline\s*=\s*[\"']?([^\"'\r\n;]+)", sec, re.IGNORECASE)
                atc_airline = airline_m.group(1).strip().upper() if airline_m else ""

                # icao_airline is the reliable code (FSLTL uses atc_airline
                # for the spoken name, e.g. "Speedbird", not the code)
                icao_m = re.search(r"icao_airline\s*=\s*[\"']?([^\"'\r\n;]+)", sec, re.IGNORECASE)
                icao_airline = icao_m.group(1).strip().upper() if icao_m else ""

                if title and atc_model and "$" not in atc_model:
                    if atc_model not in self.models_by_icao:
                        self.models_by_icao[atc_model] = []
                    self.models_by_icao[atc_model].append((title, atc_airline, icao_airline))

        except Exception as e:
            pass

        # Fast title lookup for manual overrides (title -> ICAO family key).
        self.title_index = {}
        for icao_key, liveries in self.models_by_icao.items():
            for title, _atc, _icao_al in liveries:
                self.title_index[title] = icao_key

    def _pick_livery(self, livery_list, airline_targets, airline: str):
        """Best livery in one type's list for the requested airline."""
        if airline_targets - {""}:
            for title, _atc_al, icao_al in livery_list:
                if (icao_al in airline_targets
                        or _livery_airline_code(title) in airline_targets):
                    return title
        if airline:
            for title, atc_airline, _icao_al in livery_list:
                if atc_airline and airline in atc_airline.split():
                    return title
        for title, _a, _i in livery_list:
            if title.upper().endswith("_ZZZZ"):
                return title
        for title, _a, _i in livery_list:
            if "STUB" not in title.upper():
                return title
        return livery_list[0][0]

    def get_best_model(self, icao_type: str, airline_code: str = "") -> Optional[Tuple[str, str]]:
        """
        Find best matching container title for an ICAO aircraft type and airline.
        Preference: exact airline livery (icao_airline/title code) >
        atc_airline name > generic (ZZZZ) paint > any real paint > STUB.
        Returns: (container_title, matched_icao_type) or None if catalog is empty.
        """
        if not icao_type and not self.models_by_icao:
            return None

        icao = (icao_type or "").strip().upper()
        airline = airline_code.strip().upper()
        # Resolve brand-identical code aliases (e.g. EJU -> EZY)
        airline_targets = {airline}
        if airline in AIRLINE_CODE_ALIASES:
            airline_targets.add(AIRLINE_CODE_ALIASES[airline])

        # Empty or unknown type -> no match (do NOT fall back to A320)
        if not icao:
            return None

        # Manual override (set in the dashboard's Model matching dialog).
        override = self.overrides.get(icao)
        if override:
            if override.startswith("title:"):
                title = override[6:]
                family = self.title_index.get(title)
                if family:
                    return (title, family)
            elif override.startswith("type:"):
                family = override[5:]
                liveries = self.models_by_icao.get(family)
                if liveries:
                    return (self._pick_livery(liveries, airline_targets,
                                              airline), family)

        # Build prioritized list of ICAOs to try: direct hit + aliases
        candidates = [icao] if icao else []
        if icao in MODEL_ALIASES:
            candidates.extend(MODEL_ALIASES[icao])

        # Only fall back to a generic type if this is a known airliner family
        # (never guess a GA/regional type as an A320)
        if icao in AirlinerFallback:
            candidates.extend(g for g in AirlinerFallback[icao] if g not in candidates)

        # Last resort: the geometrically closest type that is installed. This
        # keeps flights whose type FSLTL never modelled (757, MD-80, ...) in
        # the sky instead of silently dropping them.
        if not any(self.models_by_icao.get(c) for c in candidates):
            nearest = nearest_available_type(icao, self.models_by_icao)
            if nearest and nearest not in candidates:
                candidates.append(nearest)

        for cand_icao in candidates:
            livery_list = self.models_by_icao.get(cand_icao)
            if not livery_list:
                continue
            return (self._pick_livery(livery_list, airline_targets, airline),
                    cand_icao)

        # No match found for this type - return None so we skip it rather
        # than injecting a wrong-model aircraft.
        return None


# ==============================================================================
# SIMCONNECT WRAPPER & DISPATCH EXTENSION
# ==============================================================================

class _DispatchSimConnect(PySimConnect):
    """PySimConnect subclass that captures ASSIGNED_OBJECT_ID (AI creation) replies.

    The base class binds its dispatch callback inside __init__, so the override
    must live on a subclass to be picked up (reassigning the attribute afterwards
    has no effect on the already-registered ctypes callback).
    """

    def __init__(self, *args, **kwargs):
        self.assigned_objects: Dict[int, int] = {}  # request_id -> object_id
        self.ground_alt: Dict[int, Tuple[float, float]] = {}  # object_id -> (ft MSL, monotonic)
        self.lock = threading.Lock()
        self.DEFINITION_POS = None  # lat/lon/alt/heading/pitch/bank (all aircraft)
        self.DEFINITION_GROUND = None  # readable GROUND ALTITUDE (terrain under object)
        super().__init__(*args, **kwargs)

    def handle_exception_event(self, exc):
        # SimConnect routinely reports benign exceptions for injected AI
        # (objects outside the reality bubble, transient AI-state errors).
        # The base class turns each one into a scary "SIMCONNECT_EXCEPTION_*"
        # line, which looked like a real failure in the log. They are not
        # actionable, so they are dropped silently.
        return

    def my_dispatch_proc(self, pData, cbData, pContext):
        try:
            if pData and pData.contents:
                if pData.contents.dwID == SIMCONNECT_RECV_ID.SIMCONNECT_RECV_ID_ASSIGNED_OBJECT_ID:
                    obj = cast(pData, POINTER(SIMCONNECT_RECV_ASSIGNED_OBJECT_ID)).contents
                    with self.lock:
                        self.assigned_objects[obj.dwRequestID] = obj.dwObjectID
                elif pData.contents.dwID == SIMCONNECT_RECV_ID.SIMCONNECT_RECV_ID_SIMOBJECT_DATA:
                    # Reply to our per-object GROUND ALTITUDE request: the
                    # sim's own terrain/airport elevation under the aircraft.
                    # Using this instead of the feed's airport elevation is
                    # what keeps a taxying aircraft on the actual scenery
                    # surface (feed elevations can differ by a couple of
                    # metres, which showed up as a floating aircraft).
                    obj = cast(pData, POINTER(SIMCONNECT_RECV_SIMOBJECT_DATA)).contents
                    if int(obj.dwDefineID) == self.DEFINITION_GROUND:
                        # dwData is a raw buffer: reinterpret the first 8 bytes
                        # as the FLOAT64 we defined (reading dwData[0] would
                        # give only the low 4 bytes as an integer).
                        val = cast(obj.dwData, POINTER(c_double)).contents.value
                        with self.lock:
                            self.ground_alt[obj.dwObjectID] = (
                                float(val), time.monotonic())
        except Exception:
            pass
        return super().my_dispatch_proc(pData, cbData, pContext)


class ElevateXSimConnect:
    """Enhanced SimConnect wrapper supporting AI assigned object IDs and tracking."""

    def __init__(self):
        if not SIMCONNECT_AVAILABLE:
            raise RuntimeError("SimConnect package is not installed.")

        self.sm = _DispatchSimConnect(auto_connect=False)
        self.aircraft_requests = None
        self._gear_events = None
        self._light_events = None
        self._write_fails: Dict[int, int] = {}  # object_id -> consecutive failures
        self._ground_req_fails = 0  # GROUND ALTITUDE request errors (warn once)
        self._ground_def_failed = False  # definition creation failed (warn once)
        self._ground_ok_logged = False  # first successful terrain readback logged

    @property
    def assigned_objects(self) -> Dict[int, int]:
        return self.sm.assigned_objects

    @property
    def lock(self) -> threading.Lock:
        return self.sm.lock

    def connect(self) -> bool:
        try:
            self.sm.connect()
            self.aircraft_requests = AircraftRequests(self.sm, 10, 3)
            return True
        except Exception as e:
            print(f"[SimConnect] Connection failed: {e}")
            return False

    def _check(self, hr: int, what: str) -> bool:
        if self.sm.IsHR(hr, 0):
            return True
        print(f"[SimConnect] {what} failed (hr=0x{hr & 0xFFFFFFFF:08X})")
        return False

    def get_user_position(self) -> Optional[Tuple[float, float]]:
        """Get the user aircraft's current lat/lon, or None if unavailable."""
        try:
            lat = self.aircraft_requests.get("PLANE_LATITUDE")
            lon = self.aircraft_requests.get("PLANE_LONGITUDE")
            if lat is None or lon is None:
                return None
            lat = float(lat)
            lon = float(lon)
            if abs(lat) < 0.0001 and abs(lon) < 0.0001:
                return None  # At menu / not in a valid position
            return (lat, lon)
        except Exception:
            return None

    def get_user_atc_id(self) -> str:
        """Get the user aircraft's ATC ID (tail number), or empty string."""
        try:
            atc_id = self.aircraft_requests.get("ATC_ID")
            if atc_id is None:
                return ""
            # SimConnect may return a string simvar as bytes - decode it
            # (str() on bytes would give "B'ASXGS'" and break matching).
            if isinstance(atc_id, (bytes, bytearray)):
                atc_id = atc_id.decode("utf-8", errors="ignore")
            return str(atc_id).replace("\x00", "").strip().upper()
        except Exception:
            return ""

    def get_user_altitude_ft(self) -> float:
        """Get the user aircraft's altitude in feet, or 0.0."""
        try:
            alt = self.aircraft_requests.get("PLANE_ALTITUDE")
            if alt is None:
                return 0.0
            return float(alt)
        except Exception:
            return 0.0

    def get_user_speed_kt(self) -> float:
        """Get the user aircraft's ground speed in knots, or 0.0."""
        try:
            gs = self.aircraft_requests.get("GROUND_VELOCITY")
            if gs is None:
                return 0.0
            return float(gs)
        except Exception:
            return 0.0

    def request_ground_altitude(self, object_id: int) -> None:
        """Ask the sim for the terrain/airport elevation under an object.

        MSFS knows the scenery surface at the object's position; the feed only
        knows the airport reference elevation, which can be a couple of metres
        off (the aircraft then taxies slightly above or below the apron). The
        reply arrives asynchronously on the dispatch thread and lands in
        `_DispatchSimConnect.ground_alt`.
        """
        try:
            if self.sm.DEFINITION_GROUND is None:
                def_id = self.sm.new_def_id()
                hr = self.sm.dll.AddToDataDefinition(
                    self.sm.hSimConnect,
                    def_id.value,
                    b"GROUND ALTITUDE",
                    b"Feet",
                    SIMCONNECT_DATATYPE.SIMCONNECT_DATATYPE_FLOAT64,
                    0,
                    SIMCONNECT_UNUSED,
                )
                if not self.sm.IsHR(hr, 0):
                    if not self._ground_def_failed:
                        self._ground_def_failed = True
                        print("[SimConnect] GROUND ALTITUDE definition failed "
                              f"(hr=0x{hr & 0xFFFFFFFF:08X}); using feed elevation.")
                    return
                self.sm.DEFINITION_GROUND = int(def_id.value)
            req = self.sm.new_request_id()
            hr = self.sm.dll.RequestDataOnSimObject(
                self.sm.hSimConnect,
                req.value,
                self.sm.DEFINITION_GROUND,
                object_id,
                SIMCONNECT_PERIOD.SIMCONNECT_PERIOD_ONCE,
                0,
                0,
                0,
                0,
            )
            if not self.sm.IsHR(hr, 0):
                self._ground_req_fails += 1
                if self._ground_req_fails == 1:
                    print(f"[SimConnect] GROUND ALTITUDE request failed "
                          f"(hr=0x{hr & 0xFFFFFFFF:08X}); using feed elevation.")
        except Exception:
            pass

    def ground_altitude(self, object_id: int, max_age: float = 8.0) -> Optional[float]:
        """Last reported sim ground elevation (ft MSL) under `object_id`."""
        with self.sm.lock:
            rec = self.sm.ground_alt.get(object_id)
        if not rec:
            return None
        val, t = rec
        if time.monotonic() - t > max_age:
            return None
        if not (-1500.0 <= val <= 30000.0):
            return None
        if not self._ground_ok_logged:
            self._ground_ok_logged = True
            print(f"[SimConnect] Sim terrain elevation readback active "
                  f"({val:.0f} ft MSL) - ground aircraft will sit on the scenery.")
        return val

    def spawn_airborne_aircraft(self, title: str, tail: str, lat: float, lon: float, alt_ft: float, heading: float, speed_kt: float, on_ground: bool = False) -> int:
        """Spawn an injected AI object at a real-world position.

        The object is ALWAYS created with OnGround = 0 and Airspeed = 0,
        even when the flight is on a runway/gate. That is what JoinFS does,
        and it matters: an AICreateNonATCAircraft spawned with OnGround = 1
        enters MSFS's AI ground state, which clamps PLANE ALTITUDE and leaves
        the aircraft glued to the ground (the nose rotates, but it never
        climbs). The ground look is preserved by the altitude we write (the
        caller snaps it to the field elevation), and the velocity write in
        update_position() carries the aircraft through the take-off.
        """
        req_id = self.sm.new_request_id().value

        init_pos = SIMCONNECT_DATA_INITPOSITION()
        init_pos.Latitude = lat
        init_pos.Longitude = lon
        init_pos.Altitude = alt_ft
        init_pos.Heading = heading
        init_pos.Pitch = 0.0
        init_pos.Bank = 0.0
        init_pos.OnGround = 0   # never start in the sim's ground-locked state
        init_pos.Airspeed = 0   # we drive motion ourselves via position+velocity

        hr = self.sm.dll.AICreateNonATCAircraft(
            self.sm.hSimConnect,
            title.encode("utf-8"),
            tail.encode("utf-8"),
            init_pos,
            req_id
        )
        self._check(hr, f"AICreateNonATCAircraft('{title}')")
        return req_id

    def spawn_parked_aircraft(self, title: str, tail: str, airport_icao: str) -> int:
        """Spawn parked AI aircraft at airport gate. Returns request_id."""
        req_id = self.sm.new_request_id().value
        hr = self.sm.dll.AICreateParkedATCAircraft(
            self.sm.hSimConnect,
            title.encode("utf-8"),
            tail.encode("utf-8"),
            airport_icao.encode("utf-8"),
            req_id
        )
        self._check(hr, f"AICreateParkedATCAircraft('{title}' @ {airport_icao})")
        return req_id

    def _get_gear_events(self):
        """Map the gear-up / gear-down events once and cache their IDs."""
        if self._gear_events is None:
            self._gear_events = {
                True: self.sm.map_to_sim_event(b"GEAR_DOWN"),
                False: self.sm.map_to_sim_event(b"GEAR_UP"),
            }
        return self._gear_events

    def set_gear(self, object_id: int, down: bool) -> bool:
        """Raise/lower an AI object's landing gear.

        A teleported/injected AI object never runs the sim's own gear logic, so
        its gear would stay wherever it spawned. We command it explicitly via
        the GEAR_DOWN / GEAR_UP events addressed to the object.
        """
        ev = self._get_gear_events().get(bool(down))
        if ev is None:
            return False
        hr = self.sm.dll.TransmitClientEvent(
            self.sm.hSimConnect,
            object_id,
            ev.value,
            0,  # not used by the gear events
            SIMCONNECT_GROUP_PRIORITY_HIGHEST.value,
            SIMCONNECT_EVENT_FLAG.SIMCONNECT_EVENT_FLAG_GROUPID_IS_PRIORITY.value,
        )
        return self.sm.IsHR(hr, 0)

    def _get_light_events(self):
        """Map the light state events once and cache their IDs.

        A teleported/injected AI object never runs the model's own light logic, so its
        nav/beacon/strobe/taxi/landing lights stay dark. We command the light
        STATES ourselves (matching what JoinFS/vPilot do): the *_SET events
        take 1=on / 0=off. Logo and wing lights are left as the model had them.
        """
        if self._light_events is None:
            self._light_events = {
                name: self.sm.map_to_sim_event(name.encode())
                for name in ("NAV_LIGHTS_SET", "BEACON_LIGHTS_SET",
                             "STROBE_LIGHTS_SET", "TAXI_LIGHTS_SET",
                             "LANDING_LIGHTS_SET")
            }
        return self._light_events

    def set_lights(self, object_id: int, states: Dict[str, bool]) -> Dict[str, bool]:
        """Set light states on an AI object. `states` maps event name -> bool.

        Returns the subset of states the sim accepted, so the caller can
        remember only what actually took and retry the rest next frame.
        """
        events = self._get_light_events()
        accepted: Dict[str, bool] = {}
        for name, on in states.items():
            ev = events.get(name)
            if ev is None:
                continue
            hr = self.sm.dll.TransmitClientEvent(
                self.sm.hSimConnect,
                object_id,
                ev.value,
                1 if on else 0,  # 1 = on, 0 = off
                SIMCONNECT_GROUP_PRIORITY_HIGHEST.value,
                SIMCONNECT_EVENT_FLAG.SIMCONNECT_EVENT_FLAG_GROUPID_IS_PRIORITY.value,
            )
            if self.sm.IsHR(hr, 0):
                accepted[name] = on
        return accepted

    def update_position(self, object_id: int, lat: float, lon: float, alt_ft: float, heading: float, speed_kt: float, on_ground: bool = False, pitch_deg: float = 0.0, bank_deg: float = 0.0, vs_fpm: float = 0.0, vel_mps: Optional[Tuple[float, float, float]] = None):
        """Update position/heading/attitude/velocity of an active aircraft.

        Uses continuous coordinate writes (PLANE LATITUDE / LONGITUDE /
        ALTITUDE / HEADING / PITCH / BANK) rather than "Initial Position",
        which reloads scenery and resets flight dynamics.

        We deliberately do NOT freeze the object any more (JoinFS/PSXT don't
        either). FREEZE_ALTITUDE_SET makes MSFS hold PLANE ALTITUDE at the
        value it had when the freeze was sent, so a ground aircraft that was
        frozen on spawn can rotate but never climb. Instead of freezing, we
        write the position every tick and also push VELOCITY BODY (m/s), so
        the sim's own integration carries the aircraft up and renders a
        continuous motion between our 30 Hz polls -- this is exactly how
        JoinFS keeps its traffic smooth (it steers with velocity and only
        snaps position when the sim drifts, with a special 0.2 m altitude
        tolerance on the ground because MSFS otherwise glues AI to the
        ground).
        """
        heading_rad = math.radians(heading % 360.0)
        pitch_rad = math.radians(pitch_deg)

        if self.sm.DEFINITION_POS is None:
            self.sm.DEFINITION_POS = self.sm.new_def_id()
            for var, unit in [
                (b"PLANE LATITUDE", b"Degrees"),
                (b"PLANE LONGITUDE", b"Degrees"),
                (b"PLANE ALTITUDE", b"Feet"),
                (b"PLANE HEADING DEGREES TRUE", b"Radians"),
                (b"PLANE PITCH DEGREES", b"Radians"),
                (b"PLANE BANK DEGREES", b"Radians"),
                (b"VELOCITY BODY X", b"Meters per second"),
                (b"VELOCITY BODY Y", b"Meters per second"),
                (b"VELOCITY BODY Z", b"Meters per second"),
            ]:
                self.sm.dll.AddToDataDefinition(
                    self.sm.hSimConnect,
                    self.sm.DEFINITION_POS.value,
                    var,
                    unit,
                    SIMCONNECT_DATATYPE.SIMCONNECT_DATATYPE_FLOAT64,
                    0,
                    SIMCONNECT_UNUSED,
                )
            print(f"[SimConnect] Position/attitude/velocity definition ready "
                  f"(id={self.sm.DEFINITION_POS.value}, 9 fields).")
        def_id = self.sm.DEFINITION_POS.value

        # Velocity in body axes (X forward, Y up, Z right), the same kind of
        # write JoinFS uses. MSFS integrates this velocity for the model
        # between our writes, which is what makes the motion smooth instead
        # of a 30 Hz teleport. Crucially it also gives the sim an upward
        # component at rotation, so MSFS releases the "glued to the ground"
        # AI state instead of pinning the aircraft to the runway while the
        # nose pitches up.
        #
        # `vel_mps` is the velocity the CALLER measured from the position
        # delta it is about to write (see the tracking loop). That matters:
        # the sim integrates this vector between our 30 Hz writes, so if it
        # disagrees with the path we actually draw, the aircraft overshoots
        # between writes and is pulled back on the next one -- the visible
        # back-and-forth "stutter". Using the real delta makes the sim's
        # motion between writes land exactly on the next written position.
        # The fallback below (nominal speed along the heading) is only used
        # for the first write after a spawn, when no delta exists yet.
        if vel_mps is not None:
            vel_x, vel_y, vel_z = vel_mps
        else:
            gs_mps = max(speed_kt, 0.0) * 0.514444      # kt -> m/s
            up_mps = vs_fpm * 0.00508                   # fpm -> m/s
            cos_p, sin_p = math.cos(pitch_rad), math.sin(pitch_rad)
            vel_x = gs_mps * cos_p + up_mps * sin_p     # forward
            vel_y = -gs_mps * sin_p + up_mps * cos_p    # up
            vel_z = 0.0                                 # no sideslip

        data = (c_double * 9)(
            lat, lon, alt_ft, heading_rad,
            # MSFS convention: +PITCH = nose DOWN, +BANK = LEFT wing down.
            # Our values are natural (+pitch up, +bank right) -> negate.
            -pitch_rad, -math.radians(bank_deg),
            vel_x, vel_y, vel_z,
        )

        hr = self.sm.dll.SetDataOnSimObject(
            self.sm.hSimConnect,
            def_id,
            object_id,
            0,
            0,
            sizeof(data),
            data
        )
        # Track write health per object instead of swallowing errors:
        # persistent failures mean the sim is rejecting our writes.
        if self.sm.IsHR(hr, 0):
            self._write_fails.pop(object_id, None)
        else:
            n = self._write_fails.get(object_id, 0) + 1
            self._write_fails[object_id] = n
            if n == 1 or n % 300 == 0:
                print(f"[SimConnect] Position write FAILED for object {object_id} "
                      f"(x{n}, hr=0x{hr & 0xFFFFFFFF:08X})", file=sys.stderr)

    def remove_object(self, object_id: int):
        """Despawn injected ElevateX object."""
        req_id = self.sm.new_request_id().value
        hr = self.sm.dll.AIRemoveObject(self.sm.hSimConnect, object_id, req_id)
        self._check(hr, f"AIRemoveObject({object_id})")
        with self.sm.lock:
            self.sm.ground_alt.pop(object_id, None)


# ==============================================================================
# INJECTOR ENGINE
# ==============================================================================

@dataclasses.dataclass
class ActiveInjectedAircraft:
    flight_id: str
    callsign: str
    request_id: int
    object_id: Optional[int]
    container_title: str
    icao_type: str
    last_lat: float
    last_lon: float
    last_alt: float
    spawn_time: float
    smooth_alt: Optional[float] = None  # low-pass filtered altitude for jitter-free motion
    smooth_lat: Optional[float] = None  # smoothed lat/lon/heading (kills feed-sample jumps)
    smooth_lon: Optional[float] = None
    smooth_hdg: Optional[float] = None
    smooth_pitch: Optional[float] = None  # smoothed attitude (deg) for banked turns / climbs
    smooth_bank: Optional[float] = None
    course: Optional[float] = None  # smoothed course over ground (deg)
    ground_lock: float = 0.0  # 0..1 blend of rendered altitude onto the field elevation
    on_ground_latch: bool = False  # sticky on-ground state (survives feed noise)
    gear_down: Optional[bool] = None  # last commanded gear state (None = not yet sent)
    lights: Dict[str, bool] = dataclasses.field(default_factory=dict)  # last accepted light states
    last_tick: float = 0.0  # monotonic time of last position write
    last_write_t: float = 0.0  # monotonic time of the last accepted position write
    ground_req_t: float = 0.0  # monotonic time of last GROUND ALTITUDE request
    logged: bool = False  # first-write coordinates logged once (render diagnosis)
    att: Optional[object] = None  # AttitudeModel: pitch/bank + rotation, flare, derotation
    dbg_t: float = 0.0  # throttled altitude diagnostics
    dbg_flare: bool = False
    dbg_landed: bool = False


class LiveTrafficInjector:
    """Main orchestrator for fetching feed, matching models, and managing AI objects."""

    def __init__(
        self,
        radius_nm: float = 50.0,
        max_aircraft: int = 40,
        poll_interval: float = FEED_POLL_INTERVAL_S,  # fixed cadence (not user-tuned)
        track_interval: float = TRACK_INTERVAL_S,  # ~30 Hz; smoothing is dt-based
        custom_community: Optional[str] = None,
        dry_run: bool = False,
        user_lat: float = 51.4700,  # Default LHR
        user_lon: float = -0.4543,
        duration: float = 0.0,
        catalog: Optional["ModelCatalog"] = None,
        my_elevatex_username: str = "",
    ):
        self.radius_nm = radius_nm
        self.max_aircraft = max_aircraft
        self.poll_interval = poll_interval
        self.track_interval = track_interval
        self.dry_run = dry_run
        self.duration = duration
        self._running = True  # stop flag for graceful shutdown from GUI

        # User position state
        self.user_lat = user_lat
        self.user_lon = user_lon
        self.user_registration = ""  # user's own tail number, to exclude from injection
        self.my_elevatex_username = my_elevatex_username.strip()  # optional: ElevateX username
        self._username_check_state = None  # None | "missing" | "verified"
        self._my_callsign = ""  # callsign of the user's own flight (when found)
        self._model_notes = set()  # (feed type, substituted type) already logged
        self.user_alt_ft = 0.0
        self.user_speed_kt = 0.0

        self.feed = ElevateXFeed()
        self.catalog = catalog if catalog else ModelCatalog(custom_community_path=custom_community)
        self.sim: Optional[ElevateXSimConnect] = None
        self.active_aircraft: Dict[str, ActiveInjectedAircraft] = {}  # flight_id -> ActiveInjectedAircraft
        self.aircraft_lock = threading.Lock()  # guards active_aircraft (GUI reads it too)
        # User-state cache: a background thread polls SimConnect (which can
        # block) so the ~30 Hz loop never hitches on user reads.
        self._user_cache = {"pos": None, "alt": 0.0, "gs": 0.0}
        self._user_lock = threading.Lock()

    def start(self):
        print("=" * 70)
        print(" ELEVATEX LIVE TRAFFIC INJECTOR")
        print("=" * 70)
        print(f" Settings: Radius={self.radius_nm} NM | Max ElevateX Aircraft={self.max_aircraft}")
        print(f" Mode: {'DRY RUN (SimConnect Disabled)' if self.dry_run else 'LIVE INJECTION'}")
        print("=" * 70)

        if self.dry_run:
            # Dry run: single pass, print snapshot, exit
            self._update_traffic_feed()
            return

        if not SIMCONNECT_AVAILABLE:
            print("[Error] SimConnect Python module is required for live injection.", file=sys.stderr)
            sys.exit(1)

        if not self.catalog.fsltl_found:
            print("[Error] FSLTL Traffic Base Models not found in any Community folder.", file=sys.stderr)
            print("        Install via FlyByWire Installer -> FSLTL -> Traffic Base Models,", file=sys.stderr)
            print("        or pass --community <path-to-your-Community-folder>.", file=sys.stderr)
            sys.exit(1)

        print("[SimConnect] Connecting to Microsoft Flight Simulator...")
        self.sim = ElevateXSimConnect()
        if not self.sim.connect():
            print("[Error] Failed to connect to MSFS. Make sure simulator is running and in a flight.", file=sys.stderr)
            sys.exit(1)
        print("[SimConnect] Connected successfully!")

        # Initial user position (AircraftRequests may need a moment to populate)
        for _ in range(5):
            pos = self.sim.get_user_position()
            if pos:
                self.user_lat, self.user_lon = pos
                break
            time.sleep(1.0)
        if pos:
            print(f"[Injector] User aircraft at {self.user_lat:.4f}, {self.user_lon:.4f}")
        else:
            print(f"[Injector] Using fallback position ({self.user_lat:.4f}, {self.user_lon:.4f}) - make sure you are in a flight.")

        # Read user's own tail number (used to detect THEIR live ElevateX flight)
        self.user_registration = self.sim.get_user_atc_id()
        if self.user_registration:
            print(f"[Injector] ATC ID: {self.user_registration} - own aircraft will be excluded only if found in the live feed.")
        if self.my_elevatex_username:
            print(f"[Injector] ElevateX username: '{self.my_elevatex_username}' - will verify against live feed and exclude your flights.")

        last_sync = 0.0
        last_heartbeat = 0.0
        start_mono = time.monotonic()

        # 1 ms timer resolution on Windows so our ~30 Hz sleeps land
        # on time (default 15.6 ms granularity causes visible judder).
        if sys.platform == "win32":
            try:
                ctypes.windll.winmm.timeBeginPeriod(1)
            except Exception:
                pass

        # Feed downloads run on a background thread so the ~30 Hz tracking
        # loop never hitches on network I/O (this was a major stutter source).
        feed_thread = threading.Thread(target=self._feed_loop, daemon=True)
        feed_thread.start()
        # User-state reads (SimConnect .get calls can block) run on their
        # own background thread; the main loop reads the cache.
        user_thread = threading.Thread(target=self._user_state_loop,
                                       daemon=True)
        user_thread.start()
        # Prime the first snapshot synchronously so aircraft appear immediately
        self._sync_aircraft()

        next_tick = time.monotonic()
        try:
            while self._running:
                now = time.monotonic()

                if self.duration > 0 and now - start_mono >= self.duration:
                    print(f"\n[Injector] Duration limit ({self.duration:.0f}s) reached, stopping...")
                    break

                # User position from the background cache (never blocks)
                with self._user_lock:
                    cached_pos = self._user_cache["pos"]
                if cached_pos:
                    self.user_lat, self.user_lon = cached_pos

                # Spawn/despawn from the latest background snapshot (1 Hz).
                # Guarded: one bad flight must never kill the loop (and
                # stop all spawning); log and retry next second instead.
                if now - last_sync >= 1.0:
                    try:
                        self._sync_aircraft()
                    except Exception as e:
                        print(f"[Injector] sync error (retrying next cycle): {e}",
                              file=sys.stderr)
                    last_sync = now

                # Position tracking & interpolation tick (high frequency for smooth motion)
                try:
                    self._update_aircraft_positions()
                except Exception as e:
                    print(f"[Injector] track error (retrying next tick): {e}",
                          file=sys.stderr)

                # Heartbeat: proves the loop is alive and shows whether an
                # empty sky means "no traffic nearby" (feed ~120 worldwide)
                # rather than something broken.
                if now - last_heartbeat >= 60.0:
                    try:
                        with self.aircraft_lock:
                            n_active = len(self.active_aircraft)
                        n_feed = len(self.feed.snapshot())
                        print(f"[Injector] Status: {n_active} injected | "
                              f"{n_feed} in feed | you @ "
                              f"{self.user_lat:.3f},{self.user_lon:.3f}")
                    except Exception:
                        pass
                    last_heartbeat = now

                # Fixed schedule: hold a steady ~30 Hz without drift.
                # If we fall far behind, resync instead of spiral-catching-up.
                next_tick += self.track_interval
                while self._running:
                    wait = next_tick - time.monotonic()
                    if wait <= 0:
                        break
                    time.sleep(min(0.1, wait))
                if next_tick < time.monotonic() - 0.5:
                    next_tick = time.monotonic()

        except KeyboardInterrupt:
            print("\n[Injector] Stopping live traffic injector...")
        finally:
            self._cleanup()

    def stop(self):
        """Signal the injector loop to exit (thread-safe)."""
        self._running = False

    def username_status(self) -> Tuple[str, str]:
        """Dashboard helper: (state, callsign) where state is
        "" (no username entered), "missing" (not in feed),
        or "verified" (own flight found)."""
        if not self.my_elevatex_username:
            return "", ""
        return (self._username_check_state or "checking",
                getattr(self, "_my_callsign", ""))

    def _cleanup(self):
        """Remove injected ElevateX objects from the sim. Safe to call multiple times."""
        if sys.platform == "win32":
            try:
                ctypes.windll.winmm.timeEndPeriod(1)
            except Exception:
                pass
        with self.aircraft_lock:
            actives = list(self.active_aircraft.values())
            self.active_aircraft.clear()
        if self.sim and not self.dry_run and actives:
            print("[Injector] Removing injected ElevateX aircraft from MSFS...")
            for active in actives:
                if active.object_id:
                    try:
                        self.sim.remove_object(active.object_id)
                    except Exception:
                        pass
            print("[Injector] All injected aircraft removed.")

    def _user_state_loop(self):
        """Background thread: poll user position/altitude/speed into a cache.

        python-SimConnect .get calls can block for hundreds of ms; doing
        them here keeps the ~30 Hz tracking loop hitch-free.
        """
        while self._running:
            try:
                if self.sim:
                    pos = self.sim.get_user_position()
                    alt = self.sim.get_user_altitude_ft()
                    gs = self.sim.get_user_speed_kt()
                    with self._user_lock:
                        if pos:
                            self._user_cache["pos"] = pos
                        self._user_cache["alt"] = alt
                        self._user_cache["gs"] = gs
            except Exception:
                pass
            slept = 0.0
            while slept < 0.5 and self._running:
                time.sleep(0.1)
                slept += 0.1

    def _cached_user_alt_gs(self) -> Tuple[float, float]:
        """Non-blocking read of the cached user altitude/speed."""
        with self._user_lock:
            return self._user_cache["alt"], self._user_cache["gs"]

    def _feed_loop(self):
        """Background thread: download the feed on a fixed cadence.

        Network I/O never runs on the tracking loop, so position writes
        stay at full rate on every PC.
        """
        # Immediate first fetch is done by the caller via _sync path;
        # here we just keep it fresh.
        while self._running:
            try:
                self.feed.fetch()
            except Exception as e:
                print(f"[Feed] Background fetch error: {e}", file=sys.stderr)
            slept = 0.0
            while slept < FEED_POLL_INTERVAL_S and self._running:
                time.sleep(0.25)
                slept += 0.25

    def _update_traffic_feed(self):
        """Dry-run / legacy path: fetch once, then sync (blocking)."""
        self.feed.fetch()
        self._sync_aircraft()

    def _sync_aircraft(self):
        """Spawn/despawn ElevateX aircraft from the latest feed snapshot.

        Runs on the main loop (1 Hz); the download itself happens on the
        background feed thread.
        """
        flights = self.feed.snapshot()
        now = time.monotonic()

        # ── Own-aircraft exclusion ─────────────────────────────────────────
        # Only exclude the user's own aircraft when we can prove they are on a
        # live ElevateX flight. If they are NOT connected to ElevateX there is
        # nothing to exclude — every nearby aircraft is somebody else's.
        #
        # Proof = the feed contains a flight whose ElevateX username matches
        # the one entered in the dashboard (verified against the live feed),
        # or a flight with the user's MSFS tail number.
        #
        # Exclusion is IDENTITY-ONLY (username / user-ID / normalized tail).
        # There is deliberately NO position fallback: a nearby position can
        # never prove an aircraft is yours, and at a busy airport it would
        # eat gate neighbours. Worst case without it is a single duplicate
        # of your own aircraft, never an empty sky.
        my_reg = _norm_reg(self.user_registration)
        # Normalize aggressively: casefold (better Unicode than lower),
        # strip whitespace. Also accept a pasted user-ID (UUID) — the
        # feed carries both userId and username per flight.
        my_uname = self.my_elevatex_username.strip().casefold()
        my_uid = self.my_elevatex_username.strip()
        user_on_elevatex = False
        my_flight_callsign = ""
        matched_by = ""  # "username" (authoritative) or "registration"
        if flights:
            # Pass 1: username (or user-ID) match wins over everything
            if my_uname:
                for f in flights:
                    if (f.user_name.strip().casefold() == my_uname
                            or (my_uid and f.user_id.strip() == my_uid)):
                        user_on_elevatex = True
                        my_flight_callsign = f.callsign
                        matched_by = "username"
                        break
            # Pass 2: registration fallback (only if username didn't match)
            if not user_on_elevatex and my_reg:
                for f in flights:
                    if _norm_reg(f.registration) == my_reg:
                        user_on_elevatex = True
                        my_flight_callsign = f.callsign
                        matched_by = "registration"
                        break
        # Remember for the dashboard status line
        if my_flight_callsign:
            self._my_callsign = my_flight_callsign

        # Verify the entered username against the live feed database.
        # Tri-state: None = not checked yet, "missing" = checked, not flying,
        # "verified" = seen in the feed. Each state logs only once.
        if my_uname:
            state = getattr(self, "_username_check_state", None)
            if state is None and flights:
                if matched_by == "username":
                    print(f"[Injector] Username '{self.my_elevatex_username}' VERIFIED in ElevateX feed - flying as {my_flight_callsign}. Own aircraft excluded.")
                    self._username_check_state = "verified"
                else:
                    print(f"[Injector] Username '{self.my_elevatex_username}' checked against live feed: not flying right now. Nothing excluded.")
                    self._username_check_state = "missing"
            elif state == "missing" and matched_by == "username":
                print(f"[Injector] Username '{self.my_elevatex_username}' now flying as {my_flight_callsign} - own aircraft excluded.")
                self._username_check_state = "verified"
            elif state == "verified" and matched_by != "username" and not user_on_elevatex:
                print(f"[Injector] Flight ended (or left feed) - no longer excluding anything.")
                self._username_check_state = "missing"

        if user_on_elevatex != getattr(self, "_was_on_elevatex", None):
            if user_on_elevatex:
                if not my_uname or self._username_check_state != "verified":
                    print(f"[Injector] Own aircraft detected in ElevateX feed ({my_flight_callsign}) - excluding it from injection.")
                self._was_on_elevatex = True
            else:
                print("[Injector] Not on a live ElevateX flight - no exclusion, all nearby traffic injected.")
                self._was_on_elevatex = False

        # Snapshot of currently injected ids (for hysteresis scoring)
        with self.aircraft_lock:
            active_ids_now = set(self.active_aircraft)

        # Margins follow the user's radius selection (live from the GUI)
        grace = min(RADIUS_GRACE_NM, max(1.0, self.radius_nm * 0.15))
        hyst = min(SELECT_HYST_NM, max(0.5, self.radius_nm * 0.05))

        # Find flights within radius. Aircraft that are already injected
        # get a grace ring past the radius so edge traffic doesn't pop
        # in and out as feed positions jitter.
        nearby_flights: List[Tuple[float, TrafficFlight]] = []
        for flight in flights:
            dist = haversine_distance_nm(
                self.user_lat, self.user_lon,
                flight.position.latitude, flight.position.longitude
            )
            is_active = flight.id in active_ids_now
            if dist > self.radius_nm + (grace if is_active else 0.0):
                continue

            # Skip the user's own aircraft (prevents self-overlap) - ONLY when
            # we have confirmed they are on a live ElevateX flight, and ONLY
            # by identity match. Never by position (see above).
            if user_on_elevatex:
                if my_uname and (flight.user_name.strip().casefold() == my_uname
                                 or (my_uid and flight.user_id.strip() == my_uid)):
                    continue
                if my_reg and _norm_reg(flight.registration) == my_reg:
                    continue

            nearby_flights.append((dist, flight))

        # Nearest-first with hysteresis: already-injected aircraft score
        # closer by the (radius-scaled) margin, so the max-aircraft
        # boundary doesn't flicker every sync. Furthest beyond the cap
        # are trimmed.
        nearby_flights.sort(
            key=lambda x: x[0] - (hyst
                                  if x[1].id in active_ids_now else 0.0))
        selected_flights = nearby_flights[:self.max_aircraft]
        selected_ids = {f.id for _, f in selected_flights}

        # If nothing is injected yet but flights are nearby, say WHY the sky is
        # still empty: a moving aircraft is held back until its second feed
        # sample arrives (so it does not pop in and slide backwards). Without
        # this line an empty table for the first minute looks like a failure.
        try:
            with self.aircraft_lock:
                none_active = not self.active_aircraft
            if selected_flights and none_active and not self.dry_run:
                now_w = time.monotonic()
                if now_w - getattr(self, "_wait_note_t", 0.0) > 20.0:
                    self._wait_note_t = now_w
                    ready_n = sum(1 for _, f in selected_flights
                                  if f.track.ready)
                    print(f"[Injector] Waiting for position data: "
                          f"{len(selected_flights)} flight(s) nearby, "
                          f"{ready_n} ready to spawn.")
        except Exception:
            pass

        # Despawn aircraft that left radius / ended / got trimmed by the cap
        with self.aircraft_lock:
            to_remove = [fid for fid in self.active_aircraft if fid not in selected_ids]
            removed = [self.active_aircraft.pop(fid) for fid in to_remove]
        feed_map = {f.id: f for f in flights}
        for active in removed:
            f = feed_map.get(active.flight_id)
            trimmed = (
                f is not None
                and not f.completed and not f.cancelled
                and haversine_distance_nm(
                    self.user_lat, self.user_lon,
                    f.position.latitude, f.position.longitude)
                <= self.radius_nm + grace
            )
            if trimmed:
                print(f"[Trim] {active.callsign} ({active.icao_type}) trimmed - beyond nearest {self.max_aircraft}.")
            else:
                print(f"[Despawn] {active.callsign} ({active.icao_type}) left area or ended flight.")
            if self.sim and active.object_id and not self.dry_run:
                self.sim.remove_object(active.object_id)

        # Spawn new flights (per-flight guard: one corrupt flight must
        # never abort the loop and starve every flight listed after it)
        for dist, flight in selected_flights:
            with self.aircraft_lock:
                if flight.id in self.active_aircraft:
                    continue  # Already injected

            try:
                # Match model
                match = self.catalog.get_best_model(flight.aircraft_icao, flight.airline_icao)
                if not match:
                    continue  # No matching ElevateX model available for this type

                container_title, matched_icao = match
                want_type = (flight.aircraft_icao or "").strip().upper()
                got_type = (matched_icao or "").strip().upper()
                if want_type and got_type and want_type != got_type:
                    note = (want_type, got_type)
                    if note not in self._model_notes:
                        self._model_notes.add(note)
                        print(f"[Model] No {want_type} model installed - "
                              f"using the closest type ({got_type}) for "
                              f"{flight.callsign}.")

                # Spawn at the delayed-interpolation pose (on the curve,
                # never at a predicted point). Skip this cycle if the
                # track has no pose yet (single sample, still priming).
                flight.track.elevs = (flight.origin_elev_ft, flight.dest_elev_ft)
                pose = flight.track.step(now)
                if pose is None:
                    continue
                # A moving flight needs two samples before its real feed
                # cadence (and therefore its playback delay) is known. If we
                # spawn it on the first sample it appears at the live position
                # and is then yanked backwards when the playhead is anchored.
                # Parked/slow traffic is stationary, so it can appear at once.
                if not flight.track.ready and pose.gs >= 50.0:
                    continue
                lat, lon, alt_ft = pose.lat, pose.lon, pose.alt
                heading, gs_kt = pose.hdg, pose.gs
                on_ground = is_on_ground(pose, flight.origin_elev_ft,
                                         flight.dest_elev_ft)
                if on_ground:
                    # Snap to nearest airport elevation so wheels sit on
                    # the runway, not floating above it (feed altitude is
                    # slightly off for ground aircraft).
                    elevs = [e for e in (flight.origin_elev_ft,
                                         flight.dest_elev_ft)
                             if e > -100.0]
                    if elevs:
                        alt_ft = min(elevs, key=lambda e: abs(e - alt_ft))

                if not self.dry_run:
                    print(f"[Spawn] {flight.callsign:<8} | ICAO: {flight.aircraft_icao:<4} -> Model: '{container_title}' | Dist: {dist:.1f} NM | Alt: {int(alt_ft)} ft")

                req_id = -1
                if not self.dry_run and self.sim:
                    # Always spawn at the aircraft's actual feed position (even if on
                    # the ground) so it appears where ElevateX reports it, not at its
                    # origin airport.
                    req_id = self.sim.spawn_airborne_aircraft(container_title, flight.registration, lat, lon, alt_ft, heading, gs_kt, on_ground=on_ground)

                new_ac = ActiveInjectedAircraft(
                    flight_id=flight.id,
                    callsign=flight.callsign,
                    request_id=req_id,
                    object_id=None,
                    container_title=container_title,
                    icao_type=flight.aircraft_icao,
                    last_lat=lat,
                    last_lon=lon,
                    last_alt=alt_ft,
                    spawn_time=now,
                    smooth_lat=lat,
                    smooth_lon=lon,
                    smooth_hdg=heading,
                    smooth_alt=alt_ft,
                    last_tick=now,
                    # A ground aircraft is already pinned to the field at spawn;
                    # start the blend engaged so the first tick does not write
                    # the raw (noisy) feed altitude and then visibly sink onto
                    # the runway over the next half second.
                    ground_lock=1.0 if on_ground else 0.0,
                )
            except Exception as e:
                print(f"[Spawn] Skipping {flight.callsign}: {e}",
                      file=sys.stderr)
                continue
            # Insert under lock with double-check (another sync may have raced us)
            with self.aircraft_lock:
                if flight.id not in self.active_aircraft:
                    self.active_aircraft[flight.id] = new_ac


        if self.dry_run:
            self._print_dry_run_summary(selected_flights)

    def _update_aircraft_positions(self):
        if self.dry_run or not self.sim:
            return

        now = time.monotonic()

        # Snapshot under lock: the sync path may add/remove entries on this
        # same thread between ticks, and the GUI reads the dict too.
        with self.aircraft_lock:
            snapshot = list(self.active_aircraft.items())

        # Resolve pending object IDs from SimConnect
        with self.sim.lock:
            for fid, active in snapshot:
                if active.object_id is None and active.request_id in self.sim.assigned_objects:
                    active.object_id = self.sim.assigned_objects[active.request_id]
                    print(f"[Inject] {active.callsign} live in sim (ObjectID {active.object_id}), tracking...")

        # Drop pending spawns that never got an ObjectID (MSFS refused or
        # dropped the request). Frees the slot for a retry on next sync.
        expired = []
        with self.aircraft_lock:
            for fid, active in list(self.active_aircraft.items()):
                if active.object_id is None and (now - active.spawn_time) > SPAWN_TIMEOUT_S:
                    expired.append(self.active_aircraft.pop(fid))
        for active in expired:
            print(f"[Retry] {active.callsign} spawn timed out after {SPAWN_TIMEOUT_S:.0f}s (no ObjectID) - will retry.")

        # Update position for each active ElevateX aircraft
        for fid, active in snapshot:
            if not active.object_id:
                continue

            with self.feed.lock:
                flight = self.feed.flights.get(fid)
            if not flight:
                continue
            # Delayed-interpolation playback: the pose glides along a
            # Hermite curve between two known samples (fixed delay in
            # the past), so there is never a prediction to correct.
            # Field elevations let the track time touchdown / lift-off properly.
            flight.track.elevs = (flight.origin_elev_ft, flight.dest_elev_ft)
            pose = flight.track.step(now)
            if pose is None:
                continue
            lat, lon = pose.lat, pose.lon
            heading, gs_kt = pose.hdg, pose.gs
            dt = now - active.last_tick if active.last_tick else TRACK_INTERVAL_S
            if active.att is None:
                active.att = AttitudeModel()
            # Ground reference: ask the sim for the terrain/airport elevation
            # under the object as soon as we are low enough to care, and use it
            # for BOTH the attitude/flare surface and the final altitude blend.
            # The feed's airport elevation comes from a different source than
            # the sim scenery and can be a couple of metres off; aiming the
            # flare at the feed value made an otherwise perfect arrival touch
            # down/roll out slightly above the apron. The reply is
            # asynchronous, so until it arrives (or if the sim refuses) we keep
            # using the feed elevation. Refresh every 2 s while low.
            feed_ref = ground_ref(pose, flight.origin_elev_ft, flight.dest_elev_ft)
            pose_agl = None if feed_ref is None else pose.alt - feed_ref
            elevs_ref = [e for e in (flight.origin_elev_ft, flight.dest_elev_ft)
                         if e > -100.0]
            ground_sim = None
            # Ask for the sim terrain whenever we are low OR slow: a taxiing
            # aircraft is identified by its speed, not by the feed's altitude,
            # which can be a long way off if the other pilot's altimeter/QNH
            # is bad. Without this, a bad altitude read meant no terrain
            # reference was fetched and the aircraft fell back to the feed's
            # airport elevation -- the "sometimes floats while taxiing" case.
            if pose_agl is None or pose_agl < 800.0 or gs_kt < 60.0:
                if now - active.ground_req_t > 2.0:
                    active.ground_req_t = now
                    self.sim.request_ground_altitude(active.object_id)
                ground_sim = self.sim.ground_altitude(active.object_id)
            # Sanity: GROUND ALTITUDE must be near one of the two airports the
            # flight is associated with, otherwise it is a stale/garbage reply
            # (e.g. the sea under a just-despawned object).
            if ground_sim is not None and elevs_ref:
                nearest = min(elevs_ref, key=lambda e: abs(e - ground_sim))
                if abs(ground_sim - nearest) > 100.0:
                    ground_sim = None
            elev_ref = ground_sim if ground_sim is not None else feed_ref
            pitch_deg, bank_deg, alt_model = active.att.update(
                pose, elev_ref, dt)

            # Rendered altitude is always the AttitudeModel output: it is
            # continuous through rotation, flare and derotation. Near the
            # runway we additionally EASE it onto the known field elevation,
            # because the feed's on-ground altitude is noisy by tens of feet.
            # Previously we snapped to the field elevation the instant the
            # on-ground flag tripped, which produced a visible single-frame
            # drop on touchdown (the landing "step").
            agl = agl_ft(pose, flight.origin_elev_ft, flight.dest_elev_ft)
            elevs = elevs_ref
            # Determine on-ground ROBUSTLY. The feed altitude is noisy by
            # tens of feet, so a plain "agl < X" test flips from frame to
            # frame: that made the altitude blend oscillate (jitter on the
            # ground roll) and bounce the aircraft on roll-out. We instead use
            # only cues that are stable per feed segment, and otherwise HOLD
            # the previous state:
            #   * definite ground  : very low ground speed, or already landed
            #   * definite airborne: flying the flare, or the NEXT sample is
            #                        clearly higher than this one (lift-off) at
            #                        a speed at which a climb is possible. The
            #                        150 ft margin sits above the feed's
            #                        altitude noise so a rolling aircraft cannot
            #                        trip it -- and the aircraft must be
            #                        ACCELERATING: a roll-out is fast too, and
            #                        a single noisy altitude sample on the
            #                        roll-out used to be read as a lift-off,
            #                        which lifted the aircraft off the runway
            #                        and made it float while taxiing.
            accelerating_ahead = (pose.next_gs is None
                                  or pose.next_gs >= pose.gs + 5.0)
            climbing_ahead = (pose.next_alt is not None
                              and pose.next_alt - pose.alt > 150.0
                              and gs_kt > 80.0
                              and accelerating_ahead)
            # A clearly-airborne height above the field is a stable cue on its
            # own (the feed altitude noise is only tens of feet). Without it a
            # take-off whose NEXT sample is less than 150 ft higher never
            # clears the latch: the aircraft would rotate, but the altitude
            # blend would keep pinning it to the field elevation. It must not
            # apply to a taxi/roll-out (GS below take-off speed, decelerating).
            clearly_airborne = (agl is not None and agl > 200.0
                                and gs_kt > 80.0
                                and (pose.next_gs is None
                                     or pose.next_gs >= pose.gs - 5.0))
            # Roll-out cue: low, fast, NOT accelerating and NOT flaring means
            # the wheels are down. This is the safety net for the intermittent
            # float: the feed altitude can spike for a single sample during a
            # roll-out, the latch-clearing cues above are now conservative, and
            # without this the aircraft would be handed back to the feed
            # altitude and hover a few metres above the surface for a second
            # or two before settling. The `not flare` guard keeps it out of
            # the landing flare (which starts much higher) and the
            # `not accelerating` guard keeps it out of the take-off roll.
            low_rollout = (agl is not None and 0.0 <= agl < 20.0
                           and gs_kt > 50.0
                           and not active.att.flare
                           and not accelerating_ahead)
            if (low_rollout
                    or is_on_ground(pose, flight.origin_elev_ft,
                                    flight.dest_elev_ft)
                    or active.att.landed):
                on_ground = True
            elif active.att.flare or climbing_ahead or clearly_airborne:
                on_ground = False
            else:
                # Neither a confident ground nor air cue: HOLD the previous
                # state. This is the key anti-jitter step -- while rolling at
                # speed the feed altitude is pure noise, so we must not let a
                # noisy agl/vs sample flip us frame to frame.
                on_ground = active.on_ground_latch
            active.on_ground_latch = on_ground
            field = (ground_sim if ground_sim is not None else
                     (min(elevs, key=lambda e: abs(e - alt_model))
                      if elevs else None))
            if field is not None:
                wants = 1.0 if on_ground else 0.0
                tau = 0.5 if wants > active.ground_lock else 1.4
                active.ground_lock += ((wants - active.ground_lock)
                                       * _smooth_factor(dt, tau))
                alt_ft = alt_model + (field - alt_model) * active.ground_lock
                if 0.0 < active.ground_lock < 1.0 and active.last_alt:
                    # Ease the hand-over between "pinned to the field" and the
                    # feed altitude. Right at lift-off the blend releases while
                    # the feed altitude can be 30+ ft above the runway, which
                    # used to move the aircraft ~35 ft/s for a frame or two --
                    # a visible pop. Cap that transition at 15 ft/s.
                    max_step = 9.0 * dt
                    alt_ft = active.last_alt + max(
                        -max_step, min(max_step, alt_ft - active.last_alt))
            else:
                alt_ft = alt_model

            # --- Temporary altitude diagnostics (remove once fixed) --------
            _fl = bool(active.att.flare)
            _ld = bool(active.att.landed)
            _agl = "None" if agl is None else f"{agl:.0f}"
            if _fl and not active.dbg_flare:
                print(f"[Alt] {active.callsign} FLARE START ref={active.att.ref:.0f} "
                      f"hf={active.att.hf:.1f} pose={pose.alt:.0f} agl={_agl} "
                      f"gs={gs_kt:.0f} vs={pose.vs:.0f}")
            if _ld and not active.dbg_landed:
                print(f"[Alt] {active.callsign} TOUCHDOWN ref={active.att.ref:.0f} "
                      f"off={active.att.off:.1f} pose={pose.alt:.0f} field="
                      f"{'None' if field is None else f'{field:.0f}'}")
            if active.dbg_flare and not _fl and not _ld:
                print(f"[Alt] {active.callsign} FLARE ABORT h={active.att.h:.1f} "
                      f"ga_t={active.att.ga_t:.2f} pose={pose.alt:.0f} agl={_agl}")
            if active.dbg_landed and not _ld:
                print(f"[Alt] {active.callsign} LANDED CLEAR pose={pose.alt:.0f} agl={_agl}")
            active.dbg_flare, active.dbg_landed = _fl, _ld
            if (now - active.dbg_t) >= 2.0 and agl is not None and (agl < 3000.0 or on_ground):
                active.dbg_t = now
                print(f"[Alt] {active.callsign} pose={pose.alt:.0f} model={alt_model:.0f} "
                      f"field={'None' if field is None else f'{field:.0f}'} out={alt_ft:.0f} "
                      f"gsim={'None' if ground_sim is None else f'{ground_sim:.0f}'} "
                      f"agl={agl:.0f} gs={gs_kt:.0f} gnd={int(on_ground)} fl={int(_fl)} "
                      f"ld={int(_ld)} gl={active.ground_lock:.2f} "
                      f"alt_s={active.att.alt_s:.0f} h={active.att.h:.1f} "
                      f"off={active.att.off:.1f} orig={flight.origin_elev_ft:.0f} "
                      f"dest={flight.dest_elev_ft:.0f}")

            # Landing gear: an injected/teleported AI object never runs the sim's
            # own gear logic, so we command it explicitly. Down on the ground
            # and on approach, up in the climb and cruise, with hysteresis so
            # it cannot flap. Retries until the sim accepts the event.
            if on_ground or active.att.landed or active.att.flare:
                want_gear = True
            elif agl is not None and agl < 800.0:
                want_gear = True
            elif agl is not None and agl > 1200.0:
                want_gear = False
            else:
                want_gear = (active.gear_down if active.gear_down is not None
                             else True)
            if active.gear_down != want_gear:
                if self.sim.set_gear(active.object_id, want_gear):
                    active.gear_down = want_gear

            # Lights: an injected AI object never runs the model's own light
            # logic, so it flies dark. Command the standard light states
            # ourselves (as JoinFS/vPilot do), driven by the same phase cues
            # as the gear: nav + beacon always (aircraft is live), strobes on
            # the runway and in the air, taxi on while on the ground, landing
            # lights on the ground and on the low approach. Only changed
            # states are sent, and accepted ones are remembered.
            want_lights = {
                "NAV_LIGHTS_SET": True,
                "BEACON_LIGHTS_SET": True,
                "STROBE_LIGHTS_SET": (not on_ground) or gs_kt > 40.0,
                "TAXI_LIGHTS_SET": on_ground,
                "LANDING_LIGHTS_SET": (on_ground or active.att.flare
                                       or (agl is not None and agl < 2500.0)),
            }
            changes = {n: v for n, v in want_lights.items()
                       if active.lights.get(n) != v}
            if changes:
                accepted = self.sim.set_lights(active.object_id, changes)
                active.lights.update(accepted)

            # Freeze events are intentionally NOT used. FREEZE_ALTITUDE_SET
            # makes MSFS hold PLANE ALTITUDE at its current value, which pins
            # a ground-spawned aircraft to the runway forever (it can rotate
            # but never climb). JoinFS/PSXT do not freeze either: they write
            # position + velocity continuously and let the sim integrate the
            # motion between updates. update_position() now does the same.
            # --- No position smoothing: the pose is already smooth (it
            # moves along the curve, never jumps). Only attitude keeps
            # a gentle exponential ease so banks/pitches fade in.
            dt = now - active.last_tick if active.last_tick else TRACK_INTERVAL_S
            active.last_tick = now

            # Pitch is already eased and rate-limited by the AttitudeModel
            # (rotation, flare, derotation); only bank gets the extra ease.
            active.smooth_pitch = pitch_deg
            if active.smooth_bank is None:
                active.smooth_bank = bank_deg
            else:
                active.smooth_bank += ((bank_deg - active.smooth_bank)
                                       * _smooth_factor(dt, 1.1))

            # Course over ground = the direction of the position delta we are
            # about to write. Writing the nose along the COURSE (rather than
            # the sparse, noisy feed heading) keeps the aircraft pointing where
            # it is actually going. Otherwise heading and motion differ by a
            # few degrees, the sim integrates the lateral part of the velocity
            # and the whole aircraft visibly slips sideways between writes.
            coslat = max(math.cos(math.radians(lat)), 0.01)
            dt_w = now - active.last_write_t if active.last_write_t else 0.0
            v_n = v_e = 0.0
            speed_ms = 0.0
            if 0.004 < dt_w < 0.5 and active.last_lat:
                v_n = (lat - active.last_lat) * 111320.0 / dt_w
                v_e = (lon - active.last_lon) * 111320.0 * coslat / dt_w
                speed_ms = math.hypot(v_n, v_e)

            target_hdg = heading
            using_course = False
            if speed_ms > 15.0 and not on_ground:
                using_course = True
                course = math.degrees(math.atan2(v_e, v_n)) % 360.0
                if active.course is None:
                    active.course = course
                else:
                    d_c = ((course - active.course + 180.0) % 360.0) - 180.0
                    active.course = (active.course
                                     + d_c * _smooth_factor(dt, 0.35)) % 360.0
                target_hdg = active.course
            else:
                active.course = None

            # Rate-limit the heading so noisy feed headings and the step at
            # each sample don't show up as a wiggling nose. PSXT 16.5 calls
            # this out explicitly: "smoother heading corrections during taxi,
            # reducing unnecessary left-right corrections". Slew on the short
            # way round and cap the turn rate so taxi turns stay coordinated
            # instead of snapping.
            if active.smooth_hdg is None:
                active.smooth_hdg = target_hdg
            else:
                dh = ((target_hdg - active.smooth_hdg + 180.0) % 360.0) - 180.0
                # Deadband: a fraction of a degree of feed heading noise used to
                # make the nose twitch left/right every frame. Ignore it.
                if abs(dh) < 0.15:
                    dh = 0.0
                lim = HDG_RATE_LIMIT_DPS * dt
                # The course already comes from the smooth rendered path, so
                # it needs almost no extra lag; the feed heading does.
                step = dh * _smooth_factor(dt, 0.2 if using_course else 0.8)
                active.smooth_hdg = (active.smooth_hdg
                                     + max(-lim, min(lim, step))) % 360.0
            heading = active.smooth_hdg

            # Velocity to write = the velocity of the position we are about
            # to write, measured from the previous written pose. The sim
            # integrates the body velocity between our 30 Hz writes; if that
            # vector does not match the path we draw, the aircraft overshoots
            # between writes and is pulled back on the next one -- the
            # "stutter"/sideways shimmer visible on every aircraft. Using the
            # real delta makes the sim's own integration land exactly on the
            # next written position. It also keeps the vertical component in
            # step with the altitude we actually render (which is the shaped
            # AttitudeModel output, not the raw feed altitude), removing the
            # vertical bob as well.
            vel_mps = None
            if 0.004 < dt_w < 0.5 and active.last_lat:
                v_u = (alt_ft - active.last_alt) * 0.3048 / dt_w
                if on_ground:
                    # Wheels down: never hand the sim a vertical velocity.
                    # A small altitude-blend step would otherwise be integrated
                    # between our writes and read as the model hopping or
                    # briefly floating; the position write alone moves it in
                    # the vertical while it is on the surface.
                    v_u = 0.0
                h_rad = math.radians(heading % 360.0)
                p_rad = math.radians(active.smooth_pitch or 0.0)
                fwd = v_n * math.cos(h_rad) + v_e * math.sin(h_rad)
                right = -v_n * math.sin(h_rad) + v_e * math.cos(h_rad)
                vx = fwd * math.cos(p_rad) + v_u * math.sin(p_rad)
                vy = -fwd * math.sin(p_rad) + v_u * math.cos(p_rad)
                vz = right * BODY_Z_RIGHT_SIGN
                # A teleport / re-spawn / huge correction must not be handed
                # to the sim as a velocity (it would fling the model). Only
                # use the measured delta when it is physically plausible; the
                # nominal velocity fallback covers the rest.
                if (math.hypot(vx, vz) <= max(gs_kt * 0.514444 * 3.0, 140.0)
                        and abs(vy) <= 40.0):
                    vel_mps = (vx, vy, vz)

            # Send position update
            try:
                if not active.logged:
                    print(f"[Track] {active.callsign} obj={active.object_id} -> "
                          f"{lat:.5f},{lon:.5f} alt={alt_ft:.0f} hdg={heading:.0f} "
                          f"gnd={on_ground} pitch={active.smooth_pitch or 0.0:.1f} "
                          f"bank={active.smooth_bank or 0.0:.1f} vs={pose.vs:.0f}")
                    active.logged = True
                self.sim.update_position(active.object_id, lat, lon, alt_ft,
                                         heading, gs_kt, on_ground,
                                         active.smooth_pitch or 0.0,
                                         active.smooth_bank or 0.0,
                                         pose.vs, vel_mps)
                active.last_lat = lat
                active.last_lon = lon
                active.last_alt = alt_ft
                active.last_write_t = now
            except Exception as e:
                pass

    def _print_dry_run_summary(self, nearby_flights: List[Tuple[float, TrafficFlight]]):
        print("\n" + "-" * 85)
        print(f" DRY RUN TRAFFIC SNAPSHOT (User pos: {self.user_lat:.4f}, {self.user_lon:.4f})")
        print("-" * 85)
        print(f" {'CALLSIGN':<10} {'ICAO':<6} {'AIRLINE':<8} {'ORIG/DEST':<10} {'DIST(NM)':<9} {'ALT(FT)':<8} {'GS(KT)':<7} {'FSLTL MODEL TITLE'}")
        print("-" * 85)

        for dist, flight in nearby_flights:
            match = self.catalog.get_best_model(flight.aircraft_icao, flight.airline_icao)
            model_title = match[0] if match else "<No matching model>"
            orig_dest = f"{flight.origin_icao or '----'}/{flight.dest_icao or '----'}"
            pos = flight.position
            print(f" {flight.callsign:<10} {flight.aircraft_icao:<6} {flight.airline_icao:<8} {orig_dest:<10} {dist:<9.1f} {int(pos.altitude_ft):<8} {int(pos.ground_speed_kt):<7} {model_title}")

        print("-" * 85 + "\n")


# ==============================================================================
# MAIN CLI
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(description="ElevateX Live Traffic Injector for MSFS using FSLTL models")
    parser.add_argument("--dry-run", action="store_true", help="Run in dry-run mode (no SimConnect injection)")
    parser.add_argument("--radius", type=float, default=50.0, help="Injection radius around user in Nautical Miles (default: 50)")
    parser.add_argument("--max-aircraft", type=int, default=40, help="Maximum AI aircraft to spawn (default: 40)")
    parser.add_argument("--community", type=str, default=None, help="Custom path to Community folder containing FSLTL base models")
    parser.add_argument("--user-lat", type=float, default=51.4700, help="User latitude for dry-run distance calculations (default: 51.4700 - LHR)")
    parser.add_argument("--user-lon", type=float, default=-0.4543, help="User longitude for dry-run distance calculations (default: -0.4543 - LHR)")
    parser.add_argument("--duration", type=float, default=0.0, help="Auto-stop after N seconds (0 = unlimited, default: 0)")
    parser.add_argument("--my-username", type=str, default="", help="Your ElevateX username (excludes YOUR live flights from injection)")

    args = parser.parse_args()

    injector = LiveTrafficInjector(
        radius_nm=args.radius,
        max_aircraft=args.max_aircraft,
        poll_interval=FEED_POLL_INTERVAL_S,
        custom_community=args.community,
        dry_run=args.dry_run,
        user_lat=args.user_lat,
        user_lon=args.user_lon,
        duration=args.duration,
        my_elevatex_username=args.my_username,
    )

    injector.start()


if __name__ == "__main__":
    main()
