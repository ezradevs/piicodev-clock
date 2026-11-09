"""
PiicoDev Chess Clock
====================
A two-player chess clock for the Raspberry Pi Pico running MicroPython,
built entirely from PiicoDev modules on one I2C chain:

    OLED SSD1306 (128x64) .... both clocks, menus and messages
    Button ................... the shared "end my turn" button
    Rotary Potentiometer ..... choose the time control
    3x RGB LED ............... whose turn it is + game status
    Buzzer ................... clicks, warnings and alarms
    LIS3DH accelerometer ..... auto-pause if the clock is knocked over

Controls
--------
  Setup screen:   turn the knob to pick a time control, press to start.
                  The last entry is CUSTOM: press it to set your own
                  minutes and increment with the knob (hold ~1 s to go back).
  During a game:  short press      -> end your turn (adds the increment)
                  hold ~1 second   -> pause / resume
                  hold ~3 seconds  -> reset to the setup screen

State machine
-------------
  SETUP <-> CUSTOM_MINUTES <-> CUSTOM_INCREMENT
    |                               |
    v                               v
  RUNNING <-> PAUSED
              |  \
              |   -> KNOCKED_OVER -> (upright + hold) -> RUNNING
              v
           FLAGGED
  Holding the button for ~3 s returns to SETUP from any game state.

Timekeeping
-----------
Each player's remaining time is stored in milliseconds. While a clock runs,
we only remember *when* the turn started and work out the elapsed time with
time.ticks_diff(). Time is never counted by adding up sleep() durations, so
the clock doesn't drift however slow the display refresh is.
"""

import time
from math import sqrt, acos, pi

from PiicoDev_SSD1306 import create_PiicoDev_SSD1306
from PiicoDev_Switch import PiicoDev_Switch
from PiicoDev_Potentiometer import PiicoDev_Potentiometer
from PiicoDev_RGB import PiicoDev_RGB
from PiicoDev_Buzzer import PiicoDev_Buzzer
from PiicoDev_LIS3DH import PiicoDev_LIS3DH
from PiicoDev_Unified import sleep_ms

ticks_ms = time.ticks_ms
ticks_diff = time.ticks_diff
ticks_add = time.ticks_add


# =============================================================================
#  Settings - tweak these to taste
# =============================================================================

# Time controls as (minutes, increment in seconds). The increment is added to
# a player's clock after each of their moves (Fischer increment).
PRESETS = [
    (1, 0), (1, 1), (2, 1), (3, 0), (3, 2), (5, 0), (5, 3),
    (10, 0), (10, 5), (15, 10), (30, 0),
]

# Custom time control: the knob picks from these lists. The last setting is
# saved to the Pico's flash so it's remembered after a power cycle.
CUSTOM_MINUTES_OPTIONS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 15, 20, 25, 30, 40, 45, 60, 90]
CUSTOM_INCREMENT_OPTIONS = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 15, 20, 30]
DEFAULT_CUSTOM = (10, 5)
CUSTOM_FILE = "custom_time.txt"

# Button timing (milliseconds)
TURN_LOCKOUT_MS = 300        # ignore new turn presses this soon after a switch
LONG_PRESS_MS = 1000         # hold this long to pause / resume
VERY_LONG_PRESS_MS = 3000    # hold this long to reset to the setup screen

# Knock-over detection
TILT_ANGLE_DEG = 35          # tilt from resting position that counts as "over"
TILT_CONFIRM_MS = 300        # ...and it must stay tilted this long
UPRIGHT_ANGLE_DEG = 25       # back within this angle counts as upright again
UPRIGHT_CONFIRM_MS = 1000    # ...and it must stay upright this long
TILT_FILTER = 0.5            # smoothing for the gravity vector (0-1, lower = smoother)
IMPACT_TOLERANCE = 0.5       # skip readings more than 50% away from 1 g (bumps, slams)
CALIBRATION_SAMPLES = 10     # readings averaged to find the resting orientation
DEBUG_TILT = False           # True = print live tilt angles to the REPL for tuning

# Clock display and warnings
SHOW_TENTHS_BELOW_MS = 10000  # show "9.4" style below 10 seconds
WARNING_AT_MS = 10000         # warning beep when the running player hits 10 s

