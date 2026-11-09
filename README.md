# PiicoDev Chess Clock

A small, portable chess clock built from a Raspberry Pi Pico and PiicoDev modules.

## Hardware

- Raspberry Pi Pico + PiicoDev Expansion Board
- PiicoDev OLED Display (SSD1306, 128x64)
- PiicoDev Button
- PiicoDev Rotary Potentiometer
- PiicoDev 3x RGB LED Module
- PiicoDev Buzzer
- PiicoDev 3-Axis Accelerometer (LIS3DH)

Daisy-chain everything with PiicoDev cables, in any order. Leave all ID switches OFF (default addresses).

## Install

Copy these files onto the Pico (Thonny or the MicroPico extension for VS Code):

```
main.py
PiicoDev_Unified.py
PiicoDev_SSD1306.py
PiicoDev_Switch.py
PiicoDev_Potentiometer.py
PiicoDev_RGB.py
PiicoDev_Buzzer.py
PiicoDev_LIS3DH.py
```

`main.py` runs automatically on boot.

## Controls

| When | Action | Result |
|---|---|---|
| Setup | Turn knob | Pick a time control (or CUSTOM) |
| Setup | Press | Start the game |
| In game | Press | End your turn (adds increment) |
| In game | Hold 1 s | Pause / resume |
| In game | Hold 3 s | Back to setup |

If the clock is knocked over mid-game, it pauses automatically. Stand it upright, wait for the beep, then hold to resume.

## Settings

Time controls, button timings, tilt sensitivity, LED brightness and more are constants at the top of `main.py`.

## Credits

PiicoDev drivers by [Core Electronics](https://github.com/CoreElectronics).
