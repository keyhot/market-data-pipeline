// /world — State to scene: advancing and positioning the cast, reactions, draw(),
// the history line.
// Split out of api/templates/world.html's inline script (#42); declarations
// unchanged. Imports its predecessor first to keep the original evaluation
// order (see main.js).

import {
  ANIM, SEATED_ANIMATIONS, restCharacter, idleTick, watchTick,
} from "./animation.js";
import { PLATE, TEXT_PLACEMENTS } from "./config.js";
import { NEUTRAL } from "./palette.js";
import { layers, model, trader, placedLabel } from "./boot.js";
import { layoutRoom, anchorFor, sitAnchorFor, drawPillars } from "./room.js";
import {
  REACTIONS, CAST_SCALE, chartTarget, GAZE_HEAD_SHARE, gazePitch, shadowTick,
} from "./cast.js";
import { setExpression } from "./character.js";
import { ambient, readAmbient } from "./ambient.js";
import { renderNowBand } from "./hud.js";

    // The two inhabitants, plus whatever an eval surface has added. A list
    // rather than a hard-coded pair so the animation sheet ticks through the
    // same code path the stream does — a sheet with its own player would be
    // showing me an animation nobody ships.
    const cast = [];

    function advanceCharacters(dt) {
      for (const char of cast) {
        if (!char) continue;
        if (char.loopAnim) {        // eval surface: run one animation forever
          char.phase = (char.phase + dt / char.duration) % 1;
          // C2 review round 1, Finding 2a: this branch was left pointed at
          // ANIM only - the sheet's trader samples silently played the
          // standing table (or, for asleep/gesture, called `undefined` and
          // threw) no matter what `character()` set `pose` to. Same
          // fallback shape as the main dispatch below.
          const loopFn = (char.container.pose === "seated" && SEATED_ANIMATIONS[char.loopAnim])
            || ANIM[char.loopAnim];
          loopFn(char, char.phase, char.amp ?? 1);
          continue;
        }
        // `playAnimation` only ever records a string (`char.anim`) — THIS is
        // the actual per-frame dispatch, so it's where a seated character has
        // to be pointed at SEATED_ANIMATIONS instead of ANIM. Falls back to
        // ANIM for any name not yet re-authored for sitting (Task 7's scope
        // is five animations, not the whole registry), so an unhandled
        // reaction still plays rather than throwing on a 24/7 stream.
        const seated = char.container.pose === "seated";
        if (!char.anim) {           // at rest, breathe so nobody is a statue
          if (seated) {
            SEATED_ANIMATIONS.breathe(char, (ambient.t / char.breath + char.phaseOffset) % 1, 1);
          } else {
            ANIM.idle(char, (ambient.t / char.breath + char.phaseOffset) % 1, 1);
          }
          idleTick(char, dt);
          watchTick(char, dt);      // last: a deliberate look outranks a glance
          continue;
        }
        char.phase += dt / char.duration;
        if (char.phase >= 1) {
          char.anim = null;
          restCharacter(char);
          continue;
        }
        const animFn = (seated && SEATED_ANIMATIONS[char.anim]) || ANIM[char.anim];
        animFn(char, char.phase, char.amp ?? 1);
      }
      // After, never during: a shadow reads the body's final height for this
      // frame, and the loop above leaves by four different paths.
      for (const char of cast) if (char) shadowTick(char);
    }

    // Mirrors world/reactions.reaction_for so the canvas and the server agree
    // on what an event means — the registry itself is injected above.
    function reactionFor(event) {
      let entry = REACTIONS.base[event.event_type] ?? REACTIONS.fallback;
      const payload = event.payload ?? {};
      if (event.event_type === "signal_resolved" && REACTIONS.outcomes[payload.outcome]) {
        entry = REACTIONS.outcomes[payload.outcome];
      } else if (event.event_type === "streak" && REACTIONS.directions[payload.direction]) {
        entry = REACTIONS.directions[payload.direction];
      }
      return entry;
    }

    // Tilt a profile head so its eye points at the charts. Computed here, not
    // in `character()`, because only now is it known where the head IS: the
    // eye's screen position is the hips' plate anchor plus the head-local eye
    // through the layer scale and the figure's own. Positive rotation turns
    // +x toward +y, so a head facing -x nods UP with a positive angle.
    function aimAtCharts(char, anchor) {
      if (!char || !char.profile) return;
      const k = CAST_SCALE * (char.figureScale || 1);
      const [ex, ey] = char.profile.eye;
      const eye = { x: anchor.x + ex * k, y: anchor.baseY + (char.headBaseY + ey) * k };
      const pitch = gazePitch(eye, chartTarget(PLATE && PLATE.screens), char.profile.side);
      char.aimRest = -char.profile.side * pitch * GAZE_HEAD_SHARE;
      char.aim.rotation = char.aimRest;
    }

    function positionCharacters() {
      layoutRoom();
      layers.chars.scale.set(CAST_SCALE);
      // Positions are therefore in layer space, which the scale then maps back
      // onto the screen fractions they're written as.
      // The anchors are the plate's, or the canvas fractions when there is no
      // plate — anchorFor owns that choice. They are divided by CAST_SCALE
      // because the layer is scaled, so these are layer coordinates.
      // NOTE: `character()` also sets a y, and it is dead — this runs on every
      // draw and overwrites it. The plan pointed this ticket at that write.
      const anchors = { model: anchorFor("model"), trader: anchorFor("trader") };
      // A seated pose is pinned by the hips, a standing one by the feet — the
      // plate measures both, and `sitAnchorFor` returns null for any plate that
      // doesn't, which leaves the standing anchor in place rather than a hole.
      for (const name of ["model", "trader"]) {
        if (anchors[name].pose !== "seated") continue;
        const hips = sitAnchorFor(name);
        if (hips) { anchors[name].x = hips.x; anchors[name].baseY = hips.baseY; }
      }
      model.container.x = anchors.model.x / CAST_SCALE;
      trader.container.x = anchors.trader.x / CAST_SCALE;
      // The two no longer share a floor: on this plate the trader sits on a
      // painted chair and the model stands on the floor lane, 148px lower.
      model.baseY = anchors.model.baseY / CAST_SCALE;
      trader.baseY = anchors.trader.baseY / CAST_SCALE;
      model.container.y = model.baseY;
      trader.container.y = trader.baseY;
      // Animations displace from a remembered rest pose, so it has to be
      // recorded here — a resize must not leave a character drifting.
      for (const char of [model, trader]) {
        char.baseX = char.container.x;
        // Once, at the first layout. `draw` re-runs this on every state
        // update, and by then breathe/cheer/slump have moved `head.y`: a
        // re-record would bank that frame's offset as the new rest, and the
        // head would random-walk off the neck one SSE event at a time.
        // `test_the_head_rest_is_recorded_once`.
        if (char.headBaseY == null) char.headBaseY = char.head.y;
      }
      aimAtCharts(trader, anchors.trader);
    }

    function draw(state) {
      positionCharacters();
      const m = state.model ?? {};
      const mood = m.reaction?.mood ?? "neutral";
      setExpression(model, mood);
      // Sprint 16 Task 10: no floating mood/record text beside the cast any
      // more. Mood is readable from the figure itself (Tasks 6/8's shading);
      // the win/loss record is `#nowband`'s own `record` span (KI-012) and a
      // second copy here had no declared surface to sit on.

      // The trader stays dormant until trader_* events exist, so the room is
      // complete whether or not the freqtrade sidecar is running.
      const traderState = state.trader ?? null;
      // No sidecar running means asleep, not dead: a closed-eye idle face.
      setExpression(trader, traderState ? traderState.mood : "idle");

      drawPillars(state);

      const h = state.history ?? {};
      // The history line sits on a surface the manifest actually measured
      // (Sprint 16 Task 10) — never at a fixed offset off app.screen, which
      // is why it used to run across the tubes at some window sizes.
      // `name-trader`/`name-model` are NOT rebuilt here: they are static
      // text built once at boot into `layers.nameplates` (see boot()'s own
      // comment for why that is a separate, un-scaled, top-most layer).
      const historyLabel = placedLabel(TEXT_PLACEMENTS.history, historyLine(h), NEUTRAL);
      if (historyLabel) layers.props.addChild(historyLabel);

      renderNowBand(state);
      readAmbient(state);
    }

    function historyLine(h) {
      if (!h.total_events) return "";
      // "seen" is deliberate: these are folded over the served window, not the
      // whole table. Overstating them would be the one thing this project
      // refuses to do with its own numbers.
      const parts = [`${h.total_events} events remembered`];
      if (h.longest_streak) {
        parts.push(`longest streak seen ${h.longest_streak.bars} bars`);
      }
      if (h.worst_loss) {
        parts.push(
          `worst loss seen ${(h.worst_loss.realized_return * 100).toFixed(2)}%`);
      }
      if (h.outages) parts.push(`${h.outages} outages`);
      return parts.join(" · ");         /* set via .text: payloads are data */
    }

export { cast, advanceCharacters, reactionFor, positionCharacters, draw };
