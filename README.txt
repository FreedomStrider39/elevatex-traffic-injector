ElevateX Live Traffic Injector v1.7
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
3. The feed only sends a new position every ~12 seconds, which would
   look jumpy if written straight to the sim. So the injector plays
   each flight back on a smooth curve ~15 seconds in the past,
   interpolating between the two nearest known samples. The aircraft
   glide continuously instead of teleporting.
4. Every frame (~30 Hz) it writes the interpolated position, heading
   and attitude (pitch/bank) to each aircraft. Aircraft are frozen
   in the sim so MSFS's AI brain doesn't fight these writes.
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

Known bugs (v1.7)
-----------------
- Pitch bug: nose-up/nose-down attitude during climbs and descents
  is not always correct yet. A fix is in progress.

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
