ElevateX Live Traffic Injector v1.14
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
  ONLY the models are needed. Do NOT run the FSLTL Traffic Injector at the
  same time - this app replaces it, and running both injects every flight
  twice and makes them fight over SimConnect.
- Python 3.12 + SimConnect package, OR just run the EXE (no Python needed)

Usage
-----
1. Start MSFS 2024 and load your flight.
2. Run ElevateXTrafficInjector.exe (admin recommended).
3. Enter your ElevateX username (optional - excludes YOUR flights).
4. Press START. Aircraft appear within ~30 s.

Updates are built in. On every launch the app asks GitHub for the latest
release; if there is a newer one a yellow "Update" button appears in the
header. It shows the release notes, downloads the package, verifies it
against the published SHA-256, swaps the running EXE and restarts itself.
Click the version number next to the logo to check manually at any time.

Known bugs (v1.14)
-----------------
- Pitch bug: nose-up/nose-down attitude during climbs and descents
  is still being tuned. The flight-path angle now uses the corrected
  speed unit (see below), which should already make it noticeably
  closer to the real attitude.

New in v1.14
------------
- Ground height is finally read from the simulator. The per-aircraft
  GROUND ALTITUDE request had never worked: the data definition was passed
  a DWORD instead of its integer value, the error was swallowed, and the
  app silently fell back to the feed's airport elevation. That is why
  aircraft hovered ~2 m over some aprons. The readback now returns the real
  surface (for example 1385 ft instead of 1417 ft at Zurich) and taxying
  aircraft sit on the scenery.
- Model matching rebuilt around one rule: exact first, closest only when
  the exact type is not installed.
    * exact ICAO type, then family aliases (A20N->A320, B38M->737,
      CRJ9->CRJ7, ...);
    * otherwise the geometrically closest installed model, scored on
      wingspan, length, seats, class (widebody/narrowbody/regional/
      turboprop/bizjet/GA), engine count and manufacturer;
    * new fallbacks for types FSLTL does not ship: 757 -> A321, MD-80/90
      and DC-9 -> A320, 717 -> E190, BAe 146 / RJ / Fokker -> CRJ7,
      military transports -> 767, large turboprops -> ATR, bizjets -> C25C,
      plus a family inference for types added to the feed later;
    * livery selection inside the chosen type: exact airline -> airline
      name -> generic paint -> any paint;
    * every substitution is logged once, e.g.
      "[Model] No B752 model installed - using the closest type (A321)".
- Manual model matching (the JoinFS-style override). A "Model matching..."
  button under FSLTL models opens a dialog where any feed type can be
  pointed at a specific installed type and livery. Overrides are stored in
  config.json (model_overrides) and apply to aircraft spawned from then on.
  This is only the manual escape hatch; automatic matching remains exact
  first.
- No more sideways slip. Airborne above ~29 kt the written heading now
  follows the course over ground taken from the position delta being
  written, instead of the sparse feed heading. Nose and motion are the same
  vector, so the body velocity has no lateral component for MSFS to
  integrate between writes (that lateral push was the whole-aircraft
  sideways shimmer). Ground/taxi still uses the feed heading for steering.
- Less yaw/bank micro-jitter: 0.15 deg heading deadband, softer heading
  easing, yaw rate limit 18 -> 12 deg/s, slower bank response and roll rate
  limit 12 -> 8 deg/s.
- Cleaner log and clearer status:
    * harmless SimConnect exception lines (SIMCONNECT_EXCEPTION_ERROR and
      friends) are suppressed instead of looking like failures;
    * the table's empty state explains itself: "Waiting for a second
      position sample... (N nearby)" or "No ElevateX flights within N NM";
    * the injector logs "Waiting for position data: N flight(s) nearby,
      M ready to spawn" so an empty first minute never looks broken.
- Sidebar no longer squished: the Model matching button uses the same style
  as Choose folder, redundant hint lines were removed and spacing tightened.
  The first-run tour now has 8 steps, including the Model matching button.

