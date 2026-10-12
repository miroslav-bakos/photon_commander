// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useState } from "react";
import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Card, CardTitle } from "../../components/Card";
import { Field } from "../../components/Field";
import {
  useAddressesQuery,
  useCancelDiscoveryMutation,
  useConnectMutation,
  useDisconnectMutation,
  useEthernetAddressMutation,
  useForgetAddressMutation,
  useHardwareProfileMutation,
  useProbeMutation,
  useStartDiscoveryMutation,
  useWifiAddressMutation,
} from "../../api/device";
import { useAstroProfilesQuery } from "../../api/astro";
import type { DeviceDescriptor } from "../../api/types";
import { useConnectionStore } from "../../store/connectionStore";

import { InteropKeyPanel } from "./InteropKeyPanel";

type Provenance = "manual" | "discovered" | "remembered" | null;

const LINK_TONE: Record<string, "neutral" | "success" | "warning" | "danger"> = {
  disconnected: "neutral",
  connecting: "warning",
  handshaking: "warning",
  ready: "success",
  degraded: "danger",
};

const PROVENANCE_LABEL: Record<Exclude<Provenance, null>, string> = {
  manual: "typed manually",
  discovered: "from a LAN scan",
  remembered: "a previously remembered address",
};

/**
 * architecture policy invariant 11 + frontend-react.md: Discover, a manual host field
 * with a real Test connection, and provenance for whichever address is
 * filled in. Connection candidates come only from explicit user actions or
 * persisted choices; no device address is shipped as a default.
 */
