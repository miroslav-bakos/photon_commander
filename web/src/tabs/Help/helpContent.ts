// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

export interface ManualTopic {
  id: string;
  title: string;
  route: string;
  summary: string;
  capabilities: string[];
  steps: string[];
  notes: string[];
}

export interface GuideTopic {
  id: string;
  title: string;
  summary: string;
  steps: string[];
  relatedManualIds: string[];
}

export interface KnowledgeArticle {
  id: string;
  title: string;
  category: "Concepts" | "Troubleshooting" | "Safety";
  body: string[];
  keywords: string[];
  relatedManualIds: string[];
}

export const manualTopics: ManualTopic[] = [
  {
    id: "dashboard",
    title: "Dashboard",
    route: "/dashboard",
    summary: "See the current observatory, capture, plan, and live-stack state at a glance.",
    capabilities: [
      "Monitor mount pointing and motion, focus, filter, dew heater, dithering, and camera activity in real time.",
      "See device connection dots, the active plan or unplanned capture, and accepted or rejected live-stack frames.",
      "Read current and recent image-quality measurements without leaving the overview.",
    ],
    steps: [
      "Open Dashboard after connecting the telescope; green corner dots show connected devices and red dots show unavailable ones.",
      "Look for accent-highlighted cards to identify tracking, motion, heating, dithering, capture, planning, or stacking activity.",
      "Open the corresponding workflow screen when you need controls or detailed diagnostics.",
    ],
    notes: [
      "Dashboard is an overview, not a second control surface.",
      "Unknown values stay explicit when a device or weather provider has not supplied data.",
    ],
  },
  {
    id: "planning",
    title: "Planning",
    route: "/planning",
    summary: "Choose a target and find out when, where, and how well it can be observed from your site.",
    capabilities: [
      "Search catalogues or enter RA and Dec, inspect the sky map, and move the framing rectangle.",
      "Compare target altitude, twilight, Moon separation, horizon limits, field of view, and mosaic coverage.",
      "Choose an optic, mount mode, session start, and length, then send the result to Scheduling.",
    ],
    steps: [
      "Set the observing location and timezone in Settings first.",
      "Pick a target, optical profile, mount mode, and start time or solar event.",
      "Review framing, visibility, Moon conditions, and the night timeline. Drag the time controls to inspect the session.",
      "Adjust a mosaic if one frame is not enough, then send the target to Scheduling when the plan looks practical.",
    ],
    notes: [
      "Planning predicts geometry; it does not move the telescope or start a capture.",
      "A wrong location, timezone, horizon, optic, or mount mode makes otherwise valid calculations misleading.",
    ],
  },
  {
    id: "scheduling",
    title: "Scheduling",
    route: "/scheduling",
    summary: "Build, validate, and time an observing plan for a particular night.",
    capabilities: [
      "Create, rename, and delete plans made from start-up, target, calibration, wait, and shutdown items.",
      "Use fixed times or offsets from solar events, inspect the compiled timeline, and resolve conflicts before observing.",
      "Edit target and capture details and save reusable plans.",
    ],
    steps: [
      "Create or select a plan and choose the observing night.",
      "Add items in the order they should run. Open an item to edit its timing and settings.",
      "Read Plan check and the timeline after each change; select a diagnostic to return to the affected item.",
      "Resolve errors and overlaps before relying on the schedule at the telescope.",
    ],
    notes: [
      "Changing the night can move event-relative items because dusk, dawn, and transit times change.",
      "A plan saved here is preparation; confirm device state and conditions before execution.",
    ],
  },
  {
    id: "mount",
    title: "Control",
    route: "/mount",
    summary: "Read and control the connected telescope, focuser, filter wheel, and dew heater.",
    capabilities: [
      "Read coordinates and mount state, jog either supported axis, and stop motion immediately.",
      "Turn tracking on or off, park or home the mount, focus, select a filter, and manage dew protection.",
      "Configure bounded, settled dithering for capture loops.",
      "Hide controls the connected device reports as unsupported.",
    ],
    steps: [
      "Connect the telescope in Settings and wait for a mount reading.",
      "Check the requested jog rate, surroundings, and cable clearance before holding a direction control.",
      "Set the filter, focus, dew protection, and dithering policy needed for the session.",
      "Use Stop whenever motion is unexpected, then investigate before moving again.",
    ],
    notes: [
      "Keep people, the tripod, cables, and hard stops clear whenever a movement command is possible.",
      "Park and home are different operations; confirm the final safe state before leaving the equipment unattended.",
    ],
  },
  {
    id: "polar-align",
    title: "Polar align",
    route: "/polar-align",
    summary: "Measure polar-axis error from several plate-solved pointings and translate it into mount-knob corrections.",
    capabilities: [
      "Choose exposure, gain, point count, total arc, direction, and an automatic or manual start point.",
      "Solve the current position, run a multi-point measurement, inspect each solve, and read altitude and azimuth corrections.",
      "Use correction guidance expressed for the physical adjustment knobs.",
    ],
    steps: [
      "Level and roughly polar-point the equatorial mount, focus the camera, and confirm plate solving works with Solve here.",
      "Choose a safe sweep with enough sky clearance, then set exposure and point count.",
      "Start the measurement and watch each point. Do not touch the mount while the automatic sweep is running.",
      "Apply the reported physical altitude and azimuth corrections, then measure again until the result is acceptable.",
    ],
    notes: [
      "Verify clearance across the entire requested arc before starting; be ready to stop the mount.",
      "Clouds, poor focus, too few stars, movement during exposure, and an obstructed sky can prevent reliable solves.",
    ],
  },
  {
    id: "capture",
    title: "Capture",
    route: "/live",
    summary: "Operate camera exposures while monitoring the current session and latest preview.",
    capabilities: [
      "Start light, dark, bias, and flat sequences and monitor progress and the latest preview.",
      "Review current exposure, calibration, loop, and camera-pipeline state.",
      "Inspect the latest frame preview and quality measurements.",
    ],
    steps: [
      "Confirm the telescope and required Alpaca devices are connected in the always-visible footer and Devices panel.",
      "Choose frame type, exposure, gain, frame count, filter, and output settings appropriate to the session.",
      "Configure focus, filter, dew protection, and dithering in Control, then test a short exposure before starting a long sequence.",
      "Monitor Capture status and preview; stop the sequence if weather, clearance, focus, or tracking becomes unsafe.",
    ],
    notes: [
      "Dark and bias frames must receive no light. Flats require an evenly illuminated field and exposure appropriate to the panel.",
      "Dithering needs working mount control; recentering needs images that can be solved reliably.",
    ],
  },
  {
    id: "live-stacking",
    title: "Live stacking",
    route: "/live-stacking",
    summary: "Register and combine incoming light frames into a progressively deeper live image.",
    capabilities: [
      "Watch the current Photon Commander capture session or another server-side FITS folder, optionally including subfolders.",
      "Apply selected or automatically matched dark, bias, and flat masters and choose full or 2×2 resolution.",
      "Inspect, pan, zoom, auto-crop, reset, save, and stop a stack.",
    ],
    steps: [
      "Use the current capture session or browse to a server-side folder containing compatible light FITS files.",
      "Choose calibration masters, or enable automatic compatible-master selection; set the optic ID when flat matching needs it.",
      "Start a new stack, then capture or place new light frames in the watched folder.",
      "Watch the frame count and errors, inspect the image, and save the result before stopping if you want a retained output.",
    ],
    notes: [
      "Frames need compatible dimensions and enough shared stars for registration. A watched path is on the Photon Commander server, not the phone.",
      "A mismatched master can make the image worse; use matching gain, exposure, temperature, optic, and orientation where applicable.",
    ],
  },
  {
    id: "streaming",
    title: "Streaming",
    route: "/streaming",
    summary: "Preview and control the configured RTSP and YouTube broadcast of Photon Commander imagery.",
    capabilities: [
      "Show the active stream source, preview, RTSP address, connection count, and encoder errors.",
      "Relay the Photon Commander picture to a reusable YouTube Live broadcast after a stream key is configured.",
      "Use the live stack or supported wide camera as the configured source.",
    ],
    steps: [
      "Configure Broadcasting in Settings, including the RTSP behavior and any YouTube stream key.",
      "Start the source workflow and confirm the Photon Commander broadcast is active and its preview is correct.",
      "Verify the YouTube broadcast privacy in YouTube Studio, then start the relay from this screen.",
      "Stop the YouTube relay when finished and confirm that external clients have disconnected as expected.",
    ],
    notes: [
      "Photon Commander cannot set YouTube privacy through a stream key. Confirm Unlisted or the privacy you intend before going live.",
      "The YouTube relay sends silent audio with the video; it does not capture a microphone or telescope sound.",
    ],
  },
  {
    id: "settings",
    title: "Settings",
    route: "/settings",
    summary: "Configure telescope access, observing data, files, broadcasting, maps, weather, and appearance.",
    capabilities: [
      "Connect and remember Seestar addresses; manage pairing and access control.",
      "Inspect private Tailscale remote-access status and copy safe setup or removal commands.",
      "Set site location, timezone, local horizon, capture paths and naming, weather, and broadcasting.",
      "Manage offline sky-map data, imagery fetching, object information, theme, text size, and build information.",
    ],
    steps: [
      "Expand the category matching what you want to configure.",
      "For a new installation, set telescope connection, observing site, capture files, and access first.",
      "Add optional weather, broadcasting, and offline map services only when you need them.",
      "Check About Photon Commander when reporting a problem so the build and version can be identified.",
    ],
    notes: [
      "Treat pairing links, access credentials, weather keys, and stream keys as secrets.",
      "Location, timezone, and horizon affect planning and scheduling; capture paths are on the Photon Commander server.",
    ],
  },
];

