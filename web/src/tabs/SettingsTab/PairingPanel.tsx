// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useState } from "react";
import { usePairingInfoQuery, useRotatePairingMutation } from "../../api/auth";
import { Button } from "../../components/Button";
import { Card, CardTitle } from "../../components/Card";
import { QrCode } from "../../components/QrCode";

/** Pair a second device (a phone, once the desktop is already set up) —
 * DESIGN.md §1's "nobody types an IP address in the dark". */
export function PairingPanel() {
  const [show, setShow] = useState(false);
  const pairing = usePairingInfoQuery(show);
  const rotate = useRotatePairingMutation();

  return (
    <Card>
      <CardTitle>Pair another device</CardTitle>
      {!show ? (
        <Button variant="primary" onClick={() => { setShow(true); }}>
          Show pairing QR
        </Button>
      ) : (
        <div className="flex flex-col items-center gap-3 text-center">
          {pairing.isPending && <p className="text-sm text-fg-muted">Loading…</p>}
          {pairing.isSuccess && (
            <>
              <QrCode value={pairing.data.pairing_url} />
              <p className="text-xs break-all text-fg-muted">{pairing.data.pairing_url}</p>
            </>
          )}
          <div className="flex gap-2">
            <Button
              variant="secondary"
              onClick={() => {
                rotate.mutate();
              }}
              disabled={rotate.isPending}
            >
              {rotate.isPending ? "Rotating…" : "Invalidate old QR & generate a new one"}
            </Button>
          </div>
        </div>
      )}
    </Card>
  );
}