New in v1.13
------------
- Smoother take-offs and landings. A frame-by-frame analyser was added to
  the offline replay and it found four real discontinuities; all four are
  fixed, with before -> after numbers from that replay.
    * Spawning on a runway no longer sinks onto it. A ground spawn now
      starts already pinned to the field instead of using the noisy feed
      altitude for one frame and then dropping onto the surface
      (up to ~80 ft/s for half a second -> 0).
    * Lift-off no longer pops. The hand-over from "pinned to the runway"
      to the feed altitude is rate-limited and slower, so the climb starts
      smoothly (vertical kick 37.8 -> 9 ft/s).
    * The landing flare is no longer yanked down by a single noisy altitude
      sample. The floor is eased and rate-limited, and after 8 s it fades
      out so a persistent altimeter/QNH bias can never hold the aircraft
      above the runway. Short-final roughness 0.031 -> 0.005 and the worst
      touchdown kink 13.5 -> 5.9 ft/s; flare-to-touchdown stays 2.6-5.5 s
      across 18 replay runs, with no aborts or repeated ground toggles.
    * Rolling away from a standstill no longer lurches: each segment now
      starts at the speed the previous segment actually ended at, so the
      rendered speed is continuous (6.1 -> 0.17 kt/frame).
- Tips point at the exact control with a drawn arrow: the settings arrows
  line up with their row, the table tip points up at its header, and the
  Activity tip points down at the log bar. Bubbles are placed before they
  are shown, so they never flash at the wrong position.
- The Activity log is now a collapsible panel. It starts closed and shows
  the latest line as a preview, colour-coded like the log itself; click the
  title or the chevron to expand it. The state is remembered in config.json.

New in v1.12
------------
- Clearer FSLTL requirement: only the FSLTL MODELS (Traffic Base) are
  needed. The FSLTL Traffic Injector must NOT be running - this app
  replaces it, and two injectors fight over the same traffic.
    * the first-run tip now says this explicitly;
    * the app checks for the FSLTL injector process at startup and when
      you press Start, and warns in the activity log if it is running;
    * the README and website requirements say the same.

New in v1.11
------------
- First-run tips: the very first time the app is opened it explains each
  setting in small orange hint boxes with black text, pointing at the
  control it describes.
    * seven steps: search radius, max aircraft, ElevateX username, FSLTL
      models, Start injection, the live aircraft table and the activity
      log;
    * each box has Next and Skip plus a step counter, and Escape closes
      the tour;
    * finishing or skipping it is remembered in config.json, so it never
      appears again;
    * clicking Start injection also dismisses the tour;
    * the "?" next to the version number replays it at any time.
- Nothing about injection changed in this release.

New in v1.10
------------
- Fresh dashboard layout: the whole interface was rebuilt to look like a
  modern desktop app instead of a 2010 console.
    * near-black canvas with rounded, borderless panels instead of boxes
      with hard 1px outlines;
    * a compact header with the wordmark, a clickable version chip and a
      status pill whose dot turns green while injecting;
    * three summary cards (MSFS, aircraft, nearby flights) with large
      numbers and no visual clutter;
    * settings moved to a fixed sidebar with proper numeric steppers
      (- / +) instead of default Windows spin boxes and a scrollbar;
    * the aircraft table uses roomier rows, muted uppercase headings,
      an alternating row tint and a subtle "waiting for traffic" state;
    * a slim, dark scrollbar that disappears entirely when the list fits,
      and no horizontal scrollbar at all - columns are sized to the panel;
    * the activity log is now a card at the bottom with a quiet Clear
      button instead of a bordered box;
    * the Windows title bar is dark, so the frame matches the app.
- Window defaults to 1180x800 and centres itself on the current screen,
  shrinking if the display is smaller (it used to open taller than a
  1080p screen and clip the log).
- The version chip, status dot and buttons all use the same rounded
  styling as the update dialog.

New in v1.9
-----------
- In-app updates: the dashboard now checks GitHub for a newer release
  (once at startup, then whenever you click the version label) and
  offers to install it without leaving the app:
    * a yellow "Update" pill appears in the header, showing the new
      version number;
    * the update dialog shows the release notes, the current and new
      version, and the download size;
    * the download runs in the background with a progress bar, so the
      dashboard stays responsive;
    * the package is verified against the SHA-256 digest published by
      GitHub before anything is unpacked;
    * on Windows the running EXE is renamed aside, the new one is moved
      into place and the app restarts itself; the leftover file is
      deleted as soon as the old process has exited (the new build
      retries for a few seconds if Windows still has it locked), so no
      duplicate builds pile up. If anything fails, the old EXE is put
      back and the error is shown instead of leaving a broken install;
    * if the app is started from source (python elevatex_dashboard.py)
      the zip is saved to Downloads and the folder is opened, so the
      same code path can be tested without a release.
  This release is the first one with the updater, so it is still a
  manual download; every release after it can be installed from inside
  the app.

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
