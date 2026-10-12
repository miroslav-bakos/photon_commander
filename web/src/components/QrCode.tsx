// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { QRCodeSVG } from "qrcode.react";
import { useThemeColor } from "../theme/useThemeColor";

/** The pairing QR (DESIGN.md §1: "nobody types an IP address in the dark").
 * Coloured from the theme tokens like everything else — a QR code rendered in
 * literal black-on-white would be the one un-themed rectangle on the page,
 * and in red-light mode a white square is exactly the thing this mode exists
 * to prevent. */
export function QrCode({ value, size = 200 }: { value: string; size?: number }) {
  const fg = useThemeColor("--ssc-fg");
  const bg = useThemeColor("--ssc-bg");

  if (!fg || !bg) return <div style={{ width: size, height: size }} aria-hidden="true" />;

  return (
    <QRCodeSVG
      value={value}
      size={size}
      fgColor={fg}
      bgColor={bg}
      level="M"
      role="img"
      aria-label="Pairing QR code — scan with the phone's camera to open Photon Commander"
    />
  );
}
