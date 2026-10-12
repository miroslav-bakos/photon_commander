// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useState } from "react";

import { Button } from "../../components/Button";
import { Card, CardTitle } from "../../components/Card";
import { Field } from "../../components/Field";
import {
  OPENWEATHERMAP_SECRET_NAME,
  VISUALCROSSING_SECRET_NAME,
  WEATHERAPI_SECRET_NAME,
  useDeleteSecretMutation,
  useSetMetnoContactMutation,
  useSetSecretMutation,
  useSetWeatherProviderMutation,
  useWeatherSettingsQuery,
  type WeatherProviderStatus,
} from "../../api/settings";

/**
 * The two shipped weather providers (`THIRD_PARTY_NOTICES.md`: every other one was
 * excluded on commercial-licensing grounds). They are configured differently
 * and this panel does not pretend otherwise:
 *
 * * **Met.no** needs an identifying *contact*, not a key. It is sent in the
 *   `User-Agent` of every request, so it is published by design — shown in
 *   plain text, editable, never treated as a secret.
 * * **OpenWeatherMap** needs the user's own *key*, which never leaves the
 *   server (invariant 8). This panel can set it and clear it; it can never
 *   read it back, and the UI says "configured", not a masked value that would
 *   imply one had been fetched.
 *
 * Every provider's live availability comes from the server, which builds it
 * from the same `availability()` call the forecast path uses — so this panel
 * cannot claim a provider works when a forecast would disagree.
 */
function ProviderStatusRow({
  status,
  selected,
}: {
  status: WeatherProviderStatus;
  selected: boolean;
}) {
  const labels: Record<string, string> = {
    metno: "Met.no",
    openweathermap: "OpenWeatherMap",
    weatherapi: "WeatherAPI.com",
    visualcrossing: "Visual Crossing",
  };
  const label = labels[status.name] ?? status.name;
  const stateLabel = selected
    ? status.available
      ? "selected"
      : "selected, unavailable"
    : status.available
      ? "available"
      : "unavailable";
  return (
    <li className="flex flex-col gap-0.5 py-1.5">
      <span className="flex items-center gap-2 text-sm">
        <span
          aria-hidden="true"
          className={`inline-block size-2 rounded-full ${status.available ? "bg-success" : "bg-fg-disabled"}`}
        />
        <span className="font-medium">{label}</span>
        <span className={status.available ? "text-success" : "text-fg-muted"}>
          {stateLabel}
        </span>
      </span>
      {/* Invariant 7: unavailable always says why, in the server's own words. */}
      {status.reason !== null && status.reason !== undefined && (
        <span className="pl-4 text-xs text-fg-muted">{status.reason}</span>
      )}
    </li>
  );
}

function ApiKeyField({
  label,
  secretName,
  configured,
  keyringUnusable,
}: {
  label: string;
  secretName: string;
  configured: boolean;
  keyringUnusable: boolean;
}) {
  const [apiKey, setApiKey] = useState("");
  const setSecret = useSetSecretMutation();
  const deleteSecret = useDeleteSecretMutation();
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-end gap-2">
        <Field
          label={`${label} API key`}
          wrapperClassName="flex-1 min-w-[16rem]"
          type="password"
          autoComplete="off"
          placeholder={configured ? "Stored — enter a new key to replace" : "Optional"}
          value={apiKey}
          disabled={keyringUnusable}
          onChange={(event) => { setApiKey(event.target.value); }}
          hint="Your own key, under your own licence. Stored in this machine's keyring and never sent to the browser."
        />
        <Button
          variant="primary"
          onClick={() => {
            setSecret.mutate(
              { name: secretName, value: apiKey.trim() },
              { onSuccess: () => { setApiKey(""); } },
            );
          }}
          disabled={setSecret.isPending || apiKey.trim() === "" || keyringUnusable}
        >
          {setSecret.isPending ? "Saving…" : "Save key"}
        </Button>
        {configured && (
          <Button
            variant="secondary"
            onClick={() => { deleteSecret.mutate(secretName); }}
            disabled={deleteSecret.isPending || keyringUnusable}
          >
            Remove key
          </Button>
        )}
      </div>
      {setSecret.isError && <p className="text-sm text-danger">{setSecret.error.message}</p>}
      {deleteSecret.isError && <p className="text-sm text-danger">{deleteSecret.error.message}</p>}
    </div>
  );
}