export const guideTopics: GuideTopic[] = [
  {
    id: "first-night", title: "Set up Photon Commander for the first night",
    summary: "Connect the telescope and establish the site information every later calculation depends on.",
    steps: ["Open Settings → Equipment and connect to the Seestar.", "Set the observing location, timezone, and local horizon under Observing site.", "Choose a capture folder and filename format, then review Appearance & access.", "Return to Planning and confirm tonight's twilight and target altitude look sensible."],
    relatedManualIds: ["settings", "planning"],
  },
  {
    id: "remote-access", title: "Use Photon Commander securely away from home",
    summary: "Keep Photon Commander private while reaching the observatory computer from a phone or tablet through Tailscale Serve.",
    steps: [
      "Install Tailscale on the computer running Photon Commander and on the remote phone or tablet, then sign both into the same tailnet.",
      "On the Photon Commander computer run sudo tailscale up, then sudo tailscale serve --bg 127.0.0.1:7777. Do not change Photon Commander to a public bind address.",
      "Open Settings → Remote access, check that Private access active is shown, and use the HTTPS address or QR code from a Tailscale-connected device.",
      "Restrict access to the intended account or devices in the tailnet policy, test state updates and Stop before leaving home, and keep independent local power control available.",
      "To remove the route, run sudo tailscale serve off. Never substitute Tailscale Funnel or router port forwarding for this private route.",
    ],
    relatedManualIds: ["settings", "mount"],
  },
  {
    id: "plan-capture", title: "Plan and capture a target",
    summary: "Move from an observable target to a monitored imaging sequence.",
    steps: ["In Planning, choose a target and validate framing, horizon clearance, Moon separation, and timing.", "Send it to Scheduling, add the remaining start-up and shutdown work, and clear every plan diagnostic.", "At the telescope, verify mount state, focus, filters, weather, and cable clearance.", "In Capture, take a short test frame before committing to the full sequence."],
    relatedManualIds: ["planning", "scheduling", "mount", "capture"],
  },
  {
    id: "polar-workflow", title: "Measure polar alignment",
    summary: "Prepare a safe, solvable sweep and turn the result into physical mount corrections.",
    steps: ["Roughly align and focus, then use Mount to place the telescope where the full measurement arc is unobstructed.", "Use Solve here in Polar align to confirm the exposure contains enough stars.", "Run the sweep without touching the mount and keep Stop within reach.", "Apply the knob corrections and repeat the measurement to verify improvement."],
    relatedManualIds: ["mount", "polar-align"],
  },
  {
    id: "calibrate-stack", title: "Calibrate and live-stack images",
    summary: "Use matching calibration data to build a cleaner live view from incoming light frames.",
    steps: ["Capture dark, bias, and flat data appropriate to the light-frame settings and optical train.", "Start Live stacking on the current capture session or select a server-side watch folder.", "Select compatible masters manually or enable automatic selection, then begin a new stack.", "Monitor registration and the combined image; save the stack before ending it when you want an output file."],
    relatedManualIds: ["capture", "live-stacking"],
  },
  {
    id: "broadcast", title: "Broadcast an observing session",
    summary: "Configure, verify, and safely publish the Photon Commander picture over RTSP or YouTube.",
    steps: ["Set Broadcasting options in Settings and store a YouTube stream key only if that relay is needed.", "Start the intended picture source and confirm the Streaming preview.", "Open the reusable broadcast in YouTube Studio and verify its title and privacy before starting the relay.", "Watch the preview and client count, then stop the external relay when the session ends."],
    relatedManualIds: ["settings", "live-stacking", "streaming"],
  },
];

