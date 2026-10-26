# Sethster landmark tapper

This local-only tool records the GT-C user-tap ground truth described in
`docs/plans/alignment-v2.md`. It uses only Python's standard library.

Start it with:

```sh
python3 ~/sethster/tools/tapper/server.py
```

Then open <http://127.0.0.1:8765>. Stop it with `Ctrl-C`.

The first screen runs the required two-minute, 90 bpm latency calibration. Audio is fetched on demand,
read-only, from `$SETHSTER_NAS_SSH:$SETHSTER_NAS_ROOT/` (set in the environment or the repo-root `local.env`; see `local.env.example`) and cached under
`~/sethster/data/gt/audio-cache/`. The page's **Clear audio cache** button deletes only that local cache.
Taps are saved atomically after every change under `~/sethster/data/gt/taps/<work>/<performance>.json`;
the plan and calibration live alongside them. Reloading the page resumes the saved state.

Keyboard controls: Space taps, J/K seeks backward/forward five seconds, S skips the current shared
landmark, and U removes the most recent tap in the selected performance. Each landmark needs two taps.
Use **Replay from 10 s before** after the first tap when that is quicker than making a complete second pass.

To regenerate the deterministic eight-work plan from current inventories and result files:

```sh
python3 ~/sethster/tools/tapper/build_plan.py
```

Regenerating the plan is not recommended after tapping has begun because it may change the selected
performances or proposed landmark labels.
