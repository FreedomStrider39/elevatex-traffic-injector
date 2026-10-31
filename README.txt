ElevateX Live Traffic Injector v1.8
===================================
Injects live ElevateX network traffic into MSFS 2024 as AI aircraft
using FSLTL models via SimConnect.

How it works
------------
1. Every 12 seconds the injector downloads the ElevateX live traffic
   feed (a JSON list of all currently flying ElevateX flights with
   their positions, speeds and headings).
2. It picks the flights nearest to you (within your chosen radius),
   matches each one to an FSLTL aircraft model + airline livery
   (e.g. a British Airways A320 gets an FSLTL BA A320), and spawns
   them in the sim via SimConnect.
3. The feed publishes in ~30 second batches, and an individual flight's
   position may only update every 30-120 seconds, which would look jumpy
   if written straight to the sim. So the injector plays each flight back
   on a smooth curve in the past: each flight learns its own update
   cadence and plays about one recent gap + 3 s behind the newest sample
   (~20 s for fast-updating flights, ~90 s for flights that only update
   every 90 s), interpolating between the two nearest known samples.
   The aircraft glide continuously instead of teleporting. A moving
   aircraft is not spawned until its second position sample arrives, so
   it appears where it will actually fly from instead of popping in at
   the live position and then sliding backwards.
4. Every frame (~30 Hz) it writes the interpolated position, heading,
   attitude (pitch/bank) and body velocity to each aircraft. The velocity
   sent is measured from the exact position delta being written, so the
   sim's own integration between our updates lands on the next written
   position instead of overshooting and being pulled back (that mismatch
   is what caused a slight sideways/back-and-forth shimmer). Nothing is
   frozen: MSFS gets continuous position + velocity, exactly like JoinFS
   and PSXT, so its own AI brain does not fight the writes.
5. Aircraft that leave your radius, land, or end their flight are
   removed; new ones spawn as they come nearby.