# Potentiometer
POT_SMOOTHING = 0.2          # smoothing for the knob reading (0-1, lower = smoother)
POT_HYSTERESIS = 2.0         # dead-band (percent of knob travel) between presets
POT_REVERSED = False         # set True if turning clockwise goes the wrong way

# RGB LEDs
LED_BRIGHTNESS = 30          # global brightness, 0-255 (keep it modest)
LED_WHITE_PIXEL = 0          # LED next to White's side (swap with
LED_STATUS_PIXEL = 1         # LED_BLACK_PIXEL if your module is
LED_BLACK_PIXEL = 2          # mounted the other way around)

# Buzzer
BUZZER_VOLUME = 2            # 0-2 (only used by older buzzer hardware revisions)

# Loop rates (milliseconds between updates)
BUTTON_POLL_MS = 10          # fast, so presses are never missed
ACCEL_POLL_MS = 50           # ~20 Hz
POT_POLL_MS = 50             # ~20 Hz
DISPLAY_MS = 100             # ~10 Hz - OLED.show() is slow over I2C
FLASH_MS = 250               # LED / text flash rate


# =============================================================================
#  Constants
# =============================================================================

SETUP, CUSTOM_MINUTES, CUSTOM_INCREMENT, RUNNING, PAUSED, KNOCKED_OVER, FLAGGED = range(7)
SETUP_STATES = (SETUP, CUSTOM_MINUTES, CUSTOM_INCREMENT)   # before a game starts
WHITE, BLACK = 0, 1
PLAYER_NAMES = ("WHITE", "BLACK")

SHORT_PRESS, LONG_PRESS, VERY_LONG_PRESS = range(3)

GRAVITY = 9.80665            # the LIS3DH driver reports acceleration in m/s^2

# Colours as [red, green, blue], 0-255 (scaled by LED_BRIGHTNESS)
OFF = [0, 0, 0]
TURN_COLOUR = [255, 255, 255]
GREEN = [0, 255, 0]
AMBER = [255, 90, 0]
RED = [255, 0, 0]
DIM_BLUE = [0, 0, 100]

# Sound patterns: lists of (frequency Hz, duration ms). Frequency 0 = silence.
SOUNDS = {
    "click":   [(2200, 25)],
    "select":  [(3000, 8)],
    "start":   [(880, 80), (0, 30), (1320, 140)],
    "pause":   [(1000, 90), (0, 40), (600, 150)],    # falling = stop
    "resume":  [(600, 90), (0, 40), (1000, 150)],    # rising = go
    "reset":   [(500, 250)],
    "knocked": [(700, 150), (0, 100), (700, 150)],   # low double-beep
    "upright": [(1500, 40)],
    "denied":  [(250, 200)],
    "warning": [(1800, 40), (0, 50), (1800, 40), (0, 50), (1800, 40)],
    "flag":    [(2000, 120), (0, 60), (2000, 120), (0, 60), (2000, 120), (0, 400)] * 4,
}


# =============================================================================
#  Large digit renderer
#  The built-in OLED font is only 8x8, so clock digits are drawn as chunky
#  seven-segment shapes out of filled rectangles.
# =============================================================================

#      a
#    f   b
#      g
#    e   c
#      d
SEGMENTS = {
    "0": "abcdef", "1": "bc", "2": "abdeg", "3": "abcdg", "4": "bcfg",
    "5": "acdfg", "6": "acdefg", "7": "abc", "8": "abcdefg", "9": "abcdfg",
}


def big_char_width(ch, w, t):
    """Width in pixels of one large character."""
    if ch in ":.":
        return t
    return w


def big_text_width(text, w, t, gap):
    """Width in pixels of a large-text string."""
    total = sum(big_char_width(ch, w, t) for ch in text)
    return total + gap * (len(text) - 1)