export function WeatherPanel() {
  const weather = useWeatherSettingsQuery();
  const setContact = useSetMetnoContactMutation();
  const setProvider = useSetWeatherProviderMutation();

  const serverContact = weather.data?.metno_contact ?? "";
  const [contact, setContactDraft] = useState(serverContact);
  // Re-seed the draft only when the SERVER's value actually changes — React's
  // documented adjust-state-during-render pattern, not an effect. An effect
  // keyed on `serverContact` fires on every refetch that returns the same
  // string, so a background poll landing between keystrokes would wipe out
  // what the user was typing.
  const [seenContact, setSeenContact] = useState(serverContact);
  if (seenContact !== serverContact) {
    setSeenContact(serverContact);
    if (contact === seenContact) setContactDraft(serverContact);
  }

  if (weather.isPending) return <Card>Loading weather providers…</Card>;
  if (weather.isError && weather.data === undefined) {
    return <Card className="text-danger">Could not load weather provider settings.</Card>;
  }

  const data = weather.data;
  const fromEnvironment = data.metno_contact_source === "environment";
  const keyringUnusable = !data.secrets_backend_available;

  return (
    <Card>
      {(weather.isError) && <p role="alert" className="mb-3 text-sm text-danger">Could not refresh settings. Showing the last saved values; your edits are retained.</p>}
      <CardTitle>Weather providers</CardTitle>

      <label className="mb-4 flex max-w-sm flex-col gap-1 text-sm">
        <span className="text-xs tracking-wide text-fg-muted uppercase">Forecast provider</span>
        <select
          aria-label="Forecast provider"
          className="min-h-11 rounded-md border border-border-strong bg-bg px-3 text-fg"
          value={data.provider}
          disabled={setProvider.isPending}
          onChange={(event) => {
            setProvider.mutate(event.target.value as typeof data.provider);
          }}
        >
          <option value="metno">Met.no</option>
          <option value="openweathermap">OpenWeatherMap</option>
          <option value="weatherapi">WeatherAPI.com</option>
          <option value="visualcrossing">Visual Crossing</option>
        </select>
      </label>
      {setProvider.isError && (
        <p className="mb-3 text-sm text-danger">{setProvider.error.message}</p>
      )}

      <ul className="mb-4 border-b border-border pb-3">
        {data.providers.map((provider) => (
          <ProviderStatusRow
            key={provider.name}
            status={provider}
            selected={provider.name === data.provider}
          />
        ))}
      </ul>

      <div className="flex flex-col gap-5">
        <div className="flex flex-col gap-2">
          <div className="flex flex-wrap items-end gap-2">
            <Field
              label="Met.no contact"
              wrapperClassName="flex-1 min-w-[16rem]"
              type="text"
              inputMode="email"
              autoComplete="off"
              placeholder="you@example.com or https://your.site"
              value={contact}
              onChange={(e) => {
                setContactDraft(e.target.value);
              }}
              hint="Met.no requires a real contact in the User-Agent of every request. It is sent to them with each forecast, so do not use an address you would not publish."
            />
            <Button
              variant="primary"
              onClick={() => {
                setContact.mutate(contact.trim() === "" ? null : contact.trim());
              }}
              disabled={setContact.isPending || contact.trim() === serverContact.trim()}
            >
              {setContact.isPending ? "Saving…" : "Save"}
            </Button>
            {serverContact !== "" && (
              <Button
                variant="secondary"
                onClick={() => {
                  setContact.mutate(null);
                }}
                disabled={setContact.isPending}
              >
                Clear
              </Button>
            )}
          </div>
          {fromEnvironment && (
            <p className="text-xs text-warning">
              In force from the SSC_WEATHER_CONTACT environment variable. Saving here overrides
              it.
            </p>
          )}
          {setContact.isError && (
            <p className="text-sm text-danger">{setContact.error.message}</p>
          )}
        </div>

        <ApiKeyField label="OpenWeatherMap" secretName={OPENWEATHERMAP_SECRET_NAME} configured={data.openweathermap_key_set} keyringUnusable={keyringUnusable} />
        <ApiKeyField label="WeatherAPI.com" secretName={WEATHERAPI_SECRET_NAME} configured={data.weatherapi_key_set} keyringUnusable={keyringUnusable} />
        <ApiKeyField label="Visual Crossing" secretName={VISUALCROSSING_SECRET_NAME} configured={data.visualcrossing_key_set} keyringUnusable={keyringUnusable} />
        <div>
          {keyringUnusable && (
            <p className="text-sm text-danger">
              This machine&rsquo;s keyring cannot store a secret, so a key entered here would be
              lost: {data.secrets_backend_reason ?? "the OS keyring is unavailable"}
            </p>
          )}
        </div>
      </div>
    </Card>
  );
}