Requirements
------------
- Microsoft Flight Simulator 2024
- FSLTL Traffic Base installed in your Community folder
  (https://github.com/FSLTL/fsltl-traffic-base)
- Python 3.12 + SimConnect package, OR just run the EXE (no Python needed)

Usage
-----
1. Start MSFS 2024 and load your flight.
2. Run ElevateXTrafficInjector.exe (admin recommended).
3. Enter your ElevateX username (optional - excludes YOUR flights).
4. Press START. Aircraft appear within ~30 s.

Known bugs (v1.8)
-----------------
- Pitch bug: nose-up/nose-down attitude during climbs and descents
  is still being tuned. The flight-path angle now uses the corrected
  speed unit (see below), which should already make it noticeably
  closer to the real attitude.

Fixed in v1.8
-------------
- Speeds (take-off roll too slow): the feed's `groundSpeed` is in METRES
  PER SECOND, not knots, but it was being used as knots directly. Every
  aircraft therefore flew at 51 % of its real speed: a 150 kt take-off
  roll was played back as ~77 kt, so rotation and the whole departure
  looked sluggish. Verified by sampling the feed twice and dividing the
  great-circle distance flown by the sample interval: it reproduces
  `groundSpeed` exactly in m/s (median cruise ~240 m/s = 466 kt). The
  value is now converted to knots on ingest, so rotation happens around
  120-130 kt and approach/rollout speeds match the real flights.
- Stutter / sideways shimmer: the body velocity written to MSFS was the
  nominal speed along the (smoothed) heading, while the position we drew
  followed the interpolated path. Those two disagree slightly - more so
  in turns and while altitude is being shaped - so the sim overshot
  between our 30 Hz writes and the next write pulled it back, which shows
  up as a small back-and-forth/sideways glitch on every aircraft. The
  velocity is now measured from the actual per-tick position delta
  (rotated into the body frame), so the sim's motion between writes
  matches our path exactly. It uses the same mechanism for vertical
  motion, so it also removes the vertical bob.
- Ground height / floating aircraft: the rendered altitude on the ground
  used the feed's airport reference elevation, which comes from a
  different source than the sim scenery and can be a couple of metres
  off (one arrival was seen taxiing/rolling out ~2 m in the air). The
  injector now asks MSFS for GROUND ALTITUDE under each low/taxying
  aircraft and eases onto the actual terrain/runway surface, falling
  back to the feed elevation if the sim does not answer. Log lines show
  both values (`field=`/`gsim=`) when `[Alt]` diagnostics are on.
- Intermittent floating while taxiing / rolling out: the on-ground state
  was too easy to clear. A single noisy feed altitude sample on the
  roll-out could look like a lift-off (or like a "clearly airborne"
  height), which released the altitude blend and let the aircraft hover
  a few feet above the taxiway until the next sample. Now:
    * clearing the ground state needs the aircraft to be ACCELERATING
      (+5 kt) and genuinely higher (150 ft, smoothed), so a roll-out
      (always decelerating) can never be mistaken for a departure;
    * a low, fast, non-flaring, non-accelerating aircraft (AGL < 20 ft,
      GS > 50 kt) is forced onto the ground: that is the roll-out cue;
    * the "landed" flag is only cleared by the same accelerate-and-climb
      test, never by one high altitude reading;
    * the sim terrain elevation is now requested whenever the aircraft
      is low OR slow (GS < 60 kt), not just when the feed altitude looks
      low - a bad altimeter read on the other pilot's side used to skip
      the terrain lookup entirely and fall back to the feed airport
      elevation;
    * a terrain reply more than 100 ft away from either airport of the
      flight is treated as garbage and ignored;
    * the vertical component of VELOCITY BODY is forced to zero while
      on the ground, so a small altitude-blend step cannot be integrated
      by the sim as a hop.
- Smoothness (JoinFS comparison): JoinFS gets live positions at 10 Hz,
  so it can steer each aircraft by velocity every sim frame. Our feed is
  30-120 seconds between samples, so the only way to avoid jumps is to
  play in the past. This release fixes the playback clock itself: it used
  to be anchored to the 12 s poll interval and then creep towards the
  right delay at only 5 %/s, so in practice it stayed 15-25 s behind
  while gaps were 30-120 s. That starved the track almost continuously,
  forced dead-reckoning, and produced the visible jumps and sideways
  corrections. Now the delay is learned per flight from its largest
  recent gap (capped at 90 s) and the playhead is anchored exactly when
  the second sample arrives (before a moving aircraft is spawned).
  When data does run out, the aircraft continues the turn/speed trend
  from the last two samples instead of a frozen-heading straight line,
  and the return to real data is speed-capped (15 m/s horizontal,
  2.5 ft/s vertical), so a large prediction error fades in over a
  minute instead of swerving across the sky in a few seconds.
  Straight-line flights replay at exactly their real ground speed
  (verified with a 250 kt replay test).
- Take-off climb: aircraft rotated but stayed pinned to the runway.
  The injector froze ALTITUDE and LAT/LON on every object, and MSFS
  FREEZE_ALTITUDE_SET holds PLANE ALTITUDE at the value it had when
  the freeze was sent, so the climb-out altitude writes were ignored.
  Freezing is now removed entirely (JoinFS/PSXT do not freeze either).
  Position is written every tick and VELOCITY BODY X/Y/Z (m/s) is
  written with it, so MSFS integrates the motion between our 30 Hz
  updates (smoother) and releases the "glued to the ground" AI state
  at rotation.

Fixed in v1.7
-------------
- Aircraft lights: injected traffic is no longer dark. A frozen AI
  object never runs the model's own light logic, so the injector now
  commands the standard light states itself (as JoinFS/vPilot do):
  nav + beacon always, strobes on the runway and in the air, taxi
  lights on the ground, and landing lights on the ground and on the
  low approach.

Fixed in v1.6
-------------
- Landing float: aircraft no longer hover just above the runway and
  drift down past it. The flare altitude is now clamped to the real
  (smoothed) altitude, so it touches down on the runway on time.
- Landing gear: injected aircraft now lower and raise their gear.
  A frozen AI object never runs MSFS's own gear logic, so the
  injector now commands gear down on the ground and on approach
  (below ~800 ft) and gear up in the climb and cruise.
- Take-off rotation: a single clean rotation. The old test also
  fired on noisy sink-rate/altitude readings, so the nose pitched
  up, settled back onto the runway, then pitched up again.

Fixed in v1.5
-------------
- Takeoff/landing speed surge: aircraft no longer step or surge on
  the runway. The along-track speed is now continuous across feed
  samples, so taxi, take-off and roll-out glide smoothly.
- Landing flare: a proper nose-up flare that eases onto the runway
  instead of a late pitch twitch, with no altitude snap at touchdown.
- Bank twitch: noisy feed headings no longer roll the aircraft; bank
  is eased and rate-limited, and wings are held level on the runway.

Notes
-----
- Config saved to %APPDATA%\ElevateXTrafficInjector\config.json
- See LICENSE.txt for the disclaimer (unofficial, not affiliated
  with ElevateX; freeware).