export function DeviceConnectionPanel() {
  const [host, setHost] = useState("");
  const [port, setPort] = useState("");
  const [provenance, setProvenance] = useState<Provenance>(null);
  const [ethernetHostDraft, setEthernetHostDraft] = useState<string | null>(null);
  const [ethernetPortDraft, setEthernetPortDraft] = useState<string | null>(null);
  const [wifiHostDraft, setWifiHostDraft] = useState<string | null>(null);
  const [wifiPortDraft, setWifiPortDraft] = useState<string | null>(null);

  const link = useConnectionStore((s) => s.snapshot?.link);
  const discovery = useConnectionStore((s) => s.snapshot?.discovery);
  const remembered = useConnectionStore((s) => s.snapshot?.settings);

  const startDiscovery = useStartDiscoveryMutation();
  const cancelDiscovery = useCancelDiscoveryMutation();
  const probe = useProbeMutation();
  const connect = useConnectMutation();
  const disconnect = useDisconnectMutation();
  const addresses = useAddressesQuery();
  const forget = useForgetAddressMutation();
  const profiles = useAstroProfilesQuery();
  const setHardwareProfile = useHardwareProfileMutation();
  const setEthernetAddress = useEthernetAddressMutation();
  const setWifiAddress = useWifiAddressMutation();
  const configuredEthernetHost = addresses.data?.remembered.ethernet_host ?? "";
  const configuredEthernetPort = addresses.data?.remembered.ethernet_control_port;
  const ethernetHost = ethernetHostDraft ?? configuredEthernetHost;
  const ethernetPort = ethernetPortDraft ??
    (configuredEthernetPort === null || configuredEthernetPort === undefined
      ? ""
      : String(configuredEthernetPort));
  const configuredWifiHost = addresses.data?.remembered.host ?? "";
  const configuredWifiPort = addresses.data?.remembered.control_port;
  const wifiHost = wifiHostDraft ?? configuredWifiHost;
  const wifiPort = wifiPortDraft ??
    (configuredWifiPort === null || configuredWifiPort === undefined
      ? ""
      : String(configuredWifiPort));
  const activeProfile = profiles.data?.profiles.find(
    (profile) => profile.id === remembered?.hardware_profile_id,
  );

  function pick(device: DeviceDescriptor, from: Provenance) {
    setHost(device.host);
    setPort(String(device.control_port));
    setProvenance(from);
    probe.reset();
  }

  function parsedPort(): number | undefined {
    const trimmed = port.trim();
    if (trimmed === "") return undefined;
    const n = Number(trimmed);
    return Number.isInteger(n) && n > 0 ? n : undefined;
  }

  function parsedEthernetPort(): number | undefined {
    const trimmed = ethernetPort.trim();
    if (trimmed === "") return undefined;
    const n = Number(trimmed);
    return Number.isInteger(n) && n >= 1 && n <= 65535 ? n : undefined;
  }

  const ethernetPortValid = ethernetPort.trim() === "" || parsedEthernetPort() !== undefined;

  function parsedWifiPort(): number | undefined {
    const trimmed = wifiPort.trim();
    if (trimmed === "") return undefined;
    const n = Number(trimmed);
    return Number.isInteger(n) && n >= 1 && n <= 65535 ? n : undefined;
  }

  const wifiPortValid = wifiPort.trim() === "" || parsedWifiPort() !== undefined;

  return (
    <div className="space-y-4">
      <InteropKeyPanel />
      <Card>
        <CardTitle>Hardware profile</CardTitle>
        <label className="block max-w-md text-sm text-fg-muted">
          Telescope model
          <select
            className="mt-1 w-full rounded-md border border-border bg-surface-raised px-3 py-2 text-fg"
            value={remembered?.hardware_profile_id ?? ""}
            onChange={(event) => {
              if (event.target.value !== "") setHardwareProfile.mutate(event.target.value);
            }}
            disabled={profiles.isPending || setHardwareProfile.isPending}
          >
            <option value="" disabled>
              Detect or choose a model…
            </option>
            {(profiles.data?.profiles ?? []).map((profile) => (
              <option key={profile.id} value={profile.id}>
                {profile.name}
              </option>
            ))}
          </select>
        </label>
        {activeProfile !== undefined ? (
          <div className="mt-3 text-sm text-fg-muted">
            <p>
              {remembered?.hardware_profile_source === "detected"
                ? "Detected from the telescope"
                : "Confirmed manually"}
              {" · "}
              {activeProfile.optics.map((optic) => optic.label).join(" + ")}
              {" · "}
              {activeProfile.eq_mode_available ? "Alt-az / EQ" : "Alt-az only"}
            </p>
            <p className="mt-1">
              Wide camera {activeProfile.capabilities.wide_camera ? "available" : "not fitted"}
              {" · "}LP/dark filters {activeProfile.capabilities.filter_wheel ? "available" : "not fitted"}
            </p>
          </div>
        ) : (
          <p className="mt-2 text-sm text-warning">
            No model is active. Test the connection for automatic detection, or confirm it above;
            model-specific scheduling and framing will not be guessed.
          </p>
        )}
        {setHardwareProfile.isError && (
          <p role="alert" className="mt-2 text-sm text-danger">
            Could not save the hardware profile: {String(setHardwareProfile.error)}
          </p>
        )}
      </Card>

      <Card>
        <CardTitle>Currently in use</CardTitle>
        {link === undefined ? (
          <p className="text-sm text-fg-muted">Waiting for state…</p>
        ) : (
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <Badge tone={LINK_TONE[link.status] ?? "neutral"}>{link.status}</Badge>
            <span className="text-fg-muted">
              {link.endpoint ?? "no address connected"}
            </span>
            {link.status !== "disconnected" && (
              <Button
                variant="secondary"
                onClick={() => {
                  disconnect.mutate();
                }}
                disabled={disconnect.isPending}
              >
                Disconnect
              </Button>
            )}
          </div>
        )}
        <div className="mt-2 space-y-1 text-xs text-fg-muted">
          {remembered?.ethernet_host !== null && remembered?.ethernet_host !== undefined && (
            <p>
              Preferred Ethernet: {remembered.ethernet_host}
              {remembered.ethernet_control_port !== null
                ? `:${String(remembered.ethernet_control_port)}`
                : " (default port)"}
            </p>
          )}
          {remembered?.remembered_host !== null && remembered?.remembered_host !== undefined && (
            <p>
              Wi-Fi fallback: {remembered.remembered_host}
              {remembered.remembered_control_port !== null
                ? `:${String(remembered.remembered_control_port)}`
                : " (default port)"}
            </p>
          )}
        </div>
        {link?.reason !== null &&
          link?.reason !== undefined &&
          (link.status === "disconnected" || link.status === "degraded") && (
            <p role="alert" className="mt-3 text-sm text-danger">
              {link.reason}
            </p>
          )}
      </Card>

      <Card>
        <CardTitle>Connection priority</CardTitle>
        <p className="mb-3 text-sm text-fg-muted">
          A fresh connection tries this wired address first, then the remembered Wi-Fi
          address. A healthy active connection is never switched mid-capture.
        </p>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
          <Field
            label="Ethernet IP"
            value={ethernetHost}
            onChange={(event) => { setEthernetHostDraft(event.target.value); }}
            placeholder="e.g. 10.42.0.2"
            autoComplete="off"
            spellCheck={false}
            className="sm:w-56"
          />
          <Field
            label="Control port"
            value={ethernetPort}
            onChange={(event) => { setEthernetPortDraft(event.target.value); }}
            placeholder="default"
            inputMode="numeric"
            className="sm:w-32"
          />
          <Button
            variant="primary"
            onClick={() => {
              setEthernetAddress.mutate({
                host: ethernetHost.trim(),
                control_port: parsedEthernetPort(),
              });
            }}
            disabled={
              ethernetHost.trim() === "" || !ethernetPortValid || setEthernetAddress.isPending
            }
          >
            {setEthernetAddress.isPending ? "Saving…" : "Save preferred Ethernet"}
          </Button>
          {configuredEthernetHost !== "" && (
            <Button
              variant="secondary"
              onClick={() => {
                setEthernetAddress.mutate({ host: null });
                setEthernetHostDraft("");
                setEthernetPortDraft("");
              }}
              disabled={setEthernetAddress.isPending}
            >
              Clear
            </Button>
          )}
        </div>
        {!ethernetPortValid && (
          <p role="alert" className="mt-2 text-sm text-danger">
            Control port must be a whole number from 1 to 65535.
          </p>
        )}
        {setEthernetAddress.isError && (
          <p role="alert" className="mt-2 text-sm text-danger">
            Could not save the Ethernet address: {String(setEthernetAddress.error)}
          </p>
        )}
        {setEthernetAddress.isSuccess && (
          <p className="mt-2 text-sm text-success">
            Ethernet preference saved. It will be tried on the next connection.
          </p>
        )}
        <div className="mt-5 flex flex-col gap-3 border-t border-border pt-4 sm:flex-row sm:items-end">
          <Field
            label="Wi-Fi fallback IP"
            value={wifiHost}
            onChange={(event) => { setWifiHostDraft(event.target.value); }}
            placeholder="e.g. 192.168.1.77"
            autoComplete="off"
            spellCheck={false}
            className="sm:w-56"
          />
          <Field
            label="Control port"
            value={wifiPort}
            onChange={(event) => { setWifiPortDraft(event.target.value); }}
            placeholder="default"
            inputMode="numeric"
            className="sm:w-32"
          />
          <Button
            variant="secondary"
            onClick={() => {
              setWifiAddress.mutate({
                host: wifiHost.trim(),
                control_port: parsedWifiPort(),
              });
            }}
            disabled={wifiHost.trim() === "" || !wifiPortValid || setWifiAddress.isPending}
          >
            {setWifiAddress.isPending ? "Saving…" : "Save Wi-Fi fallback"}
          </Button>
        </div>
        {!wifiPortValid && (
          <p role="alert" className="mt-2 text-sm text-danger">
            Wi-Fi control port must be a whole number from 1 to 65535.
          </p>
        )}
        {setWifiAddress.isError && (
          <p role="alert" className="mt-2 text-sm text-danger">
            Could not save the Wi-Fi fallback: {String(setWifiAddress.error)}
          </p>
        )}
      </Card>

      <Card>
        <CardTitle>Discover</CardTitle>
        <div className="flex items-center gap-3">
          <Button
            variant="primary"
            onClick={() => {
              startDiscovery.mutate(undefined);
            }}
            disabled={discovery?.running === true || startDiscovery.isPending}
          >
            {discovery?.running === true ? "Scanning…" : "Scan the LAN"}
          </Button>
          {discovery?.running === true && (
            <Button
              variant="secondary"
              onClick={() => {
                cancelDiscovery.mutate();
              }}
            >
              Cancel
            </Button>
          )}
          {discovery?.running === true && discovery.total > 0 && (
            <span className="text-sm text-fg-muted">
              {discovery.completed} / {discovery.total}
            </span>
          )}
        </div>
        {discovery?.error !== null && discovery?.error !== undefined && (
          <p role="alert" className="mt-2 text-sm text-danger">
            {discovery.error}
          </p>
        )}
        {discovery !== undefined && (discovery.found ?? []).length > 0 && (
          <ul className="mt-3 space-y-2">
            {(discovery.found ?? []).map((device) => (
              <li key={`${device.host}:${String(device.control_port)}`}>
                <DeviceRow device={device} onUse={() => { pick(device, "discovered"); }} />
              </li>
            ))}
          </ul>
        )}
        {discovery !== undefined &&
          !discovery.running &&
          discovery.finished_at !== null &&
          (discovery.found ?? []).length === 0 && (
            <p className="mt-2 text-sm text-fg-muted">No devices answered on this scan.</p>
          )}
      </Card>

      <Card>
        <CardTitle>Manual address</CardTitle>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
          <Field
            label="Host / IP"
            value={host}
            onChange={(e) => {
              setHost(e.target.value);
              setProvenance("manual");
            }}
            placeholder="e.g. 192.168.1.77"
            autoComplete="off"
            spellCheck={false}
            className="sm:w-56"
          />
          <Field
            label="Control port"
            value={port}
            onChange={(e) => {
              setPort(e.target.value);
              setProvenance("manual");
            }}
            placeholder="default"
            inputMode="numeric"
            className="sm:w-32"
          />
          <Button
            variant="primary"
            onClick={() => {
              probe.mutate({ host: host.trim(), control_port: parsedPort() });
            }}
            disabled={host.trim() === "" || probe.isPending}
          >
            {probe.isPending ? "Testing…" : "Test connection"}
          </Button>
        </div>
        {provenance !== null && (
          <p className="mt-2 text-xs text-fg-muted">This address is {PROVENANCE_LABEL[provenance]}.</p>
        )}

        {probe.isError && (
          <p role="alert" className="mt-3 text-sm text-danger">
            The test itself failed to run: {String(probe.error)}
          </p>
        )}
        {probe.isSuccess && <ProbeResult descriptor={probe.data.device} testedAt={probe.data.tested_at} />}

        <div className="mt-3">
          <Button
            variant="secondary"
            onClick={() => {
              connect.mutate({ host: host.trim() || undefined, control_port: parsedPort() });
            }}
            disabled={connect.isPending}
          >
            {connect.isPending ? "Connecting…" : host.trim() === "" ? "Connect to remembered" : "Connect"}
          </Button>
        </div>
      </Card>

      <Card>
        <CardTitle>Known addresses</CardTitle>
        {addresses.isPending && <p className="text-sm text-fg-muted">Loading…</p>}
        {addresses.isSuccess && addresses.data.known.length === 0 && (
          <p className="text-sm text-fg-muted">Nothing remembered yet.</p>
        )}
        {addresses.isSuccess && addresses.data.known.length > 0 && (
          <ul className="space-y-2">
            {addresses.data.known.map((known) => (
              <li
                key={`${known.host}:${String(known.control_port)}`}
                className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border p-2 text-sm"
              >
                <div>
                  <p className="font-medium text-fg">
                    {known.host}:{known.control_port}
                    {known.label !== null && known.label !== undefined && ` — ${known.label}`}
                  </p>
                  <p className="text-xs text-fg-muted">
                    {known.model ?? "unknown model"} · firmware {known.firmware ?? "unknown"}
                    {known.hardware_profile_id !== null &&
                      known.hardware_profile_id !== undefined &&
                      ` · profile ${known.hardware_profile_id}`}
                    {known.last_good_at !== null &&
                      known.last_good_at !== undefined &&
                      ` · last good ${known.last_good_at}`}
                  </p>
                </div>
                <div className="flex gap-2">
                  <Button
                    variant="secondary"
                    onClick={() => {
                      setEthernetAddress.mutate({
                        host: known.host,
                        control_port: known.control_port,
                      });
                    }}
                  >
                    Set Ethernet
                  </Button>
                  <Button
                    variant="secondary"
                    onClick={() => {
                      setWifiAddress.mutate({
                        host: known.host,
                        control_port: known.control_port,
                      });
                    }}
                  >
                    Set Wi-Fi
                  </Button>
                  <Button
                    variant="secondary"
                    onClick={() => {
                      setHost(known.host);
                      setPort(String(known.control_port));
                      setProvenance("remembered");
                      probe.reset();
                    }}
                  >
                    Use
                  </Button>
                  <Button
                    variant="danger"
                    onClick={() => {
                      forget.mutate({ host: known.host, controlPort: known.control_port });
                    }}
                  >
                    Forget
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}

function DeviceRow({ device, onUse }: { device: DeviceDescriptor; onUse: () => void }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border p-2 text-sm">
      <div>
        <p className="font-medium text-fg">
          {device.host}:{device.control_port}
        </p>
        <p className="text-xs text-fg-muted">
          {device.model ?? "unknown model"} · firmware {device.firmware ?? "unknown"}
          {!device.firmware_verified && device.firmware !== null && " (unverified)"}
        </p>
      </div>
      <Button variant="secondary" onClick={onUse}>
        Use this address
      </Button>
    </div>
  );
}

/** Never a bare green tick: reachable, handshake outcome, firmware and
 * capabilities are all shown, and an unreachable host says why. */
function ProbeResult({ descriptor, testedAt }: { descriptor: DeviceDescriptor; testedAt: string }) {
  const capabilityEntries = Object.entries(descriptor.capabilities ?? {});
  return (
    <div className="mt-3 rounded-md border border-border bg-surface-raised p-3 text-sm">
      <div className="flex items-center gap-2">
        <Badge tone={descriptor.reachable ? "success" : "danger"}>
          {descriptor.reachable ? "Reachable" : "Not reachable"}
        </Badge>
        <span className="text-xs text-fg-muted">tested {testedAt}</span>
      </div>
      <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
        <dt className="text-fg-muted">Model</dt>
        <dd>{descriptor.model ?? "unknown"}</dd>
        <dt className="text-fg-muted">Serial</dt>
        <dd>{descriptor.serial ?? "unknown"}</dd>
        <dt className="text-fg-muted">Firmware</dt>
        <dd>
          {descriptor.firmware ?? "unknown"}
          {descriptor.firmware !== null && (
            <Badge tone={descriptor.firmware_verified ? "success" : "warning"} className="ml-2">
              {descriptor.firmware_verified ? "verified" : "unverified"}
            </Badge>
          )}
        </dd>
        {capabilityEntries.length > 0 && (
          <>
            <dt className="text-fg-muted">Capabilities</dt>
            <dd className="flex flex-wrap gap-1">
              {capabilityEntries.map(([name, supported]) => (
                <Badge key={name} tone={supported ? "success" : "neutral"}>
                  {name}
                </Badge>
              ))}
            </dd>
          </>
        )}
        {descriptor.note !== null && (
          <>
            <dt className="text-fg-muted">Note</dt>
            <dd>{descriptor.note}</dd>
          </>
        )}
      </dl>
    </div>
  );
}