export const knowledgeArticles: KnowledgeArticle[] = [
  {
    id: "remote-access", title: "Private remote access with Tailscale", category: "Safety",
    body: [
      "Recommended topology: Photon Commander listens only on 127.0.0.1:7777 on the observatory computer. Tailscale Serve terminates private HTTPS and proxies that address to other authorized devices in the same tailnet. This needs no router port forwarding and does not require Photon Commander to accept a public network interface.",
      "Install Tailscale on the Photon Commander host and the remote phone or tablet. On Linux or Raspberry Pi OS, connect the host with sudo tailscale up. When both devices appear in the same tailnet, run sudo tailscale serve --bg 127.0.0.1:7777 on the host. Background Serve resumes after a reboot or Tailscale restart.",
      "Settings → Remote access reads tailscale status --json and tailscale serve status --json locally. When the exact Photon Commander proxy is active it shows the private HTTPS URL and a QR code. Photon Commander never installs Tailscale, stores an auth key, signs into your account, edits tailnet policy, or starts or stops Serve itself.",
      "Use Tailscale access-control grants to limit TCP 443 on the observatory computer to the people or devices that should control the telescope. A new tailnet may allow broader member access than intended, so review policy before relying on the URL alone. Photon Commander pairing can remain enabled as a second gate; pair through the remote URL shown by Photon Commander rather than reusing a LAN-domain cookie.",
      "Tailscale Funnel is different: it publishes a service to the wider internet. Photon Commander treats detected Funnel exposure as an error. Do not use Funnel, router port forwarding, or a public reverse proxy for normal telescope control.",
      "Troubleshooting: run tailscale status and tailscale serve status on the host; confirm the host is online, the phone VPN is connected, and Serve points to http://127.0.0.1:7777. Check Settings → Remote access again. Before relying on an unattended night, test page loading, live state, a harmless command, reconnect after reboot, and the emergency Stop path while still on site.",
      "Remote control does not replace physical safety. Verify clearance and weather before leaving, keep Stop reachable, and maintain a separate local way to remove power if the browser, internet connection, host, or tailnet fails.",
    ],
    keywords: ["tailscale", "remote", "away", "vpn", "serve", "funnel", "https", "magicdns", "tailnet", "phone", "router", "port forwarding"],
    relatedManualIds: ["settings", "mount"],
  },
  {
    id: "external-tools", title: "Install optional tools and telescope bridges", category: "Concepts",
    body: ["Settings → Integrations & tools checks the computer running Photon Commander for optional programs. Install software on that computer—not on the phone or tablet used as the remote screen. The catalog links to each upstream project because packages and commands differ between Linux, Raspberry Pi OS, macOS, and Windows.", "FFmpeg enables RTSP and YouTube encoding. ASTAP needs both its executable and a star database: start with D20 for the Seestar main camera and W08 for the wide camera; D50 is an alternative for smaller fields. Download the program and databases from the official ASTAP page linked in Settings. Windows installers normally use Program Files/astap; Photon also checks Program Files (x86) and LOCALAPPDATA/astap. Set an absolute executable or database path under Integrations & tools when automatic detection misses a custom installation. dwarfAlp is an unofficial experimental GPL bridge: install, configure, and run it as a separate process, then let Photon Commander discover its Alpaca devices. Photon Commander uses that bridge as a separate process.", "Browse in Settings lists folders on the computer running Photon Commander, including a Raspberry Pi when the phone is the remote display. It does not select a phone folder. Choose the ASTAP executable, star-database directory or capture root, then Save. An empty ASTAP override restores automatic detection; an unavailable solver or missing database remains visible rather than silently falling back."],
    keywords: ["install", "integration", "external", "ffmpeg", "astap", "dwarf", "dwarfalp", "alpaca", "raspberry", "license"], relatedManualIds: ["settings", "streaming", "polar-align"],
  },
  {
    id: "connection-states", title: "Connection indicators and missing controls", category: "Troubleshooting",
    body: ["The footer reports backend, telescope-control, and Alpaca-device connections. A screen may be available while a particular device is not.", "Photon Commander hides or disables operations the connected hardware reports as unsupported. If a control is missing, confirm the device connection and capability rather than assuming the page failed to load."],
    keywords: ["offline", "connection", "footer", "alpaca", "disabled", "missing", "capability"], relatedManualIds: ["settings", "mount", "capture"],
  },
  {
    id: "location-time", title: "Why location, timezone, and horizon matter", category: "Concepts",
    body: ["Altitude, twilight, solar-event timing, target visibility, and local clock labels depend on the observing site. The horizon profile adds local obstructions that a geometric horizon cannot know about.", "If Planning or Scheduling times look wrong, verify latitude, longitude, timezone, night date, and horizon before changing the target or plan."],
    keywords: ["wrong time", "timezone", "location", "latitude", "longitude", "horizon", "twilight"], relatedManualIds: ["settings", "planning", "scheduling"],
  },
  {
    id: "slew-sync", title: "Slew, jog, sync, home, and park", category: "Concepts",
    body: ["Jog moves an axis while you command it. Slew moves to coordinates. Sync declares that the current physical pointing equals supplied coordinates and changes the mount model without being a normal movement command.", "Home moves to the device's reference position. Park moves to its safe stored position and commonly ends normal observing motion. Exact support comes from the connected mount."],
    keywords: ["slew", "sync", "jog", "home", "park", "coordinates", "mount"], relatedManualIds: ["mount"],
  },
  {
    id: "plate-solving", title: "When plate solving fails", category: "Troubleshooting",
    body: ["Plate solving needs a focused, untrailed star field with a plausible field of view. Clouds, dew, obstruction, over- or under-exposure, motion, and too few stars can all prevent a solution.", "Stop movement, check focus and dew, try a moderate exposure in a richer unobstructed field, and verify optical-profile information before repeating an automated workflow."],
    keywords: ["solve", "plate solve", "stars", "focus", "cloud", "dew", "exposure", "failed"], relatedManualIds: ["polar-align", "capture"],
  },
  {
    id: "frame-types", title: "Light, dark, bias, and flat frames", category: "Concepts",
    body: ["Lights contain the sky signal. Darks use the same exposure and sensor conditions with no light. Bias frames are shortest practical no-light exposures. Flats record even illumination through the same optical path.", "A useful master must match the properties relevant to its type. Flats are especially sensitive to optic, focus, filter, dust, and camera orientation; darks are sensitive to exposure, gain, and temperature."],
    keywords: ["light", "dark", "bias", "flat", "master", "calibration", "temperature", "gain"], relatedManualIds: ["capture", "live-stacking"],
  },
  {
    id: "stack-registration", title: "Live stack does not gain frames", category: "Troubleshooting",
    body: ["Confirm the stack is active and watching the intended server folder, then check that new light FITS files are arriving after the stack starts.", "Frames must have compatible dimensions and enough common stars. Check tracking, focus, clouds, exposure, file type, folder recursion, and any error shown beside the stack before resetting it."],
    keywords: ["stack", "registration", "frame count", "watch folder", "fits", "not updating"], relatedManualIds: ["live-stacking", "capture"],
  },
  {
    id: "mount-safety", title: "Safe remote mount operation", category: "Safety",
    body: ["Before any jog, slew, home, park, or polar-alignment sweep, check people, tripod clearance, cables, nearby walls, and the complete intended path—not only the starting position.", "Keep Stop reachable and maintain a way to remove power locally. Do not rely on a browser or network connection as the only emergency control."],
    keywords: ["safety", "stop", "collision", "cable", "clearance", "emergency", "power"], relatedManualIds: ["mount", "polar-align"],
  },
  {
    id: "stream-privacy", title: "YouTube privacy and stream keys", category: "Safety",
    body: ["A stream key authorizes publishing to its channel and must be treated as a secret. Photon Commander uses it to send video but cannot choose the broadcast's privacy.", "Verify the reusable event in YouTube Studio is Unlisted or has the privacy you intend before starting. Rotate a key immediately if it is exposed."],
    keywords: ["youtube", "privacy", "unlisted", "stream key", "secret", "broadcast"], relatedManualIds: ["streaming", "settings"],
  },
  {
    id: "server-paths", title: "Server paths versus phone files", category: "Concepts",
    body: ["Capture folders, live-stack watch folders, offline data, and generated outputs live on the computer running Photon Commander. A path entered in the web app is not normally a path on the phone or tablet displaying it.", "Use Browse where offered and make sure the Photon Commander service account can read source folders and write output folders."],
    keywords: ["folder", "path", "phone", "server", "permission", "files", "browse"], relatedManualIds: ["settings", "live-stacking", "capture"],
  },
];
