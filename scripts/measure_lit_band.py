"""The lit band delivers the LAMP'S HUE, not the volume's own.

Re-runnable evidence for character-redesign increment 9. `shade(base, level)`
mixes `base` toward `LIGHT.warmth` by an ABSOLUTE 0.3 for the lit band, so
every cool volume's lit band lands on the lamp's hue (34 deg). Verified
against pixels sampled from a live /world frame, and against the reference art.

    python3 lit_band_measure.py
"""
import colorsys

WARMTH, AMBIENT = "#f0a848", "#1a2030"
RAMP = {"lit": 0.3, "base": 0.0, "shade": -0.34}
HUMAN = {
    "hair": "#4a3528",
    "skin": "#c08f68",
    "sweater": "#8a97a8",
    "jeans": "#5a6478",
    "shoes": "#2a2e3c",
}
# Dominant colours counted off rest.png (1920x960, /world on :8077, 2026-09-21).
MEASURED = {"shoes.lit": "#655340", "skin.lit": "#ce975e", "skin.shade": "#886955"}


def rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def hexs(r):
    return "#%02x%02x%02x" % tuple(int(round(x)) for x in r)


def mix(a, b, t):
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


def hsv(r):
    h, s, v = colorsys.rgb_to_hsv(*[x / 255 for x in r])
    return h * 360, s, v


def hue_gap(a, b):
    d = abs(a - b) % 360
    return min(d, 360 - d)


def band(base_hex, rung):
    level = RAMP[rung]
    toward = rgb(WARMTH) if level >= 0 else rgb(AMBIENT)
    return hexs(mix(rgb(base_hex), toward, abs(level)))


def main():
    lamp_hue = hsv(rgb(WARMTH))[0]
    print(f"lamp warmth {WARMTH} hue {lamp_hue:.0f}   ambient {AMBIENT}\n")
    head = ("volume", "authored", "hue", "val", "lit band", "hue", "val",
            "d_hue", "->lamp")
    print("%-8s %-9s %5s %5s | %-9s %5s %5s | %6s %7s" % head)
    for name, authored in HUMAN.items():
        lit = band(authored, "lit")
        bh, _, bv = hsv(rgb(authored))
        lh, _, lv = hsv(rgb(lit))
        print("%-8s %-9s %5.0f %5.2f | %-9s %5.0f %5.2f | %6.0f %7.0f"
              % (name, authored, bh, bv, lit, lh, lv,
                 hue_gap(bh, lh), hue_gap(lh, lamp_hue)))

    print("\nprediction vs frame:")
    for key, seen in MEASURED.items():
        vol, rung = key.split(".")
        print(f"  {key:<12} predicted {band(HUMAN[vol], rung)}  measured {seen}")


if __name__ == "__main__":
    main()
