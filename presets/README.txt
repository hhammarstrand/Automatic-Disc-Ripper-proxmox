HandBrake Custom Presets
========================

Place JSON preset files here (.json).

How to export a preset from HandBrake GUI:
  1. Open HandBrake
  2. Go to Presets
  3. Right-click your preset -> Export to file
  4. Save the .json file in this folder

Then configure the path in Automatic Disc Ripper:
  - Via web: Settings -> Encoding -> HandBrake preset file
  - Or in config/adr.yaml:
      handbrake_preset_file: "/opt/adr/presets/my-preset.json"
      handbrake_preset: "My Preset Name"

NOTE: The preset name in handbrake_preset must match the name
inside the JSON file exactly.
