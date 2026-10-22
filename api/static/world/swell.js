// /world — B8, the tier-3 swell: camera push, callout and cue.
// Split out of api/templates/world.html's inline script (#42); declarations
// unchanged. Imports its predecessor first to keep the original evaluation
// order (see main.js).

import "./hud.js";
import { app } from "./boot.js";
import { ambient } from "./ambient.js";

    // --- B8: the tier-3 swell ----------------------------------------------
    //
    // The room's whole register is calm-that-swells, and until now the swell
    // was a brightness ramp and a bigger reaction. This is the beat: push ->
    // spike -> callout -> decay, choreographed with explicit timings so it
    // reads as a *moment* rather than as a setting that changed. The character
    // spike is already react()'s job; this puts the camera and the words
    // around it.
    //
    // Only tier 3 earns it. A swell that fired on the room's ordinary traffic
    // would be a swell that means nothing, which is the same mistake as a
    // glance that happens constantly (B7).
    const SWELL_TIER = 3;
    const SWELL_PUSH = 0.055;      // 5.5%: a push-in, not a zoom
    const SWELL_PUSH_S = 0.7;
    const SWELL_HOLD_S = 2.6;
    const SWELL_DECAY_S = 1.8;
    // The floor between two swells. Without it a burst of tier-3 events reads
    // as a strobe, and the room's biggest moment becomes its most annoying one
    // — the dwell guard the director already has, applied to the camera.
    const SWELL_FLOOR_S = 20;
    const CALLOUT_LEAD_S = 0.18;   // the body moves first; the words land after
    const swell = { active: false, t: 0, last: -1e9 };

    const calloutEl = document.getElementById("callout");
    const calloutText = document.createElement("span");
    const calloutWhere = document.createElement("span");
    calloutWhere.className = "where";
    calloutEl.appendChild(calloutText);
    calloutEl.appendChild(calloutWhere);

    function swellEase(x) {
      const t = Math.max(0, Math.min(1, x));
      return t * t * (3 - 2 * t);
    }

    function swellHeadline(event) {
      // Derived from the type, not a second copy of overlay_events.html's
      // HEADLINES map — two hand-kept copies of one vocabulary is how KI-019
      // happened, and a headline that drifts between two surfaces of the same
      // stream is worse than a plain one. Short by construction, which is also
      // what keeps it inside its box (KI-031).
      return (event.event_type || "").replace(/_/g, " ");
    }

    function emitSwellCue(event) {
      // The music cue. There is no audio bed yet — choosing DMCA-safe music is
      // a human step still open from Sprint 11 — so this fires an event and
      // nothing listens. Deliberately not a `playSwell()` stub: a function that
      // looks like it makes a sound and doesn't is a lie the next reader has to
      // discover. When the bed lands, it subscribes here.
      window.dispatchEvent(new CustomEvent("world:swell", {
        detail: {
          event_type: event.event_type,
          symbol: event.symbol ?? null,
          hold_seconds: SWELL_PUSH_S + SWELL_HOLD_S + SWELL_DECAY_S,
        },
      }));
    }

    function restCamera() {
      // Exactly rest. A push that lands on 0.999 leaves the room fractionally
      // more zoomed after every swell and nothing on screen would ever say so
      // — the failure `turn` and `hop` already taught this page, on the stage.
      app.stage.scale.set(1);
      app.stage.position.set(0, 0);
    }

    function startSwell(event) {
      if (swell.active) return;              // one push per moment
      if (ambient.t - swell.last < SWELL_FLOOR_S) return;
      swell.active = true;
      swell.t = 0;
      swell.last = ambient.t;
      calloutText.textContent = swellHeadline(event);
      calloutWhere.textContent = event.symbol ?? "";
      emitSwellCue(event);
    }

    function swellTick(dt) {
      if (!swell.active) return;
      swell.t += dt;
      const t = swell.t;
      const held = SWELL_PUSH_S + SWELL_HOLD_S;
      let push;
      if (t < SWELL_PUSH_S) {
        push = swellEase(t / SWELL_PUSH_S);
      } else if (t < held) {
        push = 1;
      } else if (t < held + SWELL_DECAY_S) {
        push = 1 - swellEase((t - held) / SWELL_DECAY_S);
      } else {
        swell.active = false;
        calloutEl.classList.remove("in");
        restCamera();
        return;
      }
      if (t >= CALLOUT_LEAD_S && t < held) calloutEl.classList.add("in");
      if (t >= held) calloutEl.classList.remove("in");
      const scale = 1 + SWELL_PUSH * push;
      // Push in on the room's centre. Scaling the stage on its own would drag
      // the frame down and to the right, which reads as a camera falling over
      // rather than leaning in.
      app.stage.scale.set(scale);
      app.stage.position.set(
        (1 - scale) * app.screen.width / 2,
        (1 - scale) * app.screen.height / 2,
      );
    }

export { SWELL_TIER, swell, startSwell, swellTick };
