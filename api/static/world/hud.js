// /world — DOM overlays: the now-band price cells and speech bubbles.
// Split out of api/templates/world.html's inline script (#42); declarations
// unchanged. Imports its predecessor first to keep the original evaluation
// order (see main.js).

import "./monitors.js";
import { WORLD_CONFIG, SYMBOLS } from "./config.js";
import { MOOD_COLOR } from "./palette.js";

    // --- B9: the always-on now-band ---------------------------------------
    // Everything a newcomer needs in one glance, with no event required:
    // what's being traded, at what price, how the market feels, and — the
    // point of the whole project — how the model is actually doing.
    const nowband = document.getElementById("nowband");

    function formatPrice(value) {
      if (value == null) return "—";
      // Crypto spans 4 orders of magnitude; keep it readable at both ends.
      const digits = value >= 1000 ? 0 : value >= 1 ? 2 : 6;
      return value.toLocaleString("en-US", { minimumFractionDigits: digits,
                                             maximumFractionDigits: digits });
    }

    function bandCell(className, text, tint) {
      const span = document.createElement("span");
      span.className = className;
      span.textContent = text;          // payload data, never markup
      if (tint) span.style.color = tint;
      return span;
    }

    function renderNowBand(state) {
      const prices = state.prices ?? {};
      const symbols = state.symbols ?? {};
      nowband.textContent = "";
      for (const symbol of SYMBOLS) {
        const group = document.createElement("span");
        const mood = symbols[symbol]?.mood ?? "quiet";
        group.appendChild(bandCell("sym", symbol.replace(/USDT$/, "")));
        group.appendChild(document.createTextNode(" "));
        group.appendChild(bandCell("px", formatPrice(prices[symbol])));
        group.appendChild(document.createTextNode(" · "));
        group.appendChild(bandCell("mood", mood, MOOD_COLOR[mood]));
        nowband.appendChild(group);
      }
      // KI-065: the trader's own numbers. Task 10 deleted this readout from
      // the room - rightly, it floated beside the figure with no surface
      // under it - and nothing else in the room or the overlays carried it.
      // The show's premise is that an AI trades crypto live, loses money and
      // publishes every loss forever: this is the loss, so it gets a home
      // rather than staying dropped.
      //
      // `state.trader` is null until a `trader_*` event arrives, and those
      // come from the freqtrade sidecar - an opt-in compose profile. The slot
      // then reads "—" instead of vanishing, exactly as the model's record
      // does when its accuracy is unknown: a named empty slot says "no
      // positions reported", an absent one silently un-publishes the premise.
      const traderState = state.trader;
      const pnl =
        traderState && Number.isFinite(traderState.profit_pct)
          ? traderState.profit_pct
          : null;
      const trader = document.createElement("span");
      trader.className = "trader";
      trader.appendChild(document.createTextNode("trader "));
      const positions = document.createElement("b");
      positions.textContent = traderState
        ? `${traderState.open_trades ?? 0} open · ` +
          (pnl == null ? "—" : `${pnl >= 0 ? "+" : ""}${pnl.toFixed(2)}%`)
        : "—";
      trader.appendChild(positions);
      nowband.appendChild(trader);

      // The honest record, on the same last-N basis as the strips (KI-012).
      const acc = state.model?.accuracy ?? {};
      const record = document.createElement("span");
      record.className = "record";
      const rate = acc.hit_rate == null
        ? "—" : `${(acc.hit_rate * 100).toFixed(1)}%`;
      record.appendChild(document.createTextNode("model "));
      const bold = document.createElement("b");
      bold.textContent = `${acc.wins ?? 0}W ${acc.losses ?? 0}L · ${rate}`;
      record.appendChild(bold);
      record.appendChild(document.createTextNode(` over last ${acc.window ?? 0}`));
      nowband.appendChild(record);
    }

    // --- B5: on-screen commentary -----------------------------------------
    // The director speaks through Piper, but TTS is a deferred ops step and the
    // stream is silent — so a line that isn't drawn may as well not exist. One
    // bubble per line: who said it, in their own accent colour, and what they
    // said, quoted verbatim from the payload.
    const CHARACTER_COLOR = WORLD_CONFIG.character_colors;
    const speech = document.getElementById("speech");
    const BUBBLE_MAX = 3;          // more than this and the room becomes a chat log
    const BUBBLE_HOLD_MS = 7000;   // long enough to read a full line aloud

    function speak(event) {
      const payload = event.payload ?? {};
      const text = payload.text;
      if (!text) return;
      const who = payload.character ?? "someone";
      const bubble = document.createElement("div");
      bubble.className = "bubble";
      bubble.style.borderLeftColor = CHARACTER_COLOR[who] ?? "var(--fg)";

      const name = document.createElement("span");
      name.className = "who";
      name.style.color = CHARACTER_COLOR[who] ?? "var(--fg)";
      // textContent only — payloads are data, never markup, on a public stream.
      name.textContent = who;

      const said = document.createElement("span");
      said.className = "said";
      said.textContent = text;

      bubble.appendChild(name);
      bubble.appendChild(said);
      speech.appendChild(bubble);
      // Next frame, so the transition actually runs instead of snapping in.
      requestAnimationFrame(() => bubble.classList.add("in"));

      while (speech.children.length > BUBBLE_MAX) {
        speech.removeChild(speech.firstChild);
      }
      setTimeout(() => {
        bubble.classList.remove("in");
        setTimeout(() => bubble.remove(), 400);
      }, BUBBLE_HOLD_MS);
    }

export { renderNowBand, speak };
