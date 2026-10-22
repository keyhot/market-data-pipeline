// /world — Entry point: live reactions, the state refresh, the SSE subscription, and
// boot().
//
// Split out of api/templates/world.html's inline script (#42); every
// declaration is unchanged. The page loads this one module; each module
// imports its predecessor first (config.js ... swell.js, then this), so the
// modules evaluate in exactly the order the single script ran its statements.
// Forward imports (a function calling one defined later) are cycles that
// only resolve at call time, never during evaluation.

import { SWELL_TIER, startSwell } from "./swell.js";
import { tierOf } from "./tiers.js";
import { model, trader, retryBoot, boot } from "./boot.js";
import { setExpression } from "./character.js";
import { playAnimation, lookAt } from "./animation.js";
import { reactionFor, draw } from "./scene.js";
import { speak } from "./hud.js";

    // A live event nudges the room immediately; the next refresh reconciles it
    // with the authoritative server-side projection. The nudge scales with the
    // event's tier, so a calm event barely stirs and a tier-3 event visibly
    // swells (then settles back — never sticky-amped).
    function react(event) {
      // A spoken line is the director talking about an event, not an event the
      // room reacts to physically — it gets a bubble instead of a nudge.
      if (event.event_type === "commentary_spoken") {
        speak(event);
        return;
      }
      const target = event.event_type.startsWith("trader_") ? trader : model;
      const tier = tierOf(event.event_type, event.severity);
      // B1: the reaction registry decides the face and the movement; the tier
      // only decides how big it reads.
      const reaction = reactionFor(event);
      setExpression(target, reaction.mood);
      // Sprint 16 Task 10: no floating mood text on a live reaction either —
      // `setExpression` is the room's only mood channel now.
      playAnimation(target, reaction.animation, tier);
      // B7: the one who didn't act notices the one who did. Only from tier 1 —
      // at tier 0 the pair would spend the day turning to look at each other,
      // and a glance that happens constantly stops meaning anything.
      if (tier >= 1) lookAt(target === model ? trader : model, target, 0.25);
      // B8: the top tier earns the whole room — camera, callout and a cue.
      if (tier >= SWELL_TIER) startSwell(event);
    }

    // The last /world/state snapshot. Declared here, beside refresh(), its
    // one writer: an imported binding is read-only in every other module.
    let world = null;

    async function refresh() {
      try {
        const response = await fetch("/world/state");
        if (!response.ok) return;
        world = (await response.json()).data;
        draw(world);
      } catch (e) {
        // A transient API blip must never blank the stream: keep the last frame.
      }
    }

    function subscribe() {
      const source = new EventSource("/stream/world/events");
      source.onmessage = (message) => react(JSON.parse(message.data));
    }

    boot().catch(() => {
      retryBoot("renderer failed to start");
    });

export { react, refresh, subscribe };