def draw_big_char(oled, ch, x, y, w, h, t):
    """Draw one large character with its top-left corner at (x, y)."""
    half = h // 2
    if ch == ":":
        oled.fill_rect(x, y + h // 3 - t // 2, t, t, 1)
        oled.fill_rect(x, y + 2 * h // 3 - t // 2, t, t, 1)
    elif ch == ".":
        oled.fill_rect(x, y + h - t, t, t, 1)
    elif ch == "+":
        oled.fill_rect(x, y + half - t // 2, w, t, 1)                # bar
        oled.fill_rect(x + (w - t) // 2, y + half - w // 2, t, w, 1)  # stem
    else:
        segs = SEGMENTS.get(ch, "")
        if "a" in segs: oled.fill_rect(x, y, w, t, 1)
        if "d" in segs: oled.fill_rect(x, y + h - t, w, t, 1)
        if "g" in segs: oled.fill_rect(x, y + half - t // 2, w, t, 1)
        if "f" in segs: oled.fill_rect(x, y, t, half + 1, 1)
        if "b" in segs: oled.fill_rect(x + w - t, y, t, half + 1, 1)
        if "e" in segs: oled.fill_rect(x, y + half, t, h - half, 1)
        if "c" in segs: oled.fill_rect(x + w - t, y + half, t, h - half, 1)


def draw_big_text(oled, text, centre_x, y, w, h, t, gap):
    """Draw a large-text string horizontally centred on centre_x."""
    x = centre_x - big_text_width(text, w, t, gap) // 2
    for ch in text:
        draw_big_char(oled, ch, x, y, w, h, t)
        x += big_char_width(ch, w, t) + gap


def draw_small_text(oled, text, centre_x, y, colour=1):
    """Draw built-in 8x8 font text horizontally centred on centre_x."""
    oled.text(text, centre_x - len(text) * 4, y, colour)


def format_time(ms):
    """Format remaining time as M:SS, or S.T (tenths) when under 10 seconds."""
    if ms < 0:
        ms = 0
    if ms < SHOW_TENTHS_BELOW_MS:
        tenths = ms // 100
        return "{}.{}".format(tenths // 10, tenths % 10)
    secs = ms // 1000
    return "{}:{:02d}".format(secs // 60, secs % 60)


def format_preset(preset):
    minutes, increment = preset
    return "{}+{}".format(minutes, increment)


def load_custom():
    """Read the saved custom time control, or fall back to the default."""
    try:
        with open(CUSTOM_FILE) as f:
            minutes, increment = (int(v) for v in f.read().split(","))
        if minutes in CUSTOM_MINUTES_OPTIONS and increment in CUSTOM_INCREMENT_OPTIONS:
            return (minutes, increment)
    except (OSError, ValueError):
        pass                               # no file yet, or it's corrupted
    return DEFAULT_CUSTOM


def save_custom(custom):
    try:
        with open(CUSTOM_FILE, "w") as f:
            f.write("{},{}".format(custom[0], custom[1]))
    except OSError:
        print("Warning: couldn't save custom time control")


# =============================================================================
#  Helper classes
# =============================================================================

class SoundPlayer:
    """Plays beep patterns without ever blocking the main loop.

    The buzzer module times each tone itself (tone(freq, duration)), so all we
    do is send the next note once the previous one has finished.
    """

    def __init__(self, buzzer):
        self.buzzer = buzzer
        self.steps = []
        self.next_ms = ticks_ms()

    def play(self, name):
        """Start a pattern from SOUNDS, replacing whatever was playing."""
        self.steps = list(SOUNDS[name])
        self.next_ms = ticks_ms()

    def update(self, now):
        if self.steps and ticks_diff(now, self.next_ms) >= 0:
            freq, duration = self.steps.pop(0)
            if freq:
                self.buzzer.tone(freq, duration)
            else:
                self.buzzer.noTone()
            self.next_ms = ticks_add(now, duration)


class PressDetector:
    """Turns raw button polling into short / long / very-long press events.

    - A short press is reported on release, and only if it never reached the
      long-press threshold.
    - Long and very-long presses are reported the moment the hold time
      crosses their threshold, while the button is still held down.
    So a long press can never also count as a short press.
    """

    def __init__(self, button):
        self.button = button
        self.is_down = False
        self.down_ms = 0          # when the current (or last) press started
        self.long_sent = False
        self.very_long_sent = False

    def poll(self, now):
        """Read the button once. Returns a press event, or None."""
        pressed = self.button.is_pressed

        if pressed and not self.is_down:            # just pressed
            self.is_down = True
            self.down_ms = now
            self.long_sent = False
            self.very_long_sent = False
            return None

        if pressed and self.is_down:                # still held
            held = ticks_diff(now, self.down_ms)
            if held >= VERY_LONG_PRESS_MS and not self.very_long_sent:
                self.very_long_sent = True
                self.long_sent = True
                return VERY_LONG_PRESS
            if held >= LONG_PRESS_MS and not self.long_sent:
                self.long_sent = True
                return LONG_PRESS
            return None

        if not pressed and self.is_down:            # just released
            self.is_down = False
            if not self.long_sent:
                return SHORT_PRESS
        return None

    def held_ms(self, now):
        """How long the button has been held (0 if it isn't pressed)."""
        return ticks_diff(now, self.down_ms) if self.is_down else 0


class KnobSelector:
    """Maps the knob to one of `count` choices, with smoothing and hysteresis.

    The knob's 0-100 travel is split into equal bands, one per choice. To
    move to a neighbouring choice the smoothed reading has to go
    POT_HYSTERESIS past the edge of the current band, so a knob resting on
    a boundary can't flicker between two choices.
    """

    def __init__(self, pot, count):
        self.pot = pot
        self.count = count
        self.band = 100.0 / count
        self.filtered = None
        self.index = 0

    def update(self):
        """Read the knob. Returns True if the selected preset changed."""
        value = self.pot.value
        if value != value:                 # NaN means the read failed
            return False
        if POT_REVERSED:
            value = 100 - value

        if self.filtered is None:          # first reading: jump straight there
            self.filtered = value
            self.index = self._band_of(value)
            return True
        self.filtered += POT_SMOOTHING * (value - self.filtered)

        low_edge = self.index * self.band - POT_HYSTERESIS
        high_edge = (self.index + 1) * self.band + POT_HYSTERESIS
        if low_edge <= self.filtered <= high_edge:
            return False
        new_index = self._band_of(self.filtered)
        changed = new_index != self.index
        self.index = new_index
        return changed

    def _band_of(self, value):
        return min(self.count - 1, max(0, int(value / self.band)))


class TiltMonitor:
    """Measures how far the clock has tilted from its resting orientation.

    Gravity always points down, so the angle between the current gravity
    vector and the resting one is how far the clock has tipped. Two things
    stop a hard button slam from looking like a tip-over:
      1. Readings far from 1 g (impacts, vibration) are skipped entirely.
      2. The remaining readings are smoothed, and the game logic also
         requires the tilt to last for TILT_CONFIRM_MS.
    """

    def __init__(self, accel):
        self.accel = accel
        self.reference = None
        self.filtered = None

    def _read(self):
        try:
            return self.accel.acceleration
        except Exception:                  # I2C hiccup: skip this sample
            return None

    def calibrate(self):
        """Average a few readings to record the resting orientation."""
        sx = sy = sz = 0.0
        n = 0
        for _ in range(CALIBRATION_SAMPLES):
            a = self._read()
            if a is not None:
                sx += a[0]
                sy += a[1]
                sz += a[2]
                n += 1
            sleep_ms(20)
        if n == 0:
            self.reference = None          # no accelerometer: detection off
            print("Warning: accelerometer not responding, knock detection disabled")
            return
        self.reference = (sx / n, sy / n, sz / n)
        self.filtered = list(self.reference)

    def angle(self):
        """Smoothed tilt from rest in degrees, or None if this sample was unusable."""
        if self.reference is None:
            return None
        a = self._read()
        if a is None:
            return None
        magnitude = sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2])
        if abs(magnitude - GRAVITY) > IMPACT_TOLERANCE * GRAVITY:
            if DEBUG_TILT:
                print("tilt: skipped, |a| = {:.1f} m/s^2".format(magnitude))
            return None                    # being bumped / slammed / thrown

        f = self.filtered
        for i in range(3):
            f[i] += TILT_FILTER * (a[i] - f[i])

        r = self.reference
        dot = f[0] * r[0] + f[1] * r[1] + f[2] * r[2]
        lengths = sqrt(f[0] * f[0] + f[1] * f[1] + f[2] * f[2]) * \
            sqrt(r[0] * r[0] + r[1] * r[1] + r[2] * r[2])
        if lengths == 0:
            return None
        cos_angle = max(-1.0, min(1.0, dot / lengths))
        angle = acos(cos_angle) * 180 / pi
        if DEBUG_TILT:
            print("tilt: {:.0f} deg  (|a| = {:.1f} m/s^2)".format(angle, magnitude))
        return angle


# =============================================================================
#  The chess clock
# =============================================================================

class ChessClock:

    def __init__(self):
        self._connect_hardware()

        self.sound = SoundPlayer(self.buzzer)
        self.presses = PressDetector(self.button)
        # One extra knob position after the presets for the CUSTOM entry.
        self.selector = KnobSelector(self.pot, len(PRESETS) + 1)
        self.custom_index = len(PRESETS)
        self.custom = load_custom()        # (minutes, increment)
        self.edit_knob = None              # knob selector while editing custom time
        self.tilt = TiltMonitor(self.accel)

        self.state = SETUP
        self.time_left = [0, 0]    # ms remaining, as of turn_start for the running player
        self.turn = WHITE
        self.turn_start = 0        # ticks_ms when the running clock last started
        self.increment_ms = 0
        self.move_number = 1
        self.last_switch = 0       # ticks_ms of the last turn switch (for the lockout)
        self.warned = [False, False]
        self.tilt_since = None     # when a possible tip-over was first seen
        self.upright_since = None  # when the clock was first seen upright again
        self.upright = False       # knocked over, but now confirmed upright
        self.last_leds = None

    # ----------------------------------------------------------------- setup

    def _connect_hardware(self):
        # Every module uses its default I2C address (all ID / ASW switches OFF).
        self.oled = create_PiicoDev_SSD1306()
        self.oled.fill(0)
        draw_small_text(self.oled, "Chess Clock", 64, 20)
        draw_small_text(self.oled, "starting...", 64, 36)
        self.oled.show()

        try:
            self.button = PiicoDev_Switch()
            # The button driver reports "pressed" if it can't be reached, so
            # make sure it's really there before relying on it.
            if self.button.whoami is None:
                raise OSError("no reply")
            self.pot = PiicoDev_Potentiometer()
            if self.pot.whoami is None:
                raise OSError("no reply")
            self.leds = PiicoDev_RGB(bright=LED_BRIGHTNESS)
            self.buzzer = PiicoDev_Buzzer(volume=BUZZER_VOLUME)
            self.accel = PiicoDev_LIS3DH()
        except Exception as e:
            print("Hardware not found:", e)
            self.oled.fill(0)
            draw_small_text(self.oled, "Module missing!", 64, 16)
            draw_small_text(self.oled, "Check cables and", 64, 32)
            draw_small_text(self.oled, "ID switches", 64, 44)
            self.oled.show()
            raise

    # ------------------------------------------------------------ timekeeping

    def remaining(self, player, now):
        """Milliseconds left on a player's clock right now."""
        if self.state == RUNNING and player == self.turn:
            return self.time_left[player] - ticks_diff(now, self.turn_start)
        return self.time_left[player]

    def _stop_clock_at(self, moment):
        """Charge the running player for time used up to `moment`."""
        used = ticks_diff(moment, self.turn_start)
        if used > 0:
            self.time_left[self.turn] -= used
            self.turn_start = moment

    # ------------------------------------------------------------ transitions

    def edit_custom(self, state):
        """Enter one of the custom-time editing steps."""
        options = CUSTOM_MINUTES_OPTIONS if state == CUSTOM_MINUTES else CUSTOM_INCREMENT_OPTIONS
        self.state = state
        self.edit_knob = KnobSelector(self.pot, len(options))
        self.edit_knob.update()            # the knob is absolute: start where it points
        self._apply_edit_knob()

    def _apply_edit_knob(self):
        """Copy the knob's current choice into the custom time control."""
        minutes, increment = self.custom
        if self.state == CUSTOM_MINUTES:
            minutes = CUSTOM_MINUTES_OPTIONS[self.edit_knob.index]
        else:
            increment = CUSTOM_INCREMENT_OPTIONS[self.edit_knob.index]
        self.custom = (minutes, increment)

    def start_game(self, minutes, increment):
        self.time_left = [minutes * 60000, minutes * 60000]
        self.increment_ms = increment * 1000
        self.turn = WHITE
        self.move_number = 1
        self.warned = [False, False]
        self.tilt_since = None

        # Capture the resting orientation before the clock starts running.
        self.oled.fill(0)
        draw_small_text(self.oled, "Hold still...", 64, 28)
        self.oled.show()
        self.tilt.calibrate()
        if self.tilt.reference is None:
            # Make a dead accelerometer obvious rather than silently
            # playing without knock-over protection.
            self.oled.fill(0)
            draw_small_text(self.oled, "Tilt sensor", 64, 20)
            draw_small_text(self.oled, "not responding", 64, 32)
            self.oled.show()
            sleep_ms(1500)

        now = ticks_ms()
        self.turn_start = now
        self.last_switch = now     # the start press also gets the lockout
        self.state = RUNNING
        self.sound.play("start")

    def end_turn(self, now):
        """The running player has moved: add their increment, start the other clock."""
        self._stop_clock_at(now)
        if self.time_left[self.turn] <= 0:
            self.flag()            # out of time before the press arrived
            return
        self.time_left[self.turn] += self.increment_ms
        if self.time_left[self.turn] > WARNING_AT_MS + 2000:
            self.warned[self.turn] = False    # increment lifted them clear of the warning
        if self.turn == BLACK:
            self.move_number += 1
        self.turn = BLACK if self.turn == WHITE else WHITE
        self.turn_start = now
        self.last_switch = now
        self.sound.play("click")

    def pause(self, moment):
        # Pause from when the button went *down*, so the 1-second hold
        # doesn't cost the running player any time.
        self._stop_clock_at(moment)
        self.state = PAUSED
        self.sound.play("pause")

    def resume(self, now):
        self.turn_start = now
        self.tilt_since = None
        self.state = RUNNING
        self.sound.play("resume")

    def knock_over(self):
        # Stop the clock at the moment the tilt was first seen, not when it
        # was confirmed, so nobody loses the confirmation time.
        self._stop_clock_at(self.tilt_since)
        self.state = KNOCKED_OVER
        self.upright = False
        self.upright_since = None
        self.sound.play("knocked")

    def flag(self):
        self.time_left[self.turn] = 0
        self.state = FLAGGED
        self.sound.play("flag")

    def reset(self):
        self.state = SETUP
        self.sound.play("reset")

    # ---------------------------------------------------------------- inputs

    def handle_press(self, event, now):
        state = self.state

        if state == SETUP:
            if event == SHORT_PRESS:
                if self.selector.index == self.custom_index:
                    self.sound.play("select")
                    self.edit_custom(CUSTOM_MINUTES)
                else:
                    self.start_game(*PRESETS[self.selector.index])
            return

        # Custom time editor: press = next step, hold = back one step.
        if state == CUSTOM_MINUTES:
            if event == SHORT_PRESS:
                self.sound.play("select")
                self.edit_custom(CUSTOM_INCREMENT)
            elif event == LONG_PRESS:
                self.state = SETUP
                self.sound.play("reset")
            return

        if state == CUSTOM_INCREMENT:
            if event == SHORT_PRESS:
                save_custom(self.custom)
                self.start_game(*self.custom)
            elif event == LONG_PRESS:
                self.sound.play("reset")
                self.edit_custom(CUSTOM_MINUTES)
            return

        # From any game state, a very long hold goes back to setup.
        if event == VERY_LONG_PRESS:
            self.reset()
            return

        if state == RUNNING:
            if event == SHORT_PRESS:
                # Lockout: a press that *started* within TURN_LOCKOUT_MS of
                # the last switch is a bounce or double-tap, so ignore it.
                if ticks_diff(self.presses.down_ms, self.last_switch) >= TURN_LOCKOUT_MS:
                    self.end_turn(now)
            elif event == LONG_PRESS:
                self.pause(self.presses.down_ms)

        elif state == PAUSED:
            if event == LONG_PRESS:
                self.resume(now)

        elif state == KNOCKED_OVER:
            if event == LONG_PRESS:
                if self.upright:
                    self.resume(now)
                else:
                    self.sound.play("denied")
        # FLAGGED: only the very long press (reset) does anything.

    def update_tilt(self, now):
        angle = self.tilt.angle()
        if angle is None:          # unusable sample: keep the current state
            return

        if self.state == RUNNING:
            if angle > TILT_ANGLE_DEG:
                if self.tilt_since is None:
                    self.tilt_since = now
                elif ticks_diff(now, self.tilt_since) >= TILT_CONFIRM_MS:
                    self.knock_over()
            else:
                self.tilt_since = None

        elif self.state == KNOCKED_OVER:
            if angle < UPRIGHT_ANGLE_DEG:
                if self.upright_since is None:
                    self.upright_since = now
                elif not self.upright and \
                        ticks_diff(now, self.upright_since) >= UPRIGHT_CONFIRM_MS:
                    self.upright = True
                    self.sound.play("upright")
            else:
                self.upright_since = None
                self.upright = False

    def check_clock(self, now):
        """Flag fall and low-time warning for the running player."""
        left = self.remaining(self.turn, now)
        if left <= 0:
            self._stop_clock_at(now)
            self.flag()
        elif left <= WARNING_AT_MS and not self.warned[self.turn]:
            self.warned[self.turn] = True
            self.sound.play("warning")

    # ---------------------------------------------------------------- output

    def draw(self, now):
        oled = self.oled
        oled.fill(0)
        if self.state == SETUP:
            self._draw_setup()
        elif self.state in (CUSTOM_MINUTES, CUSTOM_INCREMENT):
            self._draw_custom_editor()
        elif self.state == KNOCKED_OVER:
            self._draw_knocked_over(now)
        else:
            self._draw_clocks(now)
        oled.show()

    def _draw_setup(self):
        oled = self.oled
        is_custom = self.selector.index == self.custom_index
        if is_custom:
            draw_small_text(oled, "CUSTOM", 64, 0)
            preset = self.custom
        else:
            draw_small_text(oled, "TIME CONTROL", 64, 0)
            preset = PRESETS[self.selector.index]
        draw_big_text(oled, format_preset(preset), 64, 12, w=18, h=32, t=4, gap=4)

        # A row of dots shows where you are in the list (last dot = custom).
        count = len(PRESETS) + 1
        x = 64 - (count * 8) // 2
        for i in range(count):
            if i == self.selector.index:
                oled.fill_rect(x + 1, 47, 5, 5, 1)   # selected: big filled dot
            else:
                oled.rect(x + 2, 48, 3, 3, 1)        # others: small hollow dot
            x += 8
        draw_small_text(oled, "press to edit" if is_custom else "press to start", 64, 56)

    def _draw_custom_editor(self):
        """Show the custom time with the field being edited underlined."""
        oled = self.oled
        editing_minutes = self.state == CUSTOM_MINUTES
        draw_small_text(oled, "SET MINUTES" if editing_minutes else "SET INCREMENT", 64, 0)

        w, t, gap = 18, 4, 4
        minutes_text = str(self.custom[0])
        increment_text = str(self.custom[1])
        text = minutes_text + "+" + increment_text
        draw_big_text(oled, text, 64, 12, w=w, h=32, t=t, gap=gap)

        # Work out where each field was drawn so we can underline one of them.
        left = 64 - big_text_width(text, w, t, gap) // 2
        if editing_minutes:
            x = left
            width = big_text_width(minutes_text, w, t, gap)
        else:
            x = left + big_text_width(minutes_text + "+", w, t, gap) + gap
            width = big_text_width(increment_text, w, t, gap)
        oled.fill_rect(x, 47, width, 3, 1)

        draw_small_text(oled, "press: next" if editing_minutes else "press: start", 64, 56)

    def _draw_clocks(self, now):
        oled = self.oled
        flash_on = (now // FLASH_MS) % 2 == 0

        for player in (WHITE, BLACK):
            left_x = 0 if player == WHITE else 64
            centre_x = left_x + 32

            # Player name, inverted for whoever's turn it is (flashing for the
            # player who lost on time).
            highlight = player == self.turn
            if self.state == FLAGGED and player == self.turn:
                highlight = flash_on
            if highlight:
                oled.fill_rect(left_x, 0, 64, 11, 1)
            draw_small_text(oled, PLAYER_NAMES[player], centre_x, 2, 0 if highlight else 1)

            draw_big_text(oled, format_time(self.remaining(player, now)),
                          centre_x, 16, w=11, h=22, t=3, gap=2)

            if player == self.turn:
                oled.fill_rect(left_x + 8, 42, 48, 2, 1)   # turn underline

        oled.vline(63, 13, 33, 1)                          # divider
        self._draw_status_line(now, flash_on)

    def _draw_status_line(self, now, flash_on):
        oled = self.oled
        held = self.presses.held_ms(now)

        # While the button is held past the long-press time, show progress
        # towards a reset so players know what will happen if they keep holding.
        if held >= LONG_PRESS_MS:
            fraction = min(1.0, (held - LONG_PRESS_MS) / (VERY_LONG_PRESS_MS - LONG_PRESS_MS))
            oled.text("RESET", 0, 55, 1)
            oled.rect(44, 55, 84, 8, 1)
            oled.fill_rect(44, 55, int(84 * fraction), 8, 1)
            return

        alternate = (now // 1500) % 2 == 0
        if self.state == RUNNING:
            draw_small_text(oled, "Move {}".format(self.move_number), 64, 55)
        elif self.state == PAUSED:
            oled.fill_rect(0, 53, 128, 11, 1)
            draw_small_text(oled, "PAUSED" if alternate else "hold to resume", 64, 55, 0)
        elif self.state == FLAGGED:
            if alternate:
                draw_small_text(oled, "FLAG! {} LOST".format(PLAYER_NAMES[self.turn]), 64, 55)
            else:
                draw_small_text(oled, "hold 3s: reset", 64, 55)

    def _draw_knocked_over(self, now):
        oled = self.oled
        flash_on = (now // FLASH_MS) % 2 == 0
        if self.upright:
            draw_small_text(oled, "Upright -", 64, 8)
            draw_small_text(oled, "hold to resume", 64, 22)
        else:
            if flash_on:
                oled.fill_rect(0, 4, 128, 14, 1)
            draw_small_text(oled, "KNOCKED OVER", 64, 7, 0 if flash_on else 1)
            draw_small_text(oled, "set upright", 64, 26)

        oled.hline(0, 40, 128, 1)
        times = "W {}  B {}".format(format_time(self.time_left[WHITE]),
                                     format_time(self.time_left[BLACK]))
        draw_small_text(oled, times, 64, 50)

    def update_leds(self, now):
        flash_on = (now // FLASH_MS) % 2 == 0
        white = black = OFF

        if self.state in SETUP_STATES:
            status = DIM_BLUE
        else:
            if self.turn == WHITE:
                white = TURN_COLOUR
            else:
                black = TURN_COLOUR
            if self.state == RUNNING:
                status = GREEN
            elif self.state == PAUSED:
                status = AMBER
            elif self.state == KNOCKED_OVER:
                status = AMBER if (self.upright or flash_on) else OFF
            else:  # FLAGGED
                status = RED if flash_on else OFF

        colours = (white, status, black)
        if colours == self.last_leds:
            return                                 # skip pointless I2C writes
        try:
            self.leds.setPixel(LED_WHITE_PIXEL, white)
            self.leds.setPixel(LED_STATUS_PIXEL, status)
            self.leds.setPixel(LED_BLACK_PIXEL, black)
            self.leds.show()
            self.last_leds = colours
        except OSError:
            pass                                   # try again next refresh

    # ------------------------------------------------------------- main loop

    def run(self):
        now = ticks_ms()
        next_button = next_accel = next_pot = next_display = now

        while True:
            now = ticks_ms()

            # 1. Button: polled fast so no press is missed.
            if ticks_diff(now, next_button) >= 0:
                next_button = ticks_add(now, BUTTON_POLL_MS)
                event = self.presses.poll(now)
                if event is not None:
                    self.handle_press(event, ticks_ms())
                    next_display = now             # redraw right away

            # 2. Knob: only matters before a game starts.
            if self.state in SETUP_STATES and ticks_diff(now, next_pot) >= 0:
                next_pot = ticks_add(now, POT_POLL_MS)
                if self.state == SETUP:
                    changed = self.selector.update()
                else:
                    changed = self.edit_knob.update()
                    if changed:
                        self._apply_edit_knob()
                if changed:
                    self.sound.play("select")
                    next_display = now

            # 3. Accelerometer: watch for tip-overs during play.
            if self.state in (RUNNING, KNOCKED_OVER) and ticks_diff(now, next_accel) >= 0:
                next_accel = ticks_add(now, ACCEL_POLL_MS)
                self.update_tilt(now)

            # 4. Flag fall and low-time warning.
            if self.state == RUNNING:
                self.check_clock(ticks_ms())

            # 5. Sound sequencer (non-blocking).
            self.sound.update(now)

            # 6. Display and LEDs at a gentle refresh rate.
            if ticks_diff(now, next_display) >= 0:
                next_display = ticks_add(now, DISPLAY_MS)
                self.update_leds(now)
                self.draw(now)

            sleep_ms(1)


if __name__ == "__main__":
    ChessClock().run()
